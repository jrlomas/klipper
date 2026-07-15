#!/usr/bin/env python3
"""Deterministic Atlas incident grouping, evidence, and privacy tests."""

import hashlib
import json
import os
import pathlib
import stat
import sys
import tempfile


HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from atlas.daemon import AtlasDaemon  # noqa: E402
from atlas.decode import decode_klippy_log  # noqa: E402
from atlas.diagnosis import Matcher  # noqa: E402
from atlas.history import IncidentStore  # noqa: E402
from atlas.memory import MachineMemoryStore  # noqa: E402


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _append(path, text):
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def _failure_session(wall, mono, filename, reason):
    return (
        "Start printer at X (%s %s)\n" % (wall, mono)
        + "Loaded MCU 'ebb36' 145 commands (v0.13.0-helix / gcc)\n"
        + "Received %s: b'{\"script\":\"SDCARD_PRINT_FILE " % mono
        + "FILENAME=\\\"%s\\\"\"}'\n" % filename
        + "Starting SD card print (position 0)\n"
        + "Stats %s: virtual_sdcard: sd_pos=32 mcu: " % (mono + 1)
        + "bytes_retransmit=2 bytes_invalid=0\n"
        + "Traceback (most recent call last):\n"
        + "  File \"/home/private/operator/klippy.py\", line 1\n"
        + "RuntimeError: password=hunter2\n"
        + "MCU 'ebb36' shutdown: %s\n" % reason)


def test_healthy_state_has_no_case_or_archive():
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        log = root / "klippy.log"
        log.write_text("Start printer at X (100.0 5.0)\n"
                       "Stats 6.0: gcodein=0\n")
        daemon = AtlasDaemon(
            str(log), str(root / "status.json"), str(root), patterns=[],
            history_path=str(root / "incidents.sqlite3"),
            incident_dir=str(root / "archive"))
        state = daemon.poll_once()
        assert state["diagnosis"]["case"] is None
        assert state["service"]["incident_count"] == 0
        assert state["service"]["incident_occurrences"] == 0
        assert list((root / "archive").iterdir()) == []
        daemon.close()
        print("PASS: healthy state has neither a case nor an occurrence")


