#!/usr/bin/env python3
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'klippy'))
from extras import tmc5160


def make_helper(sense_resistor, globalscaler_min):
    helper = tmc5160.TMC5160CurrentHelper.__new__(
        tmc5160.TMC5160CurrentHelper)
    helper.sense_resistor = sense_resistor
    helper.globalscaler_min = globalscaler_min
    return helper


def field_current(helper, globalscaler, bits):
    scaler = globalscaler or 256
    return (scaler * (bits + 1) * tmc5160.VREF
            / (256. * 32. * math.sqrt(2.) * helper.sense_resistor))


def test_legacy_scaler_floor_is_compatible():
    helper = make_helper(.075, 32)
    globalscaler, irun, ihold = helper._calc_current(.6, .6)
    assert (globalscaler, irun, ihold) == (50, 31, 31)
    assert abs(field_current(helper, globalscaler, irun) - .6) < .01


def test_recommended_scaler_matches_fluidnc_strategy():
    helper = make_helper(.075, 128)
    globalscaler, irun, ihold = helper._calc_current(.6, .6)
    assert (globalscaler, irun, ihold) == (133, 11, 11)
    assert abs(field_current(helper, globalscaler, irun) - .6) < .005


def test_low_hold_current_uses_same_scaler():
    helper = make_helper(.075, 128)
    globalscaler, irun, ihold = helper._calc_current(.6, .3)
    assert (globalscaler, irun, ihold) == (133, 11, 5)
    assert abs(field_current(helper, globalscaler, ihold) - .3) < .005


def test_below_recommended_range_clamps_safely():
    helper = make_helper(.075, 128)
    globalscaler, irun, ihold = helper._calc_current(.05, .05)
    assert (globalscaler, irun, ihold) == (133, 0, 0)
    assert abs(field_current(helper, globalscaler, irun) - .05) < .002


def test_rodent_obsolete_22_milliohm_value_explains_underdrive():
    # Early Rodent schematics and example configurations incorrectly stated
    # 22mOhm.  The board must be configured as 75mOhm.  Registers selected for
    # the obsolete value deliver only about 0.18A when the user requests 0.6A.
    configured = make_helper(.022, 128)
    actual = make_helper(.075, 128)
    globalscaler, irun, _ = configured._calc_current(.6, .6)
    delivered = field_current(actual, globalscaler, irun)
    assert .17 < delivered < .18
    assert abs(.6 / delivered - 3.4) < .1


if __name__ == '__main__':
    test_legacy_scaler_floor_is_compatible()
    test_recommended_scaler_matches_fluidnc_strategy()
    test_low_hold_current_uses_same_scaler()
    test_below_recommended_range_clamps_safely()
    test_rodent_obsolete_22_milliohm_value_explains_underdrive()
    print("PASS: TMC5160 current scaler selection")
