#!/usr/bin/env python3
"""Regression tests for heater qualification acceptance gates."""

import importlib.util
import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'helix_heater_compare', ROOT / 'scripts' / 'helix_heater_compare.py')
comparison = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(comparison)


def record(**updates):
    result = {
        'target_c': 60., 'initial_temperature_c': 30.,
        'ready_band_c': 1., 'ready_hold_s': 60.,
        'time_to_print_s': 100., 'steady_temperature_error_rms_c': .4,
        'steady_temperature_peak_error_c': .9,
        'steady_power_delta_rms': .1, 'overshoot_c': .7,
        'fault_samples': 0,
    }
    result.update(updates)
    return result


def main():
    baseline = record()
    accepted = comparison.compare(baseline, record(
        initial_temperature_c=30.5, time_to_print_s=95.,
        steady_temperature_error_rms_c=.3,
        steady_temperature_peak_error_c=.8,
        steady_power_delta_rms=.04, overshoot_c=.8))
    assert accepted['pass']
    slow = comparison.compare(baseline, record(time_to_print_s=106.))
    assert not slow['pass']
    assert not slow['checks']['time_to_print']['pass']
    noisy = comparison.compare(
        baseline, record(steady_power_delta_rms=.051))
    assert not noisy['pass']
    assert not noisy['checks']['steady_power_delta_rms']['pass']
    print('PASS: heater comparison enforces speed and stability gates')


if __name__ == '__main__':
    main()
