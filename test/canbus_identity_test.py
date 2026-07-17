#!/usr/bin/env python3
"""Unit tests for canonical HELIX CAN discovery."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'klippy'))

import canbus_identity as ci


class Message:
    def __init__(self, arbitration_id, data, is_extended_id=False):
        self.arbitration_id = arbitration_id
        self.data = bytearray(data)
        self.is_extended_id = is_extended_id


class CanModule:
    Message = Message


class FakeBus:
    def __init__(self, raw_id, collide=False, assigned=False):
        self.raw_id = bytes(raw_id)
        self.collide = collide
        self.assigned = assigned
        self.queue = []
        self.closed = False

    def send(self, msg):
        if msg.data[0] == ci.CMD_QUERY_UNASSIGNED:
            if not self.assigned:
                self.queue.append(Message(ci.CANBUS_ID_ADMIN + 1,
                                          bytes([ci.RESP_NEED_NODEID])
                                          + bytes.fromhex('112233445566')
                                          + b'\x01'))
            return
        if msg.data[0] == ci.CMD_QUERY_ASSIGNED:
            if self.assigned:
                self.queue.append(Message(ci.CANBUS_ID_ADMIN + 1,
                                          bytes([ci.RESP_ASSIGNED_ID])
                                          + bytes.fromhex('112233445566')
                                          + b'\x04'))
            return
        offset = msg.data[7]
        family = 1
        crc = ci.board_id_crc(family, self.raw_id)
        fragment = self.raw_id[offset:offset + 3].ljust(3, b'\0')
        payload = bytes([ci.RESP_BOARD_ID, family, len(self.raw_id), offset]) \
            + fragment + bytes([crc])
        self.queue.append(Message(ci.CANBUS_ID_ADMIN + 1, payload))
        if self.collide and offset == 0:
            other = bytearray(payload)
            other[4] ^= 0xff
            self.queue.append(Message(ci.CANBUS_ID_ADMIN + 1, other))

    def recv(self, timeout):
        return self.queue.pop(0) if self.queue else None

    def shutdown(self):
        self.closed = True


def main():
    raw_id = bytes.fromhex('00112233445566778899aabb')
    bus = FakeBus(raw_id)
    nodes = ci.scan_bus('helixcan0', bus_factory=lambda **kw: bus,
                        can_module=CanModule, timeout=.01,
                        response_window=.001)
    assert nodes == [{'interface': 'helixcan0',
                      'board_id': 'stm32:' + raw_id.hex(),
                      'legacy_uuid': '112233445566',
                      'application': 1}]
    assert bus.closed
    assigned_bus = FakeBus(raw_id, assigned=True)
    assigned_nodes = ci.scan_bus(
        'helixcan0', bus_factory=lambda **kw: assigned_bus,
        can_module=CanModule, timeout=.01, response_window=.001)
    assert assigned_nodes == nodes
    assert ci.normalize_board_id('STM32:' + raw_id.hex().upper()) \
        == 'stm32:' + raw_id.hex()
    try:
        ci.scan_bus('helixcan0', bus_factory=lambda **kw: FakeBus(
                    raw_id, collide=True), can_module=CanModule,
                    timeout=.01, response_window=.001)
    except ci.IdentityError as exc:
        assert 'multiple board identities' in str(exc)
    else:
        raise AssertionError('legacy-handle collision was not rejected')
    print('PASS: canonical CAN board discovery and collision rejection')


if __name__ == '__main__':
    main()
