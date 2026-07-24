#!/usr/bin/env python3
"""Workstation regressions for coordinated trajectory execution grants."""

import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'klippy'))
sys.path.insert(0, os.path.join(ROOT, 'klippy', 'extras'))
sys.modules['chelper'] = types.ModuleType('chelper')

import trajectory_queuing


class FakeReactor:
    NEVER = 1.e30
    NOW = 0.

    def __init__(self):
        self.now = 0.
        self.pause_hook = None
        self.timer_updates = []

    def monotonic(self):
        return self.now

    def pause(self, waketime):
        self.now = waketime
        if self.pause_hook is not None:
            self.pause_hook(waketime)
        return waketime

    def update_timer(self, timer, waketime):
        self.timer_updates.append((timer, waketime))


class FakePrinter:
    def __init__(self):
        self.reactor = FakeReactor()
        self.toolhead = FakeToolhead()
        self.virtual_sdcard = None
        self.events = []

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name, default=None):
        if name == 'toolhead':
            return self.toolhead
        if name == 'virtual_sdcard':
            return self.virtual_sdcard
        return default

    def send_event(self, name, *args):
        self.events.append((name, args))

    def command_error(self, message):
        return RuntimeError(message)


class FakeToolhead:
    def __init__(self):
        self.last_move_time = 0.

    def get_last_move_time(self):
        return self.last_move_time


class FakeMCU:
    def __init__(self, name, frequency, estimate):
        self.name = name
        self.frequency = frequency
        self.estimate = estimate
        self.paused = False

    def get_name(self):
        return self.name

    def estimated_print_time(self, eventtime):
        return self.estimate

    def print_time_to_clock(self, print_time):
        return int(round(print_time * self.frequency))

    def is_link_paused(self):
        return self.paused


class FakeMember:
    def __init__(self, mcu):
        self.mcu = mcu
        self.name = mcu.get_name()
        self.sent = []
        self.configured = []
        self.state = None

    def is_paused(self):
        return self.mcu.is_link_paused()

    def grant(self, *args):
        self.sent.append(args)

    def configure(self, *args):
        self.configured.append(args)


class FakeCommand:
    def __init__(self):
        self.sent = []

    def send(self, args=(), **kwargs):
        self.sent.append((args, kwargs))


class FakeConnectMCU:
    error = RuntimeError

    def __init__(self):
        self.response_format = None

    def get_name(self):
        return 'mcu'

    def alloc_command_queue(self):
        return object()

    def try_lookup_command(self, msgformat, cq=None):
        return FakeCommand()

    def register_serial_response(self, callback, msgformat, oid=None):
        self.response_format = msgformat


def make_owner():
    owner = trajectory_queuing.TrajectoryQueuing.__new__(
        trajectory_queuing.TrajectoryQueuing)
    owner.printer = FakePrinter()
    owner.execution_grants = True
    owner.execution_grant_horizon = 1.5
    owner.execution_grant_interval = .250
    owner.execution_group_id = 9
    owner.execution_epoch_hi = 0x12345678
    owner.execution_epoch_lo = 0x9abcdef0
    owner.group_sequence = 0
    owner.group_pending = None
    owner.group_next_proposal = 0.
    owner.group_proposal_time = None
    owner.group_committed_sequence = 0
    owner.group_committed_until = None
    owner.group_renewal_fault = None
    owner.group_grant_ready = False
    owner.group_config_pending = None
    owner.group_config_error = None
    owner.recovery_grant_active = False
    owner.group_timer = object()
    owner.recovery_active = False
    owner.recovery_trigger = None
    owner.steppers = []
    primary = FakeMCU('mcu', 12_000_000., 20.)
    rodent = FakeMCU('rodent', 80_000_000., 20.1)
    owner.machine = primary
    owner.group_members = {
        'mcu': FakeMember(primary),
        'rodent': FakeMember(rodent),
    }
    owner.get_machine_mcu = lambda: owner.machine
    owner.is_mcu_synced = lambda mcu: True
    return owner


class FakeRecoveryStepper:
    def __init__(self):
        self.recovery_hold = False
        self.motion_horizon_clock = None
        self.stopped = []

    def note_rebase_needed(self, stopped=False):
        self.stopped.append(stopped)


def ack_for(owner, member):
    pending = owner.group_pending
    owner._handle_group_state(member, {
        'group_id': owner.execution_group_id,
        'epoch_hi': owner.execution_epoch_hi,
        'epoch_lo': owner.execution_epoch_lo,
        'sequence': pending['sequence'],
        'machine_clock': pending['machine_clock'],
        'flags': trajectory_queuing.TGF_CONFIGURED
                 | trajectory_queuing.TGF_ARMED,
        'reject_reason': trajectory_queuing.TGR_OK,
    })


