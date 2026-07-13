# Host-side v2 transport for klippy: the "envelope" that lets klippy speak
# intentproto v2 to an MCU WITHOUT changing serialqueue's v1 protocol.
#
# The design (FD-0001 doc 10 / docs/Protocol_v2.md, docs/Upstream_Tracking.md):
# klippy's serialqueue keeps producing/consuming stock v1 frames and owns the
# v1 ARQ (seq/ack/retransmit). This module sits BELOW the serial fd as a
# stateless FRAMING TRANSFORM between the v1 byte stream klippy speaks and the
# v2 wire an MCU speaks. Two wire modes, one transform contract:
#
#   * "bch"      — console framing v2: each v1 frame is re-framed as a BCH
#                  (t=3) error-correcting frame (payload+seq preserved, CRC
#                  trailer replaced by BCH). Byte-stream links (serial/USB).
#   * "datagram" — the UDP envelope: whole v1 frames wrapped in authenticated
#                  (truncated HMAC) + erasure-FEC datagrams. Packet links.
#
# Both preserve v1's ARQ end-to-end (v1 seq/CRC ride inside the envelope for
# datagram mode; for bch mode the far side reconstructs the exact v1 frame and
# re-checks its CRC). This is NOT intentproto's HostSession, which REPLACES v1
# ARQ and is only for proto.cpp-cored peers (the bootloader).
#
# The BCH codec is the C library's, reached through the intentproto cffi
# binding, so the error-correcting code is never reimplemented here.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
import sys

# ---- stock v1 frame constants (mirror msgblock.h; used only to delimit and
#      rebuild frames — no protocol logic is duplicated) --------------------
MESSAGE_MIN = 5
MESSAGE_MAX = 64
MESSAGE_HEADER_SIZE = 2
MESSAGE_TRAILER_SIZE = 3
MESSAGE_SYNC = 0x7e
MESSAGE_DEST = 0x10
MESSAGE_SEQ_MASK = 0x0f
FRAME_V2_FLAG = 0x80
FRAME_V2_MAX = MESSAGE_MAX + 4  # v2 trailer is 4 BCH bytes vs v1's 2 CRC


def _load_intentproto():
    # The cffi binding lives in lib/intentproto/python. Locate it relative to
    # this file so klippy need not install it separately.
    here = os.path.dirname(os.path.abspath(__file__))
    cand = os.path.join(here, '..', 'lib', 'intentproto', 'python')
    if cand not in sys.path:
        sys.path.insert(0, cand)
    import intentproto
    return intentproto


class FrameError(Exception):
    pass


def v1_split(buf):
    """Delimit stock v1 frames from a byte buffer.

    Returns (frames, remaining): a list of complete v1 frame bytes and any
    trailing partial bytes. Mirrors msgblock_check's framing (length byte,
    trailing 0x7e, CRC16), including skipping a lone resync 0x7e byte that
    serialqueue prepends to a retransmit block.
    """
    ip = _load_intentproto()
    frames = []
    i = 0
    n = len(buf)
    while i < n:
        blen = buf[i]
        if blen < MESSAGE_MIN or blen > MESSAGE_MAX:
            # Not a frame start (e.g. a lone resync sync byte) — skip it.
            i += 1
            continue
        if i + blen > n:
            break  # incomplete frame; keep as remaining
        frame = buf[i:i + blen]
        if frame[blen - 1] != MESSAGE_SYNC:
            i += 1
            continue
        crc = ip.crc16_ccitt(bytes(frame[:blen - MESSAGE_TRAILER_SIZE]))
        want = (frame[blen - 3] << 8) | frame[blen - 2]
        if crc != want:
            i += 1
            continue
        frames.append(bytes(frame))
        i += blen
    return frames, bytes(buf[i:])


def v1_payload_seq(frame):
    """Extract (payload, seq_byte) from a validated v1 frame."""
    return bytes(frame[MESSAGE_HEADER_SIZE:len(frame) - MESSAGE_TRAILER_SIZE]), \
        frame[1]


def v1_build(payload, seq_byte):
    """Rebuild a stock v1 frame from payload + seq byte (recomputes CRC)."""
    ip = _load_intentproto()
    total = MESSAGE_HEADER_SIZE + len(payload) + MESSAGE_TRAILER_SIZE
    if total > MESSAGE_MAX:
        raise FrameError("payload too large for a v1 frame")
    body = bytes([total, seq_byte]) + bytes(payload)
    crc = ip.crc16_ccitt(body)
    return body + bytes([crc >> 8, crc & 0xff, MESSAGE_SYNC])


class BchConsoleCodec(object):
    """Stateless v1<->v2(BCH) framing transform for byte-stream links."""
    def __init__(self):
        self._ip = _load_intentproto()
        self._v1_tail = b""   # partial v1 bytes from klippy
        self._v2_tail = b""   # partial v2 bytes from the wire

    # klippy (v1) -> wire (v2)
    def to_wire(self, data):
        buf = self._v1_tail + data
        frames, self._v1_tail = v1_split(buf)
        out = []
        for f in frames:
            payload, seq = v1_payload_seq(f)
            # frame_v2 carries only the 4-bit seq nibble; the constant
            # MESSAGE_DEST bit is re-added when the far side rebuilds the v1
            # frame (from_wire / the MCU de-frame).
            out.append(self._ip.frame_v2_encode(payload, seq & MESSAGE_SEQ_MASK))
        return b"".join(out)

    # wire (v2) -> klippy (v1)
    def from_wire(self, data):
        buf = self._v2_tail + data
        out = []
        i, n = 0, len(buf)
        while i < n:
            blen = buf[i]
            if blen < MESSAGE_MIN or blen > FRAME_V2_MAX:
                i += 1
                continue
            if i + blen > n:
                break
            frame = buf[i:i + blen]
            if frame[blen - 1] != MESSAGE_SYNC or not (frame[1] & FRAME_V2_FLAG):
                i += 1
                continue
            try:
                payload, seq, _corr = self._ip.frame_v2_decode(bytes(frame))
            except ValueError:
                i += 1  # uncorrectable — resync; v1 ARQ will retransmit
                continue
            # Re-add the constant MESSAGE_DEST bit stripped by v2's seq nibble.
            out.append(v1_build(payload, MESSAGE_DEST | (seq & MESSAGE_SEQ_MASK)))
            i += blen
        self._v2_tail = bytes(buf[i:])
        return b"".join(out)


# The datagram (UDP) codec already exists, wire-identical to the C library, in
# lib/intentproto/tools/udp_bridge.py. Import it so both modes share one
# implementation rather than a second copy.
def load_datagram_codec():
    here = os.path.dirname(os.path.abspath(__file__))
    tools = os.path.join(here, '..', 'lib', 'intentproto', 'tools')
    if tools not in sys.path:
        sys.path.insert(0, tools)
    from udp_bridge import DatagramCodec
    return DatagramCodec
