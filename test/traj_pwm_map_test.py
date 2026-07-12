#!/usr/bin/env python3
# Standalone unit test for the trajectory PWM/DAC sub-unit -> duty
# mapping (RFC 0001 doc 04 PWM/DAC backend).  Exercises the pure
# host-side subunit_to_duty() helper, which mirrors traj_pwm_duty() in
# src/traj_pwm.c bit for bit: duty = pos_su * max_value / scale,
# truncated and clamped to [0, max_value].  No printer/MCU/chelper is
# required.  Exits 0 on success, non-zero on failure.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                "..", "klippy"))

from extras.trajectory_pwm import subunit_to_duty  # noqa: E402


def check(pos_su, scale, max_value, expect):
    got = subunit_to_duty(pos_su, scale, max_value)
    if got != expect:
        raise AssertionError(
            "subunit_to_duty(%r, %r, %r) = %r, expected %r"
            % (pos_su, scale, max_value, got, expect))


def main():
    SCALE = 65536   # full_scale = 1.0 native unit -> full duty
    MAX = 1000      # PWM_MAX

    # Zero and negative positions produce no output.
    check(0, SCALE, MAX, 0)
    check(-1, SCALE, MAX, 0)
    check(-1000000, SCALE, MAX, 0)

    # Linear region.
    check(SCALE // 2, SCALE, MAX, 500)          # half scale -> half duty
    check(SCALE // 4, SCALE, MAX, 250)
    check(100, SCALE, MAX, 1)                    # truncation toward zero
    check(65, SCALE, MAX, 0)                     # 65*1000//65536 == 0

    # Full scale and clamp.
    check(SCALE, SCALE, MAX, MAX)                # exactly full scale
    check(SCALE + 1, SCALE, MAX, MAX)            # clamps at the top
    check(SCALE * 100, SCALE, MAX, MAX)          # far past full scale

    # Degenerate scale is safe (matches the C div-by-zero guard).
    check(12345, 0, MAX, 0)

    # A different full-scale mapping (full_scale = 2.0).
    check(65536, 131072, MAX, 500)              # 1.0 native of 2.0 -> 50%
    check(131072, 131072, MAX, MAX)

    print("traj_pwm_map_test: OK")
    return 0


if __name__ == '__main__':
    sys.exit(main())
