"""HELIX Classical-CAN discovery and canonical board identities."""

import time


_MONOTONIC = getattr(time, 'monotonic', time.time)


CANBUS_ID_ADMIN = 0x3f0
CMD_QUERY_UNASSIGNED = 0x00
CMD_QUERY_BOARD_ID = 0x03
CMD_QUERY_ASSIGNED = 0x04
RESP_NEED_NODEID = 0x20
RESP_BOARD_ID = 0x21
RESP_ASSIGNED_ID = 0x22

FAMILY_NAMES = {
    0: 'generic',
    1: 'stm32',
    2: 'rp2040',
    3: 'atsam',
    4: 'atsamd',
    5: 'lpc176x',
}


class IdentityError(Exception):
    pass


def drain_session_tail(bus, max_time=.150, quiet_time=.025,
                       monotonic=_MONOTONIC):
    """Discard old node frames after a session-reset acknowledgement.

    A response block may already be split across FDCAN hardware buffers when
    the out-of-band reset is processed.  The acknowledgement proves firmware
    state was reset, but the last pieces of that old block can still follow it
    through SocketCAN.  Wait for a bounded quiet interval before attaching the
    new framed parser so it never starts in the middle of the old block.
    """
    deadline = monotonic() + max_time
    quiet_deadline = min(deadline, monotonic() + quiet_time)
    frames = byte_count = 0
    while True:
        now = monotonic()
        timeout = min(deadline - now, quiet_deadline - now)
        if timeout <= 0.:
            break
        msg = bus.recv(timeout)
        if msg is None:
            break
        frames += 1
        byte_count += len(msg.data)
        quiet_deadline = min(deadline, monotonic() + quiet_time)
    return frames, byte_count


def board_id_crc(family, raw_id):
    crc = 0x5a ^ family ^ len(raw_id)
    for value in raw_id:
        crc ^= value
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xff if crc & 0x80 \
                else (crc << 1) & 0xff
    return crc


def format_board_id(family, raw_id):
    family_name = FAMILY_NAMES.get(family, 'family-%d' % (family,))
    return '%s:%s' % (family_name, bytes(raw_id).hex())


def normalize_board_id(board_id):
    try:
        family, raw_hex = board_id.strip().lower().split(':', 1)
        raw = bytes.fromhex(raw_hex)
    except (AttributeError, TypeError, ValueError):
        raise IdentityError("Invalid board_id '%s'" % (board_id,))
    if family not in FAMILY_NAMES.values() or not raw or len(raw) > 16:
        raise IdentityError("Invalid board_id '%s'" % (board_id,))
    return '%s:%s' % (family, raw.hex())


def _recv_until(bus, deadline):
    while True:
        remaining = deadline - _MONOTONIC()
        if remaining <= 0.:
            return
        msg = bus.recv(remaining)
        if msg is None:
            return
        yield msg


def _query_identity(bus, can_module, legacy_uuid, response_window=.12):
    uuid_bytes = bytes.fromhex(legacy_uuid)
    raw = bytearray()
    family = total_len = expected_crc = None
    offset = 0
    while total_len is None or offset < total_len:
        request = can_module.Message(
            arbitration_id=CANBUS_ID_ADMIN,
            data=bytes([CMD_QUERY_BOARD_ID]) + uuid_bytes + bytes([offset]),
            is_extended_id=False)
        bus.send(request)
        replies = set()
        deadline = _MONOTONIC() + response_window
        for msg in _recv_until(bus, deadline):
            data = bytes(msg.data)
            if (msg.arbitration_id == CANBUS_ID_ADMIN + 1
                    and len(data) == 8 and data[0] == RESP_BOARD_ID
                    and data[3] == offset):
                replies.add(data)
        if not replies:
            return None
        if len(replies) != 1:
            raise IdentityError(
                "legacy CAN handle %s maps to multiple board identities"
                % (legacy_uuid,))
        data = replies.pop()
        if total_len is None:
            family, total_len, expected_crc = data[1], data[2], data[7]
            if not total_len or total_len > 16:
                raise IdentityError("invalid board identity length")
        elif (family, total_len, expected_crc) != (data[1], data[2], data[7]):
            raise IdentityError("board identity changed during discovery")
        count = min(3, total_len - offset)
        raw.extend(data[4:4 + count])
        offset += count
    if board_id_crc(family, raw) != expected_crc:
        raise IdentityError("board identity CRC mismatch")
    return format_board_id(family, raw)


def scan_bus(interface, timeout=1.0, response_window=.12, bus_factory=None,
             can_module=None):
    if can_module is None:
        import can as can_module
    if bus_factory is None:
        bus_factory = can_module.interface.Bus
    filters = [{"can_id": CANBUS_ID_ADMIN + 1, "can_mask": 0x7ff,
                "extended": False}]
    bus = bus_factory(channel=interface, can_filters=filters,
                      bustype='socketcan')
    try:
        handles = {}
        for query_code, response_code in (
                (CMD_QUERY_UNASSIGNED, RESP_NEED_NODEID),
                (CMD_QUERY_ASSIGNED, RESP_ASSIGNED_ID)):
            query = can_module.Message(arbitration_id=CANBUS_ID_ADMIN,
                                       data=[query_code],
                                       is_extended_id=False)
            bus.send(query)
            deadline = _MONOTONIC() + timeout
            for msg in _recv_until(bus, deadline):
                data = bytes(msg.data)
                if (msg.arbitration_id != CANBUS_ID_ADMIN + 1
                        or len(data) < 7 or data[0] != response_code):
                    continue
                legacy_uuid = data[1:7].hex()
                # Assigned reports carry the current node id in byte seven;
                # this implementation is always the Klipper application.
                app = (data[7] if response_code == RESP_NEED_NODEID
                       and len(data) > 7 else 0x01)
                handles[legacy_uuid] = app
        nodes = []
        for legacy_uuid, app in sorted(handles.items()):
            board_id = _query_identity(bus, can_module, legacy_uuid,
                                       response_window)
            nodes.append({'interface': interface, 'board_id': board_id,
                          'legacy_uuid': legacy_uuid, 'application': app})
        return nodes
    finally:
        bus.shutdown()


def resolve_board_id(interface, board_id, **kwargs):
    wanted = normalize_board_id(board_id)
    matches = [node for node in scan_bus(interface, **kwargs)
               if node['board_id'] == wanted]
    if not matches:
        raise IdentityError("board_id %s was not found on %s"
                            % (wanted, interface))
    if len(matches) != 1:
        raise IdentityError("board_id %s is duplicated on %s"
                            % (wanted, interface))
    return matches[0]['legacy_uuid']
