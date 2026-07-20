#!/usr/bin/env python3
"""Regression tests for ADC effective-resolution analysis."""

import math, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

import analyze_adc_enob


def test_ideal_128x_ceiling_and_dc_correlation():
    values = [32760 + value for value in (-2, -1, 0, 1, 2, 1, 0, -1) * 20]
    result = analyze_adc_enob.analyze(values, 12, 128, 3)
    assert result['representation_bits'] == 16
    assert result['ideal_oversample_gain_bits'] == 3.5
    assert result['ideal_enob_ceiling'] == 15.5
    assert result['dc']['sigma_codes'] > 0.
    assert -1. <= result['dc']['lag1_correlation'] <= 1.
    assert result['dc']['dc_resolution_resolved']
    assert result['dc']['rms_noise_limited_bits'] is not None


def test_stuck_dc_code_does_not_claim_resolution():
    result = analyze_adc_enob.analyze([32760] * 100, 12, 128, 3)
    assert not result['dc']['dc_resolution_resolved']
    assert result['dc']['rms_noise_limited_bits'] is None
    assert result['dc']['noise_free_bits'] is None


def test_sine_fit_counts_distortion_and_noise_as_residual():
    count, rate, frequency = 1000, 1000., 7.
    values = []
    for pos in range(count):
        phase = 2. * math.pi * frequency * pos / rate
        values.append(2048 + 1000 * math.sin(phase)
                      + 2 * math.sin(2 * phase))
    result = analyze_adc_enob.analyze(
        values, 12, 1, 0, rate, frequency)
    assert abs(result['sine']['fundamental_amplitude_codes'] - 1000) < 1.e-6
    assert 1.3 < result['sine']['residual_rms_codes'] < 1.5
    assert result['sine']['sinad_enob'] > 8.


if __name__ == '__main__':
    test_ideal_128x_ceiling_and_dc_correlation()
    test_stuck_dc_code_does_not_claim_resolution()
    test_sine_fit_counts_distortion_and_noise_as_residual()
    print('PASS: ADC ENOB ceilings and measured residuals are explicit')
