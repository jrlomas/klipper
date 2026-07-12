#!/usr/bin/env python3
# Standalone unit test for host-side motion resume reconciliation
# (RFC 0001 doc 08 "Resume workflow").  Stubs the MCU / stepper /
# execlog interfaces and drives FailureRecovery._resume_motion through
# three scenarios:
#
#   1. normal reconnect-resume - the board never rebooted, so every
#      joint is rebased at its authoritative held accumulator and the
#      print resumes;
#   2. underrun-truncated stream - the reconciler uses the board's
#      held (ramp-end) position and flags the truncation;
#   3. board reset - the extruder is reclassified auto-resumable while a
#      positional axis is flagged for re-qualification, blocking resume.
#
# No printer, MCU, or chelper build is required.  Exits 0 on success.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                "..", "klippy"))

import extras.failure_recovery as fr  # noqa: E402
from extras.failure_recovery import (  # noqa: E402
    EL_SEG_DONE, EL_UNDERRUN)


# ---- Stubs -----------------------------------------------------------

class FakeReactor:
    def monotonic(self):
        return 0.


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
    def __init__(self, name, oid, mcu, recovery_class,
                 held=None, last_intention=None):
        self.name = name
        self.oid = oid
        self.mcu = mcu
        self.recovery_class = recovery_class
        self._held = held           # (clock, pos_su) or None / Exception
        self._last_intention = last_intention
        self.reconciled_with = None
        self.reprimed = False

    def read_held(self):
        if isinstance(self._held, Exception):
            raise self._held
        return self._held

    def last_intention(self):
        return self._last_intention

    def resume_reconcile(self, clock, pos_su):
        self.reconciled_with = (int(clock), int(pos_su))

    def note_reprime(self):
        self.reprimed = True


class FakeTrajQueuing:
    def __init__(self, steppers):
        self._steppers = steppers

    def get_trajectory_steppers(self):
        return list(self._steppers)


class FakePauseResume:
    def __init__(self, paused=True):
        self._paused = paused

    def get_status(self, eventtime):
        return {'is_paused': self._paused}


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


# ---- Scenarios -------------------------------------------------------

def test_normal_resume():
    printer = FakePrinter()
    pr = FakePauseResume(paused=True)
    printer.objects['pause_resume'] = pr
    mcu = FakeMcu('mcu')
    x = FakeTrajStepper('stepper_x', 1, mcu, 'reference',
                        held=(1000, 320000), last_intention=(500, 1000, 320000))
    e = FakeTrajStepper('extruder', 2, mcu, 'extruder',
                        held=(1000, 65536), last_intention=(500, 1000, 65536))
    printer.objects['trajectory_queuing'] = FakeTrajQueuing([x, e])
    f = make_fr(printer)
    f.execlogs = [FakeExecLog(mcu, [
        (0, EL_SEG_DONE, 1, 900, 320000, 0),
        (1, EL_SEG_DONE, 2, 900, 65536, 0)])]
    f._resume_motion(gcmd=None)
    assert x.reconciled_with == (1000, 320000), x.reconciled_with
    assert e.reconciled_with == (1000, 65536), e.reconciled_with
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
    x = FakeTrajStepper('stepper_x', 1, mcu, 'reference',
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
    assert x.reconciled_with == (1200, 337000), x.reconciled_with
    story = f.last_recovery['reconciled'][0][2]
    assert story['truncated'] is True, story
    assert story['gap'] == 400000 - 337000, story
    assert printer.gcode.scripts == ["RESUME"], printer.gcode.scripts
    print("PASS: underrun-truncated stream reconciles to the ramp-end pos")


def test_board_reset():
    printer = FakePrinter()
    printer.objects['pause_resume'] = FakePauseResume(paused=True)
    # Toolhead board rebooted: it stays in the paused-link set (reconnect
    # boot-detection refused to clear it), so its accumulators are gone.
    mcu = FakeMcu('toolhead')
    e = FakeTrajStepper('extruder', 2, mcu, 'extruder',
                        held=(0, 0), last_intention=(500, 1000, 65536))
    y = FakeTrajStepper('stepper_y', 3, mcu, 'reference',
                        held=(0, 0), last_intention=(500, 1000, 240000))
    printer.objects['trajectory_queuing'] = FakeTrajQueuing([e, y])
    f = make_fr(printer)
    f.link_paused_mcus.add('toolhead')
    f.execlogs = [FakeExecLog(mcu, [])]
    f._resume_motion(gcmd=None)
    # E is relative -> reclassified auto-resumable (re-prime); it must
    # NOT be rebased at a faked accumulator.
    assert e.reprimed is True
    assert e.reconciled_with is None
    # Positional axis with an independent reference -> flagged for
    # re-qualification; resume is blocked and no position is faked.
    assert y.reconciled_with is None
    assert f.last_recovery['blocked'] is True
    assert printer.gcode.scripts == [], printer.gcode.scripts
    classes = dict((r['joint'], r['class']) for r in f.last_recovery['reset'])
    assert classes == {'extruder': 'extruder', 'stepper_y': 'reference'}, classes
    yentry = [r for r in f.last_recovery['reset'] if r['joint'] == 'stepper_y'][0]
    assert 're-qualify' in yentry['action'] or 're-home' in yentry['action']
    assert yentry['last_intention'] == (500, 1000, 240000)
    print("PASS: board reset - E reprimed, positional axis flagged, resume"
          " blocked")


def main():
    test_normal_resume()
    test_underrun_truncated()
    test_board_reset()
    print("ALL PASS")


if __name__ == '__main__':
    main()
