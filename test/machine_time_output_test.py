#!/usr/bin/env python3
# Host acceptance tests for cross-MCU machine-time digital outputs.

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                "../klippy"))

import mcu as mcu_module  # noqa: E402
from extras import multi_pin, output_pin  # noqa: E402


class FakeCommand:
    def __init__(self):
        self.sent = []

    def send(self, args, **kwargs):
        self.sent.append((list(args), kwargs))


class FakeQueryCommand(FakeCommand):
    def __init__(self, response):
        super().__init__()
        self.response = response

    def send(self, args, **kwargs):
        super().send(args, **kwargs)
        return dict(self.response)


class FakeMCU:
    def __init__(self, name, frequency):
        self.name = name
        self.frequency = frequency

    def get_name(self):
        return self.name

    def print_time_to_clock(self, print_time):
        return int(round(print_time * self.frequency))

    def seconds_to_clock(self, seconds):
        return int(round(seconds * self.frequency))


class FakeTimesync:
    def __init__(self, converged=True):
        self.converged = converged

    def is_mcu_synced(self, _name):
        return self.converged


class FakePrinter:
    @staticmethod
    def command_error(message):
        return RuntimeError(message)


def make_digital(mcu, oid):
    pin = mcu_module.MCU_digital_out.__new__(mcu_module.MCU_digital_out)
    pin._mcu = mcu
    pin._oid = oid
    pin._invert = False
    pin._last_clock = 0
    pin._machine_set_cmd = FakeCommand()
    pin._timing_query_cmd = None
    return pin


def test_one_machine_timestamp_fans_out_with_local_transport_deadlines():
    primary = FakeMCU('mcu', 12000000)
    secondary = FakeMCU('ebb36', 64000000)
    pico_pin = make_digital(primary, 4)
    ebb_pin = make_digital(secondary, 9)
    fanout = multi_pin.PrinterMultiPin.__new__(multi_pin.PrinterMultiPin)
    fanout.mcu_pins = [pico_pin, ebb_pin]

    out = output_pin.PrinterOutputPin.__new__(output_pin.PrinterOutputPin)
    out.last_value = 0.
    out.is_pwm = False
    out.machine_time = True
    out.machine_mcu = primary
    out.mcu_pin = fanout
    out.target_mcus = [primary, secondary]
    out.timesync = FakeTimesync()
    out.printer = FakePrinter()

    out._set_pin(2.5, 1.)
    machine_clock = 30000000
    assert pico_pin._machine_set_cmd.sent == [
        ([4, machine_clock, True],
         {'minclock': 0, 'reqclock': 30000000})]
    assert ebb_pin._machine_set_cmd.sent == [
        ([9, machine_clock, True],
         {'minclock': 0, 'reqclock': 160000000})]
    print("PASS: one machine timestamp fans out while each link retains its"
          " local transmission deadline")


def test_unconverged_secondary_fails_before_send():
    primary = FakeMCU('mcu', 12000000)
    secondary = FakeMCU('ebb36', 64000000)
    pin = make_digital(secondary, 9)
    out = output_pin.PrinterOutputPin.__new__(output_pin.PrinterOutputPin)
    out.last_value = 0.
    out.is_pwm = False
    out.machine_time = True
    out.machine_mcu = primary
    out.mcu_pin = pin
    out.target_mcus = [secondary]
    out.timesync = FakeTimesync(False)
    out.printer = FakePrinter()
    try:
        out._set_pin(2.5, 1.)
    except RuntimeError as exc:
        assert "not converged" in str(exc)
    else:
        raise AssertionError("unconverged synchronized output was sent")
    assert not pin._machine_set_cmd.sent
    assert out.last_value == 0.
    print("PASS: an unconverged target fails closed before output is sent")


def test_legacy_comparator_uses_original_print_time_fanout():
    class LegacyFanout:
        def __init__(self):
            self.calls = []
        def set_digital(self, print_time, value):
            self.calls.append((print_time, value))
        def set_digital_machine_time(self, *args):
            raise AssertionError("legacy comparator used machine time")

    fanout = LegacyFanout()
    out = output_pin.PrinterOutputPin.__new__(output_pin.PrinterOutputPin)
    out.last_value = 0.
    out.mcu_pin = fanout
    out._set_pin_legacy_timing(2.5, 1.)
    assert fanout.calls == [(2.5, 1.)]
    assert out.last_value == 1.
    print("PASS: legacy comparator uses original per-MCU print-time fanout")


def test_fanout_reports_each_mcu_isr_lateness():
    primary = FakeMCU('mcu', 12000000)
    secondary = FakeMCU('ebb36', 64000000)
    pico_pin = make_digital(primary, 4)
    ebb_pin = make_digital(secondary, 9)
    pico_pin._timing_query_cmd = FakeQueryCommand({
        'value': 1, 'dropped': 0, 'scheduled': 1000,
        'actual': 1003, 'late': 3})
    ebb_pin._timing_query_cmd = FakeQueryCommand({
        'value': 1, 'dropped': 0, 'scheduled': 5000,
        'actual': 5016, 'late': 16})
    fanout = multi_pin.PrinterMultiPin.__new__(multi_pin.PrinterMultiPin)
    fanout.mcu_pins = [pico_pin, ebb_pin]

    states = fanout.query_digital_timing()
    assert [(mcu.get_name(), state['late']) for mcu, state in states] == [
        ('mcu', 3), ('ebb36', 16)]
    assert pico_pin._timing_query_cmd.sent == [([4], {})]
    assert ebb_pin._timing_query_cmd.sent == [([9], {})]
    print("PASS: fanout exposes each MCU's post-edge ISR latency")


def main():
    test_one_machine_timestamp_fans_out_with_local_transport_deadlines()
    test_unconverged_secondary_fails_before_send()
    test_legacy_comparator_uses_original_print_time_fanout()
    test_fanout_reports_each_mcu_isr_lateness()
    print("ALL PASS")


if __name__ == '__main__':
    main()
