"""Wire codec and bounded service model for Helix network gateways.

The outer intentproto datagram supplies authentication, replay protection,
and loss accounting.  This module deliberately preserves CAN frames as
messages and serial ports as byte streams instead of pretending both are a
generic stream.
"""

import dataclasses
import struct
import time


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
CONTROL_ACK = 5
CONTROL_TIME_SYNC = 6
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

ACK_FORMAT = struct.Struct('<III')
TIME_SYNC_FORMAT = struct.Struct('<BBHIQQQ')
TIME_SYNC_REQUEST = 0
TIME_SYNC_RESPONSE = 1


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


@dataclasses.dataclass(frozen=True)
class Ack:
    """Selective acknowledgement for one gateway owner epoch.

    ``sequence`` is the newest accepted packet and bit zero of ``mask``
    acknowledges it.  The remaining bits describe the preceding 31 packet
    sequence numbers.  ACK packets are never themselves acknowledged.
    """
    epoch: int
    sequence: int
    mask: int = 1

    def encode(self):
        if not self.mask & 1:
            raise GatewayProtocolError('ACK mask must include sequence')
        return ACK_FORMAT.pack(self.epoch, self.sequence, self.mask)

    @classmethod
    def decode(cls, data):
        if len(data) != ACK_FORMAT.size:
            raise GatewayProtocolError('invalid ACK length')
        ack = cls(*ACK_FORMAT.unpack(data))
        if not ack.mask & 1:
            raise GatewayProtocolError('invalid ACK mask')
        return ack

    def contains(self, epoch, sequence):
        if epoch != self.epoch:
            return False
        distance = (self.sequence - sequence) & 0xffffffff
        return distance < 32 and bool(self.mask & (1 << distance))


@dataclasses.dataclass(frozen=True)
class TimeExchange:
    """Authenticated four-timestamp exchange before host receipt ``t4``."""
    action: int
    epoch: int
    t1: int
    t2: int = 0
    t3: int = 0
    quality: int = 0

    def encode(self):
        if (self.action not in (TIME_SYNC_REQUEST, TIME_SYNC_RESPONSE)
                or not self.epoch or not self.t1 or not 0 <= self.quality < 256
                or (self.action == TIME_SYNC_REQUEST and (self.t2 or self.t3))
                or (self.action == TIME_SYNC_RESPONSE
                    and (not self.t2 or self.t3 < self.t2))):
            raise GatewayProtocolError('invalid time exchange')
        return TIME_SYNC_FORMAT.pack(self.action, self.quality, 0,
                                     self.epoch, self.t1, self.t2, self.t3)

    @classmethod
    def decode(cls, data):
        if len(data) != TIME_SYNC_FORMAT.size:
            raise GatewayProtocolError('invalid time exchange length')
        action, quality, reserved, epoch, t1, t2, t3 = \
            TIME_SYNC_FORMAT.unpack(data)
        if reserved:
            raise GatewayProtocolError('invalid time exchange reserved field')
        value = cls(action, epoch, t1, t2, t3, quality)
        value.encode()
        return value


class PacketWindow:
    """Bounded packet accounting with an explicit no-blind-replay policy.

    Idempotent control/status packets may be returned by ``due()`` for a new
    authenticated outer encoding.  Packets carrying CAN, serial, or console
    data become UNKNOWN on timeout instead of being replayed and possibly
    actuating twice.  Upper HELIX/Klipper ARQ remains responsible for those
    data streams.
    """
    def __init__(self, capacity=32, retry_after=.100, max_attempts=4,
                 clock=None):
        if capacity < 1 or retry_after <= 0 or max_attempts < 1:
            raise ValueError('invalid packet window geometry')
        self.capacity = capacity
        self.retry_after = retry_after
        self.max_attempts = max_attempts
        self.clock = clock or time.monotonic
        self.pending = {}
        self.stats = {name: 0 for name in (
            'tracked', 'acked', 'retransmitted', 'unknown', 'overflow')}

    def track(self, packet, raw, replay_safe=False):
        key = (packet.epoch, packet.sequence)
        if key in self.pending:
            raise GatewayProtocolError('packet already tracked')
        if len(self.pending) >= self.capacity:
            self.stats['overflow'] += 1
            raise GatewayProtocolError('packet acknowledgement window full')
        self.pending[key] = {
            'packet': packet, 'raw': bytes(raw), 'safe': bool(replay_safe),
            'attempts': 1, 'deadline': self.clock() + self.retry_after}
        self.stats['tracked'] += 1

    def acknowledge(self, ack):
        removed = []
        for key in tuple(self.pending):
            if ack.contains(*key):
                removed.append(self.pending.pop(key))
        self.stats['acked'] += len(removed)
        return removed

    def due(self, now=None):
        now = self.clock() if now is None else now
        retries, unknown = [], []
        for key, item in tuple(self.pending.items()):
            if now < item['deadline']:
                continue
            if item['safe'] and item['attempts'] < self.max_attempts:
                item['attempts'] += 1
                item['deadline'] = now + self.retry_after
                retries.append(item['raw'])
                self.stats['retransmitted'] += 1
            else:
                unknown.append(self.pending.pop(key))
                self.stats['unknown'] += 1
        return retries, unknown

    def reset(self):
        unknown = list(self.pending.values())
        self.pending.clear()
        self.stats['unknown'] += len(unknown)
        return unknown


