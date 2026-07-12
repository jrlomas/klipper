# A5 failure-pattern schema — signature -> cause -> fix, with provenance
# and confidence (FD-0002 §4, §6).
#
# A pattern is authored as YAML data in the repo (the knowledge base is a
# first-class, signed, reviewable artifact — FD-0002 §6).  The schema
# core, however, is plain-dict in and validated Python out, so the
# deterministic floor tests run on any CPU with no third-party dependency;
# YAML is only needed to load .yaml files from disk (see load_catalog).
#
# A signature is a conjunction of predicates.  Each predicate is
# satisfied when *some* event in the timeline matches it; a pattern
# matches when every predicate is satisfied.  Supported predicates:
#   event_kind:    [<kind>, ...]      any event.kind in the list
#   fault_class:   [<class>, ...]     any event.fields.fault_class in list
#   summary_regex: "<regex>"          any event.summary matches
#   min_severity:  "<severity>"       some event at or above this severity
#   field_min:     {section, key, value, [source]}
#                                     some stats event whose
#                                     fields.sections[section][key] >= value
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import re
from dataclasses import dataclass, field
from typing import Optional

from ..timeline import SEVERITY_ORDER, Event

_KNOWN_PREDICATES = {
    "event_kind", "fault_class", "summary_regex", "min_severity", "field_min",
}
_VALID_PROVENANCE = {"seed", "user", "model-proposed", "multi-machine"}


class PatternError(ValueError):
    """A pattern definition is malformed."""


@dataclass
class Pattern:
    """A validated failure pattern."""
    id: str
    signature: dict
    cause: str
    fix: str
    provenance: str = "seed"
    confidence: float = 0.5
    version: int = 1
    _compiled_regex: Optional[re.Pattern] = field(default=None, repr=False)

    # -- predicate evaluation ---------------------------------------------

    def matches(self, events: list[Event]) -> Optional[list[int]]:
        """Return the seqs of matching events, or None if no match.

        Every predicate must be satisfied by at least one event.  The
        union of the events that satisfied each predicate is returned as
        the evidence set.
        """
        evidence: set[int] = set()
        for key, spec in self.signature.items():
            hit = self._eval_predicate(key, spec, events)
            if not hit:
                return None
            evidence.update(hit)
        return sorted(evidence)

    def _eval_predicate(self, key, spec, events) -> list[int]:
        if key == "event_kind":
            want = set(spec)
            return [e.seq for e in events if e.kind in want]
        if key == "fault_class":
            want = set(spec)
            return [e.seq for e in events
                    if e.fields.get("fault_class") in want]
        if key == "summary_regex":
            rx = self._compiled_regex or re.compile(spec)
            self._compiled_regex = rx
            return [e.seq for e in events if rx.search(e.summary)]
        if key == "min_severity":
            floor = SEVERITY_ORDER[spec]
            return [e.seq for e in events if e.sev_rank() >= floor]
        if key == "field_min":
            return self._eval_field_min(spec, events)
        raise PatternError("unknown predicate %r in pattern %s"
                           % (key, self.id))

    def _eval_field_min(self, spec, events) -> list[int]:
        section, fkey, value = spec["section"], spec["key"], spec["value"]
        want_source = spec.get("source")
        hits = []
        for e in events:
            if e.kind != "stats":
                continue
            if want_source is not None and e.source != want_source:
                continue
            sec = e.fields.get("sections", {}).get(section, {})
            v = sec.get(fkey)
            if isinstance(v, (int, float)) and v >= value:
                hits.append(e.seq)
        return hits


def load_pattern(data: dict) -> Pattern:
    """Validate a plain dict into a Pattern (raises PatternError)."""
    if not isinstance(data, dict):
        raise PatternError("pattern must be a mapping, got %s"
                           % type(data).__name__)
    for req in ("id", "signature", "cause", "fix"):
        if req not in data:
            raise PatternError("pattern missing required field %r" % req)
    sig = data["signature"]
    if not isinstance(sig, dict) or not sig:
        raise PatternError("pattern %s: signature must be a non-empty mapping"
                           % data["id"])
    unknown = set(sig) - _KNOWN_PREDICATES
    if unknown:
        raise PatternError("pattern %s: unknown predicate(s) %s"
                           % (data["id"], ", ".join(sorted(unknown))))
    prov = data.get("provenance", "seed")
    if prov not in _VALID_PROVENANCE:
        raise PatternError("pattern %s: invalid provenance %r (expected %s)"
                           % (data["id"], prov,
                              ", ".join(sorted(_VALID_PROVENANCE))))
    conf = data.get("confidence", 0.5)
    if not isinstance(conf, (int, float)) or not (0.0 <= conf <= 1.0):
        raise PatternError("pattern %s: confidence must be in [0,1]"
                           % data["id"])
    pat = Pattern(
        id=data["id"], signature=sig, cause=data["cause"], fix=data["fix"],
        provenance=prov, confidence=float(conf),
        version=int(data.get("version", 1)))
    # Fail fast on malformed predicates by compiling regex / checking keys.
    if "summary_regex" in sig:
        try:
            pat._compiled_regex = re.compile(sig["summary_regex"])
        except re.error as exc:
            raise PatternError("pattern %s: bad summary_regex: %s"
                               % (pat.id, exc))
    if "min_severity" in sig and sig["min_severity"] not in SEVERITY_ORDER:
        raise PatternError("pattern %s: unknown severity %r"
                           % (pat.id, sig["min_severity"]))
    if "field_min" in sig:
        fm = sig["field_min"]
        for k in ("section", "key", "value"):
            if k not in fm:
                raise PatternError("pattern %s: field_min missing %r"
                                   % (pat.id, k))
    return pat


def load_patterns(items) -> list[Pattern]:
    """Validate a list of dicts into Patterns, checking for duplicate ids."""
    patterns, seen = [], set()
    for item in items:
        pat = load_pattern(item)
        if pat.id in seen:
            raise PatternError("duplicate pattern id %r" % pat.id)
        seen.add(pat.id)
        patterns.append(pat)
    return patterns


def load_catalog(path) -> list[Pattern]:
    """Load every *.yaml/*.yml pattern under a directory.

    An empty (or absent) catalog is a valid starting condition, not an
    error — Milestone A ships zero patterns and the harness must still run
    (FD-0002 §4).  Returns [] for a missing or empty directory.
    """
    import os

    if not os.path.isdir(path):
        return []
    files = sorted(f for f in os.listdir(path)
                   if f.endswith((".yaml", ".yml")))
    if not files:
        return []
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise PatternError(
            "loading YAML pattern files needs PyYAML "
            "(pip install -r atlas/requirements.txt)") from exc
    items = []
    for name in files:
        with open(os.path.join(path, name), "r") as fh:
            doc = yaml.safe_load(fh)
        if doc is None:
            continue
        if isinstance(doc, list):
            items.extend(doc)
        else:
            items.append(doc)
    return load_patterns(items)
