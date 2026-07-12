#!/usr/bin/env python3
# Standalone unit test for higher-order (cubic / quintic) trajectory
# Bezier segments (FD-0001 doc 02 "Higher-order segments").
#
# Proves the NON-NEGOTIABLE invariant of the segment protocol: the MCU's
# exact per-segment end delta equals, bit-for-bit, the host reference
# computation, so chaining N segments accumulates zero drift. The host
# reference exists in two independent implementations that MUST agree:
#   * the host-compiled C (klippy/chelper/segfit.c:segfit_end_delta_ho),
#     which is the SAME integer algorithm the MCU runs in
#     src/trajq.c:trajq_end_delta_seg(); and
#   * the pure-Python mirror (trajectory_queuing.py:py_end_delta_ho).
# It also checks the Bezier control-point -> power-basis -> wire
# quantization lands on the intended endpoint within mechanical
# tolerance.
#
# Exits 0 on success, non-zero on failure. Run: python3 test/<name>.py
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
import random
import sys

KDIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "klippy")
sys.path.insert(0, KDIR)
sys.path.insert(0, os.path.join(KDIR, "extras"))

import chelper  # noqa: E402
import trajectory_queuing as tqm  # noqa: E402

Q32 = 2.0 ** 32
# Skip cases whose exact delta cannot be represented in the fixed 64-bit
# accumulator (the MCU would shutdown on these; the host C wraps). They
# are far outside any physical move.
INT64_SAFE = 1 << 60


def test_c_vs_python_bit_exact(lib):
    # The host C (== MCU algorithm) and the Python mirror must agree
    # bit-for-bit across the physically reachable coefficient space.
    random.seed(20260712)
    tested = skipped = 0
    for _ in range(300000):
        d = random.randint(1, 1 << 20)
        v = random.randint(-(1 << 18), 1 << 18)
        a = random.randint(-(1 << 26), 1 << 26)
        # cubic half the time, quintic the other half
        if random.random() < 0.5:
            j = random.randint(-(1 << 15), 1 << 15)
            s = c = 0
        else:
            j = random.randint(-(1 << 14), 1 << 14)
            s = random.randint(-(1 << 12), 1 << 12)
            c = random.randint(-(1 << 9), 1 << 9)
        pval = tqm.py_end_delta_ho(d, v, a, j, s, c)
        if abs(pval) >= INT64_SAFE:
            skipped += 1
            continue
        cval = int(lib.segfit_end_delta_ho(d, v, a, j, s, c))
        tested += 1
        if cval != pval:
            raise AssertionError(
                "C/Python end_delta mismatch: seg=%r C=%d py=%d"
                % ((d, v, a, j, s, c), cval, pval))
    if tested < 80000:
        raise AssertionError("too few physical cases tested (%d)" % tested)
    print("  C == Python end_delta_ho over %d physical cases (%d skipped): OK"
          % (tested, skipped))


def test_zero_drift_chain(lib):
    # Chain a long mixed stream of cubic and quintic segments. The host
    # accumulator (Python) and the C accumulator must stay identical at
    # every boundary -- that identity IS "zero drift": both sides agree
    # on where every segment starts and ends, forever.
    random.seed(99)
    acc_py = 0
    acc_c = 0
    n = 4000
    for i in range(n):
        d = random.randint(1000, 1 << 18)
        v = random.randint(-(1 << 17), 1 << 17)
        a = random.randint(-(1 << 24), 1 << 24)
        if i % 2 == 0:
            coeffs = (v, a, random.randint(-(1 << 14), 1 << 14), 0, 0)
        else:
            coeffs = (v, a, random.randint(-(1 << 13), 1 << 13),
                      random.randint(-(1 << 11), 1 << 11),
                      random.randint(-(1 << 8), 1 << 8))
        dp = tqm.py_end_delta_ho(d, *coeffs)
        if abs(dp) >= INT64_SAFE:
            continue
        dc = int(lib.segfit_end_delta_ho(d, *coeffs))
        if dp != dc:
            raise AssertionError("chain step %d: C=%d py=%d" % (i, dc, dp))
        acc_py += dp
        acc_c += dc
        if acc_py != acc_c:
            raise AssertionError(
                "accumulator diverged at step %d: py=%d c=%d"
                % (i, acc_py, acc_c))
    # A second, independent replay must reproduce the exact same anchor.
    random.seed(99)
    replay = 0
    for i in range(n):
        d = random.randint(1000, 1 << 18)
        v = random.randint(-(1 << 17), 1 << 17)
        a = random.randint(-(1 << 24), 1 << 24)
        if i % 2 == 0:
            coeffs = (v, a, random.randint(-(1 << 14), 1 << 14), 0, 0)
        else:
            coeffs = (v, a, random.randint(-(1 << 13), 1 << 13),
                      random.randint(-(1 << 11), 1 << 11),
                      random.randint(-(1 << 8), 1 << 8))
        dp = tqm.py_end_delta_ho(d, *coeffs)
        if abs(dp) >= INT64_SAFE:
            continue
        replay += dp
    if replay != acc_py:
        raise AssertionError("replay drift: %d != %d" % (replay, acc_py))
    print("  zero-drift chain of %d cubic/quintic segments, host==MCU"
          " accumulator bit-exact: OK (final anchor=%d Q32.32)"
          % (n, acc_py))


