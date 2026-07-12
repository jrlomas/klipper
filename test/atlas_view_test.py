#!/usr/bin/env python3
# Standalone unit test for the Atlas A3 trace viewer (FD-0002 §3).
# Checks the filter contract (severity / source / kind / subsystem) the
# CLI and a future Mainsail panel share, and the live-tail incremental
# decode over a growing klippy.log.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                ".."))

from atlas.decode import decode_klippy_log  # noqa: E402
from atlas.view import LiveTail, TimelineFilter, render  # noqa: E402

LOG = """\
Start printer at X (100.0 5.0)
Stats 6.0: gcodein=0 mcu: bytes_retransmit=0
Heater heater_bed not heating at expected rate
MCU 'mcu' shutdown: Timer too close
"""


def test_min_severity_filter():
    tl = decode_klippy_log(LOG)
    errs = TimelineFilter(min_severity="error").select(tl)
    kinds = {e.kind for e in errs}
    assert "stats" not in kinds and "session_start" not in kinds
    assert {"heater_fault", "mcu_shutdown"} <= kinds
    print("PASS: min_severity keeps only events at/above the floor")


def test_kind_filter():
    tl = decode_klippy_log(LOG)
    only = TimelineFilter(kinds=["mcu_shutdown"]).select(tl)
    assert len(only) == 1 and only[0].kind == "mcu_shutdown"
    print("PASS: kind filter selects a single event type")


def test_source_filter():
    tl = decode_klippy_log(LOG)
    mcu = TimelineFilter(sources=["mcu"]).select(tl)
    assert mcu and all("mcu" in e.source for e in mcu)
    host = TimelineFilter(sources=["host"]).select(tl)
    assert host and all("host" in e.source for e in host)
    print("PASS: source substring filter partitions by board")


def test_render_lines():
    tl = decode_klippy_log(LOG)
    lines = render(tl, TimelineFilter(min_severity="error"))
    assert all(isinstance(x, str) for x in lines)
    assert any("Timer too close" in x for x in lines)
    print("PASS: render produces formatted lines for matched events")


def test_ordered_vs_arrival():
    tl = decode_klippy_log(LOG)
    ordered = TimelineFilter(ordered=True).select(tl)
    arrival = TimelineFilter(ordered=False).select(tl)
    assert {e.seq for e in ordered} == {e.seq for e in arrival}
    # arrival order follows the file; ordered follows mtime then seq
    assert [e.seq for e in arrival] == sorted(e.seq for e in arrival)
    print("PASS: ordered and arrival views select the same set, differ order")


def test_live_tail_incremental():
    # Write the log in two appends and confirm the tail emits only the
    # newly-decoded events on each poll, with decoder state preserved.
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as fh:
        path = fh.name
        fh.write("Start printer at X (100.0 5.0)\n"
                 "Stats 6.0: gcodein=0 mcu: bytes_retransmit=0\n")
    try:
        tail = LiveTail(path, TimelineFilter(ordered=False))
        first = tail.poll()
        first_kinds = [e.kind for e in first]
        assert "session_start" in first_kinds and "stats" in first_kinds
        # Append an incident; the next poll must surface only the new ones.
        with open(path, "a") as fh:
            fh.write("MCU 'mcu' shutdown: Timer too close\n")
        second = tail.poll()
        assert len(second) == 1
        assert second[0].kind == "mcu_shutdown"
        assert second[0].fields["fault_class"] == "timer_too_close"
        # A poll with no new data yields nothing.
        assert tail.poll() == []
        print("PASS: live tail decodes incrementally, emits only new events")
    finally:
        os.unlink(path)


def test_live_tail_split_traceback():
    # A traceback split across two appends must still be captured whole.
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as fh:
        path = fh.name
        fh.write("Start printer at X (100.0 5.0)\n"
                 "Traceback (most recent call last):\n"
                 '  File "x.py", line 1, in <module>\n')
    try:
        tail = LiveTail(path)
        tail.poll()  # traceback not yet terminated -> no traceback event
        assert not any(e.kind == "traceback"
                       for e in tail.decoder.timeline.events)
        with open(path, "a") as fh:
            fh.write("mcu.error: boom\n")
        new = tail.poll()
        assert any(e.kind == "traceback" and e.fields["exc_msg"] == "boom"
                   for e in new)
        print("PASS: a traceback split across polls is captured whole")
    finally:
        os.unlink(path)


def main():
    test_min_severity_filter()
    test_kind_filter()
    test_source_filter()
    test_render_lines()
    test_ordered_vs_arrival()
    test_live_tail_incremental()
    test_live_tail_split_traceback()
    print("ALL PASS")


if __name__ == "__main__":
    main()