class DeliveryLedger:
    """Cookie-correlated CAN delivery state and conservation accounting."""
    TERMINAL = frozenset((DELIVERY_COMPLETED, DELIVERY_FAILED,
                          DELIVERY_UNKNOWN))
    TRANSITIONS = {
        None: frozenset((DELIVERY_ADMITTED, DELIVERY_FAILED,
                         DELIVERY_UNKNOWN)),
        DELIVERY_ADMITTED: frozenset((DELIVERY_SUBMITTED, DELIVERY_FAILED,
                                      DELIVERY_UNKNOWN)),
        DELIVERY_SUBMITTED: TERMINAL,
    }

    def __init__(self, capacity=4096):
        self.capacity = capacity
        self.states = {}
        self.counts = {state: 0 for state in range(
            DELIVERY_ADMITTED, DELIVERY_UNKNOWN + 1)}
        self.invalid_transitions = 0

    def update(self, delivery):
        old = self.states.get(delivery.cookie)
        if old in self.TERMINAL:
            if old == delivery.state:
                return False
            self.invalid_transitions += 1
            raise GatewayProtocolError('terminal delivery state changed')
        if delivery.state not in self.TRANSITIONS.get(old, ()):
            self.invalid_transitions += 1
            raise GatewayProtocolError('invalid delivery transition')
        if old is None and len(self.states) >= self.capacity:
            raise GatewayProtocolError('delivery ledger full')
        self.states[delivery.cookie] = delivery.state
        self.counts[delivery.state] += 1
        return True

    def mark_nonterminal_unknown(self):
        changed = []
        for cookie, state in tuple(self.states.items()):
            if state not in self.TERMINAL:
                self.update(Delivery(DELIVERY_UNKNOWN, cookie))
                changed.append(cookie)
        return changed

    def snapshot(self):
        current = {state: 0 for state in range(
            DELIVERY_ADMITTED, DELIVERY_UNKNOWN + 1)}
        for state in self.states.values():
            current[state] += 1
        admitted = len(self.states)
        terminal = sum(current[state] for state in self.TERMINAL)
        inflight = admitted - terminal
        return {'schema_version': 1, 'admitted': admitted,
                'inflight': inflight, 'terminal': terminal,
                'completed': current[DELIVERY_COMPLETED],
                'failed': current[DELIVERY_FAILED],
                'unknown': current[DELIVERY_UNKNOWN],
                'residual': admitted - terminal - inflight,
                'invalid_transitions': self.invalid_transitions}


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
            'takeovers', 'duplicates')}
        self.received_mask = 0

    def register(self, service, callback, credits=None):
        if service in self.services or not 0 <= service < MAX_SERVICES:
            raise GatewayProtocolError('invalid or duplicate service')
        self.services[service] = callback
        self.credits[service] = (self.credit_limit if credits is None
                                 else credits)

    def set_owner(self, epoch):
        self.owner_epoch = epoch
        self.last_sequence = None
        self.received_mask = 0
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
        if (packet.epoch == self.owner_epoch and self.last_sequence is not None
                and not delta):
            self.stats['duplicates'] += 1
            return packet
        if packet.epoch != self.owner_epoch or (self.last_sequence is not None
                and delta > 0x7fffffff):
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
        if self.last_sequence is None or delta >= 32:
            self.received_mask = 1
        else:
            self.received_mask = ((self.received_mask << delta) | 1) \
                & 0xffffffff
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

    def acknowledgement(self):
        if self.owner_epoch is None or self.last_sequence is None:
            return None
        return Ack(self.owner_epoch, self.last_sequence,
                   self.received_mask or 1)
