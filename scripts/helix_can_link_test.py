#!/usr/bin/env python3
"""Exercise a physical CAN link between two SocketCAN interfaces."""

import argparse
import select
import socket
import struct
import time


CAN_FRAME = struct.Struct('=IB3x8s')


def open_can(interface):
    sock = socket.socket(socket.PF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    sock.bind((interface,))
    sock.setblocking(False)
    return sock


def drain(sock):
    while True:
        try:
            sock.recv(CAN_FRAME.size)
        except BlockingIOError:
            return


def transfer(source, destination, can_id, payload, count, timeout):
    frame = CAN_FRAME.pack(can_id, len(payload), payload.ljust(8, b'\0'))
    drain(destination)
    for _ in range(count):
        source.send(frame)
        time.sleep(0.005)
    received = 0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and received < count:
        ready, _, _ = select.select([destination], [], [],
                                    deadline - time.monotonic())
        if not ready:
            break
        raw = destination.recv(CAN_FRAME.size)
        got_id, length, data = CAN_FRAME.unpack(raw)
        if got_id == can_id and data[:length] == payload:
            received += 1
    return received


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('interface_a')
    parser.add_argument('interface_b')
    parser.add_argument('--count', type=int, default=8)
    parser.add_argument('--timeout', type=float, default=2.0)
    args = parser.parse_args()
    if args.count < 1:
        parser.error('--count must be positive')
    a = open_can(args.interface_a)
    b = open_can(args.interface_b)
    try:
        a_to_b = transfer(a, b, 0x321, b'A2B', args.count, args.timeout)
        b_to_a = transfer(b, a, 0x322, b'B2A', args.count, args.timeout)
    finally:
        a.close()
        b.close()
    print('%s -> %s: %d/%d' % (args.interface_a, args.interface_b,
                                a_to_b, args.count))
    print('%s -> %s: %d/%d' % (args.interface_b, args.interface_a,
                                b_to_a, args.count))
    if a_to_b != args.count or b_to_a != args.count:
        return 1
    print('PASS: bidirectional physical CAN link')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
