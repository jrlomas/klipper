#!/usr/bin/env python3
"""Host regression tests for machine-time Class-0 preflight."""

import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'klippy'))
sys.path.insert(0, os.path.join(ROOT, 'klippy', 'extras'))

import timesync
import mcu as klippy_mcu
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


def test_real_mcu_exposes_clocksync():
    clocksync = object()
    mcu = klippy_mcu.MCU.__new__(klippy_mcu.MCU)
    mcu._clocksync = clocksync
    assert mcu.get_clocksync() is clocksync


class FakeMCU:
    def __init__(self, frequency=1_000_000., clocksync=None):
        self.frequency = frequency
        self.clocksync = clocksync or FakeClockSync()
        self.commands = {
            'sync_beacon_relay': FakeCommand(),
            'timesync_setup': FakeCommand(),
            'timesync_query': FakeCommand({
                'flags': timesync.TS_ENABLED | timesync.TS_PRIMED
                         | timesync.TS_CONVERGED,
                'last_err': 2,
                'rate': 1 << timesync.RATE_SHIFT,
            }),
        }

    def get_name(self):
        return 'mcu toolhead'

    def get_constant_float(self, name):
        assert name == 'CLOCK_FREQ'
        return self.frequency

    def get_clocksync(self):
        return self.clocksync

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
    link = timesync.SecondaryLink(mcu, 1_000_000.)
    link.setup(5., .000010)
    link.relay(7, 1000, 20.)
    link.relay(8, 2000, 20.001)
    link.query()
    assert link.is_converged(24.999)
    assert not link.is_converged(25.002)
    assert mcu.commands['timesync_setup'].sent == [
        [5_000_000, 10, 1 << timesync.RATE_SHIFT]]
    assert mcu.commands['sync_beacon_relay'].sent == [
        [7, 1000, 20_000_000], [8, 2000, 20_001_000]]
    assert link.sample_rate == 1.


def test_mixed_frequency_rate_representation():
    link = timesync.SecondaryLink(FakeMCU(64_000_000.), 12_000_000.)
    encoded = round(link.nominal_rate * (1 << timesync.RATE_SHIFT))
    decoded = encoded / (1 << timesync.RATE_SHIFT)
    relative_error_ppm = (decoded / link.nominal_rate - 1.) * 1e6
    assert encoded < 2**32
    assert abs(relative_error_ppm) < .02
    link.setup(5., .000010)
    assert link.mcu.commands['timesync_setup'].sent == [
        [320_000_000, 640, encoded]]


class NoisyClockSync:
    def __init__(self, noise):
        self.noise = iter(noise)

    def systime_to_local_clock(self, systime):
        return int(systime * 1_000_000.) + next(self.noise)


def test_relay_regression_rejects_endpoint_jitter():
    noise = [0, 100, -100, 50, -50, 80, -80, 1000]
    mcu = FakeMCU(clocksync=NoisyClockSync(noise))
    link = timesync.SecondaryLink(mcu, 1_000_000.)
    for index in range(8):
        link.relay(index, index * 1_000_000, float(index))
    raw = 7_001_000
    sent = mcu.commands['sync_beacon_relay'].sent[-1][2]
    assert link.last_raw_local_est == raw
    assert abs(sent - 7_000_000) < abs(raw - 7_000_000) / 2
    assert abs(link.relay_rate - 1.) < .0001


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
    test_real_mcu_exposes_clocksync()
    print("PASS: the real MCU API exposes its per-link clock regression")
    test_secondary_freshness()
    print("PASS: host freewheel freshness mirrors the firmware gate")
    test_mixed_frequency_rate_representation()
    print("PASS: Q8.24 represents a 64MHz/12MHz MCU ratio below 0.02ppm")
    test_relay_regression_rejects_endpoint_jitter()
    print("PASS: relay regression suppresses noisy endpoint estimates")
    test_trajectory_fails_before_fitter_advance()
    print("PASS: trajectory fitting fails before unsynchronized send")
    test_value_trajectory_fails_before_fitter_advance()
    print("PASS: value fitting fails before unsynchronized send")


if __name__ == '__main__':
    main()
