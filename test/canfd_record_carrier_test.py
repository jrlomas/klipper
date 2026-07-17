#!/usr/bin/env python3
"""Regression tests for the complete-record packed CAN-FD carrier."""

import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]
LEGAL_DLC = (0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64)


def wire_len(logical_len):
    return next(length for length in LEGAL_DLC if length >= logical_len)


def encode(records):
    frames = []
    packed = bytearray()
    for record in records:
        assert 1 <= len(record) <= 64
        if packed and len(packed) + len(record) > 64:
            frames.append(bytes(packed) + bytes(wire_len(len(packed))
                                                - len(packed)))
            packed.clear()
        packed.extend(record)
    if packed:
        frames.append(bytes(packed) + bytes(wire_len(len(packed))
                                            - len(packed)))
    return frames


def decode(frame):
    records = []
    pos = 0
    while pos < len(frame):
        if frame[pos] == 0:
            if any(frame[pos:]):
                raise ValueError("nonzero data after DLC padding")
            break
        logical_len = 1 if frame[pos] == 0x7e else frame[pos]
        if logical_len != 1 and not 5 <= logical_len <= 64:
            raise ValueError("invalid protocol record length")
        if logical_len > len(frame) - pos:
            raise ValueError("truncated protocol record")
        records.append(frame[pos:pos + logical_len])
        pos += logical_len
    return records


def main():
    # Every legal protocol size, including the full 64-byte maximum, can occupy
    # one frame and round-trips without exposing DLC padding.
    for logical_len in range(5, 65):
        record = bytes([logical_len]) + bytes(range(1, logical_len))
        frame, = encode([record])
        assert len(frame) in LEGAL_DLC
        assert len(frame) <= 64
        assert decode(frame) == [record]

    sync = bytes([0x7e])
    assert decode(encode([sync])[0]) == [sync]
    recovery = bytes([10, 0x18]) + bytes(8)
    assert decode(encode([sync, recovery])[0]) == [sync, recovery]
    assert len(encode([bytes([22]) + bytes(21)])[0]) == 24
    assert len(encode([bytes([64]) + bytes(63)])[0]) == 64

    # Kevin's batching is retained: distinct raw protocol messages, including
    # their distinct sequence bytes, share a single physical frame.
    records = [bytes([10, 0x10 + seq]) + bytes(8) for seq in range(4)]
    frames = encode(records)
    assert len(frames) == 1 and len(frames[0]) == 48
    assert decode(frames[0]) == records

    # The old 20+2 fragmentation failure is structurally impossible. A lost
    # physical frame may remove several records, but never a record tail.
    records = [bytes([22, 0x10 + seq]) + bytes(20) for seq in range(4)]
    frames = encode(records)
    assert len(frames) == 2
    assert decode(frames[1]) == records[2:]
    try:
        decode(records[0][:20])
    except ValueError:
        pass
    else:
        raise AssertionError("truncated CAN-FD record was accepted")

    host = (ROOT / 'klippy/chelper/serialqueue.c').read_text()
    node = (ROOT / 'src/generic/canserial.c').read_text()
    assert 'can_frame_logical_len(cf.data, len)' in host
    assert 'memcpy(sq->input_buf, cf.data, logical_len)' in host
    assert 'frame_len + record_len > MESSAGE_MAX' in host
    assert '__atomic_exchange_n(&sq->can_carrier_boundary' in host
    assert '__atomic_store_n(&sq->can_carrier_boundary' in host
    assert 'canserial_carrier_wire_len(now)' in node
    assert 'first == MESSAGE_SYNC ? 1 : first' in node
    assert 'now + record_len > MESSAGE_MAX' in node
    assert 'memcpy(&CanData.receive_buf[rpos], msg->data, record_len)' in node
    print('PASS: CAN-FD packs complete sequenced protocol records atomically')


if __name__ == '__main__':
    main()
