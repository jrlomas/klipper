#!/usr/bin/env python3
# Standalone unit test for host-side motion resume reconciliation
# (FD-0001 doc 08 "Resume workflow").  Stubs the MCU / stepper /
# execlog interfaces and drives FailureRecovery._resume_motion through
# three scenarios:
#
#   1. normal reconnect-resume - the board never rebooted, so every
#      joint is rebased at its authoritative held accumulator and the
#      print resumes;
#   2. underrun-truncated stream - the reconciler uses the board's
#      held (ramp-end) position and flags the truncation;
#   3. board reset - HELIX homing-retained model: the extruder re-primes
#      and a retained positional axis re-anchors at its last commanded
#      position (both auto-resumable), while an axis declared
#      motion_homing_volatile blocks the resume pending a re-home.
#
# No printer, MCU, or chelper build is required.  Exits 0 on success.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
import sys
import collections

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                "..", "klippy"))

import extras.failure_recovery as fr  # noqa: E402
import extras.heaters as heaters_mod  # noqa: E402
from extras.failure_recovery import (  # noqa: E402
    EL_SEG_DONE, EL_UNDERRUN)


# ---- Stubs -----------------------------------------------------------

class FakeReactor:
    def __init__(self):
        self.callbacks = []

    def monotonic(self):
        return 0.

    def register_callback(self, callback):
        self.callbacks.append(callback)


class FakeGcode:
    def __init__(self):
        self.commands = {}
        self.scripts = []

    def register_command(self, name, fn, desc=None):
        self.commands[name] = fn

    def run_script(self, script):
        self.scripts.append(script)


class FakeMcu:
    def __init__(self, name, shutdown=False):
        self._name = name
        self._shutdown = shutdown

    def get_name(self):
        return self._name

    def is_fileoutput(self):
        return False

    def is_shutdown(self):
        return self._shutdown


class FakeExecLog:
    def __init__(self, mcu, records):
        self.mcu = mcu
        self._records = records

    def drain(self):
        return list(self._records)


class FakeTrajStepper:
    def __init__(self, name, oid, mcu, is_relative=False,
                 homing_volatile=False, held=None, last_intention=None,
                 held_commanded=None):
        self.name = name
        self.oid = oid
        self.mcu = mcu
        self.is_relative = is_relative
        self.homing_volatile = homing_volatile
        self._held = held           # (clock, pos_su) or None / Exception
        self._last_intention = last_intention
        self._held_commanded = held_commanded
        self.reconciled_with = None
        self.reanchored = False

    def homing_retained(self):
        return self.is_relative or not self.homing_volatile

    def read_held(self):
        if isinstance(self._held, Exception):
            raise self._held
        return self._held

    def last_intention(self):
        return self._last_intention

    def resume_reconcile(self, clock, pos_su, anchor_print_time):
        self.reconciled_with = (
            int(clock), int(pos_su), float(anchor_print_time))
        return self._held_commanded

    def note_resume_reanchor(self, anchor_print_time=None):
        self.reanchored = True


class FakeTrajQueuing:
    def __init__(self, steppers):
        self._steppers = steppers
        self.completed_recovery = False

    def get_trajectory_steppers(self):
        return list(self._steppers)

    def get_recovery_anchor_time(self):
        return 42.25

    def complete_recovery_hold(self):
        self.completed_recovery = True

    def get_status(self, eventtime=None):
        return {'recovery_active': not self.completed_recovery,
                'recovery_trigger': None}


class FakePauseResume:
    def __init__(self, paused=True):
        self._paused = paused

    def get_status(self, eventtime):
        return {'is_paused': self._paused}


class FakeToolhead:
    class Kin:
        def __init__(self, names):
            self.steppers = [type('S', (), {'get_name': lambda self, n=n: n})()
                             for n in names]
        def get_steppers(self):
            return self.steppers
        def calc_position(self, positions):
            return [0.5 * (positions['stepper_x']
                           + positions['stepper_y']),
                    0.5 * (positions['stepper_x']
                           - positions['stepper_y']),
                    positions['stepper_z']]
    def __init__(self):
        self.kin = self.Kin(['stepper_x', 'stepper_y', 'stepper_z'])
        self.position = [90., 90., 90., 0.]
        self.set_positions = []
    def get_kinematics(self):
        return self.kin
    def get_position(self):
        return list(self.position)
    def set_position(self, position):
        self.position = list(position)
        self.set_positions.append(list(position))


