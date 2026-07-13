#!/usr/bin/env python3
"""Host regression tests for machine-time Class-0 preflight."""

import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'klippy'))
sys.path.insert(0, os.path.join(ROOT, 'klippy', 'extras'))

import timesync
sys.modules['chelper'] = types.ModuleType('chelper')
import trajectory_queuing
import trajectory_pwm


class FakeCommand:
    def __init__(self, response=None):
        self.response = response
        self.sent = []

    def send(self, args=None):
        self.sent.append([] if args is None else args)
        return self.response


class FakeClockSync:
    def systime_to_local_clock(self, systime):
        return int(systime * 1_000_000.)


class FakeMCU:
    def __init__(self):
        self.commands = {
            'sync_beacon_relay': FakeCommand(),
            'timesync_setup': FakeCommand(),
            'timesync_query': FakeCommand({
                'flags': timesync.TS_ENABLED | timesync.TS_PRIMED
                         | timesync.TS_CONVERGED,
                'last_err': 2,
                'rate': 1 << 30,
            }),
        }

    def get_name(self):
        return 'mcu toolhead'

    def get_constant_float(self, name):
        assert name == 'CLOCK_FREQ'
        return 1_000_000.

    def get_clocksync(self):
        return FakeClockSync()

    def lookup_command(self, fmt):
        return self.commands[fmt.split()[0]]

    def lookup_query_command(self, request, response):
        assert request == 'timesync_query'
        assert response.startswith('timesync_state')
        return self.commands[request]

    def error(self, message):
        return RuntimeError(message)


def test_secondary_freshness():
    mcu = FakeMCU()
    link = timesync.SecondaryLink(mcu)
    link.setup(5., .000010)
    link.relay(7, 1000, 20.)
    link.query()
    assert link.is_converged(24.999)
    assert not link.is_converged(25.001)
    assert mcu.commands['timesync_setup'].sent == [[5_000_000, 10]]
    assert mcu.commands['sync_beacon_relay'].sent == [[7, 1000, 20_000_000]]


class FakeStepperKinematics:
    pass


class FakeMCUStepper:
    def get_stepper_kinematics(self):
        return FakeStepperKinematics()


class GuardFFI:
    def itersolve_check_active(self, sk, flush_time):
        return 1.

    def segfit_get_anchor(self, segfit):
        raise AssertionError("fitter advanced before Class-0 preflight")


class UnsyncedOwner:
    def is_mcu_synced(self, mcu):
        return False


class UnsyncedTimeSync:
    def is_mcu_synced(self, mcu_name):
        return False


class FakePrinter:
    def lookup_object(self, name, default=None):
        assert name == 'timesync'
        return UnsyncedTimeSync()

    def command_error(self, message):
        return RuntimeError(message)


def test_trajectory_fails_before_fitter_advance():
    stepper = trajectory_queuing.TrajectoryStepper.__new__(
        trajectory_queuing.TrajectoryStepper)
    stepper.owner = UnsyncedOwner()
    stepper.mcu = FakeMCU()
    stepper.mcu_stepper = FakeMCUStepper()
    stepper.ffi_lib = GuardFFI()
    stepper.segfit = object()
    stepper.anchored = False
    try:
        stepper.flush(3., 3.)
    except RuntimeError as exc:
        assert 'not converged' in str(exc)
    else:
        raise AssertionError("unsynchronized Class-0 traffic was accepted")


def test_value_trajectory_fails_before_fitter_advance():
    pwm = trajectory_pwm.TrajectoryPWM.__new__(trajectory_pwm.TrajectoryPWM)
    pwm.printer = FakePrinter()
    pwm.mcu = FakeMCU()
    pwm.queue_cmd = FakeCommand()
    pwm._fitter = None
    try:
        pwm._feed_value_knots([(1., 0.), (2., 1.)])
    except RuntimeError as exc:
        assert 'not converged' in str(exc)
    else:
        raise AssertionError("unsynchronized value trajectory was accepted")
    assert pwm._fitter is None


def main():
    test_secondary_freshness()
    print("PASS: host freewheel freshness mirrors the firmware gate")
    test_trajectory_fails_before_fitter_advance()
    print("PASS: trajectory fitting fails before unsynchronized send")
    test_value_trajectory_fails_before_fitter_advance()
    print("PASS: value fitting fails before unsynchronized send")


if __name__ == '__main__':
    main()
