#!/usr/bin/env python3
# Standalone unit test for the Atlas A4 blackbox decoder (FD-0002 §4).
# Feeds a representative stock klippy.log and checks that the merged
# Timeline recovers typed events, machine-time ordering, fault
# classification, and honest time-basis provenance.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                ".."))

from atlas.decode import decode_klippy_log  # noqa: E402
from atlas.timeline import Timeline  # noqa: E402

# A representative stock klippy.log: session banner, periodic stats with
# an MCU section and a heater section, a heater fault, an MCU shutdown
# with a classic fault reason, the host-side shutdown transition, a
# Python traceback, and a rollover marker.  Bare messages, no per-line
# timestamps — exactly what Klipper writes.
SAMPLE_LOG = """\
Args: ['/home/pi/klipper/klippy/klippy.py', '-I', '/tmp/printer']
Start printer at Sat Jul 12 10:00:00 2026 (1752314400.0 6.7)
Stats 7.0: gcodein=0 mcu: mcu_awake=0.001 mcu_task_avg=0.000012 \
bytes_write=1024 bytes_read=2048 bytes_retransmit=0 bytes_invalid=0 \
send_seq=100 receive_seq=100 freq=63999998 heater_bed: target=60 \
temp=59.8 pwm=0.42
Stats 8.0: gcodein=0 mcu: mcu_awake=0.002 bytes_retransmit=12 \
bytes_invalid=3 freq=64000100 heater_bed: target=60 temp=25.1 pwm=1.0
Heater heater_bed not heating at expected rate
Transition to shutdown state: Heater heater_bed not heating at expected rate
MCU 'mcu' shutdown: Timer too close
Traceback (most recent call last):
  File "/home/pi/klipper/klippy/mcu.py", line 100, in _handle
    raise error("boom")
mcu.error: boom
=============== Log rollover at Sat Jul 12 10:05:00 2026 ===============
"""


def _decode() -> Timeline:
    return decode_klippy_log(SAMPLE_LOG)


def test_returns_timeline():
    tl = _decode()
    assert isinstance(tl, Timeline)
    assert len(tl) > 0
    print("PASS: decoder returns a populated Timeline")


def test_session_anchor():
    tl = _decode()
    assert tl.anchor is not None
    assert abs(tl.anchor["monotime"] - 6.7) < 1e-6
    assert abs(tl.anchor["systime"] - 1752314400.0) < 1e-3
    # wall time of the first stats line (monotime 7.0) is anchor + 0.3s
    wall = tl.wall_time_of(7.0)
    assert wall is not None and abs(wall - 1752314400.3) < 1e-3
    print("PASS: session banner anchors monotonic<->wall time")


def test_typed_events_present():
    tl = _decode()
    kinds = {e.kind for e in tl.events}
    for expected in ("session_start", "stats", "heater_fault",
                     "shutdown", "mcu_shutdown", "traceback", "rollover"):
        assert expected in kinds, "missing kind %r (have %s)" % (
            expected, sorted(kinds))
    print("PASS: all expected typed events recognized")


def test_stats_parsing():
    tl = _decode()
    stats = tl.of_kind("stats")
    assert len(stats) == 2
    first = stats[0].fields["sections"]
    assert first["mcu"]["bytes_retransmit"] == 0
    assert first["mcu"]["freq"] == 63999998
    assert abs(first["heater_bed"]["temp"] - 59.8) < 1e-6
    second = stats[1].fields["sections"]
    assert second["mcu"]["bytes_retransmit"] == 12  # rising retransmits
    print("PASS: stats sections and typed values parsed")


def test_fault_classification():
    tl = _decode()
    shut = tl.of_kind("mcu_shutdown")[0]
    assert shut.fields["fault_class"] == "timer_too_close"
    assert shut.fields["mcu"] == "mcu"
    heat = tl.of_kind("heater_fault")[0]
    assert heat.fields["fault_class"] == "heater_not_heating"
    print("PASS: MCU/heater faults classified by canonical class")


def test_severity_and_errors():
    tl = _decode()
    errs = tl.errors()
    err_kinds = {e.kind for e in errs}
    assert {"heater_fault", "shutdown", "mcu_shutdown",
            "traceback"} <= err_kinds
    # stats and session_start are info, not errors
    assert "stats" not in err_kinds
    print("PASS: severity assigned; errors() surfaces the incident")


def test_machine_time_ordering():
    tl = _decode()
    ordered = tl.ordered()
    times = [e.mtime for e in ordered if e.mtime is not None]
    assert times == sorted(times), "timeline not monotonic in mtime"
    # The shutdown must come after the 8.0 stats snapshot on the timeline.
    shut = tl.of_kind("shutdown")[0]
    stats8 = [e for e in tl.of_kind("stats") if e.mtime == 8.0][0]
    o = tl.ordered()
    assert o.index(shut) > o.index(stats8)
    print("PASS: events merge into a machine-time-ordered narrative")


def test_time_basis_honesty():
    tl = _decode()
    # The shutdown line carries no timestamp of its own: it must be marked
    # inferred (t_exact False) and basis host_monotonic, and the decoder
    # must note the precision caveat.
    shut = tl.of_kind("shutdown")[0]
    assert shut.time_basis == "host_monotonic"
    assert shut.t_exact is False
    assert any("inferred" in n for n in tl.notes)
    print("PASS: inferred timestamps flagged; provenance noted honestly")


def test_traceback_capture():
    tl = _decode()
    tb = tl.of_kind("traceback")[0]
    assert tb.fields["exc_type"] == "mcu.error"
    assert tb.fields["exc_msg"] == "boom"
    print("PASS: host traceback captured with exception type/message")


def test_print_lifecycle_and_mcu_identity():
    log = ("Start printer at X (100.0 5.0)\n"
           "Loaded MCU 'ebb36' 145 commands (v0.13.0-helix / gcc)\n"
           "Received 6.0: b'{\"script\":\"SDCARD_PRINT_FILE "
           "FILENAME=\\\"cube.gcode\\\"\"}'\n"
           "Starting SD card print (position 123)\n"
           "Exiting SD card print (position 456)\n"
           "Finished SD card print\n")
    tl = decode_klippy_log(log)
    assert tl.versions["mcu:ebb36"] == "v0.13.0-helix"
    assert tl.of_kind("mcu_identified")[0].fields["command_count"] == 145
    assert tl.of_kind("print_request")[0].fields["filename"] == "cube.gcode"
    assert tl.of_kind("print_start")[0].fields["position"] == 123
    assert tl.of_kind("print_exit")[0].fields["position"] == 456
    assert len(tl.of_kind("print_finish")) == 1
    print("PASS: print lifecycle and MCU identity become typed evidence")


def test_clean_log_no_false_faults():
    clean = ("Start printer at Sat Jul 12 10:00:00 2026 (1752314400.0 6.7)\n"
             "Stats 7.0: gcodein=0 mcu: bytes_retransmit=0\n")
    tl = decode_klippy_log(clean)
    assert tl.errors() == []
    print("PASS: a clean log yields no false faults")


def main():
    test_returns_timeline()
    test_session_anchor()
    test_typed_events_present()
    test_stats_parsing()
    test_fault_classification()
    test_severity_and_errors()
    test_machine_time_ordering()
    test_time_basis_honesty()
    test_traceback_capture()
    test_print_lifecycle_and_mcu_identity()
    test_clean_log_no_false_faults()
    print("ALL PASS")


if __name__ == "__main__":
    main()
