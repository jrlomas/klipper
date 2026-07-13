#!/usr/bin/env python3
# Unit test for the trajectory PWM value-trajectory fitter integration
# (FD-0001 doc 04): ValueTrajectoryFitter runs a piecewise-linear
# (print_time, value) polyline through the SAME C segfit fitter the
# stepper path uses and emits chained quadratic wire segments.
#
# Asserts the two invariants that matter:
#   * exactness - the fitter's chained Q32.32 anchor equals the sum of
#     segfit_end_delta_ho() over the emitted segments (what the MCU's
#     accumulator computes), i.e. zero drift by construction; and
#   * fidelity - the quantized piecewise polynomial tracks the commanded
#     polyline within the configured tolerance at every sample point.
#
# Needs chelper (builds c_helper.so); skips politely without a compiler.
# Exits 0 on success. Run: python3 test/traj_pwm_fitter_test.py
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
import sys

KDIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..",
                    "klippy")
sys.path.insert(0, KDIR)
sys.path.insert(0, os.path.join(KDIR, "extras"))

MCU_FREQ = 16000000.
SAMPLE_TIME = 0.001
Q32 = 2.0 ** 32


def poly_value(segs, ticks):
    # Evaluate the emitted quantized piecewise polynomial at an absolute
    # tick offset from the anchor; returns sub-units (float).
    pos = 0.
    t = ticks
    for (d, v, a) in segs:
        vq, aq = v / 65536., a / Q32
        if t <= d:
            return pos + vq * t + .5 * aq * t * t
        pos += vq * d + .5 * aq * d * d
        t -= d
    return pos


def main():
    try:
        import chelper
        _, lib = chelper.get_ffi()
    except Exception as e:
        print("SKIP: chelper unavailable (%s)" % (e,))
        return 0
    from trajectory_pwm import ValueTrajectoryFitter, SUBUNITS

    tol_su = SUBUNITS / 256.
    fit = ValueTrajectoryFitter(MCU_FREQ, tol_su, SAMPLE_TIME)

    # Laser-raster-shaped polyline: ramp up, hold, ramp down, hold at 0.
    knots = [(1.000, 0.00), (1.500, 0.80), (1.700, 0.80),
             (2.000, 0.20), (2.100, 0.00), (2.300, 0.00)]
    pos_su0 = fit.anchor(knots[0][0], knots[0][1])
    assert pos_su0 == 0

    segs = []
    end_su = fit.feed(knots, lambda d, v, a: segs.append((d, v, a)))
    assert segs, "fitter emitted no segments"

    # --- exactness: chained anchor == sum of MCU-convention end deltas
    acc = pos_su0 << 32
    total_ticks = 0
    for (d, v, a) in segs:
        acc += int(lib.segfit_end_delta_ho(d, v, a, 0, 0, 0))
        total_ticks += d
    assert (acc >> 32) == end_su, ((acc >> 32), end_su)

    # --- coverage: emitted duration spans the polyline to within one
    # sample quantum (the fitter samples on the sample_time grid).
    span_ticks = (knots[-1][0] - knots[0][0]) * MCU_FREQ
    sample_ticks = SAMPLE_TIME * MCU_FREQ
    assert abs(span_ticks - total_ticks) <= sample_ticks + 1, \
        (span_ticks, total_ticks)

    # --- fidelity: quantized polynomial vs the commanded polyline at
    # every sample instant, within tolerance (+1 su rounding slack).
    def polyline(t):
        for (t0, v0), (t1, v1) in zip(knots[:-1], knots[1:]):
            if t <= t1:
                return v0 + (v1 - v0) * (t - t0) / (t1 - t0)
        return knots[-1][1]
    n_samples = int(total_ticks // sample_ticks)
    worst = 0.
    for i in range(1, n_samples + 1):
        ticks = i * sample_ticks
        t = knots[0][0] + ticks / MCU_FREQ
        got_su = poly_value(segs, ticks)
        want_su = polyline(t) * SUBUNITS
        worst = max(worst, abs(got_su - want_su))
    assert worst <= tol_su + 1., "worst deviation %.2f su > tol %.2f" \
        % (worst, tol_su)
    print("  fit: %d segments, worst deviation %.2f su (tol %.2f): OK"
          % (len(segs), worst, tol_su))

    # --- end value lands on the last knot within tolerance
    assert abs(end_su - knots[-1][1] * SUBUNITS) <= tol_su + 1.

    # --- continuation: a second chunk starting at the stream end chains
    # exactly (no rebase), and discontinuous times are rejected.
    knots2 = [(fit.end_time, fit.end_value), (fit.end_time + 0.5, 0.60)]
    segs2 = []
    end_su2 = fit.feed(knots2, lambda d, v, a: segs2.append((d, v, a)))
    acc2 = acc
    for (d, v, a) in segs2:
        acc2 += int(lib.segfit_end_delta_ho(d, v, a, 0, 0, 0))
    assert (acc2 >> 32) == end_su2
    assert abs(end_su2 - 0.60 * SUBUNITS) <= tol_su + 1.
    try:
        fit.feed([(0.5, 0.), (0.4, 1.)], lambda *a: None)
        raise AssertionError("non-increasing times were accepted")
    except ValueError:
        pass
    print("  continuation chunk chains exactly; bad input rejected: OK")

    print("traj_pwm_fitter_test: OK")
    return 0


if __name__ == '__main__':
    sys.exit(main())
