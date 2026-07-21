#!/usr/bin/env python3
"""Authenticated UDP <-> SocketCAN/serial proxy for a Helix gateway.

This is the deployable host half of the common gateway protocol.  It lets the
same Klipper SocketCAN path used by the qualified USB bridge run against an
Ethernet gateway later, while preserving typed CAN-FD frames and serial byte
streams.  Static PSK datagrams are supported first; the on-wire payload is
also suitable for the existing rotating-key DatagramCarrier session.
"""

import argparse
import asyncio
import os
import secrets
import socket
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'klippy'))
sys.path.insert(0, os.path.join(ROOT, 'lib', 'intentproto', 'tools'))

from helix_gateway import (CAN_FRAME, SERIAL_DATA, SERVICE_CAN,
                           SERVICE_SERIAL, PACKET_RESET, CanFrame, Packet,
                           Record, Runtime)
from udp_bridge import DatagramCodec

CAN_RAW_FD_FRAMES = 5
CANFD = struct.Struct('=IBBBB64s')
CAN_CLASSIC = struct.Struct('=IB3x8s')
CANMSG_FLAG_FD = 1 << 0
CANMSG_FLAG_BRS = 1 << 1
CANMSG_FLAG_ESI = 1 << 2
CANFD_BRS = 1 << 0
CANFD_ESI = 1 << 1


class GatewayProxy(asyncio.DatagramProtocol):
    def __init__(self, board, psk, interface, serial_channels):
        self.board = board
        self.codec = DatagramCodec(psk)
        self.epoch = secrets.randbits(32) or 1
        self.sequence = 0
        self.transport = None
        self.can = socket.socket(socket.PF_CAN, socket.SOCK_RAW,
                                 socket.CAN_RAW)
        self.can.setsockopt(socket.SOL_CAN_RAW, CAN_RAW_FD_FRAMES, 1)
        self.can.setblocking(False)
        self.can.bind((interface,))
        self.serial = serial_channels
        self.runtime = Runtime()
        self.runtime.register(SERVICE_CAN, self._remote_can)
        self.runtime.register(SERVICE_SERIAL, self._remote_serial)

    def connection_made(self, transport):
        self.transport = transport
        loop = asyncio.get_running_loop()
        loop.add_reader(self.can.fileno(), self._local_can)
        for fd in self.serial.values():
            loop.add_reader(fd, self._local_serial, fd)

    def _send(self, record):
        self.sequence += 1
        flags = PACKET_RESET if self.sequence == 1 else 0
        raw = Packet(self.epoch, self.sequence, (record,), flags).encode()
        self.transport.sendto(self.codec.encode(raw), self.board)

    def _local_can(self):
        raw = self.can.recv(CANFD.size)
        if len(raw) == CAN_CLASSIC.size:
            can_id, length, data = CAN_CLASSIC.unpack(raw)
            helix_flags = 0
        else:
            can_id, length, flags, _r0, _r1, data = CANFD.unpack(raw)
            helix_flags = CANMSG_FLAG_FD
            if flags & CANFD_BRS:
                helix_flags |= CANMSG_FLAG_BRS
            if flags & CANFD_ESI:
                helix_flags |= CANMSG_FLAG_ESI
        frame = CanFrame(can_id, data[:length], helix_flags)
        self._send(Record(SERVICE_CAN, CAN_FRAME, data=frame.encode()))

    def _local_serial(self, fd):
        data = os.read(fd, 128)
        if not data:
            return
        channel = next(key for key, value in self.serial.items()
                       if value == fd)
        self._send(Record(SERVICE_SERIAL, SERIAL_DATA, channel=channel,
                          data=data))

    def _remote_can(self, record):
        self.runtime.add_credits(SERVICE_CAN, 1)
        if record.opcode != CAN_FRAME:
            return
        frame = CanFrame.decode(record.data)
        if frame.flags & CANMSG_FLAG_FD:
            flags = (CANFD_BRS if frame.flags & CANMSG_FLAG_BRS else 0)
            flags |= (CANFD_ESI if frame.flags & CANMSG_FLAG_ESI else 0)
            raw = CANFD.pack(frame.can_id, len(frame.data), flags, 0, 0,
                             frame.data.ljust(64, b'\0'))
        else:
            raw = CAN_CLASSIC.pack(frame.can_id, len(frame.data),
                                   frame.data.ljust(8, b'\0'))
        self.can.send(raw)

    def _remote_serial(self, record):
        self.runtime.add_credits(SERVICE_SERIAL, 1)
        if record.opcode == SERIAL_DATA and record.channel in self.serial:
            os.write(self.serial[record.channel], record.data)

    def datagram_received(self, data, addr):
        if addr != self.board:
            return
        for payload in self.codec.decode(data):
            if payload:
                self.runtime.dispatch(payload)


def parse_serial(values):
    result = {}
    for value in values:
        channel, path = value.split(':', 1)
        result[int(channel)] = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    return result


async def amain(args):
    host, port = args.board.rsplit(':', 1)
    with open(args.psk_file, 'rb') as f:
        psk = f.read().strip()
    if not psk:
        raise SystemExit('empty PSK file')
    proxy = GatewayProxy((host, int(port)), psk, args.interface,
                         parse_serial(args.serial))
    await asyncio.get_running_loop().create_datagram_endpoint(
        lambda: proxy, local_addr=('0.0.0.0', args.listen_port))
    await asyncio.Event().wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--board', required=True, help='gateway host:port')
    parser.add_argument('--psk-file', required=True)
    parser.add_argument('--interface', default='helixcan0')
    parser.add_argument('--listen-port', type=int, default=41415)
    parser.add_argument('--serial', action='append', default=[],
                        metavar='CHANNEL:DEVICE')
    asyncio.run(amain(parser.parse_args()))


if __name__ == '__main__':
    main()
