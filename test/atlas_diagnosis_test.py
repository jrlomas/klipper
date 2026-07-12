#!/usr/bin/env python3
# Standalone unit test for the Atlas A5 diagnosis engine (FD-0002 §4).
# Exercises the pattern schema/validation, the matcher across every
# predicate type, and — critically — the empty-catalog "no match -> case
# captured" path that must be a first-class, useful output.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                ".."))

from atlas.decode import decode_klippy_log  # noqa: E402
from atlas.diagnosis import (Matcher, diagnose, load_pattern,  # noqa: E402
                             load_patterns)
from atlas.diagnosis.schema import PatternError  # noqa: E402

INCIDENT_LOG = """\
Start printer at Sat Jul 12 10:00:00 2026 (1752314400.0 6.7)
Stats 7.0: gcodein=0 mcu: bytes_retransmit=0 bytes_invalid=0
Stats 8.0: gcodein=0 mcu: bytes_retransmit=1500 bytes_invalid=40
MCU 'mcu' shutdown: Timer too close
Transition to shutdown state: MCU 'mcu' shutdown: Timer too close
"""

CLEAN_LOG = ("Start printer at Sat Jul 12 10:00:00 2026 (1752314400.0 6.7)\n"
             "Stats 7.0: gcodein=0 mcu: bytes_retransmit=0\n")

TIMER_PATTERN = {
    "id": "mcu-timer-too-close",
    "signature": {"event_kind": ["mcu_shutdown"],
                  "fault_class": ["timer_too_close"]},
    "cause": "Host overload — a timer deadline passed before service.",
    "fix": "Reduce host CPU/swap load; check the link.",
    "provenance": "seed",
    "confidence": 0.6,
}

FLAKY_WIRE_PATTERN = {
    "id": "flaky-wire-retransmits",
    "signature": {"field_min": {"section": "mcu", "key": "bytes_retransmit",
                                "value": 1000}},
    "cause": "High retransmit count indicates a marginal comms link.",
    "fix": "Reseat/replace the cable; check CAN termination.",
    "confidence": 0.5,
}


# -- schema / validation -------------------------------------------------

def test_pattern_validation_ok():
    pat = load_pattern(TIMER_PATTERN)
    assert pat.id == "mcu-timer-too-close"
    assert pat.confidence == 0.6
    print("PASS: a well-formed pattern validates")


def test_pattern_validation_rejects():
    bad_cases = [
        ({"id": "x", "cause": "c", "fix": "f"}, "missing signature"),
        ({"id": "x", "signature": {}, "cause": "c", "fix": "f"},
         "empty signature"),
        ({"id": "x", "signature": {"nope": [1]}, "cause": "c", "fix": "f"},
         "unknown predicate"),
        ({"id": "x", "signature": {"event_kind": ["k"]}, "cause": "c",
          "fix": "f", "confidence": 2.0}, "confidence out of range"),
        ({"id": "x", "signature": {"summary_regex": "("}, "cause": "c",
          "fix": "f"}, "bad regex"),
        ({"id": "x", "signature": {"min_severity": "nope"}, "cause": "c",
          "fix": "f"}, "bad severity"),
        ({"id": "x", "signature": {"event_kind": ["k"]}, "cause": "c",
          "fix": "f", "provenance": "hearsay"}, "bad provenance"),
    ]
    for data, why in bad_cases:
        try:
            load_pattern(data)
        except PatternError:
            continue
        raise AssertionError("expected PatternError for: %s" % why)
    print("PASS: malformed patterns are rejected (%d cases)" % len(bad_cases))


def test_duplicate_ids_rejected():
    try:
        load_patterns([TIMER_PATTERN, dict(TIMER_PATTERN)])
    except PatternError:
        print("PASS: duplicate pattern ids rejected")
        return
    raise AssertionError("expected PatternError for duplicate ids")


# -- matching ------------------------------------------------------------

def test_match_event_kind_and_fault_class():
    tl = decode_klippy_log(INCIDENT_LOG)
    diag = diagnose(tl, load_patterns([TIMER_PATTERN]))
    assert diag.matched()
    assert diag.best.pattern_id == "mcu-timer-too-close"
    assert diag.best.matched_seqs  # evidence recorded
    print("PASS: event_kind + fault_class predicate matches the incident")


def test_match_field_min_threshold():
    tl = decode_klippy_log(INCIDENT_LOG)
    diag = diagnose(tl, load_patterns([FLAKY_WIRE_PATTERN]))
    assert diag.matched(), "field_min over stats should fire at 1500 >= 1000"
    # And it must NOT fire on a clean log below threshold.
    clean = decode_klippy_log(CLEAN_LOG)
    assert not diagnose(clean, load_patterns([FLAKY_WIRE_PATTERN])).matched()
    print("PASS: field_min threshold fires above, stays quiet below")


def test_confidence_ordering():
    tl = decode_klippy_log(INCIDENT_LOG)
    diag = diagnose(tl, load_patterns([FLAKY_WIRE_PATTERN, TIMER_PATTERN]))
    assert len(diag.matches) == 2
    # highest confidence first: timer (0.6) before flaky-wire (0.5)
    assert diag.matches[0].pattern_id == "mcu-timer-too-close"
    print("PASS: multiple matches ordered by confidence")


# -- the empty-catalog path (the load-bearing behaviour) -----------------

def test_empty_catalog_captures_case():
    tl = decode_klippy_log(INCIDENT_LOG)
    diag = Matcher([]).diagnose(tl)          # zero patterns, on purpose
    assert not diag.matched()
    assert diag.case is not None
    assert diag.case.case_hash
    assert diag.case.salient, "case must carry the salient incident events"
    assert "no known pattern" in diag.case.note
    print("PASS: empty catalog -> case captured (not a failure, not silence)")


def test_case_hash_is_stable_and_content_addressed():
    # Same incident, different volatile numbers (times/counters) -> same
    # case hash, so cross-machine dedup works (FD-0002 §6a).
    log_a = INCIDENT_LOG
    log_b = INCIDENT_LOG.replace("1500", "9999").replace("8.0", "88.0")
    case_a = Matcher([]).diagnose(decode_klippy_log(log_a)).case
    case_b = Matcher([]).diagnose(decode_klippy_log(log_b)).case
    assert case_a.case_hash == case_b.case_hash, "hash must ignore volatiles"
    # A structurally different incident hashes differently.
    other = decode_klippy_log(
        "Start printer at X (1.0 2.0)\n"
        "MCU 'mcu' shutdown: ADC out of range\n")
    case_c = Matcher([]).diagnose(other).case
    assert case_c.case_hash != case_a.case_hash
    print("PASS: case hash is content-addressed and volatile-insensitive")


def test_clean_log_still_captures_gracefully():
    diag = Matcher([]).diagnose(decode_klippy_log(CLEAN_LOG))
    assert not diag.matched()
    assert diag.case is not None  # never crashes on a clean log
    print("PASS: clean log yields a graceful (empty-incident) case")


def main():
    test_pattern_validation_ok()
    test_pattern_validation_rejects()
    test_duplicate_ids_rejected()
    test_match_event_kind_and_fault_class()
    test_match_field_min_threshold()
    test_confidence_ordering()
    test_empty_catalog_captures_case()
    test_case_hash_is_stable_and_content_addressed()
    test_clean_log_still_captures_gracefully()
    print("ALL PASS")


if __name__ == "__main__":
    main()