class FakePrinter:
    def __init__(self):
        self.reactor = FakeReactor()
        self.gcode = FakeGcode()
        self.objects = {'gcode': self.gcode, 'configfile': object()}
        self.events = {}

    def get_reactor(self):
        return self.reactor

    def register_event_handler(self, name, cb):
        self.events.setdefault(name, []).append(cb)

    def send_event(self, name, *args):
        pass

    def load_object(self, config, name):
        return self.objects[name]

    def lookup_object(self, name, default="__sentinel__"):
        if name in self.objects:
            return self.objects[name]
        if default != "__sentinel__":
            return default
        raise KeyError(name)


class FakeConfig:
    def __init__(self, printer):
        self.printer = printer

    def get_printer(self):
        return self.printer

    def getint(self, name, default, minval=None, maxval=None):
        return default

    def getboolean(self, name, default):
        return default

    def getfloat(self, name, default, **kw):
        return default

    def get(self, name, default=None):
        return default

    def getlist(self, name, default):
        return default

    def get_prefix_sections(self, prefix):
        return []

    def error(self, msg):
        return Exception(msg)


def make_fr(printer):
    return fr.FailureRecovery(FakeConfig(printer))


def test_heater_hold_tracks_live_target_and_mcu_state():
    class Command:
        def __init__(self, response=None):
            self.calls = []
            self.response = response
        def send(self, args):
            self.calls.append(list(args))
            return self.response
    class Heater:
        target = 0.
        sensor = type('Sensor', (), {
            'adc_convert': type('Convert', (), {
                'calc_adc': staticmethod(lambda temp: temp / 100.)})()
        })()
        def get_status(self, eventtime):
            return {'target': self.target}
    class Heaters:
        def __init__(self, heater):
            self.heater = heater
        def lookup_heater(self, name):
            assert name == 'heater_bed'
            return self.heater
    class Printer:
        def __init__(self, heaters):
            self.heaters = heaters
        def lookup_object(self, name):
            assert name == 'heaters'
            return self.heaters
    class Mcu:
        def seconds_to_clock(self, seconds):
            return int(seconds * 1000)

    heater = Heater()
    hold = fr.HeaterHold.__new__(fr.HeaterHold)
    hold.printer = Printer(Heaters(heater))
    hold.name = 'heater_bed'
    hold.max_temp_cfg = 130.
    hold.hold_max_temp = 65.
    hold.hold_max_duration = 20.
    hold.hold_ping_timeout = 2.
    hold.mcu = Mcu()
    hold.adc_max = 4095
    hold.oid = 7
    hold.heater = None
    hold.setup_cmd = Command()
    hold.engage_cmd = Command()
    hold.release_cmd = Command()
    hold.query_cmd = Command({
        'state': fr.HH_EXPIRED, 'adc': 4321, 'samples': 80})
    hold.armed_target = None
    hold.state = fr.HH_DISABLED
    hold.last_adc = hold.engaged_samples = 0
    hold.rearm_pending = False
    hold.engaged = False

    assert hold.sync_target(0., force=True)
    assert hold.armed_target == 0. and hold.state == fr.HH_ARMED
    heater.target = 55.
    assert hold.sync_target(1.)
    assert hold.setup_cmd.calls[-1][1] == 2252
    assert not hold.sync_target(2.)
    heater.target = 80.
    assert hold.sync_target(3.)
    assert hold.armed_target == 65.
    assert hold.setup_cmd.calls[-1][1] == 2662

    hold.engage()
    assert hold.engaged and hold.state == fr.HH_ENGAGED
    hold.release()
    assert not hold.engaged and hold.state == fr.HH_DISABLED
    assert hold.rearm_pending
    assert hold.query() == fr.HH_EXPIRED
    assert not hold.engaged and hold.last_adc == 4321
    assert hold.engaged_samples == 80
    print("PASS: heater hold follows live targets and reports MCU state")


