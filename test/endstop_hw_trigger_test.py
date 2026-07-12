#!/usr/bin/env python3
# Standalone unit test for interrupt-driven homing detection
# (RFC 0001 doc 09).  Drives klippy's MCU_endstop through home_start /
# home_wait with a stubbed MCU and trigger dispatch, asserting that:
#
#   1. when the firmware advertises the trigger_source command set, a
#      triggered=True homing move arms the hardware edge interrupt
#      (trigger_source_arm) instead of the polled endstop_home, and
#      reads the latched edge tick back from trigger_source_query with
#      no rest_ticks back-dating;
#   2. a triggered=False move (waiting for release) still uses the
#      polled endstop_home path, whose edge sense is per-move;
#   3. when the firmware lacks the commands, detection falls back to the
#      polled path even with hardware homing enabled.
#
# No printer, MCU, or chelper build is required.  Exits 0 on success.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                "..", "klippy"))

# mcu.py pulls in the serial/transport stack at import time; stub the
# modules this unit test does not exercise so it runs without pyserial or
# a live connection.  chelper is left real (it is not used here because
# TriggerDispatch is patched out below).
for _name in ("serial", "serialhdl", "msgproto", "pins", "clocksync"):
    sys.modules.setdefault(_name, types.ModuleType(_name))

import mcu as mcu_mod  # noqa: E402


# ---- Stubs -----------------------------------------------------------

class FakeCmd:
    def __init__(self, fmt, log):
        self.fmt = fmt
        self.log = log

    def send(self, data, reqclock=0, minclock=0):
        self.log.append((self.fmt.split()[0], list(data), reqclock))


class FakeQueryCmd:
    def __init__(self, fmt, resp, log, result):
        self.fmt = fmt
        self.log = log
        self.result = result

    def send(self, data, reqclock=0, minclock=0):
        self.log.append((self.fmt.split()[0], list(data), reqclock))
        return dict(self.result)


class FakeDispatch:
    def __init__(self, mcu):
        self._oid = 90
        self.stop_reason = mcu_mod.MCU_trsync.REASON_ENDSTOP_HIT
        self.started = False

    def get_oid(self):
        return self._oid

    def get_command_queue(self):
        return "cq"

    def add_stepper(self, stepper):
        pass

    def get_steppers(self):
        return []

    def start(self, print_time):
        self.started = True
        return "completion"

    def wait_end(self, t):
        pass

    def stop(self):
        return self.stop_reason


class FakePrinter:
    command_error = RuntimeError

    def get_printer(self):
        return self


class FakeMCU:
    def __init__(self, has_trigger=True, want_hw=True):
        self._has_trigger = has_trigger
        self._want_hw = want_hw
        self._oid = 0
        self.config_cmds = []
        self.sends = []
        # trigger_source_query returns the hardware-latched edge tick.
        self.trigger_clock = 4242
        self.endstop_next_clock = 9000

    # -- config plumbing --
    def create_oid(self):
        self._oid += 1
        return self._oid

    def register_config_callback(self, cb):
        pass

    def add_config_cmd(self, cmd, is_init=False, on_restart=False):
        self.config_cmds.append(cmd)

    def want_hw_endstop_trigger(self):
        return self._want_hw

    def check_valid_response(self, fmt):
        if fmt.startswith("config_trigger_gpio"):
            return self._has_trigger
        return True

    # -- command lookup --
    def lookup_command(self, fmt, cq=None):
        return FakeCmd(fmt, self.sends)

    def lookup_query_command(self, fmt, resp, oid=None, cq=None,
                             is_async=False):
        if fmt.startswith("trigger_source_query"):
            result = {'oid': oid, 'flags': 0x02, 'clock': self.trigger_clock}
        else:
            result = {'oid': oid, 'homing': 0,
                      'next_clock': self.endstop_next_clock, 'pin_value': 0}
        return FakeQueryCmd(fmt, resp, self.sends, result)

    # -- clock/time helpers --
    def print_time_to_clock(self, pt):
        return int(pt * 1000)

    def seconds_to_clock(self, t):
        return int(t * 1000)

    def clock32_to_clock64(self, c):
        return c

    def clock_to_print_time(self, clock):
        return clock / 1000.

    def is_fileoutput(self):
        return False

    def get_printer(self):
        return FakePrinter()


