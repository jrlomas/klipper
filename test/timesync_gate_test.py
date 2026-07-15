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
import motion_queuing
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


def test_trajectory_stepper_never_configures_queue_step():
    class TrajectoryBackend:
        def __init__(self):
            self.built = []
        def build_config(self, *args):
            self.built.append(args)
    class ConfigMCU:
        def __init__(self):
            self.lookups = []
        def seconds_to_clock(self, seconds):
            return round(seconds * 1_000_000)
        def lookup_query_command(self, request, response, **kwargs):
            self.lookups.append((request, response, kwargs))
            return object()

    backend = TrajectoryBackend()
    mcu = ConfigMCU()
    stepper = klippy_stepper.MCU_stepper.__new__(klippy_stepper.MCU_stepper)
    stepper._step_pulse_duration = .000002
    stepper._traj = backend
    stepper._mcu = mcu
    stepper._step_pin = 'gpio1'
    stepper._dir_pin = 'gpio2'
    stepper._invert_step = False
    stepper._invert_dir = True
    stepper._oid = 4
    stepper._build_config()
    assert backend.built == [('gpio1', 'gpio2', 0, 1, 2)]
    assert len(mcu.lookups) == 1
    assert mcu.lookups[0][0].startswith('traj_get_position')
    assert all('queue_step' not in str(item) for item in mcu.lookups)


def test_signed_trajectory_readback_preserves_negative_corexy_position():
    stepper = klippy_stepper.MCU_stepper.__new__(klippy_stepper.MCU_stepper)
    stepper._oid = 4
    stepper._get_position_cmd = FakeCommand({
        'clock': 1234, 'pos': 3493649149, 'mcu_pos': -12227})
    stepper._mcu = types.SimpleNamespace(
        clock32_to_clock64=lambda clock: clock)
    stepper._last_traj_readback = None
    clock, mcu_pos = stepper._query_traj_readback()
    assert clock == 1234
    assert mcu_pos == -12227
    assert stepper._last_traj_readback == (clock, mcu_pos)


def test_wide_trajectory_readback_unwraps_from_physical_counter():
    # Low position word wrapped negative just above +32768 microsteps.
    pos_su = (32768 * 65536 + 12345) & 0xffffffff
    assert klippy_stepper._unwrap_subunits(pos_su, 32768) == (
        32768 * 65536 + 12345)


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

    def clock_to_print_time(self, clock):
        return clock / self.frequency

    def seconds_to_clock(self, duration):
        return round(duration * self.frequency)

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
    def itersolve_get_gen_steps_pre_active(self, sk):
        return 0.

    def itersolve_get_gen_steps_post_active(self, sk):
        return 0.

    def itersolve_check_active(self, sk, flush_time):
        return 1.

    def segfit_get_gen_time(self, segfit):
        return 0.

    def segfit_check_activity(self, segfit, from_time, through_time):
        return 1

    def segfit_get_anchor(self, segfit):
        raise AssertionError("fitter advanced before Class-0 preflight")


class UnsyncedOwner:
    def is_mcu_synced(self, mcu):
        return False


class SyncedOwner:
    def is_mcu_synced(self, mcu):
        return True


class DripMotionQueuing:
    def check_drip_timing(self):
        return 12.0


class DripPrinter:
    def lookup_object(self, name, default=None):
        if name == 'motion_queuing':
            return DripMotionQueuing()
        return default


class DripOwner(SyncedOwner):
    printer = DripPrinter()


class AnchorFFI:
    def __init__(self, active_time):
        self.active_time = active_time
        self.checked_to = []
        self.generated_to = []
        self.finalized = 0

    def itersolve_get_gen_steps_pre_active(self, sk):
        return 0.

    def itersolve_get_gen_steps_post_active(self, sk):
        return 0.

    def itersolve_check_active(self, sk, flush_time):
        self.checked_to.append(flush_time)
        return self.active_time

    def segfit_check_activity(self, segfit, from_time, through_time):
        self.checked_to.append(through_time)
        return 1

    def segfit_get_activity_start(self, segfit):
        return self.active_time

    def segfit_get_activity_end(self, segfit):
        return self.active_time + 10.

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

    def segfit_check_activity(self, segfit, from_time, through_time):
        self.checked_to.append(through_time)
        return 0