def test_heater_hold_replaces_legacy_pwm_watchdog():
    class Pwm:
        def __init__(self):
            self.max_durations = []
        def setup_max_duration(self, duration):
            self.max_durations.append(duration)
    class Heater:
        def __init__(self):
            self.mcu_pwm = Pwm()
            self.hold = None
        def setup_autonomous_hold(self, hold):
            self.hold = hold
            self.mcu_pwm.setup_max_duration(0.)
    class Heaters:
        def __init__(self, heater):
            self.heater = heater
            self.lookups = []
        def lookup_heater(self, name):
            self.lookups.append(name)
            return self.heater
    class Printer:
        def __init__(self, heaters):
            self.heaters = heaters
        def lookup_object(self, name):
            assert name == 'heaters'
            return self.heaters

    heater = Heater()
    heaters = Heaters(heater)
    hold = fr.HeaterHold.__new__(fr.HeaterHold)
    hold.printer = Printer(heaters)
    hold.name = 'heater_bed'
    hold.heater = None

    hold.prepare_mcu_config()
    assert heaters.lookups == ['heater_bed']
    assert hold.heater is heater
    assert heater.hold is hold
    assert heater.mcu_pwm.max_durations == [0.]
    print("PASS: heater hold replaces legacy PWM refresh watchdog")


def test_autonomous_hold_filters_stale_and_competing_host_pwm():
    class Reactor:
        def monotonic(self):
            return 100.
    class Printer:
        def get_reactor(self):
            return Reactor()
    class Mcu:
        def min_schedule_time(self):
            return .100
        def estimated_print_time(self, eventtime):
            assert eventtime == 100.
            return 100.
    class Pwm:
        def __init__(self):
            self.calls = []
        def get_mcu(self):
            return Mcu()
        def set_pwm(self, print_time, value):
            self.calls.append((print_time, value))
    class Hold:
        def __init__(self):
            self.blocked = False
        def blocks_host_pwm(self):
            return self.blocked

    heater = heaters_mod.Heater.__new__(heaters_mod.Heater)
    heater.printer = Printer()
    heater.mcu_pwm = Pwm()
    heater.autonomous_hold = Hold()
    heater.pwm_delay = .300
    heater.target_temp = 50.
    heater.verify_mainthread_time = 200.
    heater.next_pwm_time = 0.
    heater.last_pwm_value = 0.
    heater.min_pwm_change = .01

    heater.set_pwm(90., .5)
    assert heater.mcu_pwm.calls == []
    heater.autonomous_hold.blocked = True
    heater.set_pwm(100., .5)
    assert heater.mcu_pwm.calls == []
    heater.autonomous_hold.blocked = False
    heater.set_pwm(100., .5)
    assert heater.mcu_pwm.calls == [(100.3, .5)]
    print("PASS: held heater rejects stale and competing host PWM")


def test_execlog_drain_uses_response_barrier_and_deduplicates():
    class Recorder:
        def __init__(self):
            self.persisted = []
        def _record_execution(self, mcu, record):
            self.persisted.append(record)
    class Query:
        def __init__(self):
            self.calls = 0
        def send(self, args):
            self.calls += 1
            return {'oldest_seq': 10, 'next_seq': 20, 'dropped': 0}
    class Dump:
        def __init__(self, el):
            self.el = el
            self.calls = []
        def send(self, args):
            oid, first, count = args
            self.calls.append((first, count))
            for seq in range(first, first + count):
                self.el._handle_data(
                    {'seq': seq, 'type': 1, 'src': 4,
                     'clock': 10 * seq, 'pos': 200, 'aux': 0})

    el = fr.McuExecLog.__new__(fr.McuExecLog)
    el.fr = Recorder()
    el.mcu = FakeMcu('mcu')
    el.oid = 3
    el.records = collections.deque(maxlen=16)
    el._drain_records = None
    el.size = 16
    el._persisted_order = collections.deque()
    el._persisted_seqs = set()
    el.query_cmd = Query()
    el.dump_cmd = Dump(el)
    el._handle_data({'seq': 10, 'type': 1, 'src': 4,
                     'clock': 100, 'pos': 200, 'aux': 0})
    records = el.drain()
    assert el.dump_cmd.calls == [(10, 4), (14, 4), (18, 2)]
    assert el.query_cmd.calls == 4
    assert [r[0] for r in records] == list(range(10, 20))
    assert len(el.fr.persisted) == 10
    print("PASS: execlog drain paces chunks, barriers, and deduplicates")


