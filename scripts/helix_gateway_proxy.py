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

from helix_gateway import (CAN_CONFIG, CAN_CONFIG_COMMIT, CAN_CONFIG_PREPARE,
                           CAN_DELIVERY, CAN_FRAME, SERIAL_DATA, SERVICE_CAN,
                           SERVICE_SERIAL, PACKET_RESET, RECORD_ERROR,
                           CanFrame, Packet,
                           CanConfig, Delivery, Record, Runtime)
from udp_bridge import DatagramCodec


def load_secure_session():
    binding = os.path.join(ROOT, 'lib', 'intentproto', 'python')
    if binding not in sys.path:
        sys.path.insert(0, binding)
    from intentproto import SecureSession
    return SecureSession

CAN_RAW_FD_FRAMES = 5
CANFD = struct.Struct('=IBBBB64s')
CAN_CLASSIC = struct.Struct('=IB3x8s')
CANMSG_FLAG_FD = 1 << 0
CANMSG_FLAG_BRS = 1 << 1
CANMSG_FLAG_ESI = 1 << 2
CANFD_BRS = 1 << 0
CANFD_ESI = 1 << 1
PROFILES = {
    'CLASSIC_1M': (1000000, 1000000, False, False),
    'FD_1M_NOBRS': (1000000, 1000000, True, False),
    'FD_2M_BRS': (1000000, 2000000, True, True),
    'FD_5M_BRS': (1000000, 5000000, True, True),
    'FD_8M_BRS': (1000000, 8000000, True, True),
}


class GatewayProxy(asyncio.DatagramProtocol):
    def __init__(self, board, psk, interface, serial_channels, profile=None,
                 session=False, board_id=b''):
        self.board = board
        self.codec = DatagramCodec(psk)
        self.epoch = secrets.randbits(32) or 1
        self.sequence = 0
        self.cookie = 0
        self.delivery = {}
        self.profile_status = None
        self.profile_replies = {}
        self.profile_records = None
        self.profile_attempts = 0
        self.requested_profile = profile
        self.session = (load_secure_session()(True, psk, b'helix-host')
                        if session else None)
        self.board_id = board_id
        self.session_established = False
        self.io_active = False
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
        if self.session is not None:
            self.client_hello = self.session.start()
            self.handshake_attempts = 0
            self._send_client_hello()
            return
        self._activate_io()

    def _send_client_hello(self):
        if self.session_established or self.handshake_attempts >= 10:
            return
        self.handshake_attempts += 1
        self.transport.sendto(self.client_hello, self.board)
        asyncio.get_running_loop().call_later(0.5, self._send_client_hello)

    def _activate_io(self):
        if self.io_active:
            return
        self.io_active = True
        loop = asyncio.get_running_loop()
        loop.add_reader(self.can.fileno(), self._local_can)
        for fd in self.serial.values():
            loop.add_reader(fd, self._local_serial, fd)
        if self.requested_profile:
            nominal, data_rate, fd, brs = PROFILES[self.requested_profile]
            profile_epoch = secrets.randbits(32) or 1
            prepare = CanConfig(CAN_CONFIG_PREPARE, profile_epoch, nominal,
                                data_rate, fd, brs)
            commit = CanConfig(CAN_CONFIG_COMMIT, profile_epoch, nominal,
                               data_rate, fd, brs)
            self.profile_records = (
                Record(SERVICE_CAN, CAN_CONFIG, cookie=profile_epoch,
                       data=prepare.encode()),
                Record(SERVICE_CAN, CAN_CONFIG, cookie=profile_epoch,
                       data=commit.encode()))
            self._send_profile_transaction()

    def _send_profile_transaction(self):
        if self.profile_records is None or len(self.profile_replies) == 2:
            return
        if self.profile_attempts >= 10:
            raise RuntimeError('remote CAN profile transaction timed out')
        self.profile_attempts += 1
        self._send_records(self.profile_records)
        asyncio.get_running_loop().call_later(
            0.5, self._send_profile_transaction)

    def _send(self, record):
        self._send_records((record,))

    def _send_records(self, records):
        if self.session is not None and not self.session_established:
            return
        self.sequence += 1
        flags = PACKET_RESET if self.sequence == 1 else 0
        raw = Packet(self.epoch, self.sequence, tuple(records), flags).encode()
        sealed = (self.session.encode(raw) if self.session_established
                  else self.codec.encode(raw))
        self.transport.sendto(sealed, self.board)

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
        self.cookie = (self.cookie + 1) & 0xffffffff
        if not self.cookie:
            self.cookie = 1
        self._send(Record(SERVICE_CAN, CAN_FRAME, cookie=self.cookie,
                          data=frame.encode()))

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
        if record.opcode == CAN_DELIVERY:
            delivery = Delivery.decode(record.data)
            self.delivery[delivery.cookie] = delivery
            return
        if record.opcode == CAN_CONFIG:
            config = CanConfig.decode(record.data)
            failed = bool(record.flags & RECORD_ERROR)
            self.profile_status = (config, failed)
            self.profile_replies[config.action] = failed
            if failed:
                raise RuntimeError('remote CAN profile transaction failed')
            return
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
        if self.session is not None and not self.session_established:
            final = self.session.on_handshake(data)
            if final:
                self.transport.sendto(final, self.board)
                self.transport.sendto(final, self.board)
            if self.session.established:
                peer = self.session.peer_id()
                if peer != self.board_id:
                    raise RuntimeError('gateway identity mismatch: %r' % peer)
                self.session_established = True
                self._activate_io()
            return
        if self.session_established:
            try:
                payloads = (self.session.decode(data)[0],)
            except ValueError:
                return
        else:
            payloads = self.codec.decode(data)
        for payload in payloads:
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
                         parse_serial(args.serial), args.profile,
                         args.session, args.board_id.encode())
    await asyncio.get_running_loop().create_datagram_endpoint(
        lambda: proxy, local_addr=('0.0.0.0', args.listen_port))
    await asyncio.Event().wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--board', required=True, help='gateway host:port')
    parser.add_argument('--psk-file', required=True)
    parser.add_argument('--interface', default='helixcan0')
    parser.add_argument('--listen-port', type=int, default=41415)
    parser.add_argument('--profile', choices=tuple(PROFILES),
                        help='transactionally prepare and commit remote CAN')
    parser.add_argument('--session', action='store_true',
                        help='use rotating-key authenticated HostSession')
    parser.add_argument('--board-id', default='',
                        help='required authenticated gateway identity')
    parser.add_argument('--serial', action='append', default=[],
                        metavar='CHANNEL:DEVICE')
    args = parser.parse_args()
    if args.session and not args.board_id:
        parser.error('--session requires --board-id')
    asyncio.run(amain(args))


if __name__ == '__main__':
    main()
