#!/usr/bin/env python3
# Unit tests for the physical machine-time sync-line math.

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                "../klippy"))

from extras.machine_time_sync_line import (  # noqa: E402
    _signed32, fit_affine_residuals, predict_local_clock)


def test_signed32_wrap():
    assert _signed32(0xffffffff) == -1
    assert _signed32(0x80000000) == -(1 << 31)
    assert _signed32(0x7fffffff) == (1 << 31) - 1


def test_prediction_matches_firmware_q24_math():
    mapping = {
        'machine_ref': 0xfffffff0,
        'local_ref': 1000,
        'rate': 4 << 24,
    }
    assert predict_local_clock(0x10, mapping) == 1128
    mapping['rate'] = (4 << 24) + (1 << 23)
    assert predict_local_clock(0x10, mapping) == 1144


def test_affine_fit_isolates_capture_jitter():
    # 64MHz secondary against 12MHz primary, plus a fixed propagation/ISR
    # offset. The affine fit must remove phase and rate, leaving only jitter.
    noise = [0., 1., -1., 2., -2.]
    samples = []
    for i, err in enumerate(noise):
        primary = 1000000 + i * 120000
        secondary = 8000000 + i * 640000 + err
        samples.append((primary, secondary))
    slope, _, residuals = fit_affine_residuals(samples)
    assert abs(slope - 64. / 12.) < 1.e-5
    assert max(abs(v) for v in residuals) < 2.4


def main():
    test_signed32_wrap()
    test_prediction_matches_firmware_q24_math()
    test_affine_fit_isolates_capture_jitter()
    print("ALL PASS")


if __name__ == '__main__':
    main()
