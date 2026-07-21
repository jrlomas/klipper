#!/usr/bin/env python3
"""Virtual UDP/vcan qualification lab for the unified Helix gateway."""

import argparse
import os
import socket
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'klippy'))
sys.path.insert(0, os.path.join(ROOT, 'lib', 'intentproto', 'tools'))

from helix_gateway import (Packet, Record, Runtime, PACKET_RESET,
                           SERVICE_SERIAL, SERIAL_DATA)
from udp_bridge import DatagramCodec


CAN_RAW_RECV_OWN_MSGS = 4
CAN_CLASSIC = struct.Struct('=IB3x8s')


def run_udp_faults(count=1000):
    key = b'helix-virtual-gateway-lab'
    sender = DatagramCodec(key)
    receiver = DatagramCodec(key)
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(('127.0.0.1', 0))
    server.setblocking(False)
    runtime = Runtime(credits=count + 1)
    seen = []
    runtime.register(SERVICE_SERIAL, lambda record: seen.append(record.cookie),
                     credits=count + 1)
    def drain():
        while True:
            try:
                received = server.recv(4096)
            except BlockingIOError:
                return
            for payload in receiver.decode(received):
                if not payload:
                    continue
                try:
                    runtime.dispatch(payload)
                except ValueError:
                    pass
    delayed = None
    sent = dropped = corrupted = duplicated = 0
    for sequence in range(1, count + 1):
        packet = Packet(77, sequence, (
            Record(SERVICE_SERIAL, SERIAL_DATA, cookie=sequence,
                   data=b'lab'),), PACKET_RESET if sequence == 1 else 0)
        wire = sender.encode(packet.encode())
        is_corrupt = sequence % 17 == 0
        if is_corrupt:
            wire = bytearray(wire)
            wire[-1] ^= 1
            wire = bytes(wire)
        if sequence % 13 == 0 and delayed is None:
            delayed = (wire, is_corrupt)
            continue
        if sequence % 7 == 0:
            dropped += 1
            continue
        client.sendto(wire, server.getsockname())
        sent += 1
        corrupted += int(is_corrupt)
        if sequence % 11 == 0:
            client.sendto(wire, server.getsockname())
            duplicated += 1
            corrupted += int(is_corrupt)
        if delayed is not None:
            delayed_wire, delayed_corrupt = delayed
            client.sendto(delayed_wire, server.getsockname())
            sent += 1
            corrupted += int(delayed_corrupt)
            delayed = None
        drain()
    drain()
    server.close()
    client.close()
    assert len(seen) == len(set(seen)), 'duplicate packet re-actuated data'
    assert receiver.auth_failures == corrupted, (
        receiver.auth_failures, corrupted)
    return {'schema_version': 1, 'input': count, 'sent': sent,
            'accepted': len(seen), 'dropped': dropped,
            'corrupted': corrupted, 'duplicated': duplicated,
            'auth_failures': receiver.auth_failures,
            'transport_lost': receiver.rx_lost,
            'transport_reordered': receiver.rx_reordered,
            'runtime': dict(runtime.stats)}


def run_vcan(interface):
    can = socket.socket(socket.PF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    can.setsockopt(socket.SOL_CAN_RAW, CAN_RAW_RECV_OWN_MSGS, 1)
    can.settimeout(1.)
    can.bind((interface,))
    frame = CAN_CLASSIC.pack(0x321, 8, b'HELIXLAB')
    can.send(frame)
    received = can.recv(CAN_CLASSIC.size)
    can.close()
    assert received == frame
    return {'schema_version': 1, 'interface': interface,
            'frames_sent': 1, 'frames_received': 1}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interface', default='helixvcan0')
    parser.add_argument('--require-vcan', action='store_true')
    parser.add_argument('--count', type=int, default=1000)
    args = parser.parse_args()
    print('UDP', run_udp_faults(args.count))
    try:
        print('VCAN', run_vcan(args.interface))
    except OSError as exc:
        if args.require_vcan:
            raise
        print('VCAN unavailable:', exc)


if __name__ == '__main__':
    main()
