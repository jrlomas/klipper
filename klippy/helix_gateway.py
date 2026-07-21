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
CAN_DELIVERY = 5
CAN_CONFIG_QUERY = 0
CAN_CONFIG_PREPARE = 1
CAN_CONFIG_COMMIT = 2
CAN_CONFIG_ABORT = 3
DELIVERY_ADMITTED = 1
DELIVERY_SUBMITTED = 2
DELIVERY_COMPLETED = 3
DELIVERY_FAILED = 4
DELIVERY_UNKNOWN = 5
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
        if (len(data) > MAX_RECORD_DATA
                or self.flags & ~(RECORD_REPLY | RECORD_ERROR | RECORD_MORE
                                  | RECORD_TIMESTAMP_VALID)):
            raise GatewayProtocolError('record data exceeds bounded MTU')
        return RECORD_HEADER.pack(self.service, self.opcode, self.channel,
                                  self.flags, len(data), self.cookie) + data

    @classmethod
    def decode_from(cls, data, offset=0):
        if len(data) - offset < RECORD_HEADER.size:
            raise GatewayProtocolError('truncated record header')
        service, opcode, channel, flags, length, cookie = \
            RECORD_HEADER.unpack_from(data, offset)
        if (service >= MAX_SERVICES or length > MAX_RECORD_DATA
                or flags & ~(RECORD_REPLY | RECORD_ERROR | RECORD_MORE
                             | RECORD_TIMESTAMP_VALID)):
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
        if self.flags & ~(PACKET_RESET | PACKET_ACK_ONLY):
            raise GatewayProtocolError('invalid packet flags')
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
        if (magic != MAGIC or version != VERSION
                or flags & ~(PACKET_RESET | PACKET_ACK_ONLY)):
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
        eff = bool(self.can_id & 0x80000000)
        if (len(data) > 64 or self.flags & ~0x17
                or (self.flags & 0x06 and not self.flags & 0x01)
                or (not self.flags & 0x01 and len(data) > 8)
                or self.can_id & 0x20000000
                or (not eff and (self.can_id & 0x1fffffff) > 0x7ff)
                or (self.can_id & 0x40000000 and self.flags & 0x01)):
            raise GatewayProtocolError('CAN frame exceeds CAN FD MTU')
        return CAN_HEADER.pack(self.can_id, self.hw_clock, len(data),
                               self.flags, 0) + data

    @classmethod
    def decode(cls, data):
        if len(data) < CAN_HEADER.size:
            raise GatewayProtocolError('truncated CAN frame')
        can_id, hw_clock, length, flags, reserved = CAN_HEADER.unpack_from(data)
        eff = bool(can_id & 0x80000000)
        if (reserved or length > 64 or len(data) != CAN_HEADER.size + length
                or flags & ~0x17 or (flags & 0x06 and not flags & 0x01)
                or (not flags & 0x01 and length > 8)
                or can_id & 0x20000000
                or (not eff and (can_id & 0x1fffffff) > 0x7ff)
                or (can_id & 0x40000000 and flags & 0x01)):
            raise GatewayProtocolError('invalid CAN frame geometry')
        return cls(can_id, bytes(data[CAN_HEADER.size:]), flags, hw_clock)


CAN_CONFIG_FORMAT = struct.Struct('<BBHIII')
DELIVERY_FORMAT = struct.Struct('<BBHIII')


@dataclasses.dataclass(frozen=True)
class CanConfig:
    action: int
    epoch: int
    nominal_bitrate: int
    data_bitrate: int
    fd: bool = False
    brs: bool = False

    def encode(self):
        if self.action not in (CAN_CONFIG_QUERY, CAN_CONFIG_PREPARE,
                               CAN_CONFIG_COMMIT, CAN_CONFIG_ABORT):
            raise GatewayProtocolError('invalid CAN config action')
        if self.brs and not self.fd:
            raise GatewayProtocolError('BRS requires CAN FD')
        flags = int(self.fd) | (int(self.brs) << 1)
        return CAN_CONFIG_FORMAT.pack(self.action, flags, 0,
                                      self.epoch, self.nominal_bitrate,
                                      self.data_bitrate)

    @classmethod
    def decode(cls, data):
        if len(data) != CAN_CONFIG_FORMAT.size:
            raise GatewayProtocolError('invalid CAN config length')
        action, flags, reserved, epoch, nominal, rate = \
            CAN_CONFIG_FORMAT.unpack(data)
        if (action > CAN_CONFIG_ABORT or flags > 3 or reserved
                or (flags & 2 and not flags & 1)):
            raise GatewayProtocolError('invalid CAN config')
        return cls(action, epoch, nominal, rate, bool(flags & 1),
                   bool(flags & 2))


@dataclasses.dataclass(frozen=True)
class Delivery:
    state: int
    cookie: int
    hw_clock: int = 0
    detail: int = 0
    error: int = 0

    def encode(self):
        if not DELIVERY_ADMITTED <= self.state <= DELIVERY_UNKNOWN:
            raise GatewayProtocolError('invalid delivery state')
        return DELIVERY_FORMAT.pack(self.state, self.error, 0, self.cookie,
                                    self.hw_clock, self.detail)

    @classmethod
    def decode(cls, data):
        if len(data) != DELIVERY_FORMAT.size:
            raise GatewayProtocolError('invalid delivery length')
        state, error, reserved, cookie, clock, detail = \
            DELIVERY_FORMAT.unpack(data)
        if not DELIVERY_ADMITTED <= state <= DELIVERY_UNKNOWN or reserved:
            raise GatewayProtocolError('invalid delivery')
        return cls(state, cookie, clock, detail, error)


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
        self.credits[service] = (self.credit_limit if credits is None
                                 else credits)

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
        delta = (packet.sequence - (self.last_sequence or 0)) & 0xffffffff
        if packet.epoch != self.owner_epoch or (self.last_sequence is not None
                and (not delta or delta > 0x7fffffff)):
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
