#!/usr/bin/env python3
# Structured ingestion, incident durability, and baseline monitor tests.

import json
import os
import pathlib
import sys
import tempfile
from types import SimpleNamespace

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from atlas.daemon import AtlasDaemon  # noqa: E402
from atlas.history import IncidentStore  # noqa: E402
from atlas.monitor import BaselineMonitor  # noqa: E402
from atlas.memory import MachineMemoryStore  # noqa: E402
from atlas.observe import StructuredCollector, StructuredTail  # noqa: E402
from atlas.timeline import Timeline  # noqa: E402


def _record(kind, t, fields, **extra):
    value = {"kind": kind, "machine_time": t, "source": "mcu", "fields": fields}
    value.update(extra)
    return value


def test_structured_kinds_merge_on_machine_time():
    timeline = Timeline()
    timeline.anchor = {"systime": 100.0, "monotime": 5.0}
    collector = StructuredCollector(timeline)
    records = [
        _record("execution", 3, {"command": "queue_step"}),
        _record("trace", 1, {"event": "queue_refill"}),
        _record("link_stats", 2, {"crc_errors": 1, "retransmits": 2}),
        _record("timesync", 4, {"error_us": 120}),
    ]
    for record in records:
        collector.ingest(record)
    assert [event.kind for event in timeline.ordered()] == [
        "trace", "link_stats", "execution", "timesync"]
    assert all(event.time_basis == "machine" and event.t_exact
               for event in timeline.events)
    assert all(timeline.wall_time_of_event(event) is None
               for event in timeline.events)
    assert timeline.of_kind("link_stats")[0].severity == "warning"
    print("PASS: structured sources merge exactly on machine time")


def test_structured_tail_handles_partial_rotation_and_bad_data():
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "telemetry.jsonl"
        first = json.dumps(_record("trace", 1, {"event": "one"}))
        path.write_text(first[:10])
        tail = StructuredTail(path)
        assert tail.poll() == []
        with path.open("a") as handle:
            handle.write(first[10:] + "\nnot-json\n")
        assert len(tail.poll()) == 1
        assert tail.errors == 1 and tail.last_error
        record = json.dumps(_record("trace", 2, {"event": "two"}))
        path.write_text(record + "\n")
        assert len(tail.poll()) == 1
        assert tail.rotations == 1
        print("PASS: structured tail survives partial lines, rejection, "
              "and rotation")


def _diagnosis(pattern):
    best = SimpleNamespace(pattern_id=pattern, cause="cause " + pattern)
    return SimpleNamespace(best=best, case=None)


def test_incident_store_deduplicates_persists_and_retains():
    with tempfile.TemporaryDirectory() as tmp:
        now = [100.0]
        path = os.path.join(tmp, "incidents.sqlite3")
        store = IncidentStore(path, max_incidents=2, max_age_days=1,
                              wall_clock=lambda: now[0])
        store.record(_diagnosis("p1"))
        store.record(_diagnosis("p1"))
        assert store.recent()[0]["observations"] == 2
        now[0] += 1
        store.record(_diagnosis("p2"))
        now[0] += 1
        store.record(_diagnosis("p3"))
        assert len(store) == 2
        store.close()
        reopened = IncidentStore(
            path, max_incidents=2, wall_clock=lambda: now[0])
        patterns = [item["matched_pattern"] for item in reopened.recent()]
        assert patterns == ["p3", "p2"]
        reopened.close()
        print("PASS: incident history deduplicates, persists, and enforces "
              "retention")


def test_monitor_learns_persists_and_flags_drift():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "baselines.json")
        timeline = Timeline()
        collector = StructuredCollector(timeline)
        monitor = BaselineMonitor(path, min_samples=5)
        healthy = [collector.ingest(_record(
            "link_stats", i, {"crc_errors": 0, "retransmits": 0}))
                   for i in range(5)]
        assert monitor.observe(healthy) == []
        drift = collector.ingest(_record(
            "link_stats", 6, {"crc_errors": 10, "retransmits": 0}))
        alerts = monitor.observe([drift])
        assert alerts and alerts[0]["metric"].endswith("crc_errors")
        assert BaselineMonitor(path).stats["mcu.crc_errors"]["count"] == 5
        print("PASS: per-machine baseline persists and flags pre-failure drift")


def test_daemon_unifies_structured_monitor_and_history():
    with tempfile.TemporaryDirectory() as tmp:
        now = [100.0]
        log = os.path.join(tmp, "klippy.log")
        telemetry = os.path.join(tmp, "telemetry.jsonl")
        pathlib.Path(log).write_text("Start printer at X (100.0 5.0)\n")
        records = [
            _record("link_stats", i, {"crc_errors": 0, "retransmits": 0})
            for i in range(5)]
        records.extend([
            _record("link_stats", 6, {"crc_errors": 10, "retransmits": 0}),
            _record("trace", 7, {"event": "step_underrun"},
                    severity="error", summary="step underrun"),
        ])
        pathlib.Path(telemetry).write_text(
            "".join(json.dumps(record) + "\n" for record in records))
        daemon = AtlasDaemon(
            log, os.path.join(tmp, "status.json"), tmp, patterns=[],
            telemetry_paths=[telemetry],
            history_path=os.path.join(tmp, "incidents.sqlite3"),
            baseline_path=os.path.join(tmp, "baselines.json"),
            wall_clock=lambda: now[0],
            memory_store=MachineMemoryStore(
                os.path.join(tmp, "memory.json")))
        pending = daemon.poll_once()
        kinds = {event["kind"] for event in pending["timeline"]["events"]}
        assert {"link_stats", "trace", "anomaly"}.issubset(kinds)
        assert pending["monitor"]["alerts"]
        assert pending["service"]["incident_pending"] is True
        now[0] += 3.0
        state = daemon.poll_once()
        assert state["service"]["incident_count"] == 1
        assert state["service"]["incident_occurrences"] == 1
        assert state["incidents"][0]["incident_key"].startswith("case:")
        memory = MachineMemoryStore(os.path.join(tmp, "memory.json")).memory
        assert memory.diagnoses
        assert memory.baselines["monitor"]["mcu.crc_errors"]["count"] == 5
        daemon.close()
        print("PASS: daemon publishes unified telemetry, drift, and durable "
              "incidents")


def main():
    test_structured_kinds_merge_on_machine_time()
    test_structured_tail_handles_partial_rotation_and_bad_data()
    test_incident_store_deduplicates_persists_and_retains()
    test_monitor_learns_persists_and_flags_drift()
    test_daemon_unifies_structured_monitor_and_history()
    print("ALL PASS")


if __name__ == "__main__":
    main()
