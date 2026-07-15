# Assistant helpers — tie the model to the deterministic layers
# (FD-0002 §7). The model interprets and drafts; the floor decides.
#
# interpret_incident() explains a decoded timeline, grounded by RAG.
# propose_config_edit() turns a natural-language request into an apply-
# layer Proposal — which the deterministic classifier then risk-gates. So
# the model's output is always funnelled through the safety gate; the
# model never applies anything itself.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import re

from ..apply import Proposal, apply_config_edits
from . import prompts


def answer_question(backend, question, timeline, rag_index=None, k=4,
                    history=None, config_context=None, job_context=None):
    """Answer a read-only operator question grounded in current facts."""
    summary = prompts.timeline_summary(timeline)
    query = "%s\n%s" % (question, summary)
    hits = rag_index.query(query, k=k) if rag_index is not None else None
    prompt = prompts.build_assistant_prompt(
        question, summary, hits, history=history,
        config_context=config_context, job_context=job_context)
    return backend.generate(
        prompt, system=prompts.SYSTEM_ASSISTANT).text


def interpret_incident(backend, timeline, rag_index=None, k=4,
                       structured=False, config_context=None):
    """Explain an incident timeline. Returns text, or a dict if structured.

    rag_index (optional) grounds the explanation in the KB + machine
    memory. structured=True asks the model for the SCHEMA_INTERPRETATION
    JSON (explanation / likely_cause / suggested_fix / confidence).
    """
    summary = prompts.timeline_summary(timeline)
    hits = rag_index.query(summary, k=k) if rag_index is not None else None
    prompt = prompts.build_diagnosis_prompt(summary, hits)
    if config_context:
        prompt += "\n\nRead-only current config context:\n%s" \
            % prompts._data_block("config", config_context)
    schema = prompts.SCHEMA_INTERPRETATION if structured else None
    completion = backend.generate(prompt, schema=schema,
                                  system=prompts.SYSTEM_DIAGNOSE)
    if structured:
        import json
        try:
            value = json.loads(completion.text)
        except ValueError:
            return {"explanation": completion.text, "likely_cause": "",
                    "suggested_fix": "", "confidence": 0.0,
                    "evidence_event_times": [],
                    "evidence_validation": {"valid": [], "invalid": []}}
        if not isinstance(value, dict):
            return {"explanation": completion.text, "likely_cause": "",
                    "suggested_fix": "", "confidence": 0.0,
                    "evidence_event_times": [],
                    "evidence_validation": {"valid": [], "invalid": []}}
        actual = [event.mtime for event in timeline.ordered()
                  if event.mtime is not None]
        cited = value.get("evidence_event_times", [])
        if not isinstance(cited, list):
            cited = []
        valid, invalid = [], []
        for item in cited:
            if isinstance(item, (int, float)) and any(
                    abs(float(item) - timestamp) <= 0.001
                    for timestamp in actual):
                valid.append(float(item))
            else:
                invalid.append(item)
        value["evidence_event_times"] = valid
        value["evidence_validation"] = {"valid": valid, "invalid": invalid}
        if invalid:
            value["confidence"] = 0.0
            value["explanation"] = (
                str(value.get("explanation", ""))
                + " [Atlas rejected unsupported event references.]").strip()
        return value
    return completion.text


def propose_config_edit(backend, request, current_config, rag_index=None,
                        k=4):
    """Ask the model to draft a config edit; return an apply Proposal.

    Returns None if the model did not call propose_config_edit (so the
    caller can report "no change proposed" rather than guess). The
    Proposal is NOT applied here — the caller runs it through
    ApplyPipeline, which classifies the risk and gates it.
    """
    if _is_vague_edit_request(request):
        return None
    hits = rag_index.query(request, k=k) if rag_index is not None else None
    prompt = prompts.build_config_edit_prompt(
        request, config_excerpt(current_config, request=request), hits)
    completion = backend.generate(
        prompt, tools=[prompts.TOOL_PROPOSE_CONFIG_EDIT],
        system=prompts.SYSTEM_CONFIG)
    for call in completion.tool_calls:
        if call.get("name") == "propose_config_edit":
            args = call.get("arguments", {})
            edits = args.get("edits")
            if edits:
                try:
                    after = apply_config_edits(current_config, edits)
                except (TypeError, ValueError):
                    # A grammar-valid tool envelope can still name a missing,
                    # ambiguous, or semantically invalid target. Fail closed;
                    # callers report no valid proposal rather than crashing or
                    # guessing how to repair model output.
                    return None
                return Proposal(before=current_config, after=after,
                                rationale=args.get("rationale", ""),
                                source="model")
    return None


