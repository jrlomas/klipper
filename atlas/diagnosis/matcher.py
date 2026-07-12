# A5 diagnosis matcher — run patterns against a Timeline and, when
# nothing matches, capture the case (FD-0002 §4).
#
# The load-bearing behaviour here is the *empty catalog* path: with zero
# patterns the matcher does not fail or fall silent — it says "no known
# pattern matched" plainly and emits a Case, the deterministic seed of a
# blackbox bundle (A8 will add redaction and the GitHub intake).  An
# empty knowledge base is a starting condition, not a blocker.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import hashlib
from dataclasses import dataclass, field

from ..timeline import Event, Timeline


@dataclass
class Match:
    pattern_id: str
    confidence: float
    cause: str
    fix: str
    provenance: str
    matched_seqs: list[int]


@dataclass
class Case:
    """A captured, unexplained incident — a blackbox-bundle candidate.

    case_hash is a content hash over the salient events' *structure*
    (kind + fault_class + normalized summary), so the same incident
    dedups to the same id across machines (FD-0002 §6a content hash),
    while volatile values (times, counters) do not perturb it.
    """
    case_hash: str
    summary: str
    salient: list[Event]
    versions: dict
    note: str = "no known pattern matched — case captured"

    def signature_lines(self) -> list[str]:
        return _signature_lines(self.salient)


@dataclass
class Diagnosis:
    matches: list[Match] = field(default_factory=list)
    case: "Case | None" = None
    notes: list[str] = field(default_factory=list)

    def matched(self) -> bool:
        return bool(self.matches)

    @property
    def best(self) -> "Match | None":
        return self.matches[0] if self.matches else None


def _normalize_summary(summary: str) -> str:
    """Strip volatile numbers so structurally-identical faults hash alike."""
    import re
    return re.sub(r"[\d.]+", "#", summary).strip()


def _signature_lines(events: list[Event]) -> list[str]:
    lines = []
    for e in events:
        fault = e.fields.get("fault_class", "")
        lines.append("%s|%s|%s" % (e.kind, fault,
                                   _normalize_summary(e.summary)))
    return sorted(set(lines))


def _case_hash(events: list[Event]) -> str:
    payload = "\n".join(_signature_lines(events)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


class Matcher:
    def __init__(self, patterns=None):
        self.patterns = list(patterns or [])

    def diagnose(self, timeline: Timeline) -> Diagnosis:
        events = timeline.ordered()
        matches = []
        for pat in self.patterns:
            seqs = pat.matches(events)
            if seqs is not None:
                matches.append(Match(
                    pattern_id=pat.id, confidence=pat.confidence,
                    cause=pat.cause, fix=pat.fix, provenance=pat.provenance,
                    matched_seqs=seqs))
        # Highest confidence first; ties broken by id for determinism.
        matches.sort(key=lambda m: (-m.confidence, m.pattern_id))

        diag = Diagnosis(matches=matches, notes=list(timeline.notes))
        if not matches:
            diag.case = self._capture_case(timeline, events)
        return diag

    def _capture_case(self, timeline: Timeline,
                      events: list[Event]) -> Case:
        # Salient = the errors/criticals that make this an incident; if the
        # log is clean, capture the highest-severity events we saw so the
        # case is never empty.
        salient = [e for e in events if e.sev_rank() >= 4]  # >= error
        if not salient and events:
            top = max(e.sev_rank() for e in events)
            salient = [e for e in events if e.sev_rank() == top][:5]
        if salient:
            headline = salient[0].summary
        else:
            headline = "no significant events"
        return Case(
            case_hash=_case_hash(salient),
            summary=headline,
            salient=salient,
            versions=dict(timeline.versions))


def diagnose(timeline: Timeline, patterns=None) -> Diagnosis:
    """Convenience wrapper: run patterns (or none) against a timeline."""
    return Matcher(patterns).diagnose(timeline)