class ScanWindowFFI(AnchorFFI):
    def itersolve_get_gen_steps_pre_active(self, sk):
        return .020

    def itersolve_get_gen_steps_post_active(self, sk):
        return .020

    def segfit_get_activity_start(self, segfit):
        return self.active_time - .020


class DisconnectedWindowsFFI(AnchorFFI):
    def __init__(self, windows):
        super().__init__(windows[0][0])
        self.windows = list(windows)
        self.selected = None
        self.gen_time = 0.
        self.checked_from = []

    def itersolve_get_gen_steps_pre_active(self, sk):
        return .020

    def itersolve_get_gen_steps_post_active(self, sk):
        return .020

    def segfit_check_activity(self, segfit, from_time, through_time):
        self.checked_from.append(from_time)
        self.checked_to.append(through_time)
        self.selected = next((window for window in self.windows
                              if window[1] > from_time
                              and window[0] <= through_time), None)
        return self.selected is not None

    def segfit_get_activity_start(self, segfit):
        return self.selected[0]

    def segfit_get_activity_end(self, segfit):
        return self.selected[1]

    def segfit_get_gen_time(self, segfit):
        return self.gen_time

    def segfit_generate(self, segfit, flush_time):
        self.generated_to.append(flush_time)
        self.gen_time = flush_time
        return 0


class CappedWindowFFI(DisconnectedWindowsFFI):
    def __init__(self, start_time, end_time):
        super().__init__([(start_time, end_time)])
        self.gen_time = start_time
        self.mid_time = (start_time + end_time) * .5
        self.calls = 0

    def segfit_generate(self, segfit, flush_time):
        self.generated_to.append(flush_time)
        self.calls += 1
        if self.calls == 1:
            self.gen_time = self.mid_time
            return trajectory_queuing.SEGFIT_BATCH_MAX
        self.gen_time = flush_time
        return 7


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


def test_ordinary_axis_move_ends_with_hold_before_synchronous_wait():
    stepper = trajectory_queuing.TrajectoryStepper.__new__(
        trajectory_queuing.TrajectoryStepper)
    stepper.owner = SyncedOwner()
    stepper.mcu = FakeMCU()
    stepper.mcu_stepper = FakeMCUStepper()
    ffi_lib = stepper.ffi_lib = DisconnectedWindowsFFI([(10., 10.2)])
    # This is an ordinary axis: no pressure-advance/input-shaper scan margin.
    ffi_lib.itersolve_get_gen_steps_pre_active = lambda sk: 0.
    ffi_lib.itersolve_get_gen_steps_post_active = lambda sk: 0.
    stepper.segfit = object()
    stepper.anchored = False
    stepper.activity_cursor = 0.
    stepper.intentions = []
    anchors = []
    holds = []

    def anchor(print_time):
        anchors.append(print_time)
        ffi_lib.gen_time = print_time
        stepper.anchored = True

    stepper._anchor = anchor
    stepper._queue_terminal_hold = lambda *args: holds.append(True)
    stepper._send_segs = lambda count: None
    stepper._record_intention = lambda prev_acc, prev_time: None
    # Model one Z move followed by a long M190/M109 wait. The lookahead
    # horizon extends well beyond the move, so its hold must be sent now;
    # there may be no later motion-queue callback during the heater wait.
    stepper.flush(11., 12.)
    assert anchors == [10.]
    assert ffi_lib.generated_to == [10.2]
    assert holds == [True]
    assert not stepper.anchored


def test_zero_scan_window_keeps_homing_drip_streaming_behavior():
    stepper = trajectory_queuing.TrajectoryStepper.__new__(
        trajectory_queuing.TrajectoryStepper)
    stepper.owner = DripOwner()
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
    assert ffi_lib.generated_to == [13.4]
    assert ffi_lib.checked_to == [13.4]


def test_trajectory_anchor_includes_kinematic_scan_preroll():
    stepper = trajectory_queuing.TrajectoryStepper.__new__(
        trajectory_queuing.TrajectoryStepper)
    stepper.owner = SyncedOwner()
    stepper.mcu = FakeMCU()
    stepper.mcu_stepper = FakeMCUStepper()
    ffi_lib = stepper.ffi_lib = ScanWindowFFI(12.5)
    stepper.segfit = object()
    stepper.anchored = False
    stepper.intentions = []
    anchors = []

    def anchor(print_time):
        anchors.append(print_time)
        stepper.anchored = True

    stepper._anchor = anchor
    stepper.flush(13., 13.4)
    assert anchors == [12.48]
    assert ffi_lib.checked_to == [13.4]
    assert ffi_lib.generated_to == [13.4]
    assert ffi_lib.finalized == 1


