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
import msgproto
import stepper as klippy_stepper
sys.modules['chelper'] = types.ModuleType('chelper')
import trajectory_queuing
import trajectory_pwm


class FakeCommand:
    def __init__(self, response=None):
        self.response = response
        self.sent = []
        self.send_options = []

    def send(self, args=None, **kwargs):
        self.sent.append([] if args is None else args)
        self.send_options.append(kwargs)
        return self.response


class FakeClockSync:
    def systime_to_local_clock(self, systime):
        return int(systime * 1_000_000.)


def test_real_mcu_exposes_clocksync():
    clocksync = object()
    mcu = klippy_mcu.MCU.__new__(klippy_mcu.MCU)
    mcu._clocksync = clocksync
    assert mcu.get_clocksync() is clocksync


def test_trajectory_anchor_uses_physical_step_space():
    stepper = klippy_stepper.MCU_stepper.__new__(klippy_stepper.MCU_stepper)
    stepper._step_dist = .00125
    stepper._mcu_position_offset = .028
    # A 0.166667mm logical coordinate after homing can correspond to a
    # different physical MCU count. Preserve that offset at sub-step
    # resolution instead of rebasing the board to the logical coordinate.
    pos_su = stepper.commanded_to_mcu_position_su(.166667)
    want = round((.166667 + .028) / .00125 * 65536.)
    assert pos_su == want


def test_signed_trajectory_readback_preserves_negative_corexy_position():
    stepper = klippy_stepper.MCU_stepper.__new__(klippy_stepper.MCU_stepper)
    stepper._oid = 4
    stepper._get_position_cmd = FakeCommand({
        'clock': 1234, 'pos': 3493649149})
    stepper._mcu = types.SimpleNamespace(
        clock32_to_clock64=lambda clock: clock)
    stepper._last_traj_readback = None
    clock, mcu_pos = stepper._query_traj_readback()
    assert clock == 1234
    assert mcu_pos == round(-801318147 / 65536.)
    assert stepper._last_traj_readback == (clock, mcu_pos)


def test_signed_protocol_field_normalizes_high_bit_wire_value():
    encoded = []
    msgproto.PT_uint32().encode(encoded, 3493649149)
    value, end = msgproto.PT_int32().parse(bytes(encoded), 0)
    assert end == len(encoded)
    assert value == -801318147


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

    def print_time_to_clock(self, print_time):
        return round(print_time * self.frequency)

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


class SyncedOwner:
    def is_mcu_synced(self, mcu):
        return True


class AnchorFFI:
    def __init__(self, active_time):
        self.active_time = active_time
        self.checked_to = []
        self.generated_to = []
        self.finalized = 0

    def itersolve_check_active(self, sk, flush_time):
        self.checked_to.append(flush_time)
        return self.active_time

    def segfit_get_anchor(self, segfit):
        return 0

    def segfit_get_gen_time(self, segfit):
        return self.active_time

    def segfit_generate(self, segfit, flush_time):
        self.generated_to.append(flush_time)
        return 0

    def segfit_finalize(self, segfit):
        self.finalized += 1
        return 0

    def segfit_get_segs(self, segfit):
        return []


class EndedFFI(AnchorFFI):
    def __init__(self, end_time):
        super().__init__(0.)
        self.end_time = end_time

    def segfit_get_gen_time(self, segfit):
        return self.end_time


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


def test_trajectory_anchor_starts_at_activity():
    stepper = trajectory_queuing.TrajectoryStepper.__new__(
        trajectory_queuing.TrajectoryStepper)
    stepper.owner = SyncedOwner()
    stepper.mcu = FakeMCU()
    stepper.mcu_stepper = FakeMCUStepper()
    ffi_lib = stepper.ffi_lib = AnchorFFI(12.5)
    stepper.segfit = object()
    stepper.anchored = False
    stepper.intentions = []
    anchors = []

    def anchor(print_time):
        anchors.append(print_time)
        stepper.anchored = True

    stepper._anchor = anchor
    stepper.flush(13., 13.4)
    assert anchors == [12.5]
    assert ffi_lib.checked_to == [13.4]
    assert ffi_lib.generated_to == [13.4]
    assert ffi_lib.finalized == 1


