#!/usr/bin/env python3
"""Regression tests for per-heater semantic type defaults."""

import pathlib
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from klippy.extras import heaters


def main():
    assert heaters.HEATER_TYPES == ('bed', 'hotend', 'chamber', 'generic')
    assert heaters.default_heater_type('heater_bed') == 'bed'
    assert heaters.default_heater_type('extruder') == 'hotend'
    assert heaters.default_heater_type('extruder1') == 'hotend'
    assert heaters.default_heater_type('build_plate_left') == 'generic'
    assert heaters.default_heater_gain_time('bed') == 60.
    assert heaters.default_heater_gain_time('hotend') == 20.
    assert heaters.default_heater_gain_time('chamber') == 20.
    print('PASS: heater type inference and type-specific defaults')


if __name__ == '__main__':
    main()