def test_trajectory_drains_disconnected_windows_before_trapq_cleanup():
    stepper = trajectory_queuing.TrajectoryStepper.__new__(
        trajectory_queuing.TrajectoryStepper)
    stepper.owner = SyncedOwner()
    stepper.mcu = FakeMCU()
    stepper.mcu_stepper = FakeMCUStepper()
    ffi_lib = stepper.ffi_lib = DisconnectedWindowsFFI(
        [(10., 10.2), (10.5, 10.7)])
    stepper.segfit = object()
    stepper.anchored = False
    stepper.activity_cursor = 0.
    stepper.intentions = []
    anchors = []
    holds = []

    def anchor(print_time):
        anchors.append(print_time)
        ffi_lib.gen_time = print_time
        stepper.anchored = True

    stepper._anchor = anchor
    stepper._queue_terminal_hold = lambda *args: holds.append(True)
    stepper._send_segs = lambda count: None
    stepper._record_intention = lambda prev_acc, prev_time: None
    stepper.flush(11., 12.)
    assert anchors == [10., 10.5]
    assert holds == [True, True]
    assert ffi_lib.generated_to == [10.2, 10.7]
    assert ffi_lib.checked_from == [0., 10.2, 10.7]
    assert ffi_lib.finalized == 2
    assert stepper.activity_cursor == 12.


def test_trajectory_drains_capped_segment_batch_before_hold():
    stepper = trajectory_queuing.TrajectoryStepper.__new__(
        trajectory_queuing.TrajectoryStepper)
    stepper.owner = SyncedOwner()
    stepper.mcu = FakeMCU()
    stepper.mcu_stepper = FakeMCUStepper()
    ffi_lib = stepper.ffi_lib = CappedWindowFFI(10., 10.2)
    stepper.segfit = object()
    stepper.anchored = False
    stepper.activity_cursor = 0.
    stepper.intentions = []
    sent = []
    holds = []

    def anchor(print_time):
        ffi_lib.gen_time = print_time
        stepper.anchored = True

    stepper._anchor = anchor
    stepper._queue_terminal_hold = lambda *args: holds.append(True)
    stepper._send_segs = lambda count: sent.append(count)
    stepper._record_intention = lambda prev_acc, prev_time: None
    stepper.flush(11., 12.)
    assert ffi_lib.generated_to == [10.2, 10.2]
    assert sent[:2] == [trajectory_queuing.SEGFIT_BATCH_MAX, 7]
    assert holds == [True]
    assert not stepper.anchored


def test_external_generator_delay_survives_scan_window_rescan():
    mq = motion_queuing.PrinterMotionQueuing.__new__(
        motion_queuing.PrinterMotionQueuing)
    mq.kin_flush_delay = motion_queuing.SDS_CHECK_TIME
    mq.external_kin_flush_delay = motion_queuing.SDS_CHECK_TIME
    mq.register_kin_flush_delay(
        trajectory_queuing.TRAJECTORY_KIN_FLUSH_DELAY)
    assert mq.kin_flush_delay == .100
    assert mq.external_kin_flush_delay == .100


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
    stepper.wire_clock = 12_500_000
    stepper.wire_acc = 0
    stepper.rebase_min_clock = 0
    stepper._record_wire = lambda fields: None
    stepper._send_segs = lambda n: None
    stepper._record_intention = lambda prev_acc, prev_time: None
    stepper.flush(12.6, 12.6)
    # An inactive trapq may contain only its zero-valued head sentinel.  It
    # must not be sampled as a new endpoint after the real path is complete.
    assert stepper.ffi_lib.generated_to == []
    assert stepper.hold_cmd.sent == [[4, 1000]]
    assert stepper.rebase_min_clock == 12_501_000
    assert not stepper.anchored


