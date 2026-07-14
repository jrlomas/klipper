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


if __name__ == "__main__":
    main()