PIN = {'pin': 'PA1', 'pullup': 1, 'invert': 0}


def make_endstop(has_trigger=True, want_hw=True):
    mcu = FakeMCU(has_trigger=has_trigger, want_hw=want_hw)
    # Patch out the real TriggerDispatch (chelper trdispatch + trsync) so
    # the test exercises only the endstop's detection-path selection.
    orig = mcu_mod.TriggerDispatch
    mcu_mod.TriggerDispatch = FakeDispatch
    try:
        e = mcu_mod.MCU_endstop(mcu, dict(PIN))
    finally:
        mcu_mod.TriggerDispatch = orig
    e._build_config()
    return mcu, e


def sent_names(mcu):
    return [name for name, _, _ in mcu.sends]


# ---- Scenarios -------------------------------------------------------

def test_hw_trigger_used():
    mcu, e = make_endstop(has_trigger=True, want_hw=True)
    # Both a polled config_endstop and a hardware config_trigger_gpio are
    # configured on the same pin.
    assert any(c.startswith("config_endstop") for c in mcu.config_cmds)
    assert any(c.startswith("config_trigger_gpio") for c in mcu.config_cmds)
    comp = e.home_start(1.0, 0.001, 4, 0.01, triggered=True)
    assert comp == "completion"
    # Armed the hardware edge interrupt, not the polled endstop_home.
    assert "trigger_source_arm" in sent_names(mcu)
    assert "endstop_home" not in sent_names(mcu)
    res = e.home_wait(2.0)
    # Disarmed the hardware source and read the latched edge tick back;
    # the returned trigger time is the exact tick, un-back-dated.
    assert "trigger_source_disarm" in sent_names(mcu)
    assert "trigger_source_query" in sent_names(mcu)
    assert res == mcu.trigger_clock / 1000., res
    print("PASS: firmware with trigger_source -> hardware edge interrupt")


def test_release_move_uses_polled():
    mcu, e = make_endstop(has_trigger=True, want_hw=True)
    # A move that waits for the pin to RELEASE (triggered=False) cannot use
    # the fixed-edge hardware source; it must use the polled path.
    e.home_start(1.0, 0.001, 4, 0.01, triggered=False)
    assert "endstop_home" in sent_names(mcu)
    assert "trigger_source_arm" not in sent_names(mcu)
    res = e.home_wait(2.0)
    # Polled path back-dates next_clock by rest_ticks (=0.01*1000).
    expected = (mcu.endstop_next_clock - 10) / 1000.
    assert res == expected, res
    print("PASS: triggered=False release move -> polled endstop_home")


def test_fallback_when_absent():
    mcu, e = make_endstop(has_trigger=False, want_hw=True)
    assert not any(c.startswith("config_trigger_gpio")
                   for c in mcu.config_cmds)
    e.home_start(1.0, 0.001, 4, 0.01, triggered=True)
    assert "endstop_home" in sent_names(mcu)
    assert "trigger_source_arm" not in sent_names(mcu)
    print("PASS: firmware without trigger_source -> polled fallback")


def test_disabled_opt_out():
    mcu, e = make_endstop(has_trigger=True, want_hw=False)
    assert not any(c.startswith("config_trigger_gpio")
                   for c in mcu.config_cmds)
    e.home_start(1.0, 0.001, 4, 0.01, triggered=True)
    assert "endstop_home" in sent_names(mcu)
    print("PASS: hardware_endstop_trigger=False -> polled path forced")


def main():
    test_hw_trigger_used()
    test_release_move_uses_polled()
    test_fallback_when_absent()
    test_disabled_opt_out()
    print("ALL PASS")


if __name__ == '__main__':
    main()
