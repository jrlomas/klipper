#!/usr/bin/env python3
# Standalone unit test for the Milestone B seed pattern catalog
# (FD-0002 §4, §6). Loads the real on-disk catalog and verifies each
# curated pattern (a) validates, (b) matches a representative log for its
# fault, and (c) stays quiet on a clean log — a signature that fires on
# everything is worse than no signature.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                ".."))

from atlas.decode import decode_klippy_log  # noqa: E402
from atlas.decode.trace import TraceCollector  # noqa: E402
from atlas.diagnosis import Matcher, load_catalog  # noqa: E402

_CATALOG = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                        "..", "atlas", "diagnosis", "patterns")

_BANNER = "Start printer at X (100.0 5.0)\n"
_CLEAN = _BANNER + "Stats 6.0: gcodein=0 mcu: bytes_retransmit=0\n"

# Each fault scenario: a representative klippy.log -> the pattern id it
# should match. These are the "does this signature fire when it should"
# checks; the clean-log check below is "does it stay quiet otherwise".
_SCENARIOS = {
    "thermal-runaway-heater":
        _BANNER + "Heater heater_bed not heating at expected rate\n",
    "adc-out-of-range":
        _BANNER + "MCU 'mcu' shutdown: ADC out of range\n",
    "mcu-timer-too-close":
        _BANNER + "MCU 'mcu' shutdown: Timer too close\n",
    "mcu-missed-scheduling":
        _BANNER + "MCU 'mcu' shutdown: Missed scheduling of next event\n",
    "comms-timeout-lost":
        _BANNER + "Lost communication with MCU 'mcu'\n",
    "protocol-version-mismatch":
        _BANNER + "Protocol error\n",
    "stepper-commanded-too-fast":
        _BANNER + "MCU 'mcu' shutdown: Rescheduled timer in the past\n",
    "flaky-wire-crc-storm":
        _BANNER + "Stats 6.0: gcodein=0 mcu: bytes_retransmit=5000\n",
}


def _catalog():
    return load_catalog(_CATALOG)


def test_catalog_loads_and_validates():
    patterns = _catalog()
    assert len(patterns) >= 8, "expected the seeded catalog, got %d" % len(
        patterns)
    ids = {p.id for p in patterns}
    for expected in _SCENARIOS:
        assert expected in ids, "missing pattern %s" % expected
    # Every pattern carries a cause, a fix, and a sane confidence.
    for p in patterns:
        assert p.cause and p.fix
        assert 0.0 <= p.confidence <= 1.0
        assert p.provenance == "seed"
    print("PASS: seed catalog loads, %d patterns, all well-formed"
          % len(patterns))


def test_each_pattern_matches_its_fault():
    patterns = _catalog()
    matcher = Matcher(patterns)
    for pattern_id, log in _SCENARIOS.items():
        diag = matcher.diagnose(decode_klippy_log(log))
        assert diag.matched(), "%s: nothing matched" % pattern_id
        ids = {m.pattern_id for m in diag.matches}
        assert pattern_id in ids, "%s: matched %s instead" % (pattern_id, ids)
    print("PASS: every fault scenario matches its intended pattern (%d)"
          % len(_SCENARIOS))


def test_queue_underrun_matches_trace_event():
    # The HELIX queue-underrun pattern keys on a trace event, not a log
    # line — feed a step_underrun trace record through the collector.
    import struct
    tc = TraceCollector()
    tc.ingest(event_id=1, clock=1000, sub=1, level=1,
              data=struct.pack("<2I", 1200, 0))   # step_underrun
    diag = Matcher(_catalog()).diagnose(tc.timeline)
    assert diag.matched()
    assert "queue-underrun-helix" in {m.pattern_id for m in diag.matches}
    print("PASS: HELIX queue-underrun matches a step_underrun trace event")


def test_clean_log_matches_nothing():
    diag = Matcher(_catalog()).diagnose(decode_klippy_log(_CLEAN))
    assert not diag.matched(), "a seed pattern fired on a clean log: %s" % (
        [m.pattern_id for m in diag.matches])
    assert diag.case is not None
    print("PASS: no seed pattern fires on a clean log (case captured)")


def test_no_cross_firing():
    # A given fault must not trip *unrelated* patterns. Allow only the
    # expected id (plus, for the CRC scenario, patterns that legitimately
    # key on stats fields).
    patterns = _catalog()
    matcher = Matcher(patterns)
    for pattern_id, log in _SCENARIOS.items():
        diag = matcher.diagnose(decode_klippy_log(log))
        matched = {m.pattern_id for m in diag.matches}
        # The intended pattern must be present; the fault-class patterns
        # are mutually exclusive so at most one fault_class pattern fires.
        assert pattern_id in matched
        fault_class_patterns = matched - {"flaky-wire-crc-storm",
                                          "queue-underrun-helix"}
        assert len(fault_class_patterns) <= 1, (
            "%s cross-fired: %s" % (pattern_id, matched))
    print("PASS: no fault cross-fires unrelated fault-class patterns")


def test_confidence_ordering_on_multi_match():
    # protocol_error also has no other fault; ensure ordering by confidence
    # is stable and the top match is the most confident.
    patterns = _catalog()
    diag = Matcher(patterns).diagnose(decode_klippy_log(
        _BANNER + "Protocol error\n"))
    assert diag.best.pattern_id == "protocol-version-mismatch"
    confs = [m.confidence for m in diag.matches]
    assert confs == sorted(confs, reverse=True)
    print("PASS: matches are ordered by descending confidence")


def main():
    test_catalog_loads_and_validates()
    test_each_pattern_matches_its_fault()
    test_queue_underrun_matches_trace_event()
    test_clean_log_matches_nothing()
    test_no_cross_firing()
    test_confidence_ordering_on_multi_match()
    print("ALL PASS")


if __name__ == "__main__":
    main()
