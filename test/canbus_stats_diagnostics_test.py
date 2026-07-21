#!/usr/bin/env python3
"""Regression test for disaggregated CAN receive diagnostics."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                "..", "klippy"))

from extras import canbus_stats  # noqa: E402


class Query:
    def __init__(self, response):
        self.response = response

    def send(self):
        return dict(self.response)


class MCU:
    def __init__(self, diagnostics_v2):
        self.diagnostics_v2 = diagnostics_v2
        self.status = {
            'rx_error': 12, 'tx_error': 1, 'tx_retries': 2,
            'canbus_bus_state': 'active'}
        self.diagnostics = {
            'rx_fifo_overruns': 11, 'rx_protocol_errors': 1,
            'rx_fifo_highwater': 3}
        if diagnostics_v2:
            self.diagnostics.update({
                'rx_fifo0_overruns': 0, 'rx_fifo1_overruns': 11,
                'rx_fifo0_highwater': 3, 'rx_fifo1_highwater': 3,
                'rx_service_max_delay_ticks': 1732})

    def try_lookup_command(self, command):
        if command == 'get_canbus_diagnostics_v2':
            return object() if self.diagnostics_v2 else None
        return object()

    def lookup_query_command(self, command, response):
        if command == 'get_canbus_status':
            return Query(self.status)
        if command == 'get_canbus_diagnostics':
            return Query(self.diagnostics)
        if command == 'get_canbus_diagnostics_v2':
            assert self.diagnostics_v2
            return Query(self.diagnostics)
        raise AssertionError(command)

    def check_valid_response(self, response):
        return False


class Reactor:
    NOW = 0.

    def register_timer(self, callback, waketime):
        return (callback, waketime)

    def monotonic(self):
        return 10.


class Printer:
    def __init__(self, diagnostics_v2):
        self.reactor = Reactor()
        self.mcu = MCU(diagnostics_v2)
        self.handlers = {}

    def get_reactor(self):
        return self.reactor

    def register_event_handler(self, name, callback):
        self.handlers[name] = callback

    def lookup_object(self, name):
        if name == 'mcu ebb36':
            return self.mcu
        raise AssertionError(name)


class Config:
    def __init__(self, diagnostics_v2):
        self.printer = Printer(diagnostics_v2)

    def get_printer(self):
        return self.printer

    def get_name(self):
        return 'canbus_stats ebb36'


def run_case(diagnostics_v2):
    config = Config(diagnostics_v2)
    stats = canbus_stats.PrinterCANBusStats(config)
    stats.handle_connect()
    assert stats.query_event(0.) == 11.
    status = stats.get_status(0.)
    expected = {
        'rx_error': 12, 'tx_error': 1, 'tx_retries': 2,
        'bus_state': 'active', 'rx_fifo_overruns': 11,
        'rx_protocol_errors': 1, 'rx_fifo_highwater': 3,
        'rx_fifo0_overruns': None, 'rx_fifo1_overruns': None,
        'rx_fifo0_highwater': None, 'rx_fifo1_highwater': None,
        'rx_service_max_delay_ticks': None}
    if diagnostics_v2:
        expected.update({
            'rx_fifo0_overruns': 0, 'rx_fifo1_overruns': 11,
            'rx_fifo0_highwater': 3, 'rx_fifo1_highwater': 3,
            'rx_service_max_delay_ticks': 1732})
    assert status == expected
    _, message = stats.stats(0.)
    assert 'rx_fifo_overruns=11 rx_protocol_errors=1' in message
    assert 'rx_fifo_highwater=3' in message
    if diagnostics_v2:
        assert 'rx_fifo0_overruns=0 rx_fifo1_overruns=11' in message
        assert 'rx_service_max_delay_ticks=1732' in message
    stats.handle_shutdown()
    assert stats.get_status(0.)['bus_state'] == 'unknown'


def main():
    run_case(False)
    run_case(True)
    print('PASS: CAN receive diagnostics preserve FIFO/protocol attribution')


if __name__ == '__main__':
    main()
