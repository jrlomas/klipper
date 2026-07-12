#!/usr/bin/env python3
# Standalone unit test for the HELIX_STATUS capability introspection
# command (FD-0001).  Stubs the printer / gcode / MCU and checks that
# firmware features are reported from the served dictionary (via
# check_valid_response) and host subsystems from loaded objects.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                "..", "klippy"))

import extras.helix_status as hs  # noqa: E402


class FakeGcode:
    def __init__(self):
        self.commands = {}

    def register_command(self, name, fn, desc=None):
        self.commands[name] = fn


class FakeMcu:
    def __init__(self, name, supported_formats, constants=None):
        self._name = name
        self._supported = set(supported_formats)
        self._constants = constants or {}

    def get_name(self):
        return self._name

    def check_valid_response(self, fmt):
        return fmt in self._supported

    def get_constants(self):
        return dict(self._constants)


class FakeGcmd:
    def __init__(self):
        self.info = None

    def respond_info(self, msg):
        self.info = msg


class FakePrinter:
    def __init__(self):
        self.gcode = FakeGcode()
        self.objects = {'gcode': self.gcode}
        self.mcus = []

    def lookup_object(self, name, default="__s__"):
        if name in self.objects:
            return self.objects[name]
        if default != "__s__":
            return default
        raise KeyError(name)

    def lookup_objects(self, module=None):
        return list(self.mcus)


class FakeConfig:
    def __init__(self, printer):
        self.printer = printer

    def get_printer(self):
        return self.printer


# Pull the real firmware format strings the module checks.
FMT = dict((label.strip(), fmt) for label, fmt in hs.MCU_FEATURES)


def test_feature_detection():
    printer = FakePrinter()
    # mainboard: full trajectory + higher order + triggers + framing v2
    main = FakeMcu('mcu', [
        FMT['trajectory motion'], FMT['cubic/quintic segments'],
        FMT['hardware trigger sources']],
        constants={'FRAMING_V2': 1, 'BOARD_SYSCALL_ABI': 0x10000,
                   'BOARD_SYSCALL_CAPS': 0xff})
    # a toolhead with only heater hold + execlog, no framing v2
    tool = FakeMcu('toolhead', [FMT['heater failsafe hold'],
                                FMT['execution log']])
    printer.mcus = [('mcu', main), ('mcu toolhead', tool)]
    printer.objects['trajectory_queuing'] = _FakeTQ(['stepper_x'])
    printer.objects['failure_recovery'] = object()

    status = hs.HelixStatus(FakeConfig(printer))
    assert 'HELIX_STATUS' in printer.gcode.commands
    gcmd = FakeGcmd()
    status.cmd_HELIX_STATUS(gcmd)
    out = gcmd.info

    # Firmware features surface per-MCU from the dictionary.
    assert "MCU 'mcu':" in out
    assert "trajectory motion" in out
    assert "cubic/quintic segments" in out
    assert "hardware trigger sources" in out
    assert "framing v2 (FEC)" in out
    assert "MCU 'toolhead':" in out
    assert "heater failsafe hold" in out
    # The toolhead must NOT claim features it doesn't advertise. Bound
    # the section to the toolhead's own MCU block (before the host list).
    tool_section = out.split("MCU 'toolhead':")[1].split("Host subsystems:")[0]
    assert "trajectory motion" not in tool_section
    assert "framing v2" not in tool_section
    # Host subsystems + trajectory joints.
    assert "failure recovery" in out
    assert "trajectory motion emitter" in out
    assert "stepper_x" in out
    print("PASS: HELIX_STATUS reports firmware features and host subsystems")


def test_stock_mcu():
    printer = FakePrinter()
    printer.mcus = [('mcu', FakeMcu('mcu', []))]
    status = hs.HelixStatus(FakeConfig(printer))
    gcmd = FakeGcmd()
    status.cmd_HELIX_STATUS(gcmd)
    assert "no HELIX firmware features detected" in gcmd.info
    print("PASS: a stock MCU reports no HELIX features (no false positives)")


class _FakeTQ:
    def __init__(self, names):
        self._names = names

    def get_trajectory_steppers(self):
        return [type('S', (), {'name': n}) for n in self._names]


def main():
    test_feature_detection()
    test_stock_mcu()
    print("ALL PASS")


if __name__ == '__main__':
    main()
