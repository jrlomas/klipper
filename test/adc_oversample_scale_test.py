#!/usr/bin/env python3
"""Check scale preservation for automatic ADC hardware oversampling."""

import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'klippy'))

import mcu


def test_hardware_scale_and_16bit_limit():
    assert mcu._adc_hardware_scale(4095, 128, 7) == 1.
    assert mcu._adc_hardware_scale(4095, 128, 3) == 16.
    assert 4095 * mcu._adc_hardware_scale(4095, 128, 3) == 65520
    try:
        mcu._adc_hardware_scale(4095, 256, 3)
    except ValueError:
        pass
    else:
        raise AssertionError('overflowing hardware ADC scale was accepted')


if __name__ == '__main__':
    test_hardware_scale_and_16bit_limit()
    print('PASS: ADC oversample scale is preserved within 16-bit transport')
