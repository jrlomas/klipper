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

    # The matched carrier contributes a liveness floor.  A datagram carrier
    # must not inherit an MCU-section value shorter than its ARQ backoff.
    carrier_mcu = object.__new__(mcu_mod.MCU)
    carrier_mcu._multi_mcu_homing_timeout = network
    carrier_mcu._transport_homing_timeout = default
    carrier_mcu.set_transport_homing_timeout(0.250)
    assert carrier_mcu.get_multi_mcu_homing_timeout() == 0.250

    # Renewal transmission must be based on the status observation, not the
    # future expiry.  With a 250ms watchdog, 75ms reports, and two staggered
    # MCUs, the first useful renewal advances expiry from 250ms to 362.5ms.
    # The old expire-clock tag plus serialqueue's 100ms horizon released it
    # at 262.5ms -- 12.5ms after the old watchdog had already fired.
    old_expiry = .250
    status_observation = .1125
    new_expiry = status_observation + .250
    serial_horizon = .100
    assert new_expiry - serial_horizon > old_expiry
    assert status_observation - serial_horizon <= old_expiry
    root = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..")
    with open(os.path.join(root, "klippy", "chelper", "trdispatch.c")) as f:
        trdispatch_source = f.read()
    assert ("qm->req_clock = tdm->expire_clock - tdm->expire_ticks;"
            in trdispatch_source)
    print("PASS: multi-MCU trsync timeout remains strict by default")
    print("PASS: explicit network timeout applies to the whole trsync group")
    print("PASS: single-MCU homing timeout is unchanged")
    print("PASS: carrier liveness floor dominates a shorter MCU value")
    print("PASS: renewal is eligible before the prior watchdog expires")


if __name__ == "__main__":
    main()
