#!/usr/bin/env python3
"""Regression tests for deterministic HELIX flight-recorder replay."""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import helix_motion_audit as audit  # noqa: E402


def intention(event, fields):
    return {"kind": "intention", "machine_time": 1.,
            "source": "host/mcu/stepper_x",
            "fields": dict(fields, event=event, actuator="stepper_x", oid=4)}


def execution(seq, event, clock, pos):
    return {"kind": "execution", "machine_time": 1.,
            "source": "mcu/mcu/execution",
            "fields": {"seq": seq, "event": event, "src_oid": 4,
                       "mcu_clock": clock, "position_su": pos}}


def test_clean_path_replays_and_matches_boundaries():
    velocity, duration = 65_536_000, 100
    finish = audit.end_delta(duration, velocity, 0)
    records = [
        intention("rebase", {
            "start_clock": 1000, "end_clock": 1000, "position_su": 0,
            "acc_q32": 0, "mcu_position": 0}),
        intention("segment", {
            "start_clock": 1000, "end_clock": 1100,
            "duration": duration, "flags": 0, "velocity": velocity,
            "accel": 0, "jerk": 0, "snap": 0, "crackle": 0,
            "start_position_su": 0, "end_position_su": finish >> 32,
            "start_acc_q32": 0, "end_acc_q32": finish}),
        intention("hold", {
            "start_clock": 1100, "end_clock": 1110, "duration": 10,
            "flags": 1, "velocity": 0, "accel": 0, "jerk": 0,
            "snap": 0, "crackle": 0,
            "start_position_su": finish >> 32,
            "end_position_su": finish >> 32,
            "start_acc_q32": finish, "end_acc_q32": finish}),
        execution(0, "rebase", 1000, 0),
        execution(1, "segment_done", 1100, finish >> 32),
        execution(2, "segment_done", 1110, finish >> 32),
        execution(3, "hold", 1110, finish >> 32),
    ]
    summaries, matched, triggers, executed, errors = audit.audit(records)
    assert not errors, errors
    assert summaries and "pulses=2" in summaries[0], summaries
    assert any("executed_pulses=2" in summary for summary in summaries)
    assert matched == 3 and triggers == 0 and executed == 4
    print("PASS: exact wire coefficients replay to matching MCU boundaries")


def test_underrun_is_a_failed_audit():
    records = [execution(1, "underrun", 1234, 12)]
    errors = audit.audit(records)[-1]
    assert errors and "underrun" in errors[0]
    print("PASS: an MCU underrun fails the motion audit")


def test_intentions_without_execution_evidence_fail_closed():
    records = [
        intention("rebase", {
            "start_clock": 1000, "end_clock": 1000, "position_su": 0,
            "acc_q32": 0, "mcu_position": 0}),
        intention("hold", {
            "start_clock": 1000, "end_clock": 1010, "duration": 10,
            "flags": 1, "velocity": 0, "accel": 0, "jerk": 0,
            "snap": 0, "crackle": 0,
            "start_position_su": 0, "end_position_su": 0,
            "start_acc_q32": 0, "end_acc_q32": 0}),
    ]
    errors = audit.audit(records)[-1]
    assert errors == ["no MCU execution records for recorded intentions"]
    print("PASS: missing MCU evidence fails closed")


def test_unsigned_persisted_negative_position_is_normalized():
    velocity, duration = -65_536_000, 100
    finish = audit.end_delta(duration, velocity, 0)
    unsigned_finish = (finish >> 32) & 0xffffffff
    records = [
        intention("rebase", {
            "start_clock": 1000, "end_clock": 1000, "position_su": 0,
            "acc_q32": 0, "mcu_position": 0}),
        intention("segment", {
            "start_clock": 1000, "end_clock": 1100,
            "duration": duration, "flags": 0, "velocity": velocity,
            "accel": 0, "jerk": 0, "snap": 0, "crackle": 0,
            "start_position_su": 0, "end_position_su": finish >> 32,
            "start_acc_q32": 0, "end_acc_q32": finish}),
        intention("hold", {
            "start_clock": 1100, "end_clock": 1110, "duration": 10,
            "flags": 1, "velocity": 0, "accel": 0, "jerk": 0,
            "snap": 0, "crackle": 0,
            "start_position_su": finish >> 32,
            "end_position_su": finish >> 32,
            "start_acc_q32": finish, "end_acc_q32": finish}),
        execution(0, "rebase", 1000, 0),
        execution(1, "segment_done", 1100, unsigned_finish),
        execution(2, "segment_done", 1110, unsigned_finish),
        execution(3, "hold", 1110, unsigned_finish),
    ]
    errors = audit.audit(records)[-1]
    assert not errors, errors
    print("PASS: old unsigned negative execution records audit as signed")


def test_replay_rejects_impossible_position_seed():
    fields = {
        "duration": 100, "start_clock": 1000,
        "start_acc_q32": 0, "end_acc_q32": 1 << 48,
        "velocity": 1 << 32, "accel": 0,
    }
    try:
        audit.replay_pulses(fields, -1_000_000_000)
    except ValueError as exc:
        assert "endpoint displacement" in str(exc)
    else:
        raise AssertionError("impossible replay seed was accepted")
    print("PASS: malformed replay seeds are bounded and rejected")


def test_audit_reports_impossible_seed_without_hanging():
    records = [
        intention("rebase", {
            "start_clock": 1000, "end_clock": 1000, "position_su": 0,
            "acc_q32": 0, "mcu_position": -1_000_000_000}),
        intention("segment", {
            "start_clock": 1000, "end_clock": 1100,
            "duration": 100, "flags": 0, "velocity": 1 << 32,
            "accel": 0, "jerk": 0, "snap": 0, "crackle": 0,
            "start_position_su": 0, "end_position_su": 65536,
            "start_acc_q32": 0, "end_acc_q32": 1 << 48}),
    ]
    errors = audit.audit(records)[-1]
    assert any("endpoint displacement" in error for error in errors), errors
    print("PASS: impossible flight data returns an audit error promptly")


def main():
    test_clean_path_replays_and_matches_boundaries()
    test_underrun_is_a_failed_audit()
    test_intentions_without_execution_evidence_fail_closed()
    test_unsigned_persisted_negative_position_is_normalized()
    test_replay_rejects_impossible_position_seed()
    test_audit_reports_impossible_seed_without_hanging()
    print("ALL PASS")


if __name__ == "__main__":
    main()
