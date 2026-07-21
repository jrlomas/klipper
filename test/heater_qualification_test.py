#!/usr/bin/env python3
"""Regression tests for physical heater qualification metrics."""

import importlib.util
import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'helix_heater_qualification',
    ROOT / 'scripts' / 'helix_heater_qualification.py')
qualification = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qualification)


def row(stamp, temperature, power=0., fault=0):
    return {
        'elapsed_s': stamp, 'temperature_c': temperature,
        'power': power, 'fault': fault,
    }


def main():
    rows = [row(0., 25., 1.), row(10., 54.2, .5),
            row(20., 53., .6), row(30., 54.5, .5),
            row(60., 55.2, .4), row(90., 54.9, .4)]
    # The first band entry at 10 seconds is not readiness because the
    # temperature subsequently leaves the band.  Readiness begins at 30.
    summary = qualification.summarize(rows, 55., 1., 60.)
    assert summary['time_to_print_s'] == 30.
    assert summary['time_to_first_crossing_s'] == 60.
    assert abs(summary['overshoot_c'] - .2) < 1.e-9
    assert summary['steady_samples'] == 3
    assert summary['fault_samples'] == 0
    print('PASS: heater qualification reports sustained time-to-print')


if __name__ == '__main__':
    main()
