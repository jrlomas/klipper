#!/usr/bin/env python3
# Standalone unit test for the Atlas A2 host trace collector (FD-0002 §3).
# Encodes trace records exactly as the firmware trace plane (A1,
# src/trace.c) puts them on the wire — a little-endian u32 arg blob — and
# checks the host renders them via the dictionary, maps severity, folds
# the MCU clock onto machine time, and merges into the *same* Timeline
# the klippy.log decoder fills.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                ".."))

from atlas.decode import (ClockMap, TraceCollector,  # noqa: E402
                          TraceDictionary, decode_klippy_log)


def _blob(*vals):
    # Mirror trace_send() in src/trace.c: little-endian u32 per arg.
    return struct.pack("<%dI" % len(vals), *vals)


def test_render_default_dictionary():
    tc = TraceCollector()
    # step_underrun horizon_us=1200 queue_depth=0 (event id 1, sub motion)
    ev = tc.ingest(event_id=1, clock=1000, sub=1, level=1,
                   data=_blob(1200, 0))
    assert ev.fields["event"] == "step_underrun"
    assert ev.fields["horizon_us"] == 1200
    assert ev.fields["queue_depth"] == 0
    assert ev.summary == "step_underrun horizon_us=1200 queue_depth=0"
    assert ev.severity == "warning"           # firmware level 1 -> warning
    assert ev.source == "mcu/motion"
    print("PASS: default dictionary renders event name + typed args")


def test_severity_levels():
    tc = TraceCollector()
    sev = {lvl: tc.ingest(event_id=4, clock=1, sub=3, level=lvl,
                          data=_blob(7)).severity
           for lvl in (0, 1, 2, 3)}
    assert sev == {0: "error", 1: "warning", 2: "info", 3: "debug"}
    print("PASS: firmware levels map to Atlas severities")


def test_signed_arg_rendering():
    # A %i field must render/store as signed even though the wire is u32.
    d = TraceDictionary({9: ("offset_report", 0, "err=%i")}, {0: "core"})
    tc = TraceCollector(dictionary=d)
    ev = tc.ingest(event_id=9, clock=1, sub=0, level=2,
                   data=_blob(0xFFFFFFFF))  # -1
    assert ev.fields["err"] == -1
    assert ev.summary == "offset_report err=-1"
    print("PASS: %i args decode as signed")


def test_clockmap_to_machine_time():
    # freq 64 MHz, clock0=0 at t0=10.0 -> clock 64_000_000 is t=11.0
    cm = ClockMap(freq_hz=64_000_000, clock0=0, t0=10.0)
    tc = TraceCollector(clockmap=cm)
    ev = tc.ingest(event_id=1, clock=64_000_000, sub=1, level=2,
                   data=_blob(0, 0))
    assert abs(ev.mtime - 11.0) < 1e-9
    assert ev.time_basis == "machine" and ev.t_exact is True
    print("PASS: MCU clock folds onto machine time via timesync map")


def test_clock_wraparound():
    cm = ClockMap(freq_hz=1_000_000, clock0=10, t0=0.0)
    tc = TraceCollector(clockmap=cm)
    # clock just below clock0 must read as slightly negative offset
    ev = tc.ingest(event_id=1, clock=5, sub=1, level=2, data=_blob(0, 0))
    assert abs(ev.mtime - (-5e-6)) < 1e-12
    print("PASS: 32-bit clock wraparound handled around the anchor")


def test_unknown_event_is_kept():
    tc = TraceCollector()
    ev = tc.ingest(event_id=999, clock=1, sub=0, level=2, data=_blob(5, 6))
    assert ev.kind == "trace"
    assert ev.fields["values"] == [5, 6]  # not lost
    print("PASS: an unknown event id is retained, not dropped")


def test_from_dictionary_override():
    data = {
        "enumerations": {
            "trace_event": {"custom_evt": 42},
            "trace_sub": {"widget": 0},
        },
        "constants": {"trace_fmt custom_evt": "a=%u b=%u"},
    }
    d = TraceDictionary.from_dictionary(data)
    tc = TraceCollector(dictionary=d)
    ev = tc.ingest(event_id=42, clock=1, sub=0, level=2, data=_blob(3, 4))
    assert ev.summary == "custom_evt a=3 b=4"
    print("PASS: dictionary built from a published MCU dictionary")


def test_merge_with_klippy_log():
    # Trace events and legacy-log events share one Timeline, ordered by
    # machine time (FD-0002 §3: the merge substrate Planes 2-4 read).
    tl = decode_klippy_log(
        "Start printer at X (100.0 5.0)\n"
        "Stats 6.0: gcodein=0 mcu: bytes_retransmit=0\n"
        "Stats 8.0: gcodein=0 mcu: bytes_retransmit=0\n")
    cm = ClockMap(freq_hz=1_000_000, clock0=0, t0=7.0)  # trace at t=7.0
    tc = TraceCollector(clockmap=cm, timeline=tl)
    tc.ingest(event_id=1, clock=0, sub=1, level=1, data=_blob(1200, 0))
    ordered = tl.ordered()
    trace_evs = [e for e in ordered if e.kind == "trace"]
    assert len(trace_evs) == 1
    idx = ordered.index(trace_evs[0])
    # The trace at t=7.0 must sit between the 6.0 and 8.0 stats lines.
    assert ordered[idx - 1].mtime <= 7.0 <= ordered[idx + 1].mtime
    print("PASS: trace + klippy.log merge into one machine-time timeline")


def main():
    test_render_default_dictionary()
    test_severity_levels()
    test_signed_arg_rendering()
    test_clockmap_to_machine_time()
    test_clock_wraparound()
    test_unknown_event_is_kept()
    test_from_dictionary_override()
    test_merge_with_klippy_log()
    print("ALL PASS")


if __name__ == "__main__":
    main()
