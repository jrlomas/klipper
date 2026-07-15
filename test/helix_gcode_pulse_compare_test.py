#!/usr/bin/env python3
"""Regression tests for mixed-clock holds in sliced-G-code replay."""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import helix_gcode_pulse_compare as replay  # noqa: E402


def stream(hold_command, next_clock):
    commands = [
        "config_traj_stepper oid=5 invert_dir=0",
        "trajectory_rebase_local oid=5 machine_clock=1000"
        " local_clock=64000 pos=0 mcu_pos=0",
        "%s oid=5 duration=64000" % (hold_command,),
        "trajectory_rebase_local oid=5 machine_clock=14000"
        " local_clock=%d pos=0 mcu_pos=0" % (next_clock,),
    ]
    return replay.trajectory_streams(commands)


def test_local_hold_preserves_secondary_ticks():
    # 64k local ticks end at 128k. The immutable next-local-clock barrier is
    # accepted exactly at that horizon.
    pulses = replay.replay_trajectory(
        stream("traj_hold_local", 128000), object(), 64_000_000, 12_000_000)
    assert pulses == {5: []}


def test_legacy_hold_exposes_rebase_overlap():
    # The same numeric duration on the legacy command means 64k primary
    # ticks, or about 341k EBB ticks. The old offline replay failed to model
    # that expansion and therefore missed the physical shutdown.
    try:
        replay.replay_trajectory(
            stream("traj_hold", 128000), object(), 64_000_000, 12_000_000)
    except ValueError as exc:
        assert "rebase overlaps trajectory" in str(exc)
    else:
        raise AssertionError("machine-time hold overlap was not rejected")


def test_rebase_order_is_wrap_safe():
    assert not replay.timer_is_before(128, 0xfffffff0)
    assert replay.timer_is_before(0xfffffff0, 128)


if __name__ == "__main__":
    test_local_hold_preserves_secondary_ticks()
    test_legacy_hold_exposes_rebase_overlap()
    test_rebase_order_is_wrap_safe()
    print("PASS: local holds retain secondary-MCU duration semantics")
    print("PASS: replay rejects a machine-time hold crossing a local rebase")
    print("PASS: replay orders local rebases safely across clock wrap")