def test_occurrence_is_grouped_bounded_private_and_replay_safe():
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        log = root / "klippy.log"
        config = root / "printer.cfg"
        gcodes = root / "gcodes"
        repo = root / "repo"
        archive = root / "archive"
        database = root / "incidents.sqlite3"
        memory_path = root / "memory.json"
        gcodes.mkdir()
        (repo / ".git" / "refs" / "heads").mkdir(parents=True)
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        revision = "1" * 40
        (repo / ".git" / "refs" / "heads" / "main").write_text(
            revision + "\n")
        config.write_text("[mcu]\nserial: /dev/secret\npassword=hunter2\n")
        gcode = gcodes / "private-customer-part.gcode"
        gcode.write_text(
            "; confidential customer name\n"
            "G90\nG1 X1.0 Y2.0 F3000\n"
            "M117 password=hunter2\n"
            "G1 E0.25 F120\nM400\n")
        now = [1000.0]
        log.write_text(_failure_session(
            100.0, 5.0, gcode.name, "Timer too close"))
        daemon = AtlasDaemon(
            str(log), str(root / "status.json"), str(root), patterns=[],
            wall_clock=lambda: now[0], history_path=str(database),
            incident_dir=str(archive), incident_settle=2.0,
            printer_config=str(config), gcode_dir=str(gcodes),
            repo_root=str(repo), memory_store=MachineMemoryStore(
                str(memory_path), wall_clock=lambda: now[0]))

        pending = daemon.poll_once()
        assert pending["diagnosis"]["case"] is not None
        assert pending["service"]["incident_pending"] is True
        assert pending["service"]["incident_occurrences"] == 0
        now[0] += 3.0
        settled = daemon.poll_once()
        assert settled["service"]["incident_pending"] is False
        assert settled["service"]["incident_count"] == 1
        assert settled["service"]["incident_occurrences"] == 1
        assert len(settled["occurrences"]) == 1
        assert stat.S_IMODE(archive.stat().st_mode) == 0o700
        occurrence = daemon.history.recent_occurrences()[0]
        bundle_path = archive / (occurrence["occurrence_id"] + ".json")
        assert stat.S_IMODE(bundle_path.stat().st_mode) == 0o600
        bundle = daemon.history.get_occurrence(occurrence["occurrence_id"])
        assert bundle["trigger_count"] == 2
        assert bundle["privacy"]["raw_log_included"] is False
        assert bundle["evidence"]["config"]["sha256"] == _sha256(config)
        assert bundle["evidence"]["software"]["revision"] == revision
        print_ev = bundle["evidence"]["print"]
        assert print_ev["active"] is True
        assert print_ev["file"]["sha256"] == _sha256(gcode)
        assert print_ev["position"] == 32
        assert print_ev["gcode_window"] == [
            "G90", "G1 X1.0 Y2.0 F3000", "G1 E0.25 F120", "M400"]
        assert bundle["evidence"]["versions"] == [{
            "source": "mcu:ebb36", "version": "v0.13.0-helix"}]
        encoded = json.dumps(bundle, sort_keys=True)
        for forbidden in (
                gcode.name, "confidential customer", "hunter2",
                "/home/private", "serial: /dev/secret", "M117"):
            assert forbidden not in encoded

        # A structurally identical later physical failure becomes another
        # occurrence under the same aggregate incident key.
        _append(log, _failure_session(
            200.0, 20.0, gcode.name, "Timer too close"))
        now[0] += 1.0
        daemon.poll_once()
        now[0] += 3.0
        second = daemon.poll_once()
        assert second["service"]["incident_count"] == 1
        assert second["service"]["incident_occurrences"] == 2
        assert len(second["occurrences"]) == 2
        assert second["incidents"][0]["observations"] == 2
        memory = MachineMemoryStore(str(memory_path)).memory
        assert memory.diagnoses[0]["observations"] == 2
        daemon.close()

        # Replaying the same immutable log after a daemon restart must not
        # increment either the occurrence count or aggregate observations.
        now[0] += 10.0
        replay = AtlasDaemon(
            str(log), str(root / "replay-status.json"), str(root),
            patterns=[], wall_clock=lambda: now[0],
            history_path=str(database), incident_dir=str(archive),
            incident_settle=2.0, printer_config=str(config),
            gcode_dir=str(gcodes), repo_root=str(repo),
            memory_store=MachineMemoryStore(str(memory_path)))
        replay.poll_once()
        now[0] += 3.0
        state = replay.poll_once()
        assert state["service"]["incident_occurrences"] == 2
        assert state["incidents"][0]["observations"] == 2
        assert replay.memory_store.memory.diagnoses[0]["observations"] == 2
        replay.close()
        print("PASS: occurrences are grouped, private, bounded, and replay "
              "idempotent")


def test_occurrence_retention_removes_old_private_record():
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        store = IncidentStore(
            str(root / "incidents.sqlite3"), max_occurrences=1,
            archive_dir=str(root / "archive"), wall_clock=lambda: 100.0)
        diagnosis = Matcher([]).diagnose(decode_klippy_log(
            "Start printer at X (100.0 5.0)\n"
            "MCU 'mcu' shutdown: Timer too close\n"))
        for occurred_at in (101.0, 102.0):
            store.record_occurrence(diagnosis, {
                "occurred_at": occurred_at,
                "trigger": {"seq": 1, "kind": "mcu_shutdown",
                            "source": "mcu", "severity": "critical",
                            "mtime": occurred_at},
            })
        assert store.occurrence_count() == 1
        assert len(store.recent_occurrences()) == 1
        assert len(list((root / "archive").glob("*.json"))) == 1
        store.close()
        print("PASS: occurrence retention deletes both old metadata and "
              "its private record")


def main():
    test_healthy_state_has_no_case_or_archive()
    test_occurrence_is_grouped_bounded_private_and_replay_safe()
    test_occurrence_retention_removes_old_private_record()
    print("ALL PASS")


if __name__ == "__main__":
    main()
