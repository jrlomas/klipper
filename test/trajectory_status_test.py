#!/usr/bin/env python3
"""Regression checks for trajectory status position reporting."""

import os
import sys

KDIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..",
                    "klippy")
sys.path.insert(0, KDIR)
sys.path.insert(0, os.path.join(KDIR, "extras"))

import stepper  # noqa: E402
import trajectory_queuing  # noqa: E402
import mcu  # noqa: E402
from extras import tmc  # noqa: E402


class FakeCommand:
    def __init__(self):
        self.calls = []

    def send(self, args, **kwargs):
        self.calls.append((args, kwargs))


class FakeToolhead:
    def __init__(self):
        self.flushes = 0
        self.dwells = []
        self.waits = 0

    def flush_step_generation(self):
        self.flushes += 1

    def get_last_move_time(self):
        return 10.

    def dwell(self, duration):
        self.dwells.append(duration)

    def wait_moves(self):
        self.waits += 1


class FakeMCU:
    def seconds_to_clock(self, seconds):
        return int(seconds * 1_000_000)

    def print_time_to_clock(self, print_time):
        return int(print_time * 1_000_000)

    def estimated_print_time(self, eventtime):
        return 20.

    def error(self, message):
        return RuntimeError(message)


class FakeStepper:
    def __init__(self, offset_su):
        self.offset_su = offset_su
        self.synced = []

    def commanded_to_mcu_position_su(self, position_mm):
        return int(round(position_mm * 10_000.)) + self.offset_su

    def sync_to_held_position(self, position_su):
        self.synced.append(position_su)


class FakeFFI:
    def segfit_set_anchor(self, *args):
        pass

    def segfit_set_anchor_position(self, *args):
        pass


class FakeMotionQueuing:
    def __init__(self):
        self.activity = []

    def note_mcu_movequeue_activity(self, print_time):
        self.activity.append(print_time)


class FakeReactor:
    def monotonic(self):
        return 100.


class FakeLookupMCU:
    def __init__(self):
        self.calls = []

    def lookup_command(self, message, cq=None):
        self.calls.append((message, cq))
        return "command"


class FakeErrorCheck:
    def __init__(self):
        self.check_timer = object()
        self.stops = 0
        self.starts = 0

    def stop_checks(self):
        self.stops += 1
        self.check_timer = None

    def start_checks(self):
        self.starts += 1
        self.check_timer = object()


class FakeTMC:
    def __init__(self, target_mcu):
        self.target_mcu = target_mcu

    def get_mcu(self):
        return self.target_mcu


def main():
    mcu_stepper = stepper.MCU_stepper.__new__(stepper.MCU_stepper)
    mcu_stepper._step_dist = 0.00625
    mcu_stepper._mcu_position_offset = -100.4625
    physical_su = 204_865_536
    commanded_su = mcu_stepper.mcu_to_commanded_position_su(physical_su)
    assert commanded_su == 120 * 10_485_760

    traj = trajectory_queuing.TrajectoryStepper.__new__(
        trajectory_queuing.TrajectoryStepper)
    traj.mcu_stepper = mcu_stepper
    traj.wire_acc = physical_su << 32
    assert traj.commanded_pos_su() == commanded_su
    print("PASS: trajectory status converts the wire twin to command space")

    # A kinematic-coordinate update must preserve the trajectory engine's
    # exact physical position rather than a stale itersolve cache value.
    mcu_stepper.get_commanded_position = lambda: 32.
    mcu_stepper._set_mcu_position_su(physical_su)
    assert mcu_stepper.mcu_to_commanded_position_su(physical_su) \
        == 32 * 10_485_760
    print("PASS: kinematic updates preserve the trajectory wire position")

    toolhead = FakeToolhead()
    motion_queuing = FakeMotionQueuing()
    reactor = FakeReactor()
    traj = trajectory_queuing.TrajectoryStepper.__new__(
        trajectory_queuing.TrajectoryStepper)
    traj.owner = type("Owner", (), {
        "printer": type("Printer", (), {
            "lookup_object": lambda self, name: (
                toolhead if name == "toolhead" else motion_queuing),
            "get_reactor": lambda self: reactor,
        })(),
    })()
    traj.mcu = FakeMCU()
    traj.mcu_stepper = FakeStepper(offset_su=500_000)
    traj.su_per_mm = 10_000.
    traj.cubic_cmd = object()
    traj.oid = 4
    traj.rebase_cmd = FakeCommand()
    traj.rebase_min_clock = 0
    traj.segfit = object()
    traj.ffi_lib = FakeFFI()
    traj.note_rebase_needed = lambda: None
    traj._wire_rebase = lambda clock, pos, mpos, **kwargs: None
    terminal_holds = []
    traj._queue_terminal_hold = lambda: terminal_holds.append(True)
    captured = []
    delta_su = 20_000
    traj.queue_bezier_segment = lambda duration, points: (
        captured.append(points) or (delta_su << 32))
    end = traj.bezier_move(1., [300_000, 305_000, 315_000, 320_000])
    assert captured == [[800_000, 805_000, 815_000, 820_000]]
    assert traj.rebase_cmd.calls[0][0][2:] == [800_000, 12]
    assert traj.rebase_cmd.calls[0][0][1] == 20_250_000
    assert traj.mcu_stepper.synced == []
    assert end == 320_000
    assert terminal_holds == [True]
    assert motion_queuing.activity == [21.251]
    assert toolhead.waits == 1
    print("PASS: Bezier wire coordinates preserve the physical MCU offset")

    held = 30 * 52_428_800 - 2353
    requested = 30 * 52_428_800
    assert trajectory_queuing._snap_bezier_anchor(requested, held) == held
    assert trajectory_queuing._snap_bezier_anchor(
        held + trajectory_queuing.SUBUNITS + 1, held) is None
    print("PASS: displayed Bezier P0 snaps only within one microstep")

    lookup = FakeLookupMCU()
    queue = object()
    assert mcu.MCU.try_lookup_command(lookup, "optional", cq=queue) \
        == "command"
    assert lookup.calls == [("optional", queue)]
    print("PASS: optional MCU commands retain their serial command queue")

    target_mcu = object()
    checks = FakeErrorCheck()
    tmc_helper = tmc.TMCCommandHelper.__new__(tmc.TMCCommandHelper)
    tmc_helper.mcu_tmc = FakeTMC(target_mcu)
    tmc_helper.echeck_helper = checks
    tmc_helper.printer = type(
        "Printer", (), {"is_shutdown": lambda self: False})()
    tmc_helper._trajectory_suspend_depth = 0
    tmc_helper._trajectory_checks_were_active = False
    tmc_helper._handle_trajectory_standalone_begin(target_mcu)
    tmc_helper._handle_trajectory_standalone_begin(target_mcu)
    assert checks.stops == 1 and checks.check_timer is None
    tmc_helper._handle_trajectory_standalone_end(target_mcu)
    assert checks.starts == 0
    tmc_helper._handle_trajectory_standalone_end(target_mcu)
    assert checks.starts == 1 and checks.check_timer is not None
    print("PASS: standalone higher-order motion brackets TMC polling")


if __name__ == "__main__":
    main()