def test_secondary_terminal_hold_respects_short_machine_gap():
    class MixedClockOwner:
        def __init__(self):
            self.machine = FakeMCU(12_000_000.)
        def get_machine_mcu(self):
            return self.machine

    stepper = trajectory_queuing.TrajectoryStepper.__new__(
        trajectory_queuing.TrajectoryStepper)
    stepper.owner = MixedClockOwner()
    stepper.mcu = FakeMCU(64_000_000.)
    stepper.oid = 4
    stepper.hold_cmd = FakeCommand()
    stepper.terminal_hold_ticks = 64_000
    stepper.wire_clock = 12_000_000
    stepper.execution_clock = 64_000_000
    stepper.wire_acc = 0
    stepper.rebase_min_clock = 0
    stepper._record_wire = lambda fields: None
    # The failed full-speed print had only 1,637 primary-MCU ticks between
    # extruder windows. The local hold must fit that gap instead of emitting
    # a fixed 1ms machine-domain duration as if it were local ticks.
    assert stepper._queue_terminal_hold(1_637)
    assert stepper.hold_cmd.sent == [[4, 8_730]]
    assert stepper.wire_clock == 12_001_637
    assert stepper.execution_clock == 64_008_730
    assert stepper.rebase_min_clock == 12_001_637


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
        'end_clock': 1_000_000,
        'execution_start_clock': 1_000_000,
        'execution_end_clock': 1_000_000, 'position_su': 100,
        'absolute_position_su': 100,
        'acc_q32': 100 << 32, 'mcu_position': 2}
    assert segment['start_clock'] == 1_000_000
    assert segment['end_clock'] == 1_050_000
    assert segment['velocity'] == 65536 and segment['accel'] == 0
    assert segment['start_position_su'] == 100
    assert segment['end_position_su'] == 50_100
    assert segment['absolute_start_position_su'] == 100
    assert segment['absolute_end_position_su'] == 50_100
    assert segment['start_acc_q32'] == 100 << 32
    assert segment['end_acc_q32'] == 50_100 << 32

    # Flight records keep an unwrapped host twin while exposing the exact
    # signed low word the MCU executes across a phase boundary.
    stepper.owner.records = []
    wide_start = (1 << 31) - 100
    stepper._wire_rebase(2_000_000, wide_start, 32768)
    stepper._wire_segment(0, 200, 65536, 0)
    rebase, segment = stepper.owner.records
    assert rebase['position_su'] == wide_start
    assert rebase['absolute_position_su'] == wide_start
    assert segment['start_position_su'] == wide_start
    assert segment['end_position_su'] == -(1 << 31) + 100
    assert segment['absolute_end_position_su'] == (1 << 31) + 100


def test_g1_quintic_segments_use_higher_order_wire_command():
    class SegmentFFI:
        def segfit_get_segs(self, segfit):
            return [types.SimpleNamespace(
                duration=12000, velocity=101, accel=202, jerk=303,
                snap=404, crackle=505,
                flags=trajectory_queuing.TSEG_POLY_QUINTIC)]
    class MixedClockOwner:
        def __init__(self):
            self.machine = FakeMCU(12_000_000.)
            self.records = []
        def get_machine_mcu(self):
            return self.machine
        def record_wire_intention(self, ts, fields):
            self.records.append(dict(fields))

    stepper = trajectory_queuing.TrajectoryStepper.__new__(
        trajectory_queuing.TrajectoryStepper)
    stepper.ffi_lib = SegmentFFI()
    stepper.segfit = object()
    stepper.owner = MixedClockOwner()
    stepper.mcu = FakeMCU(64_000_000.)
    stepper.name = 'stepper_x'
    stepper.oid = 7
    stepper.queue_cmd = FakeCommand()
    stepper.quintic_cmd = FakeCommand()
    stepper.hold_cmd = FakeCommand()
    stepper.wire_clock = 1_000_000
    stepper.wire_acc = 0
    stepper._send_segs(1)
    assert stepper.quintic_cmd.sent == [
        [7, trajectory_queuing.TSEG_LOCAL_TIME,
         12000, 101, 202, 303, 404, 505]]
    assert stepper.queue_cmd.sent == []
    assert stepper.hold_cmd.sent == []
    record = stepper.owner.records[-1]
    assert record['duration'] == 2250
    assert record['execution_duration'] == 12000
    assert record['end_clock'] == 1_002_250
    assert record['execution_start_clock'] == 5_333_333
    assert record['execution_end_clock'] == 5_345_333


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
        def segfit_set_anchor_position(self, segfit, position_su):
            self.anchor_position = position_su

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
    stepper.rebase_requires_hold = False
    stepper.rebase_min_clock = 9_000_000
    stepper._anchor(10.)
    assert stepper.rebase_cmd.sent == [[4, 10_000_000, 777, 12]]
    assert stepper.rebase_cmd.send_options == [{
        'minclock': 9_000_000, 'reqclock': 10_000_000}]
    assert stepper.rebase_min_clock == 0
    assert stepper.ffi_lib.offset == 652.


