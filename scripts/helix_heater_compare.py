#!/usr/bin/env python3
"""Compare paired heater qualification summaries against release gates."""

import argparse
import json
import sys


def load(path):
    with open(path) as source:
        return json.load(source)


def compare(baseline, candidate):
    checks = {}

    def check(name, passed, measured, limit):
        checks[name] = {
            'pass': bool(passed), 'measured': measured, 'limit': limit}

    check('same_target',
          baseline['target_c'] == candidate['target_c'],
          candidate['target_c'], baseline['target_c'])
    check('same_ready_band',
          baseline['ready_band_c'] == candidate['ready_band_c'],
          candidate['ready_band_c'], baseline['ready_band_c'])
    check('same_ready_hold',
          baseline['ready_hold_s'] == candidate['ready_hold_s'],
          candidate['ready_hold_s'], baseline['ready_hold_s'])
    initial_delta = abs(candidate['initial_temperature_c']
                        - baseline['initial_temperature_c'])
    check('initial_temperature_delta', initial_delta <= 1., initial_delta, 1.)
    time_limit = baseline['time_to_print_s'] * 1.05
    check('time_to_print',
          candidate['time_to_print_s'] <= time_limit,
          candidate['time_to_print_s'], time_limit)
    rms_limit = baseline['steady_temperature_error_rms_c'] * 1.05 + .02
    check('steady_temperature_error_rms',
          candidate['steady_temperature_error_rms_c'] <= rms_limit,
          candidate['steady_temperature_error_rms_c'], rms_limit)
    peak_limit = baseline['steady_temperature_peak_error_c'] + .05
    check('steady_temperature_peak_error',
          candidate['steady_temperature_peak_error_c'] <= peak_limit,
          candidate['steady_temperature_peak_error_c'], peak_limit)
    duty_limit = baseline['steady_power_delta_rms'] * .5
    check('steady_power_delta_rms',
          candidate['steady_power_delta_rms'] <= duty_limit,
          candidate['steady_power_delta_rms'], duty_limit)
    overshoot_limit = baseline['overshoot_c'] + .25
    check('overshoot', candidate['overshoot_c'] <= overshoot_limit,
          candidate['overshoot_c'], overshoot_limit)
    check('fault_samples', not candidate['fault_samples'],
          candidate['fault_samples'], 0)
    return {'pass': all(item['pass'] for item in checks.values()),
            'checks': checks}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline', required=True)
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--output')
    args = parser.parse_args()
    result = compare(load(args.baseline), load(args.candidate))
    rendered = json.dumps(result, indent=2, sort_keys=True) + '\n'
    if args.output:
        with open(args.output, 'w') as output:
            output.write(rendered)
    else:
        sys.stdout.write(rendered)
    return 0 if result['pass'] else 1


if __name__ == '__main__':
    sys.exit(main())
