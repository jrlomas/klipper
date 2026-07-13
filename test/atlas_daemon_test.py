#!/usr/bin/env python3
# Standalone tests for the always-on Atlas service and its UI contract.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                ".."))

from atlas.daemon import AtlasDaemon, build_status  # noqa: E402
from atlas.decode import decode_klippy_log  # noqa: E402
from atlas.diagnosis import Matcher, load_pattern  # noqa: E402
from atlas.view import LiveTail  # noqa: E402


START = "Start printer at X (100.0 5.0)\nStats 6.0: gcodein=0\n"
FAULT = "MCU 'mcu' shutdown: Timer too close\n"
PATTERN = load_pattern({
    "id": "timer-too-close",
    "signature": {"fault_class": ["timer_too_close"]},
    "cause": "A timer deadline passed before service.",
    "fix": "Check host load and the transport.",
    "confidence": 0.8,
})


def test_status_contract():
    timeline = decode_klippy_log(START + FAULT)
    diagnosis = Matcher([PATTERN]).diagnose(timeline)
    state = build_status(timeline, diagnosis, {"state": "running"})
    assert state["schema_version"] == 1
    assert state["timeline"]["events"][0]["kind"] == "session_start"
    assert "raw" not in state["timeline"]["events"][0]
    assert state["diagnosis"]["matched"] is True
    assert state["diagnosis"]["matches"][0]["pattern_id"] == "timer-too-close"
    print("PASS: status shape matches the Mainsail timeline/diagnosis contract")


def test_daemon_publishes_incrementally():
    with tempfile.TemporaryDirectory() as tmp:
        log = os.path.join(tmp, "klippy.log")
        state_path = os.path.join(tmp, "run", "status.json")
        with open(log, "w") as fh:
            fh.write(START)
        daemon = AtlasDaemon(log, state_path, os.path.join(tmp, "patterns"),
                             patterns=[PATTERN], wall_clock=lambda: 123.0)
        first = daemon.poll_once()
        assert first["service"]["generation"] == 1
        assert first["service"]["state"] == "running"
        with open(log, "a") as fh:
            fh.write(FAULT)
        second = daemon.poll_once()
        assert second["service"]["generation"] == 2
        assert second["diagnosis"]["matched"] is True
        with open(state_path) as fh:
            published = json.load(fh)
        assert published == second
        assert not [name for name in os.listdir(os.path.dirname(state_path))
                    if name.endswith(".tmp")]
        print("PASS: daemon follows appended events and atomically publishes "
              "state")


def test_waits_for_log_to_appear():
    with tempfile.TemporaryDirectory() as tmp:
        log = os.path.join(tmp, "missing.log")
        daemon = AtlasDaemon(log, os.path.join(tmp, "state.json"), tmp,
                             patterns=[])
        waiting = daemon.poll_once()
        assert waiting["service"]["state"] == "waiting"
        # Even an empty file changes service state from waiting -> running;
        # event arrival is not required for lifecycle state to publish.
        with open(log, "w"):
            pass
        running = daemon.poll_once()
        assert running["service"]["state"] == "running"
        assert not running["timeline"]["events"]
        with open(log, "a") as fh:
            fh.write(START)
        running = daemon.poll_once()
        assert running["timeline"]["events"]
        print("PASS: service starts before klippy.log and attaches when it "
              "appears")


def test_rotation_and_bounded_timeline():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "klippy.log")
        with open(path, "w") as fh:
            fh.write(START + "one\ntwo\n")
        tail = LiveTail(path, max_events=4)
        tail.poll()
        assert len(tail.timeline.events) == 4
        # copytruncate-style rollover: smaller file, same inode.
        with open(path, "w") as fh:
            fh.write("Stats 7.0: gcodein=1\nthree\n")
        new = tail.poll()
        assert tail.rotations == 1
        assert [event.summary for event in new][-1] == "three"
        assert len(tail.timeline.events) == 4
        assert tail.timeline.events[-1].seq > tail.timeline.events[0].seq
        print("PASS: live store survives rotation and bounds retained events")


def test_idle_poll_does_not_republish():
    with tempfile.TemporaryDirectory() as tmp:
        log = os.path.join(tmp, "klippy.log")
        with open(log, "w") as fh:
            fh.write(START)
        daemon = AtlasDaemon(log, os.path.join(tmp, "state.json"), tmp,
                             patterns=[])
        first = daemon.poll_once()
        second = daemon.poll_once()
        assert second is first
        assert second["service"]["generation"] == 1
        print("PASS: idle polling avoids redundant state rewrites")


def test_idle_heartbeat_proves_liveness():
    with tempfile.TemporaryDirectory() as tmp:
        now = [100.0]
        log = os.path.join(tmp, "klippy.log")
        with open(log, "w") as fh:
            fh.write(START)
        daemon = AtlasDaemon(
            log, os.path.join(tmp, "state.json"), tmp, patterns=[],
            heartbeat=5.0, wall_clock=lambda: now[0])
        first = daemon.poll_once()
        now[0] += 4.9
        assert daemon.poll_once() is first
        now[0] += 0.1
        heartbeat = daemon.poll_once()
        assert heartbeat is not first
        assert heartbeat["service"]["generation"] == 2
        assert heartbeat["service"]["updated_at"] == 105.0
        print("PASS: idle heartbeat distinguishes a quiet daemon from a "
              "dead one")


def test_source_failure_degrades_without_losing_state():
    with tempfile.TemporaryDirectory() as tmp:
        log = os.path.join(tmp, "klippy.log")
        with open(log, "w") as fh:
            fh.write(START)
        daemon = AtlasDaemon(log, os.path.join(tmp, "state.json"), tmp,
                             patterns=[])
        good = daemon.poll_once()
        poll = daemon.follower.poll

        def fail():
            raise PermissionError("denied for test")

        daemon.follower.poll = fail
        degraded = daemon.poll_once()
        assert degraded["service"]["state"] == "degraded"
        assert "log read failed" in degraded["service"]["last_error"]
        assert degraded["timeline"] == good["timeline"]
        daemon.follower.poll = poll
        recovered = daemon.poll_once()
        assert recovered["service"]["state"] == "running"
        assert recovered["service"]["last_error"] == ""
        print("PASS: source failures degrade and recover without losing facts")


def test_async_service_stops_cleanly():
    async def exercise():
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "klippy.log")
            with open(log, "w") as fh:
                fh.write(START)
            daemon = AtlasDaemon(
                log, os.path.join(tmp, "state.json"), tmp,
                interval=0.001, patterns=[])
            stop = asyncio.Event()
            task = asyncio.create_task(daemon.serve(stop))
            await asyncio.sleep(0.01)
            stop.set()
            await task
            assert daemon._last_state is not None
    asyncio.run(exercise())
    print("PASS: asyncio daemon stops cooperatively")


def main():
    test_status_contract()
    test_daemon_publishes_incrementally()
    test_waits_for_log_to_appear()
    test_rotation_and_bounded_timeline()
    test_idle_poll_does_not_republish()
    test_idle_heartbeat_proves_liveness()
    test_source_failure_degrades_without_losing_state()
    test_async_service_stops_cleanly()
    print("ALL PASS")


if __name__ == "__main__":
    main()