def _bezier_check(name, P_mm, D, su_per_mm, tol_native):
    P_su = [p * su_per_mm for p in P_mm]
    order, c = tqm.bezier_to_wire(P_su, D)
    args = [c['v'], c['a'], c['j']]
    if 's' in c:
        args += [c['s'], c['c']]
    ed = tqm.py_end_delta_ho(D, *args)
    end_su = ed / Q32
    analytic = (P_mm[-1] - P_mm[0]) * su_per_mm
    err_su = end_su - analytic
    err_native = err_su / tqm.SUBUNITS
    if abs(err_native) > tol_native:
        raise AssertionError(
            "%s Bezier endpoint off by %.4f native units (> %.4f): %r"
            % (name, err_native, tol_native, c))
    print("  %s Bezier endpoint err=%.3f sub-units (%.2e native units,"
          " far below 1 microstep): OK" % (name, err_su, err_native))


def test_bezier_fidelity():
    # A cubic and a quintic Bezier, monotonic (no velocity reversal),
    # over a realistic ~8 ms jerk-limited move at 1280 microsteps/mm.
    su_per_mm = tqm.SUBUNITS * 1280.0 / 32.0   # 2621440 su/mm
    F = 100e6
    D = int(0.008 * F)
    _bezier_check("cubic", [10.0, 10.4, 11.6, 12.0], D, su_per_mm, 0.01)
    _bezier_check("quintic", [0.0, 0.1, 0.5, 1.5, 1.9, 2.0], D, su_per_mm,
                  0.01)


def test_degenerate_and_validation():
    su_per_mm = tqm.SUBUNITS
    D = 100000
    # A cubic with collinear, evenly spaced control points is a straight
    # constant-velocity line: accel and jerk must quantize to ~0.
    order, c = tqm.bezier_to_wire([p * su_per_mm for p in
                                   [0.0, 1.0, 2.0, 3.0]], D)
    if c['a'] != 0 or c['j'] != 0:
        raise AssertionError("linear cubic should have a=j=0, got %r" % c)
    if order != tqm.TSEG_POLY_CUBIC:
        raise AssertionError("wrong order flag for cubic")
    # Wrong control-point count is rejected.
    for bad in ([0.0, 1.0], [0.0, 1.0, 2.0], [0.0] * 5, [0.0] * 7):
        try:
            tqm.bezier_to_wire(bad, D)
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for %d ctrl pts"
                                 % len(bad))
    print("  linear-cubic degeneracy (a=j=0) and control-point validation:"
          " OK")


def main():
    ffi_main, lib = chelper.get_ffi()
    if not hasattr(lib, 'segfit_end_delta_ho'):
        raise AssertionError("chelper missing segfit_end_delta_ho")
    test_c_vs_python_bit_exact(lib)
    test_zero_drift_chain(lib)
    test_bezier_fidelity()
    test_degenerate_and_validation()
    print("traj_higher_order_test: OK")
    return 0


if __name__ == '__main__':
    sys.exit(main())
