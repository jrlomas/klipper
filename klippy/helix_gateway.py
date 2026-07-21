"""Wire codec and bounded service model for Helix network gateways.

The outer intentproto datagram supplies authentication, replay protection,
and loss accounting.  This module deliberately preserves CAN frames as
messages and serial ports as byte streams instead of pretending both are a
generic stream.
"""

import dataclasses
import struct


MAGIC = 0x4748
VERSION = 1
HEADER = struct.Struct('<HBBIIHH')
RECORD_HEADER = struct.Struct('<BBHHHI')
CAN_HEADER = struct.Struct('<IIBBH')
MAX_RECORD_DATA = 128

SERVICE_CONTROL = 0
SERVICE_CAN = 1
SERVICE_SERIAL = 2
MAX_SERVICES = 8

PACKET_RESET = 1 << 0
PACKET_ACK_ONLY = 1 << 1
RECORD_REPLY = 1 << 0
RECORD_ERROR = 1 << 1
RECORD_MORE = 1 << 2
RECORD_TIMESTAMP_VALID = 1 << 3

CONTROL_CONSOLE = 1
CONTROL_CREDIT = 2
CONTROL_STATUS = 3
CONTROL_TAKEOVER = 4
CAN_FRAME = 1
CAN_CONFIG = 2
CAN_STATUS = 3
CAN_BUS_OFF = 4
SERIAL_DATA = 1
SERIAL_CONFIG = 2
SERIAL_STATUS = 3
SERIAL_BREAK = 4


class GatewayProtocolError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class Record:
    service: int
    opcode: int
    channel: int = 0
    flags: int = 0
    cookie: int = 0
    data: bytes = b''

    def encode(self):
        data = bytes(self.data)
        if not 0 <= self.service < MAX_SERVICES:
            raise GatewayProtocolError('service is out of range')
        if len(data) > MAX_RECORD_DATA:
            raise GatewayProtocolError('record data exceeds bounded MTU')
        return RECORD_HEADER.pack(self.service, self.opcode, self.channel,
                                  self.flags, len(data), self.cookie) + data

    @classmethod
    def decode_from(cls, data, offset=0):
        if len(data) - offset < RECORD_HEADER.size:
            raise GatewayProtocolError('truncated record header')
        service, opcode, channel, flags, length, cookie = \
            RECORD_HEADER.unpack_from(data, offset)
        if service >= MAX_SERVICES or length > MAX_RECORD_DATA:
            raise GatewayProtocolError('invalid record geometry')
        end = offset + RECORD_HEADER.size + length
        if end > len(data):
            raise GatewayProtocolError('truncated record data')
        return cls(service, opcode, channel, flags, cookie,
                   bytes(data[offset + RECORD_HEADER.size:end])), end


@dataclasses.dataclass(frozen=True)
class Packet:
    epoch: int
    sequence: int
    records: tuple
    flags: int = 0

    def encode(self):
        payload = b''.join(record.encode() for record in self.records)
        return HEADER.pack(MAGIC, VERSION, self.flags, self.epoch,
                           self.sequence, len(self.records), len(payload)) \
            + payload

    @classmethod
    def decode(cls, data):
        data = bytes(data)
        if len(data) < HEADER.size:
            raise GatewayProtocolError('truncated packet header')
        magic, version, flags, epoch, sequence, count, plen = \
            HEADER.unpack_from(data)
        if magic != MAGIC or version != VERSION:
            raise GatewayProtocolError('unsupported gateway packet')
        if plen != len(data) - HEADER.size:
            raise GatewayProtocolError('packet length mismatch')
        records = []
        offset = HEADER.size
        for _ in range(count):
            record, offset = Record.decode_from(data, offset)
            records.append(record)
        if offset != len(data):
            raise GatewayProtocolError('unclaimed packet bytes')
        return cls(epoch, sequence, tuple(records), flags)


@dataclasses.dataclass(frozen=True)
class CanFrame:
    can_id: int
    data: bytes
    flags: int = 0
    hw_clock: int = 0

    def encode(self):
        data = bytes(self.data)
        if len(data) > 64:
            raise GatewayProtocolError('CAN frame exceeds CAN FD MTU')
        return CAN_HEADER.pack(self.can_id, self.hw_clock, len(data),
                               self.flags, 0) + data

    @classmethod
    def decode(cls, data):
        if len(data) < CAN_HEADER.size:
            raise GatewayProtocolError('truncated CAN frame')
        can_id, hw_clock, length, flags, reserved = CAN_HEADER.unpack_from(data)
        if reserved or length > 64 or len(data) != CAN_HEADER.size + length:
            raise GatewayProtocolError('invalid CAN frame geometry')
        return cls(can_id, bytes(data[CAN_HEADER.size:]), flags, hw_clock)


class Runtime:
    """Host/test implementation of the firmware dispatcher contract."""
    def __init__(self, credits=64):
        self.owner_epoch = None
        self.last_sequence = None
        self.services = {}
        self.credit_limit = credits
        self.credits = {}
        self.stats = {name: 0 for name in (
            'packets', 'records', 'stale_epochs', 'malformed',
            'unknown_services', 'credit_stalls', 'service_errors',
            'takeovers')}

    def register(self, service, callback, credits=None):
        if service in self.services or not 0 <= service < MAX_SERVICES:
            raise GatewayProtocolError('invalid or duplicate service')
        self.services[service] = callback
        self.credits[service] = self.credit_limit if credits is None else credits

    def set_owner(self, epoch):
        self.owner_epoch = epoch
        self.last_sequence = None
        self.stats['takeovers'] += 1

    def add_credits(self, service, count):
        self.credits[service] = min(self.credit_limit,
                                    self.credits.get(service, 0) + count)

    def dispatch(self, raw):
        try:
            packet = Packet.decode(raw)
        except GatewayProtocolError:
            self.stats['malformed'] += 1
            raise
        if self.owner_epoch is None or (packet.flags & PACKET_RESET
                and packet.epoch != self.owner_epoch):
            self.set_owner(packet.epoch)
        if packet.epoch != self.owner_epoch or (self.last_sequence is not None
                and packet.sequence <= self.last_sequence):
            self.stats['stale_epochs'] += 1
            raise GatewayProtocolError('stale epoch or replayed sequence')
        needed = {}
        for record in packet.records:
            callback = self.services.get(record.service)
            if callback is None:
                self.stats['unknown_services'] += 1
                raise GatewayProtocolError('unknown service')
            needed[record.service] = needed.get(record.service, 0) + 1
            if needed[record.service] > self.credits.get(record.service, 0):
                self.stats['credit_stalls'] += 1
                raise GatewayProtocolError('service is out of credits')
        self.last_sequence = packet.sequence
        self.stats['packets'] += 1
        for record in packet.records:
            callback = self.services[record.service]
            self.credits[record.service] -= 1
            try:
                callback(record)
            except Exception:
                self.credits[record.service] += 1
                self.stats['service_errors'] += 1
                raise
            self.stats['records'] += 1
        return packet
