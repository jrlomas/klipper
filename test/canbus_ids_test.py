#!/usr/bin/env python3
"""Verify interface-scoped CAN allocation and canonical identity lookup."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'klippy'))

from extras import canbus_ids


class ConfigError(Exception):
    pass


class Printer:
    config_error = ConfigError


class Config:
    error = ConfigError

    def __init__(self):
        self.printer = Printer()

    def get_printer(self):
        return self.printer


def main():
    config = Config()
    ids = canbus_ids.PrinterCANBus(config)
    board_a = 'stm32:' + '11' * 12
    board_b = 'stm32:' + '22' * 12
    assert ids.add_board_id(config, board_a, 'helixcan0') == board_a
    assert ids.add_board_id(config, board_b, 'helixcan0') == board_b
    ids.add_uuid(config, 'abcdef123456', 'can1')
    assert ids.get_nodeid(board_a, 'helixcan0') == 4
    assert ids.get_nodeid(board_b, 'helixcan0') == 5
    assert ids.get_nodeid('abcdef123456', 'can1') == 4
    try:
        ids.add_board_id(config, board_a, 'helixcan0')
    except ConfigError as exc:
        assert 'Duplicate CAN identity' in str(exc)
    else:
        raise AssertionError('same-bus duplicate was accepted')

    scans = []
    original_scan = canbus_ids.canbus_identity.scan_bus
    canbus_ids.canbus_identity.scan_bus = lambda interface: scans.append(
        interface) or [
            {'board_id': board_a, 'legacy_uuid': '111111111111'},
            {'board_id': board_b, 'legacy_uuid': '222222222222'}]
    try:
        assert ids.resolve_legacy_handle(board_a, 'helixcan0') \
            == '111111111111'
        assert ids.resolve_legacy_handle(board_b, 'helixcan0') \
            == '222222222222'
        assert scans == ['helixcan0']
    finally:
        canbus_ids.canbus_identity.scan_bus = original_scan
    print('PASS: CAN identities and node IDs are scoped by named bus')


if __name__ == '__main__':
    main()