_VAGUE_EDIT = re.compile(
    r"^(?:please\s+)?(?:make|improve|optimize|fix|tune)\s+"
    r"(?:(?:my|the)\s+)?(?:printer|config|settings|it)"
    r"(?:\s+(?:better|faster|safer|nicer))?[.!]?$", re.IGNORECASE)


def _is_vague_edit_request(request):
    """Reject objective-free optimization requests before model inference."""
    return bool(_VAGUE_EDIT.match(request.strip()))


def config_excerpt(current_config, request="", max_chars=12000):
    """Return bounded, request-relevant raw sections for model grounding."""
    query = _search_terms(request)
    blocks = []
    current = []
    section = "preamble"
    source = ""

    def flush():
        if current:
            blocks.append((source, section, "".join(current)))

    for line in current_config.splitlines(True):
        if line.startswith("# Atlas source: "):
            flush()
            source = line[len("# Atlas source: "):].strip()
            section = "preamble"
            current = []
            continue
        if line.lstrip().startswith("[") and "]" in line:
            flush()
            section = line.strip().strip("[]").lower()
            current = [line]
        else:
            current.append(line)
    flush()
    ranked = []
    for index, (source, section, block) in enumerate(blocks):
        section_terms = _search_terms(section)
        body_terms = _search_terms(block)
        score = 4 * len(query & section_terms) + len(query & body_terms)
        if not query and (section == "printer"
                          or section.startswith("include")):
            score = 1
        ranked.append([score, index, source, section, block])

    positive = [item for item in ranked if item[0] > 0]
    if positive:
        threshold = max(1, (max(item[0] for item in positive) + 1) // 2)
        selected = {item[1] for item in positive if item[0] >= threshold}
    else:
        selected = {item[1] for item in ranked[:1]}

    # Include both sides of an LED reference, for example an effect block
    # containing `neopixel:board_neopixel` and its hardware section.
    section_index = {item[3]: item[1] for item in ranked}
    references = {}
    for _score, index, _source, _section, block in ranked:
        targets = set()
        for kind, name in re.findall(
                r"\b(neopixel|dotstar)\s*:\s*([A-Za-z0-9_.-]+)",
                block, re.IGNORECASE):
            target = "%s %s" % (kind.lower(), name.lower())
            if target in section_index:
                targets.add(section_index[target])
        references[index] = targets
    changed = True
    while changed:
        changed = False
        for index, targets in references.items():
            if index in selected:
                additions = targets - selected
            elif targets & selected:
                additions = {index}
            else:
                additions = set()
            if additions:
                selected.update(additions)
                changed = True

    chosen = []
    used = 0
    for score, index, source, _section, block in sorted(
            ranked, key=lambda item: (-item[0], item[1])):
        if index not in selected:
            continue
        rendered = (("# Atlas source: %s\n" % source) if source else "") \
            + block
        if used + len(rendered) > max_chars:
            continue
        chosen.append((index, rendered))
        used += len(rendered)
    return "".join(block for _, block in sorted(chosen))


def _search_terms(value):
    stop = {
        "a", "about", "and", "are", "config", "configuration", "for",
        "give", "in", "is", "located", "me", "of", "on", "printer",
        "section", "show", "the", "this", "to", "using", "we", "what",
        "where", "which", "with",
    }
    terms = set()
    for raw in re.findall(r"[a-z0-9]+", value.lower().replace("_", " ")):
        term = raw
        if len(term) > 4 and term.endswith("ies"):
            term = term[:-3] + "y"
        elif len(term) > 3 and term.endswith("s"):
            term = term[:-1]
        if term not in stop:
            terms.add(term)
    if terms & {"led", "neopixel", "dotstar"}:
        terms.update(("led", "neopixel", "dotstar"))
    return terms
