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

    def __init__(self):
        self.now = 0.

    def monotonic(self):
        return self.now


class FakePrinter:
    def __init__(self):
        self.reactor = FakeReactor()

    def get_reactor(self):
        return self.reactor


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
        self.state = None

    def is_paused(self):
        return self.mcu.is_link_paused()

    def grant(self, *args):
        self.sent.append(args)


class FakeCommand:
    def send(self, args=()):
        pass


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
    owner.group_grant_ready = False
    owner.recovery_active = False
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
    owner._grant_timer(10.1)
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
    owner._grant_timer(10.1)
    assert owner.group_pending['grant_time'] > first_grant_time


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
    assert 'trajq_synth_ramp(tq, velocity)' in source
    assert 'TQF_HALT_BARRIER' in source


def main():
    tests = [
        test_grant_requires_every_member_ack,
        test_duplicate_and_rejected_states_do_not_commit,
        test_missing_member_stops_all_renewals,
        test_renewal_respects_interval_after_all_ack,
        test_expired_host_lease_revokes_ready_state,
        test_reproposal_horizon_never_moves_backwards,
        test_proposal_advances_past_each_member_clock,
        test_member_registers_complete_response_format,
        test_firmware_has_closed_epoch_and_controlled_expiry,
    ]
    for test in tests:
        test()
        print("PASS:", test.__name__)


if __name__ == '__main__':
    main()