def test_execlog_normalizes_negative_position():
    class Recorder:
        def __init__(self):
            self.persisted = []
        def _record_execution(self, mcu, record):
            self.persisted.append(record)

    el = fr.McuExecLog.__new__(fr.McuExecLog)
    el.fr = Recorder()
    el.mcu = FakeMcu('mcu')
    el.size = 16
    el.records = collections.deque(maxlen=16)
    el._drain_records = None
    el._persisted_order = collections.deque()
    el._persisted_seqs = set()
    el._handle_data({'seq': 1, 'type': 2, 'src': 7, 'clock': 1234,
                     'pos': 3493649149, 'aux': 0})
    assert el.records[-1][4] == -801318147
    assert el.fr.persisted[-1][4] == -801318147
    print("PASS: execution log persists negative positions as signed")


def test_shutdown_drain_is_deferred_outside_no_pause_handler():
    class ExecLog:
        def __init__(self):
            self.calls = 0
        def drain(self):
            self.calls += 1
            return []

    printer = FakePrinter()
    f = make_fr(printer)
    el = ExecLog()
    f.execlogs = [el]
    f._handle_shutdown()
    assert el.calls == 0
    assert len(printer.reactor.callbacks) == 1
    printer.reactor.callbacks.pop()(0.)
    assert el.calls == 1
    assert not f._shutdown_drain_pending
    print("PASS: shutdown schedules flight-log drain after no-pause scope")


# ---- Scenarios -------------------------------------------------------

def test_normal_resume():
    printer = FakePrinter()
    pr = FakePauseResume(paused=True)
    printer.objects['pause_resume'] = pr
    mcu = FakeMcu('mcu')
    x = FakeTrajStepper('stepper_x', 1, mcu,
                        held=(1000, 320000), last_intention=(500, 1000, 320000))
    e = FakeTrajStepper('extruder', 2, mcu, is_relative=True,
                        held=(1000, 65536), last_intention=(500, 1000, 65536))
    printer.objects['trajectory_queuing'] = FakeTrajQueuing([x, e])
    f = make_fr(printer)
    f.execlogs = [FakeExecLog(mcu, [
        (0, EL_SEG_DONE, 1, 900, 320000, 0),
        (1, EL_SEG_DONE, 2, 900, 65536, 0)])]
    f._resume_motion(gcmd=None)
    assert x.reconciled_with == (1000, 320000, 42.25), x.reconciled_with
    assert e.reconciled_with == (1000, 65536, 42.25), e.reconciled_with
    assert printer.gcode.scripts == ["RESUME"], printer.gcode.scripts
    assert f.last_recovery['blocked'] is False
    assert len(f.last_recovery['reconciled']) == 2
    assert not f.last_recovery['reset']
    print("PASS: normal reconnect-resume rebases both joints and resumes")


def test_underrun_truncated():
    printer = FakePrinter()
    printer.objects['pause_resume'] = FakePauseResume(paused=True)
    mcu = FakeMcu('mcu')
    # Host INTENDED to reach 400000 su; the queue ran dry and the board
    # ramped out to 337000 su, which is what its accumulator now holds.
    x = FakeTrajStepper('stepper_x', 1, mcu,
                        held=(1200, 337000),
                        last_intention=(500, 1100, 400000))
    printer.objects['trajectory_queuing'] = FakeTrajQueuing([x])
    f = make_fr(printer)
    f.execlogs = [FakeExecLog(mcu, [
        (0, EL_SEG_DONE, 1, 800, 300000, 0),
        (1, EL_UNDERRUN, 1, 1150, 337000, 0)])]
    f._resume_motion(gcmd=None)
    # Reconciler must rebase at the board's held (ramp-end) position,
    # not at the host's unreached intention.
    assert x.reconciled_with == (1200, 337000, 42.25), x.reconciled_with
    story = f.last_recovery['reconciled'][0][2]
    assert story['truncated'] is True, story
    assert story['gap'] == 400000 - 337000, story
    assert printer.gcode.scripts == ["RESUME"], printer.gcode.scripts
    print("PASS: underrun-truncated stream reconciles to the ramp-end pos")