def config_ack_for(owner, member):
    owner._handle_group_state(member, {
        'group_id': owner.execution_group_id,
        'epoch_hi': owner.execution_epoch_hi,
        'epoch_lo': owner.execution_epoch_lo,
        'sequence': 0,
        'machine_clock': 0,
        'flags': trajectory_queuing.TGF_CONFIGURED,
        'reject_reason': trajectory_queuing.TGR_OK,
    })


def test_grant_requires_every_member_ack():
    owner = make_owner()
    next_time = owner._grant_timer(10.)
    assert next_time == 10.1
    assert owner.group_pending['sequence'] == 1
    assert all(len(member.sent) == 1
               for member in owner.group_members.values())
    members = list(owner.group_members.values())
    ack_for(owner, members[0])
    assert not owner.group_grant_ready
    assert owner.group_pending is not None
    ack_for(owner, members[1])
    assert owner.group_grant_ready
    assert owner.group_committed_sequence == 1
    assert owner.group_pending is None


def test_duplicate_and_rejected_states_do_not_commit():
    owner = make_owner()
    owner.printer.reactor.now = 10.
    owner._grant_timer(10.)
    first, second = owner.group_members.values()
    ack_for(owner, first)
    ack_for(owner, first)
    pending = owner.group_pending
    owner._handle_group_state(second, {
        'group_id': owner.execution_group_id,
        'epoch_hi': owner.execution_epoch_hi,
        'epoch_lo': owner.execution_epoch_lo,
        'sequence': pending['sequence'],
        'machine_clock': pending['machine_clock'],
        'flags': trajectory_queuing.TGF_CONFIGURED,
        'reject_reason': 6,
    })
    assert not owner.group_grant_ready
    assert owner.group_pending is None
    assert owner._grant_timer(10.1) == 10.25
    owner._grant_timer(10.25)
    assert owner.group_sequence == 2


def test_missing_member_stops_all_renewals():
    owner = make_owner()
    owner.group_grant_ready = True
    owner.group_committed_until = 20.
    owner.group_members['rodent'].mcu.paused = True
    next_time = owner._grant_timer(10.)
    assert next_time == 10.25
    assert not owner.group_grant_ready
    assert owner.group_sequence == 0
    assert owner.group_pending is None
    assert all(not member.sent for member in owner.group_members.values())


def test_active_rejection_latches_and_stops_reproposal():
    owner = make_owner()
    owner._grant_timer(10.)
    stepper = FakeRecoveryStepper()
    stepper.motion_horizon_clock = 30 * 12_000_000
    owner.steppers = [stepper]
    first, _second = owner.group_members.values()
    owner._handle_group_state(first, {
        'group_id': owner.execution_group_id,
        'epoch_hi': owner.execution_epoch_hi,
        'epoch_lo': owner.execution_epoch_lo,
        'sequence': owner.group_pending['sequence'],
        'machine_clock': owner.group_pending['machine_clock'],
        'flags': trajectory_queuing.TGF_CONFIGURED,
        'reject_reason': 5,
    })
    assert owner.group_renewal_fault['member'] == 'mcu'
    assert not owner.group_grant_ready
    assert owner.recovery_active
    assert stepper.recovery_hold
    assert stepper.stopped == [True]
    assert owner.recovery_trigger['reason'] == 'execution_grant_rejected'
    assert owner.printer.events[-1][0] == (
        'trajectory_queuing:recovery_hold')
    owner._grant_timer(10.1)
    assert owner.group_sequence == 1
    assert owner.group_pending is None


def test_timesync_loss_pauses_group_before_next_print_move():
    owner = make_owner()
    owner.printer.virtual_sdcard = types.SimpleNamespace(
        is_active=lambda: True)
    stepper = FakeRecoveryStepper()
    owner.steppers = [stepper]
    owner._handle_timesync_convergence('rodent', False)
    assert owner.recovery_active
    assert not owner.group_grant_ready
    assert stepper.recovery_hold
    assert stepper.stopped == [True]
    assert owner.recovery_trigger['reason'] == 'timesync_lost'
    assert owner.recovery_trigger['mcu'] == 'rodent'
    assert owner.printer.events == [(
        'trajectory_queuing:recovery_hold',
        (owner.recovery_trigger,))]


def test_execution_grant_uses_deadline_aware_delivery():
    mcu = FakeConnectMCU()
    owner = types.SimpleNamespace(_handle_group_state=lambda *args: None)
    member = trajectory_queuing.TrajectoryGroupMember(owner, mcu)
    member.connect()
    member.grant(9, 1, 2, 3, 4, 5)
    args, options = member.grant_cmd.sent[-1]
    assert args == [9, 1, 2, 3, 4, 5]
    assert options == {
        'retry_class': trajectory_queuing.SERIAL_RETRY_BUFFERED,
        'retry_clock': 5,
    }


