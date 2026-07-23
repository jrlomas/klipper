#!/usr/bin/env python3
"""Regression test for runtime trajectory-kinematics replacement."""

import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'klippy'))

from extras import trajectory_queuing


class FakeFFI:
    def __init__(self):
        self.setup = []
        self.fastpath = []
        self.orders = []

    def segfit_setup(self, segfit, sk, freq, su_per_mm, tolerance, sample_time):
        self.setup.append(
            (segfit, sk, freq, su_per_mm, tolerance, sample_time))

    def segfit_set_cruise_fastpath(self, segfit, enabled):
        self.fastpath.append((segfit, enabled))

    def segfit_set_order(self, segfit, order):
        self.orders.append((segfit, order))


class FakeMCU:
    def seconds_to_clock(self, seconds):
        return int(seconds * 20_000_000)


def main():
    ts = trajectory_queuing.TrajectoryStepper.__new__(
        trajectory_queuing.TrajectoryStepper)
    ts.connected = False
    ts.ffi_lib = FakeFFI()
    ts.mcu = FakeMCU()
    ts.segfit = object()
    ts.su_per_mm = 65536000.
    ts.tolerance_su = 32768.
    ts.sample_time = .001
    ts.g1_segment_order = 2

    first_sk, replacement_sk = object(), object()
    ts.update_kinematics(first_sk)
    assert not ts.ffi_lib.setup

    ts.connected = True
    ts.update_kinematics(replacement_sk)
    assert ts.ffi_lib.setup == [
        (ts.segfit, replacement_sk, 20_000_000, 65536000., 32768., .001)]
    assert ts.ffi_lib.fastpath == [(ts.segfit, 1)]
    assert ts.ffi_lib.orders == [(ts.segfit, 2)]

    with open(os.path.join(ROOT, 'klippy', 'stepper.py'),
              encoding='utf-8') as stream:
        stepper_source = stream.read()
    assert 'self._traj.update_kinematics(sk)' in stepper_source
    print('PASS: trajectory fitter follows runtime kinematics replacement')


if __name__ == '__main__':
    main()