def test_held_joints_restore_cartesian_toolhead_position():
    printer = FakePrinter()
    printer.objects['pause_resume'] = FakePauseResume(paused=False)
    printer.objects['toolhead'] = toolhead = FakeToolhead()
    mcu = FakeMcu('mcu')
    x = FakeTrajStepper('stepper_x', 1, mcu, held=(1000, 1),
                        held_commanded=70.)
    y = FakeTrajStepper('stepper_y', 2, mcu, held=(1000, 2),
                        held_commanded=30.)
    z = FakeTrajStepper('stepper_z', 3, mcu, held=(1000, 3),
                        held_commanded=42.)
    tq = printer.objects['trajectory_queuing'] = FakeTrajQueuing([x, y, z])
    f = make_fr(printer)
    f._resume_motion(gcmd=None)
    assert toolhead.set_positions == [[50., 20., 42., 0.]], (
        toolhead.set_positions)
    assert tq.completed_recovery
    print("PASS: held joint accumulators restore Cartesian coordinates")


def test_board_reset():
    printer = FakePrinter()
    printer.objects['pause_resume'] = FakePauseResume(paused=True)
    # Toolhead board rebooted: it stays in the paused-link set (reconnect
    # boot-detection refused to clear it), so its accumulators are gone.
    mcu = FakeMcu('toolhead')
    e = FakeTrajStepper('extruder', 2, mcu, is_relative=True,
                        held=(0, 0), last_intention=(500, 1000, 65536))
    # Retained absolute axis (default): homing trusted across the reset.
    y = FakeTrajStepper('stepper_y', 3, mcu,
                        held=(0, 0), last_intention=(500, 1000, 240000))
    # Volatile absolute axis: homing genuinely lost, must be re-homed.
    z = FakeTrajStepper('stepper_z', 4, mcu, homing_volatile=True,
                        held=(0, 0), last_intention=(500, 1000, 80000))
    printer.objects['trajectory_queuing'] = FakeTrajQueuing([e, y, z])
    f = make_fr(printer)
    f.link_paused_mcus.add('toolhead')
    f.execlogs = [FakeExecLog(mcu, [])]
    f._resume_motion(gcmd=None)
    # E is relative -> re-primed (re-anchored on next motion); it must
    # NOT be rebased at a faked accumulator.
    assert e.reanchored is True
    assert e.reconciled_with is None
    # Retained absolute axis -> also re-anchored at its last commanded
    # position and auto-resumable; no faked accumulator either.
    assert y.reanchored is True
    assert y.reconciled_with is None
    # Volatile axis -> homing lost: NOT re-anchored, and it blocks resume.
    assert z.reanchored is False
    assert z.reconciled_with is None
    assert f.last_recovery['blocked'] is True
    assert printer.gcode.scripts == [], printer.gcode.scripts
    disp = dict((r['joint'], r['homing_retained'])
                for r in f.last_recovery['reset'])
    assert disp == {'extruder': True, 'stepper_y': True,
                    'stepper_z': False}, disp
    zentry = [r for r in f.last_recovery['reset']
              if r['joint'] == 'stepper_z'][0]
    assert 're-home' in zentry['action']
    assert zentry['last_intention'] == (500, 1000, 80000)
    print("PASS: board reset - E reprimed, retained axis re-anchored,"
          " volatile axis blocks resume")


def main():
    test_heater_hold_tracks_live_target_and_mcu_state()
    test_heater_hold_replaces_legacy_pwm_watchdog()
    test_autonomous_hold_filters_stale_and_competing_host_pwm()
    test_execlog_drain_uses_response_barrier_and_deduplicates()
    test_execlog_normalizes_negative_position()
    test_shutdown_drain_is_deferred_outside_no_pause_handler()
    test_normal_resume()
    test_underrun_truncated()
    test_held_joints_restore_cartesian_toolhead_position()
    test_board_reset()
    print("ALL PASS")


if __name__ == '__main__':
    main()
