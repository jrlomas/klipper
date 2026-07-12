# A8 GitHub-Issue intake — shape a bundle into the structured Issue the
# §6a KB lifecycle runs on (FD-0002 §6a).
#
# The knowledge base is a public asset, so how a case becomes knowledge
# is itself public: every submission is a GitHub Issue that moves through
# labelled states, and the label IS the audit trail. This module renders
# a bundle into that Issue body and defines the fixed, public label
# vocabulary — both the state machine and the accept/reject reasons, so
# every decision carries a readable, searchable rationale.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

# The §6a state machine. The label is the audit trail.
STATE_LABELS = [
    "case/new",       # opt-in, redacted bundle arrived from the template
    "case/triage",    # deduplicated against existing cases/patterns
    "case/analysis",  # a candidate pattern drafted as a linked PR
    "case/verify",    # reproduction + non-conflict check + field feedback
    "accepted",       # PR merged into the signed catalog
]

# Fixed, public rationale vocabulary — reasons are consistent + searchable.
ACCEPT_REASONS = [
    "reproduced", "multi-machine-confirmed", "root-cause-clear",
    "fix-verified",
]
REJECT_REASONS = [
    "rejected/not-reproducible", "rejected/machine-specific",
    "rejected/duplicate", "rejected/insufficient-data",
    "rejected/unsafe-fix", "rejected/superseded",
]

ALL_LABELS = STATE_LABELS + ACCEPT_REASONS + REJECT_REASONS


def render_issue(bundle, title=None) -> dict:
    """Render a bundle into a GitHub Issue {title, body, labels}.

    The body mirrors the §6a case template: symptom, a merged-timeline
    excerpt, the diagnosis (or "no match"), versions, and the content
    hash a triage bot uses to deduplicate. A fresh submission is labelled
    'case/new'; nothing else — promotion is earned through the lifecycle.
    """
    b = bundle
    diag = b.diagnosis
    lines = ["## Symptom", b.symptom, ""]

    lines.append("## Diagnosis")
    if diag.get("matched"):
        for p in diag.get("patterns", []):
            lines.append("- matched `%s` (confidence %.2f, %s)"
                         % (p["id"], p["confidence"], p["provenance"]))
    else:
        lines.append("- **no known pattern matched** — case captured")
        if diag.get("case_hash"):
            lines.append("- case hash: `%s`" % diag["case_hash"])
    lines.append("")

    lines.append("## Merged timeline (redacted)")
    lines.append("| t (machine) | sev | source | kind | summary |")
    lines.append("| --- | --- | --- | --- | --- |")
    for e in b.timeline:
        t = "?" if e["mtime"] is None else "%.3f" % e["mtime"]
        lines.append("| %s | %s | %s | %s | %s |"
                     % (t, e["severity"], e["source"], e["kind"],
                        e["summary"]))
    lines.append("")

    lines.append("## Versions")
    if b.versions:
        for k, v in sorted(b.versions.items()):
            lines.append("- %s: `%s`" % (k, v))
    else:
        lines.append("- (none reported)")
    lines.append("")

    lines.append("## Provenance")
    lines.append("- content hash: `%s`" % b.content_hash)
    lines.append("- redacted: %s (numeric-only policy)" % b.redacted)
    for n in b.notes:
        lines.append("- note: %s" % n)

    return {
        "title": title or ("Atlas case: %s" % b.symptom[:80]),
        "body": "\n".join(lines),
        "labels": ["case/new"],
    }
