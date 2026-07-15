# A8 blackbox bundle — the redacted, submittable incident record
# (FD-0002 §6). It ties the decoder (A4) and diagnosis (A5) together:
# a merged-timeline excerpt + the diagnosis (or "no pattern matched") +
# versions, redacted by default, content-addressed for dedup.
#
# A bundle never leaves the Pi without explicit, per-event consent; this
# module only *builds* it (already redacted) so the daemon can show the
# exact payload before asking. The content hash lets the §6a triage step
# deduplicate identical incidents across machines.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import hashlib
import re
from dataclasses import dataclass, field

from .redact import DEFAULT_POLICY, redact_event

SCHEMA_VERSION = 1


def _norm(summary: str) -> str:
    return re.sub(r"[\d.]+", "#", summary).strip()


def _signature(events) -> list:
    return sorted({
        "%s|%s|%s" % (e["kind"], e["fields"].get("fault_class", ""),
                      _norm(e["summary"]))
        for e in events})


@dataclass
class BlackboxBundle:
    schema_version: int
    content_hash: str
    symptom: str
    timeline: list                 # redacted salient event dicts
    diagnosis: dict                # {matched: bool, ...}
    versions: dict
    notes: list = field(default_factory=list)
    redacted: bool = True

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "content_hash": self.content_hash,
            "symptom": self.symptom,
            "diagnosis": self.diagnosis,
            "versions": self.versions,
            "timeline": self.timeline,
            "notes": self.notes,
            "redacted": self.redacted,
        }

    def signature_lines(self) -> list:
        return _signature(self.timeline)


def _diagnosis_dict(diagnosis, red_events=None) -> tuple:
    """Return (dict, headline) from an A5 Diagnosis (or None)."""
    if diagnosis is None:
        return ({"matched": False, "note": "no diagnosis run"}, "unknown")
    if diagnosis.matched():
        best = diagnosis.best
        return ({
            "matched": True,
            "patterns": [{"id": m.pattern_id, "confidence": m.confidence,
                          "provenance": m.provenance}
                         for m in diagnosis.matches],
        }, best.cause if best else "matched")
    case = diagnosis.case
    headline = (red_events[0]["summary"] if red_events
                else "no significant events")
    return ({
        "matched": False,
        "note": case.note if case else "no active incident",
        "case_hash": case.case_hash if case else "",
    }, headline)


def assemble_bundle(timeline, diagnosis=None, policy=DEFAULT_POLICY,
                    max_events=200) -> BlackboxBundle:
    """Assemble a redacted blackbox bundle from a timeline + diagnosis."""
    salient = [e for e in timeline.ordered() if e.sev_rank() >= 4]
    if not salient and len(timeline):
        top = max(e.sev_rank() for e in timeline.events)
        salient = [e for e in timeline.ordered() if e.sev_rank() == top]
    red_events = [redact_event(e, policy) for e in salient[:max_events]]

    diag_dict, headline = _diagnosis_dict(diagnosis, red_events)
    versions = policy.fields(dict(timeline.versions))

    payload = "\n".join(_signature(red_events)).encode("utf-8")
    content_hash = hashlib.sha256(payload).hexdigest()[:16]

    return BlackboxBundle(
        schema_version=SCHEMA_VERSION, content_hash=content_hash,
        symptom=headline, timeline=red_events, diagnosis=diag_dict,
        # Timeline notes are free text and therefore never leave the Pi.
        versions=versions, notes=[])
