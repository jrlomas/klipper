#!/usr/bin/env python3
"""Regression tests for deterministic HELIX flight-recorder replay."""

import json
import pathlib
import sys
import tempfile

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


def mixed_clock_records(explicit_clocks):
    velocity, exec_duration = 65_536_000, 100
    finish = audit.end_delta(exec_duration, velocity, 0)
    rebase = {
        "start_clock": 1000, "end_clock": 1000, "position_su": 0,
        "acc_q32": 0, "mcu_position": 0}
    segment = {
        "start_clock": 1000, "end_clock": 1020, "duration": 20,
        "execution_duration": exec_duration,
        "flags": 0, "velocity": velocity, "accel": 0, "jerk": 0,
        "snap": 0, "crackle": 0, "start_position_su": 0,
        "end_position_su": finish >> 32, "start_acc_q32": 0,
        "end_acc_q32": finish}
    hold = {
        "start_clock": 1020, "end_clock": 1022, "duration": 2,
        "execution_duration": 10,
        "flags": 1, "velocity": 0, "accel": 0, "jerk": 0,
        "snap": 0, "crackle": 0, "start_position_su": finish >> 32,
        "end_position_su": finish >> 32, "start_acc_q32": finish,
        "end_acc_q32": finish}
    if explicit_clocks:
        rebase.update(execution_start_clock=5000,
                      execution_end_clock=5000)
        segment.update(execution_start_clock=5000,
                       execution_end_clock=5100)
        hold.update(execution_start_clock=5100,
                    execution_end_clock=5110)
    return [
        intention("rebase", rebase),
        intention("segment", segment),
        intention("hold", hold),
        execution(20, "rebase", 5000, 0),
        execution(21, "segment_done", 5100, finish >> 32),
        execution(22, "segment_done", 5110, finish >> 32),
        execution(23, "hold", 5110, finish >> 32),
    ]


def test_explicit_mixed_clock_metadata_replays_in_execution_domain():
    summaries, matched, unused_triggers, executed, errors = audit.audit(
        mixed_clock_records(True))
    assert summaries and not errors, errors
    assert matched == 3 and executed == 4
    print("PASS: explicit mixed-clock metadata audits in the MCU domain")


def test_legacy_mixed_clock_metadata_is_anchored_by_execution_rebase():
    summaries, matched, unused_triggers, executed, errors = audit.audit(
        mixed_clock_records(False))
    assert summaries and not errors, errors
    assert matched == 3 and executed == 4
    print("PASS: legacy mixed-clock telemetry infers its MCU clock anchor")


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


def test_stale_ring_records_before_rebase_sequence_are_ignored():
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
        # This stale record even aliases the current wire-clock interval; its
        # sequence proves that it predates the matched rebase.
        execution(99, "segment_done", 1050, 123),
        execution(100, "rebase", 1000, 0),
        execution(101, "segment_done", 1100, finish >> 32),
        execution(102, "segment_done", 1110, finish >> 32),
        execution(103, "hold", 1110, finish >> 32),
    ]
    summaries, matched, unused_triggers, executed, errors = audit.audit(records)
    assert summaries and not errors, errors
    assert matched == 3 and executed == 4
    print("PASS: stale recorder entries before the rebase are ignored")


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


def test_streaming_stats_match_materialized_replay():
    fields = {
        "duration": 100, "start_clock": 1000,
        "start_acc_q32": 0,
        "end_acc_q32": audit.end_delta(100, 65_536_000, 0),
        "velocity": 65_536_000, "accel": 0,
    }
    pulses, expected_mpos = audit.replay_pulses(fields, 0)
    count, mpos, min_interval, last_clock = audit.replay_pulse_stats(
        fields, 0)
    intervals = [b[0] - a[0] for a, b in zip(pulses, pulses[1:])
                 if b[0] > a[0]]
    assert count == len(pulses)
    assert mpos == expected_mpos
    assert min_interval == min(intervals)
    assert last_clock == pulses[-1][0]
    print("PASS: streaming pulse statistics preserve exact replay results")


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


def test_record_loader_isolates_latest_session_and_line_floor():
    records = [
        {"session_id": "old", "kind": "trace", "machine_time": 5.},
        {"session_id": "new", "kind": "trace", "machine_time": 5.},
        {"session_id": "new", "kind": "trace", "machine_time": 6.},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "telemetry.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        loaded = audit.load_records(path, session_id="latest")
        assert [r["session_id"] for r in loaded] == ["new", "new"]
        loaded = audit.load_records(path, session_id="new", after_line=2)
        assert [r["machine_time"] for r in loaded] == [6.]
        loaded = audit.load_records(path, before_line=3)
        assert [r["session_id"] for r in loaded] == ["old", "new"]
    print("PASS: audit loader isolates restart sessions and line floors")


def main():
    test_clean_path_replays_and_matches_boundaries()
    test_explicit_mixed_clock_metadata_replays_in_execution_domain()
    test_legacy_mixed_clock_metadata_is_anchored_by_execution_rebase()
    test_underrun_is_a_failed_audit()
    test_intentions_without_execution_evidence_fail_closed()
    test_stale_ring_records_before_rebase_sequence_are_ignored()
    test_unsigned_persisted_negative_position_is_normalized()
    test_replay_rejects_impossible_position_seed()
    test_streaming_stats_match_materialized_replay()
    test_audit_reports_impossible_seed_without_hanging()
    test_record_loader_isolates_latest_session_and_line_floor()
    print("ALL PASS")


if __name__ == "__main__":
    main()
