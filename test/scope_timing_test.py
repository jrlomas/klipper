#!/usr/bin/env python3
"""Unit checks for the Helix sigrok timing-correlation helper."""

import importlib.util
import os
import tempfile


SCRIPT = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                      '../scripts/helix_scope_timing.py')
SPEC = importlib.util.spec_from_file_location('helix_scope_timing', SCRIPT)
scope = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scope)


def test_csv_transition_parser():
    contents = """; generated\nlogic,logic\n0,0\n0,0\n1,0\n1,1\n"""
    with tempfile.NamedTemporaryFile('w', delete=False) as csv_file:
        csv_file.write(contents)
        path = csv_file.name
    try:
        parsed = scope.parse_sigrok_csv(path)
    finally:
        os.unlink(path)
    assert parsed == {'initial': (0, 0), 'edge_samples': (2, 3)}
    print("PASS: CSV parser finds both channel edge samples")


def test_combined_gcode_timing_response():
    messages = [{
        'time': 20.,
        'message': (
            "// mcu 'mcu': value=1 dropped=0 scheduled=100 actual=107"
            " late=7 ticks (0.583us)\n"
            "// mcu 'ebb36': value=1 dropped=0 scheduled=500 actual=652"
            " late=152 ticks (2.375us)"),
    }, {
        'time': 10.,
        'message': ("// mcu 'mcu': value=1 dropped=0 scheduled=1 actual=2"
                    " late=1 ticks (0.083us)"),
    }]
    states = scope.parse_timing_messages(messages, 1, after_time=15.)
    assert sorted(states) == ['ebb36', 'mcu']
    assert states['mcu']['late_ticks'] == 7
    assert states['ebb36']['late_ticks'] == 152
    print("PASS: combined response yields fresh timing for both MCUs")


def test_summary():
    summary = scope.summarize([-2., 1., 3.])
    assert summary['count'] == 3
    assert summary['min'] == -2.
    assert summary['max'] == 3.
    assert summary['max_abs'] == 3.
    print("PASS: timing summary retains signed range and absolute worst case")


def main():
    test_csv_transition_parser()
    test_combined_gcode_timing_response()
    test_summary()
    print("ALL PASS")


if __name__ == '__main__':
    main()
