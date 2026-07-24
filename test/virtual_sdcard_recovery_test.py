#!/usr/bin/env python3
"""Regression for a recoverable trajectory gate during virtual-SD input."""

import os
import sys
import types
import io

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'klippy'))
sys.path.insert(0, os.path.join(ROOT, 'klippy', 'extras'))

import virtual_sdcard


class FakePauseResume:
    def __init__(self):
        self.paused = False
        self.calls = 0

    def pause_for_recovery(self):
        self.calls += 1
        self.paused = True
        return True

    def get_status(self, eventtime):
        return {'is_paused': self.paused}


class FakePrinter:
    def __init__(self, recovery_active):
        self.trajectory = types.SimpleNamespace(
            is_recovery_active=lambda: recovery_active)
        self.pause_resume = FakePauseResume()

    def lookup_object(self, name, default=None):
        return {
            'trajectory_queuing': self.trajectory,
            'pause_resume': self.pause_resume,
        }.get(name, default)


class FakeReactor:
    NEVER = 1.e30
    NOW = 0.

    def monotonic(self):
        return 12.5

    def unregister_timer(self, timer):
        pass

    def pause(self, waketime):
        return waketime


class FakeMutex:
    def test(self):
        return False


class FakeGCode:
    error = RuntimeError

    def __init__(self):
        self.scripts = []

    def get_mutex(self):
        return FakeMutex()

    def run_script(self, script):
        self.scripts.append(script)
        if script.startswith('G1'):
            raise self.error('trajectory gate closed')

    def respond_raw(self, message):
        pass


class FakePrintStats:
    def __init__(self):
        self.result = None

    def note_start(self):
        pass

    def note_pause(self):
        self.result = 'pause'

    def note_error(self, message):
        self.result = ('error', message)

    def note_complete(self):
        self.result = 'complete'


def make_work_handler_vsd(recovery_active):
    vsd = virtual_sdcard.VirtualSD.__new__(virtual_sdcard.VirtualSD)
    vsd.printer = FakePrinter(recovery_active)
    vsd.reactor = FakeReactor()
    vsd.gcode = FakeGCode()
    vsd.print_stats = FakePrintStats()
    vsd.on_error_gcode = types.SimpleNamespace(
        render=lambda: 'ON_ERROR')
    vsd.current_file = io.StringIO('G1 X1\n')
    vsd.file_position = 0
    vsd.next_file_position = 0
    vsd.must_pause_work = False
    vsd.cmd_from_sd = False
    vsd.work_timer = object()
    return vsd


def test_recovery_gate_becomes_pause_not_print_error():
    vsd = virtual_sdcard.VirtualSD.__new__(virtual_sdcard.VirtualSD)
    vsd.printer = FakePrinter(True)
    vsd.reactor = types.SimpleNamespace(monotonic=lambda: 12.5)
    assert vsd._pause_trajectory_recovery()
    assert vsd.printer.pause_resume.paused
    assert vsd.printer.pause_resume.calls == 1


def test_ordinary_command_error_retains_error_path():
    vsd = virtual_sdcard.VirtualSD.__new__(virtual_sdcard.VirtualSD)
    vsd.printer = FakePrinter(False)
    vsd.reactor = types.SimpleNamespace(monotonic=lambda: 12.5)
    assert not vsd._pause_trajectory_recovery()
    assert not vsd.printer.pause_resume.paused
    assert vsd.printer.pause_resume.calls == 0


def test_recovery_error_preserves_unconsumed_command():
    vsd = make_work_handler_vsd(True)
    vsd.work_handler(0.)
    assert vsd.file_position == 0
    assert vsd.next_file_position == len('G1 X1\n')
    assert vsd.print_stats.result == 'pause'
    assert vsd.gcode.scripts == ['G1 X1']
    assert vsd.printer.pause_resume.paused


def test_ordinary_error_runs_on_error_gcode():
    vsd = make_work_handler_vsd(False)
    vsd.work_handler(0.)
    assert vsd.file_position == 0
    assert vsd.print_stats.result == (
        'error', 'trajectory gate closed')
    assert vsd.gcode.scripts == ['G1 X1', 'ON_ERROR']
    assert not vsd.printer.pause_resume.paused


def main():
    test_recovery_gate_becomes_pause_not_print_error()
    print("PASS: trajectory recovery gate becomes a virtual-SD pause")
    test_ordinary_command_error_retains_error_path()
    print("PASS: ordinary virtual-SD command errors retain on_error behavior")
    test_recovery_error_preserves_unconsumed_command()
    print("PASS: recovery preserves the rejected virtual-SD command boundary")
    test_ordinary_error_runs_on_error_gcode()
    print("PASS: non-recovery command errors still run on_error_gcode")


if __name__ == '__main__':
    main()
