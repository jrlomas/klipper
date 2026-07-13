# Prompt + tool-schema contracts for the model tier (FD-0002 §7).
#
# These are the fixed contracts between Atlas and whatever model sits
# behind the ModelBackend. They are deterministic strings/schemas — built
# and tested now — so the model layer is a plug-in: swap Qwen3-4B on the
# GPU for the compiled model on the Hailo and nothing here changes.
#
# The two jobs: INTERPRET (explain an incident, grounded by RAG over the
# KB + machine memory) and GENERATE/CONTROL (propose a concrete config
# edit, which the deterministic apply layer then validates, risk-classi-
# fies, and gates). The model drafts; it never decides a safety outcome.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

SYSTEM_DIAGNOSE = (
    "You are Atlas, a local companion for a HELIX/Klipper 3D printer. You "
    "explain machine incidents in plain language for the operator. You are "
    "given a deterministic, machine-time-ordered event timeline and any "
    "matching known-failure patterns. Ground every claim in the evidence "
    "provided; if the evidence is insufficient, say so. Never invent "
    "events. Be concise and practical."
)

SYSTEM_CONFIG = (
    "You are Atlas, editing a HELIX/Klipper printer config on the "
    "operator's request. Propose a concrete, complete edited config by "
    "calling the propose_config_edit tool. Change only what the request "
    "requires; preserve everything else exactly. You do NOT decide "
    "whether a change is safe — a deterministic classifier does that and "
    "will ask the operator to confirm anything safety-affecting. Never "
    "silently loosen a safety limit (max_temp, driver current, endstop, "
    "kinematics) to satisfy a request; propose it plainly and let the "
    "gate handle it."
)

# The tool the model calls to propose an edit. The apply layer consumes
# `after_config`, diffs it against the current config, classifies the
# risk, and journals/gates the result — so the model's output is always
# funnelled through the deterministic safety gate.
TOOL_PROPOSE_CONFIG_EDIT = {
    "type": "function",
    "function": {
        "name": "propose_config_edit",
        "description": "Propose a concrete edited printer config.",
        "parameters": {
            "type": "object",
            "properties": {
                "rationale": {
                    "type": "string",
                    "description": "One or two sentences on what changed "
                                   "and why.",
                },
                "after_config": {
                    "type": "string",
                    "description": "The complete edited config text.",
                },
            },
            "required": ["rationale", "after_config"],
        },
    },
}

# Structured interpretation output (used when a caller wants JSON rather
# than prose — e.g. to attach a candidate rule to a captured case).
SCHEMA_INTERPRETATION = {
    "type": "object",
    "properties": {
        "explanation": {"type": "string"},
        "likely_cause": {"type": "string"},
        "suggested_fix": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["explanation", "likely_cause", "suggested_fix"],
}


def _rag_block(rag_hits) -> str:
    if not rag_hits:
        return "(no related knowledge found)"
    lines = []
    for doc, score in rag_hits:
        lines.append("- [%s] %s" % (doc.source, doc.text))
    return "\n".join(lines)


def build_diagnosis_prompt(timeline_summary: str, rag_hits=None) -> str:
    """Prompt for interpreting an incident, grounded by RAG context."""
    return (
        "Incident timeline (machine-time ordered):\n%s\n\n"
        "Related known-failure knowledge and this machine's memory:\n%s\n\n"
        "Explain what happened and the most likely cause and fix."
        % (timeline_summary, _rag_block(rag_hits))
    )


def build_config_edit_prompt(request: str, current_config: str,
                             rag_hits=None) -> str:
    """Prompt for a config edit, grounded by RAG context."""
    return (
        "Operator request: %s\n\n"
        "Current config:\n```\n%s\n```\n\n"
        "Relevant knowledge / this machine's quirks:\n%s\n\n"
        "Call propose_config_edit with the complete edited config."
        % (request, current_config, _rag_block(rag_hits))
    )


def timeline_summary(timeline, max_events: int = 40) -> str:
    """Render a timeline into a compact text block for a prompt."""
    lines = []
    for e in timeline.ordered()[:max_events]:
        t = "?" if e.mtime is None else "%.3f" % e.mtime
        lines.append("%s  %-8s %-14s %s"
                     % (t, e.severity, e.kind, e.summary))
    return "\n".join(lines) if lines else "(no events)"