def test_startup_planning_lead_does_not_latch_idle_rejection():
    owner = make_owner()
    owner.printer.reactor.now = 10.
    owner._grant_timer(10.)
    # Klippy establishes a future scheduling horizon while connecting even
    # though no trajectory segment has been queued.
    owner.printer.toolhead.last_move_time = 30.
    first, _second = owner.group_members.values()
    owner._handle_group_state(first, {
        'group_id': owner.execution_group_id,
        'epoch_hi': owner.execution_epoch_hi,
        'epoch_lo': owner.execution_epoch_lo,
        'sequence': owner.group_pending['sequence'],
        'machine_clock': owner.group_pending['machine_clock'],
        'flags': trajectory_queuing.TGF_CONFIGURED,
        'reject_reason': 5,
    })
    assert owner.group_renewal_fault is None
    assert owner._grant_timer(10.1) == 10.25
    owner._grant_timer(10.25)
    assert owner.group_sequence == 2
    assert owner.group_pending is not None


def test_stopped_stepper_clears_motion_horizon():
    stepper = trajectory_queuing.TrajectoryStepper.__new__(
        trajectory_queuing.TrajectoryStepper)
    stepper.anchored = True
    stepper.rebase_requires_hold = False
    stepper.rebase_min_clock = 123
    stepper.rebase_min_execution_clock = 456
    stepper.motion_horizon_clock = 789
    stepper.recovery_rebase = False
    stepper.note_rebase_needed(stopped=True)
    assert stepper.motion_horizon_clock is None


def test_renewal_respects_interval_after_all_ack():
    owner = make_owner()
    owner.printer.reactor.now = 10.
    owner._grant_timer(10.)
    for member in list(owner.group_members.values()):
        ack_for(owner, member)
    assert owner.group_next_proposal == 10.25
    assert owner._grant_timer(10.1) == 10.25
    assert owner.group_sequence == 1
    owner._grant_timer(10.25)
    assert owner.group_sequence == 2


def test_expired_host_lease_revokes_ready_state():
    owner = make_owner()
    owner.group_grant_ready = True
    owner.group_committed_until = 21.
    assert owner._execution_grant_valid(10.)
    owner.group_committed_until = 20.
    assert not owner._execution_grant_valid(10.)
    assert not owner.group_grant_ready


def test_reproposal_horizon_never_moves_backwards():
    owner = make_owner()
    owner.printer.reactor.now = 10.
    owner._grant_timer(10.)
    first_grant_time = owner.group_pending['grant_time']
    first, _second = owner.group_members.values()
    owner._handle_group_state(first, {
        'group_id': owner.execution_group_id,
        'epoch_hi': owner.execution_epoch_hi,
        'epoch_lo': owner.execution_epoch_lo,
        'sequence': owner.group_pending['sequence'],
        'machine_clock': owner.group_pending['machine_clock'],
        'flags': trajectory_queuing.TGF_CONFIGURED,
        'reject_reason': 5,
    })
    for member in owner.group_members.values():
        member.mcu.estimate = 5.
    owner._grant_timer(10.25)
    assert owner.group_pending['grant_time'] > first_grant_time


def test_idle_reproposal_horizon_does_not_run_ahead_of_time():
    owner = make_owner()
    now = 10.
    estimate = 20.
    for _attempt in range(160):
        owner.printer.reactor.now = now
        for member in owner.group_members.values():
            member.mcu.estimate = estimate
        owner._grant_timer(now)
        pending = owner.group_pending
        assert pending is not None
        first, _second = owner.group_members.values()
        owner._handle_group_state(first, {
            'group_id': owner.execution_group_id,
            'epoch_hi': owner.execution_epoch_hi,
            'epoch_lo': owner.execution_epoch_lo,
            'sequence': pending['sequence'],
            'machine_clock': pending['machine_clock'],
            'flags': trajectory_queuing.TGF_CONFIGURED,
            'reject_reason': 5,
        })
        assert owner.group_next_proposal == (
            now + owner.execution_grant_interval)
        # The next normal cadence advances real and estimated time together.
        now += owner.execution_grant_interval
        estimate += owner.execution_grant_interval
    assert pending['grant_time'] <= (
        estimate + owner.execution_grant_horizon)


def test_proposal_advances_past_each_member_clock():
    owner = make_owner()
    old_grant_time = 30.
    machine_clock = owner.machine.print_time_to_clock(old_grant_time)
    for member in owner.group_members.values():
        member.state = {
            'group_id': owner.execution_group_id,
            'epoch_hi': owner.execution_epoch_hi,
            'epoch_lo': owner.execution_epoch_lo,
            'sequence': 12,
            'machine_clock': machine_clock & 0xffffffff,
            'local_clock': (
                member.mcu.print_time_to_clock(old_grant_time)
                & 0xffffffff),
        }
    for member in owner.group_members.values():
        member.mcu.estimate = 5.
    owner._grant_timer(10.)
    assert owner.group_pending['grant_time'] > old_grant_time


