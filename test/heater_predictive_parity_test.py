#!/usr/bin/env python3
"""Replay the physical bed envelope through host and fixed-point control."""

import csv
import importlib.util
import pathlib
import subprocess
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'helix_test_heaters', ROOT / 'klippy' / 'extras' / 'heaters.py')
heaters = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(heaters)
TRACE = (ROOT / 'docs' / 'evidence' / 'heater_control'
         / 'host-predictive-bed75-open-blend-20260720.csv')


class Reactor:
    now = 0.

    def monotonic(self):
        return self.now


class Printer:
    def __init__(self, reactor):
        self.reactor = reactor

    def get_reactor(self):
        return self.reactor


class Heater:
    def __init__(self, reactor):
        self.printer = Printer(reactor)
        self.output = 0.

    def get_max_power(self):
        return 1.

    def set_pwm(self, read_time, output):
        self.output = output


def interpolate(stamp, times, values):
    for index in range(1, len(times)):
        if times[index] >= stamp:
            fraction = ((stamp - times[index - 1])
                        / (times[index] - times[index - 1]))
            return values[index - 1] + fraction * (
                values[index] - values[index - 1])
    return values[-1]


def main():
    rows = list(csv.DictReader(TRACE.open()))
    times = [float(row['elapsed_s']) for row in rows]
    temperatures = [float(row['temperature_c']) for row in rows]
    period = .3
    sample_times = [period * (index + 1)
                    for index in range(int(times[-1] / period))]
    samples = [interpolate(stamp, times, temperatures)
               for stamp in sample_times]

    reactor = Reactor()
    heater = Heater(reactor)
    host = heaters.ControlPredictive.from_model(
        heater, period, {'gain': 80., 'tau': 150., 'delay': 2.}, {
            'horizon': 30., 'effort_penalty': 4.,
            'integral_gain': .0005, 'observer_time': 2.,
            'output_slew_rate': 1., 'control_band': 1.,
        }, 28.09)
    host_outputs = []
    for stamp, temperature in zip(sample_times, samples):
        reactor.now = stamp
        host.temperature_update(stamp, temperature, 75.)
        host_outputs.append(heater.output)

    executable = pathlib.Path(tempfile.gettempdir()) \
        / 'heater_predictive_replay'
    subprocess.run([
        'cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
        '-I', str(ROOT), '-I', str(ROOT / 'src'),
        str(ROOT / 'test' / 'heater_predictive_replay.c'),
        str(ROOT / 'src' / 'generic' / 'heater_control_math.c'),
        '-o', str(executable),
    ], check=True)
    payload = ''.join('%d\n' % int(value * 1000. + .5)
                      for value in samples)
    result = subprocess.run(
        [str(executable)], input=payload, text=True,
        capture_output=True, check=True)
    fixed_outputs = [int(line) / 65535.
                     for line in result.stdout.splitlines()]
    errors = [abs(host - fixed) for host, fixed
              in zip(host_outputs, fixed_outputs)]
    assert len(fixed_outputs) == len(host_outputs)
    assert max(errors) < .001, max(errors)
    assert sum(errors) / len(errors) < .0002
    print('PASS: physical-envelope host/fixed predictive parity '
          'max=%.7f mean=%.7f' % (max(errors), sum(errors) / len(errors)))


if __name__ == '__main__':
    main()