def test_trajectory_end_always_queues_terminal_hold():
    stepper = trajectory_queuing.TrajectoryStepper.__new__(
        trajectory_queuing.TrajectoryStepper)
    stepper.owner = SyncedOwner()
    stepper.mcu = FakeMCU()
    stepper.mcu_stepper = FakeMCUStepper()
    stepper.ffi_lib = EndedFFI(12.5)
    stepper.segfit = object()
    stepper.oid = 4
    stepper.anchored = True
    stepper.intentions = []
    stepper.hold_cmd = FakeCommand()
    stepper.terminal_hold_ticks = 1000
    stepper.rebase_min_clock = 0
    stepper._send_segs = lambda n: None
    stepper._record_intention = lambda prev_acc, prev_time: None
    stepper.flush(12.6, 12.6)
    # An inactive trapq may contain only its zero-valued head sentinel.  It
    # must not be sampled as a new endpoint after the real path is complete.
    assert stepper.ffi_lib.generated_to == []
    assert stepper.hold_cmd.sent == [[4, 1000]]
    assert stepper.rebase_min_clock == 12_501_000
    assert not stepper.anchored


def test_wire_record_preserves_exact_segment_coefficients():
    class RecordingOwner:
        def __init__(self):
            self.records = []
        def record_wire_intention(self, ts, fields):
            self.records.append(dict(fields))

    stepper = trajectory_queuing.TrajectoryStepper.__new__(
        trajectory_queuing.TrajectoryStepper)
    stepper.owner = RecordingOwner()
    stepper._wire_rebase(1_000_000, 100, 2)
    stepper._wire_segment(0, 50_000, 65536, 0)
    rebase, segment = stepper.owner.records
    assert rebase == {
        'event': 'rebase', 'start_clock': 1_000_000,
        'end_clock': 1_000_000, 'position_su': 100,
        'acc_q32': 100 << 32, 'mcu_position': 2}
    assert segment['start_clock'] == 1_000_000
    assert segment['end_clock'] == 1_050_000
    assert segment['velocity'] == 65536 and segment['accel'] == 0
    assert segment['start_position_su'] == 100
    assert segment['end_position_su'] == 50_100
    assert segment['start_acc_q32'] == 100 << 32
    assert segment['end_acc_q32'] == 50_100 << 32


def test_rebase_waits_for_previous_horizon():
    class PhysicalStepper:
        def commanded_to_mcu_position_su(self, pos):
            return 777
        def get_mcu_position(self, pos):
            return 12
    class RebaseFFI:
        def segfit_get_position(self, segfit, print_time):
            return 1.25
        def segfit_set_position_offset(self, segfit, offset):
            self.offset = offset
        def segfit_set_anchor(self, segfit, print_time, acc):
            self.anchor = (print_time, acc)

    stepper = trajectory_queuing.TrajectoryStepper.__new__(
        trajectory_queuing.TrajectoryStepper)
    stepper.mcu = FakeMCU()
    stepper.mcu_stepper = PhysicalStepper()
    stepper.ffi_lib = RebaseFFI()
    stepper.segfit = object()
    stepper.rebase_cmd = FakeCommand()
    stepper.oid = 4
    stepper.su_per_mm = 100.
    stepper.intentions = []
    stepper.rebase_min_clock = 9_000_000
    stepper._anchor(10.)
    assert stepper.rebase_cmd.sent == [[4, 10_000_000, 777, 12]]
    assert stepper.rebase_cmd.send_options == [{
        'minclock': 9_000_000, 'reqclock': 10_000_000}]
    assert stepper.rebase_min_clock == 0
    assert stepper.ffi_lib.offset == 652.


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
    test_trajectory_anchor_uses_physical_step_space()
    print("PASS: trajectory anchors preserve the physical MCU position"
          " offset")
    test_signed_trajectory_readback_preserves_negative_corexy_position()
    print("PASS: negative CoreXY trajectory readback remains signed")
    test_signed_protocol_field_normalizes_high_bit_wire_value()
    print("PASS: signed protocol fields normalize high-bit wire values")
    test_secondary_freshness()
    print("PASS: host freewheel freshness mirrors the firmware gate")
    test_mixed_frequency_rate_representation()
    print("PASS: Q8.24 represents a 64MHz/12MHz MCU ratio below 0.02ppm")
    test_relay_regression_rejects_endpoint_jitter()
    print("PASS: relay regression suppresses noisy endpoint estimates")
    test_trajectory_fails_before_fitter_advance()
    print("PASS: trajectory fitting fails before unsynchronized send")
    test_trajectory_anchor_starts_at_activity()
    print("PASS: trajectory anchor starts at activity and fits to the"
          " step-generation horizon")
    test_trajectory_end_always_queues_terminal_hold()
    print("PASS: every completed trajectory queues an explicit terminal"
          " hold")
    test_wire_record_preserves_exact_segment_coefficients()
    print("PASS: flight recording preserves exact wire coefficients and"
          " chained endpoints")
    test_rebase_waits_for_previous_horizon()
    print("PASS: a new rebase waits for the previous physical horizon")
    test_value_trajectory_fails_before_fitter_advance()
    print("PASS: value fitting fails before unsynchronized send")


if __name__ == '__main__':
    main()
