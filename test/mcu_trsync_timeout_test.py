#!/usr/bin/env python3
"""Standalone regression tests for the multi-MCU trsync timeout policy."""

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                "..", "klippy"))

for _name in ("serial", "serialhdl", "msgproto", "pins", "clocksync"):
    sys.modules.setdefault(_name, types.ModuleType(_name))

import mcu as mcu_mod  # noqa: E402


class FakeReactor:
    def completion(self):
        return object()


class FakePrinter:
    def get_reactor(self):
        return FakeReactor()


class FakeMCU:
    def __init__(self, name, timeout):
        self.name = name
        self.timeout = timeout

    def get_name(self):
        return self.name

    def get_printer(self):
        return FakePrinter()

    def get_multi_mcu_homing_timeout(self):
        return self.timeout

    def is_fileoutput(self):
        return False


class FakeTrsync:
    REASON_HOST_REQUEST = mcu_mod.MCU_trsync.REASON_HOST_REQUEST

    def __init__(self, name, timeout):
        self.mcu = FakeMCU(name, timeout)
        self.starts = []

    def get_mcu(self):
        return self.mcu

    def start(self, print_time, report_offset, completion, expire_timeout):
        self.starts.append(expire_timeout)


class FakeFFI:
    def trdispatch_start(self, dispatch, reason):
        pass


def run_start(timeouts):
    dispatch = object.__new__(mcu_mod.TriggerDispatch)
    dispatch._mcu = FakeMCU("endstop", timeouts[0])
    dispatch._trsyncs = [
        FakeTrsync("mcu%d" % (i,), timeout)
        for i, timeout in enumerate(timeouts)
    ]
    dispatch._trdispatch = object()
    old_get_ffi = mcu_mod.chelper.get_ffi
    mcu_mod.chelper.get_ffi = lambda: (object(), FakeFFI())
    try:
        dispatch.start(12.0)
    finally:
        mcu_mod.chelper.get_ffi = old_get_ffi
    return [ts.starts[0] for ts in dispatch._trsyncs]


def main():
    default = mcu_mod.TRSYNC_TIMEOUT
    assert run_start([default, default]) == [default, default]

    network = 0.100
    assert run_start([default, network]) == [network, network]

    # Same-MCU homing retains its established 250ms timeout.
    assert run_start([network]) == [mcu_mod.TRSYNC_SINGLE_MCU_TIMEOUT]
    print("PASS: multi-MCU trsync timeout remains strict by default")
    print("PASS: explicit network timeout applies to the whole trsync group")
    print("PASS: single-MCU homing timeout is unchanged")


if __name__ == "__main__":
    main()
