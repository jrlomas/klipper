#!/usr/bin/env python3
"""Regression test for macro-free trajectory recovery pause/resume."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                "..", "klippy"))

from extras import failure_recovery, pause_resume  # noqa: E402


class FakeGcode:
    def __init__(self):
        self.commands = {}
        self.scripts = []
        self.command_scripts = []
    def register_command(self, name, cb, desc=None):
        self.commands[name] = cb
    def run_script(self, script):
        self.scripts.append(script)
    def run_script_from_command(self, script):
        self.command_scripts.append(script)
    def respond_info(self, message):
        pass


class FakeWebhooks:
    def register_endpoint(self, name, cb):
        pass


class FakeVirtualSD:
    def __init__(self):
        self.active = True
        self.pauses = 0
        self.resumes = 0
    def is_active(self):
        return self.active
    def do_pause(self):
        self.active = False
        self.pauses += 1
    def do_resume(self):
        self.active = True
        self.resumes += 1


class FakePrinter:
    def __init__(self):
        self.gcode = FakeGcode()
        self.webhooks = FakeWebhooks()
        self.vsd = FakeVirtualSD()
        self.handlers = {}
        self.extra_objects = {}
    def lookup_object(self, name, default=None):
        return {'gcode': self.gcode, 'webhooks': self.webhooks,
                'virtual_sdcard': self.vsd,
                **self.extra_objects}.get(name, default)
    def register_event_handler(self, name, cb):
        self.handlers[name] = cb


class FakeConfig:
    def __init__(self, printer):
        self.printer = printer
    def get_printer(self):
        return self.printer
    def getfloat(self, name, default):
        return default


def main():
    printer = FakePrinter()
    pr = pause_resume.PauseResume(FakeConfig(printer))
    pr.handle_connect()
    printer.extra_objects['pause_resume'] = pr
    assert pr.pause_for_recovery()
    assert pr.is_paused and pr.recovery_pause
    assert printer.vsd.pauses == 1
    assert printer.gcode.scripts == ["SAVE_GCODE_STATE NAME=PAUSE_STATE"]
    assert pr.resume_from_recovery()
    assert not pr.is_paused and not pr.recovery_pause
    assert printer.vsd.resumes == 1
    assert printer.gcode.command_scripts == [
        "RESTORE_GCODE_STATE NAME=PAUSE_STATE MOVE=0"]
    # Link-loss recovery uses the same macro-free primitive.  A user PAUSE
    # macro may park/retract through the disconnected MCU and must never run
    # at this boundary.
    pr.is_paused = False
    pr.recovery_pause = False
    printer.vsd.active = True
    recovery = failure_recovery.FailureRecovery.__new__(
        failure_recovery.FailureRecovery)
    recovery.printer = printer
    recovery._comm_pause_event(0.)
    assert pr.is_paused and pr.recovery_pause
    assert printer.gcode.scripts == [
        "SAVE_GCODE_STATE NAME=PAUSE_STATE",
        "SAVE_GCODE_STATE NAME=PAUSE_STATE"]
    assert all(script != "PAUSE" for script in printer.gcode.scripts)
    print("PASS: recovery pause bypasses motion macros and resumes in-command")


if __name__ == '__main__':
    main()
