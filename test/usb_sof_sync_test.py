#!/usr/bin/env python3
# Unit tests for USB SOF affine comparison math.

import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                "../klippy"))

from extras.machine_time_sync_line import fit_affine_residuals  # noqa: E402


def test_sof_pair_fit_removes_clock_rate_and_phase():
    jitter = [0., 1., -1., 0., 2., -2.]
    pairs = []
    for frame, error in enumerate(jitter):
        primary = 100000 + frame * 12000
        secondary = 900000 + frame * 64000 + error
        pairs.append((primary, secondary))
    slope, _, residuals = fit_affine_residuals(pairs)
    assert abs(slope - 64. / 12.) < 2.e-5
    assert statistics.pstdev(residuals) < 1.5


if __name__ == '__main__':
    test_sof_pair_fit_removes_clock_rate_and_phase()
    print("ALL PASS")