def test_active_path_is_held_before_rebase_boundary():
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
        def segfit_set_anchor_position(self, segfit, position_su):
            self.anchor_position = position_su

    stepper = trajectory_queuing.TrajectoryStepper.__new__(
        trajectory_queuing.TrajectoryStepper)
    stepper.mcu = FakeMCU()
    stepper.mcu_stepper = PhysicalStepper()
    stepper.ffi_lib = RebaseFFI()
    stepper.segfit = object()
    stepper.hold_cmd = FakeCommand()
    stepper.rebase_cmd = FakeCommand()
    stepper.oid = 4
    stepper.name = 'stepper_z'
    stepper.su_per_mm = 100.
    stepper.terminal_hold_ticks = 1000
    stepper.wire_clock = 9_000_000
    stepper.wire_acc = 500 << 32
    stepper.intentions = []
    stepper.rebase_requires_hold = True
    stepper.rebase_min_clock = 0
    stepper._record_wire = lambda fields: None
    stepper._anchor(10.)
    assert stepper.hold_cmd.sent == [[4, 1000]]
    assert stepper.rebase_cmd.sent == [[4, 10_000_000, 777, 12]]
    assert stepper.rebase_cmd.send_options == [{
        'minclock': 9_001_000, 'reqclock': 10_000_000}]
    assert not stepper.rebase_requires_hold


def test_confirmed_stop_does_not_queue_a_pre_rebase_hold():
    stepper = trajectory_queuing.TrajectoryStepper.__new__(
        trajectory_queuing.TrajectoryStepper)
    stepper.anchored = True
    stepper.need_rebase = False
    stepper.rebase_requires_hold = False
    stepper.rebase_min_clock = 1234
    stepper.note_rebase_needed(stopped=True)
    assert stepper.need_rebase and not stepper.anchored
    assert not stepper.rebase_requires_hold
    assert stepper.rebase_min_clock == 0


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
    test_trajectory_stepper_never_configures_queue_step()
    print("PASS: trajectory steppers never configure the queue_step path")
    test_signed_trajectory_readback_preserves_negative_corexy_position()
    print("PASS: negative CoreXY trajectory readback remains signed")
    test_wide_trajectory_readback_unwraps_from_physical_counter()
    print("PASS: wrapped trajectory phase unwraps from physical steps")
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
    test_ordinary_axis_move_ends_with_hold_before_synchronous_wait()
    print("PASS: ordinary-axis motion ends with a hold before a"
          " synchronous wait")
    test_zero_scan_window_keeps_homing_drip_streaming_behavior()
    print("PASS: homing drip retains incremental streaming behavior")
    test_trajectory_anchor_includes_kinematic_scan_preroll()
    print("PASS: trajectory anchors include pressure-advance and shaping"
          " scan preroll")
    test_trajectory_drains_disconnected_windows_before_trapq_cleanup()
    print("PASS: one flush drains every disconnected shaped activity"
          " window before trapq cleanup")
    test_trajectory_drains_capped_segment_batch_before_hold()
    print("PASS: full fitter batches drain before the terminal hold")
    test_external_generator_delay_survives_scan_window_rescan()
    print("PASS: trajectory transport reserves its motion-generation lead")
    test_trajectory_end_always_queues_terminal_hold()
    print("PASS: every completed trajectory queues an explicit terminal"
          " hold")
    test_secondary_terminal_hold_respects_short_machine_gap()
    print("PASS: secondary-MCU holds fit short machine-time gaps")
    test_wire_record_preserves_exact_segment_coefficients()
    print("PASS: flight recording preserves exact wire coefficients and"
          " chained endpoints")
    test_g1_quintic_segments_use_higher_order_wire_command()
    print("PASS: normal G1 quintics bypass the quadratic wire command")
    test_rebase_waits_for_previous_horizon()
    print("PASS: a new rebase waits for the previous physical horizon")
    test_active_path_is_held_before_rebase_boundary()
    print("PASS: an active path gets an explicit hold before rebase")
    test_confirmed_stop_does_not_queue_a_pre_rebase_hold()
    print("PASS: a confirmed trigger stop rebases without a stale hold")
    test_value_trajectory_fails_before_fitter_advance()
    print("PASS: value fitting fails before unsynchronized send")


if __name__ == '__main__':
    main()
