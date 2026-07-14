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
                    history=None, config_context=None):
    """Answer a read-only operator question grounded in current facts."""
    summary = prompts.timeline_summary(timeline)
    query = "%s\n%s" % (question, summary)
    hits = rag_index.query(query, k=k) if rag_index is not None else None
    prompt = prompts.build_assistant_prompt(
        question, summary, hits, history=history,
        config_context=config_context)
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
    if len(current_config) <= max_chars:
        return current_config
    query = set(request.lower().replace("_", " ").split())
    blocks = []
    current = []
    section = "preamble"
    for line in current_config.splitlines(True):
        if line.lstrip().startswith("[") and "]" in line:
            if current:
                blocks.append((section, "".join(current)))
            section = line.strip().strip("[]").lower()
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append((section, "".join(current)))
    ranked = []
    for index, (section, block) in enumerate(blocks):
        searchable = block.lower().replace("_", " ")
        score = sum(1 for term in query if term in searchable)
        if section == "printer" or section.startswith("include"):
            score += 1
        ranked.append((-score, index, block))
    chosen = []
    used = 0
    for _score, index, block in sorted(ranked):
        if used + len(block) > max_chars:
            continue
        chosen.append((index, block))
        used += len(block)
    return "".join(block for _, block in sorted(chosen))
