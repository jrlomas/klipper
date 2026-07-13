#!/usr/bin/env python3
# Acceptance tests for real atomic apply, audit, and restart-safe undo.

import os
import pathlib
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))

from atlas.apply import (PersistentApplyPipeline, Proposal,  # noqa: E402
                         StaleConfigError)


CFG = "[printer]\nmax_velocity: 300\n\n[gcode_macro X]\ndescription: old\n"


def test_real_apply_persists_and_undo_survives_restart():
    with tempfile.TemporaryDirectory() as tmp:
        config = pathlib.Path(tmp) / "printer.cfg"
        journal = pathlib.Path(tmp) / "atlas-changes.sqlite3"
        config.write_text(CFG)
        os.chmod(config, 0o640)
        reloads = []
        after = CFG.replace("old", "new")
        pipe = PersistentApplyPipeline(config, journal,
                                       reload_callback=lambda: reloads.append(1))
        result = pipe.apply(Proposal(CFG, after, rationale="operator request"))
        assert result.applied and config.read_text() == after
        assert (config.stat().st_mode & 0o777) == 0o640
        assert (journal.stat().st_mode & 0o777) == 0o600
        lock = pathlib.Path(str(config) + ".atlas.lock")
        assert (lock.stat().st_mode & 0o777) == 0o600
        assert pipe.entries()[0]["rationale"] == "operator request"
        pipe.close()
        reopened = PersistentApplyPipeline(config, journal,
                                            reload_callback=lambda: reloads.append(1))
        assert reopened.undo() == CFG
        assert config.read_text() == CFG
        assert reopened.entries()[0]["reverted"] == 1
        assert len(reloads) == 2
        reopened.close()
        print("PASS: real atomic apply is audited and undo survives restart")


def test_safety_and_compare_swap_gates():
    with tempfile.TemporaryDirectory() as tmp:
        config = pathlib.Path(tmp) / "printer.cfg"
        config.write_text(CFG)
        pipe = PersistentApplyPipeline(config, pathlib.Path(tmp) / "journal.db")
        safety = CFG.replace("[printer]\n", "[printer]\nrotation_distance: 40\n")
        pending = pipe.apply(Proposal(CFG, safety))
        assert pending.needs_confirmation and not pending.applied
        assert config.read_text() == CFG and not pipe.entries()
        config.write_text(CFG + "# user edit\n")
        try:
            pipe.apply(Proposal(CFG, safety), confirmed=True)
        except StaleConfigError:
            pass
        else:
            raise AssertionError("stale proposal was applied")
        pipe.close()
        print("PASS: safety confirmation and compare-and-swap are mandatory")


def test_reload_failure_rolls_back_without_journal():
    with tempfile.TemporaryDirectory() as tmp:
        config = pathlib.Path(tmp) / "printer.cfg"
        config.write_text(CFG)
        after = CFG.replace("old", "new")

        def fail():
            raise RuntimeError("reload failed")

        pipe = PersistentApplyPipeline(
            config, pathlib.Path(tmp) / "journal.db", reload_callback=fail)
        try:
            pipe.apply(Proposal(CFG, after))
        except RuntimeError:
            pass
        else:
            raise AssertionError("reload failure was hidden")
        assert config.read_text() == CFG
        assert pipe.entries() == []
        pipe.close()
        print("PASS: reload failure rolls the file back and leaves no false audit")


def test_external_validator_runs_before_write():
    with tempfile.TemporaryDirectory() as tmp:
        config = pathlib.Path(tmp) / "printer.cfg"
        config.write_text(CFG)
        after = CFG.replace("old", "new")

        def reject(_text):
            raise ValueError("Klippy validation failed")

        pipe = PersistentApplyPipeline(
            config, pathlib.Path(tmp) / "journal.db", validate_callback=reject)
        try:
            pipe.apply(Proposal(CFG, after))
        except ValueError as exc:
            assert "Klippy validation" in str(exc)
        else:
            raise AssertionError("external validation failure was hidden")
        assert config.read_text() == CFG and pipe.entries() == []
        pipe.close()
        print("PASS: injected Klippy validation runs before the live write")


def main():
    test_real_apply_persists_and_undo_survives_restart()
    test_safety_and_compare_swap_gates()
    test_reload_failure_rolls_back_without_journal()
    test_external_validator_runs_before_write()
    print("ALL PASS")


if __name__ == "__main__":
    main()