def test_recovery_uses_fresh_epoch_and_all_mcu_grant():
    owner = make_owner()
    owner.recovery_active = True
    old_epoch = (owner.execution_epoch_hi, owner.execution_epoch_lo)

    def progress(_waketime):
        if owner.group_config_pending is not None:
            for member in owner.group_members.values():
                config_ack_for(owner, member)
            return
        if owner.recovery_grant_active and owner.group_pending is None:
            owner._grant_timer(owner.printer.reactor.monotonic())
            for member in owner.group_members.values():
                ack_for(owner, member)

    owner.printer.reactor.pause_hook = progress
    messages = []
    assert owner.acquire_recovery_grant(1., messages.append)
    assert (owner.execution_epoch_hi, owner.execution_epoch_lo) != old_epoch
    assert all(member.configured
               for member in owner.group_members.values())
    assert owner.recovery_active
    assert owner.recovery_grant_active
    assert owner.group_grant_ready
    assert owner.group_committed_sequence == 1
    try:
        owner._handle_check_move(object())
    except RuntimeError as e:
        assert "recovery hold is active" in str(e)
    else:
        raise AssertionError("normal motion admitted during recovery rebase")


def test_recovery_config_requires_every_member():
    owner = make_owner()
    owner.recovery_active = True
    owner._begin_recovery_grant()
    first, second = owner.group_members.values()
    config_ack_for(owner, first)
    assert owner.group_config_pending is not None
    assert not owner.recovery_grant_active
    config_ack_for(owner, second)
    assert owner.group_config_pending is None
    assert owner.recovery_grant_active
    assert owner.printer.reactor.timer_updates


def test_recovery_config_timeout_leaves_group_closed():
    owner = make_owner()
    owner.recovery_active = True
    first = list(owner.group_members.values())[0]

    def progress(_waketime):
        if owner.group_config_pending is not None:
            config_ack_for(owner, first)

    owner.printer.reactor.pause_hook = progress
    messages = []
    assert not owner.acquire_recovery_grant(.1, messages.append)
    assert not owner.group_grant_ready
    assert not owner.recovery_grant_active
    assert owner.group_pending is None
    assert any("epoch acknowledgement from rodent" in msg
               for msg in messages)


def test_member_registers_complete_response_format():
    mcu = FakeConnectMCU()
    member = trajectory_queuing.TrajectoryGroupMember(object(), mcu)
    member.connect()
    assert mcu.response_format == (
        "trajectory_group_state group_id=%u epoch_hi=%u epoch_lo=%u"
        " sequence=%u machine_clock=%u local_clock=%u flags=%c"
        " reject_reason=%c accepted=%hu rejected=%hu")


def test_firmware_has_closed_epoch_and_controlled_expiry():
    source = open(os.path.join(ROOT, 'src', 'trajq.c'),
                  encoding='utf-8').read()
    assert 'trajectory_group_config group_id=%u epoch_hi=%u epoch_lo=%u' \
        in source
    assert 'trajectory_group_grant group_id=%u epoch_hi=%u epoch_lo=%u' \
        in source
    assert 'traj_group_ingest_open()' in source
    assert 'local_clock = timesync_clock_to_local(machine_clock)' in source
    assert 'traj_group_all_staged(local_clock)' not in source
    assert 'trajq_synth_ramp(tq, velocity)' in source
    assert 'TQF_HALT_BARRIER' in source


def main():
    tests = [
        test_grant_requires_every_member_ack,
        test_duplicate_and_rejected_states_do_not_commit,
        test_missing_member_stops_all_renewals,
        test_active_rejection_latches_and_stops_reproposal,
        test_timesync_loss_pauses_group_before_next_print_move,
        test_execution_grant_uses_deadline_aware_delivery,
        test_startup_planning_lead_does_not_latch_idle_rejection,
        test_stopped_stepper_clears_motion_horizon,
        test_renewal_respects_interval_after_all_ack,
        test_expired_host_lease_revokes_ready_state,
        test_reproposal_horizon_never_moves_backwards,
        test_idle_reproposal_horizon_does_not_run_ahead_of_time,
        test_proposal_advances_past_each_member_clock,
        test_recovery_uses_fresh_epoch_and_all_mcu_grant,
        test_recovery_config_requires_every_member,
        test_recovery_config_timeout_leaves_group_closed,
        test_member_registers_complete_response_format,
        test_firmware_has_closed_epoch_and_controlled_expiry,
    ]
    for test in tests:
        test()
        print("PASS:", test.__name__)


if __name__ == '__main__':
    main()
