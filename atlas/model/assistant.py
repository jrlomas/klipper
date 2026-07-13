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

from ..apply import Proposal
from . import prompts


def interpret_incident(backend, timeline, rag_index=None, k=4,
                       structured=False):
    """Explain an incident timeline. Returns text, or a dict if structured.

    rag_index (optional) grounds the explanation in the KB + machine
    memory. structured=True asks the model for the SCHEMA_INTERPRETATION
    JSON (explanation / likely_cause / suggested_fix / confidence).
    """
    summary = prompts.timeline_summary(timeline)
    hits = rag_index.query(summary, k=k) if rag_index is not None else None
    prompt = prompts.build_diagnosis_prompt(summary, hits)
    schema = prompts.SCHEMA_INTERPRETATION if structured else None
    completion = backend.generate(prompt, schema=schema,
                                  system=prompts.SYSTEM_DIAGNOSE)
    if structured:
        import json
        try:
            return json.loads(completion.text)
        except ValueError:
            return {"explanation": completion.text, "likely_cause": "",
                    "suggested_fix": "", "confidence": 0.0}
    return completion.text


def propose_config_edit(backend, request, current_config, rag_index=None,
                        k=4):
    """Ask the model to draft a config edit; return an apply Proposal.

    Returns None if the model did not call propose_config_edit (so the
    caller can report "no change proposed" rather than guess). The
    Proposal is NOT applied here — the caller runs it through
    ApplyPipeline, which classifies the risk and gates it.
    """
    hits = rag_index.query(request, k=k) if rag_index is not None else None
    prompt = prompts.build_config_edit_prompt(request, current_config, hits)
    completion = backend.generate(
        prompt, tools=[prompts.TOOL_PROPOSE_CONFIG_EDIT],
        system=prompts.SYSTEM_CONFIG)
    for call in completion.tool_calls:
        if call.get("name") == "propose_config_edit":
            args = call.get("arguments", {})
            after = args.get("after_config")
            if after:
                return Proposal(before=current_config, after=after,
                                rationale=args.get("rationale", ""),
                                source="model")
    return None
