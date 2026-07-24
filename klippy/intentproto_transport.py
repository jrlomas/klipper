# -*- coding: utf-8 -*-
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
    payload = frame[MESSAGE_HEADER_SIZE:len(frame) - MESSAGE_TRAILER_SIZE]
    return bytes(payload), frame[1]


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
        self.tx_frames = self.rx_frames = self.rx_uncorrectable = 0

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
            wire = self._ip.frame_v2_encode(payload, seq & MESSAGE_SEQ_MASK)
            out.append(wire)
            self.tx_frames += 1
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
            if (frame[blen - 1] != MESSAGE_SYNC
                    or not (frame[1] & FRAME_V2_FLAG)):
                i += 1
                continue
            try:
                payload, seq, _corr = self._ip.frame_v2_decode(bytes(frame))
            except ValueError:
                self.rx_uncorrectable += 1
                i += 1  # uncorrectable — resync; v1 ARQ will retransmit
                continue
            # Re-add the constant MESSAGE_DEST bit stripped by v2's seq nibble.
            seq_flags = MESSAGE_DEST | (seq & MESSAGE_SEQ_MASK)
            out.append(v1_build(payload, seq_flags))
            self.rx_frames += 1
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


# ---- the in-process bridge ------------------------------------------------
# A PTY sits below klippy's serial fd; a background thread pumps the framing
# transform between that PTY (v1, what klippy speaks) and the wire (v2, what
# the MCU speaks). klippy opens the PTY as an ordinary serial port, so
# serialqueue/serialhdl/msgproto are untouched.
import errno
import select
import socket
import threading
import time

DATAGRAM_MAX = 1472
DATAGRAM_OVERHEAD = 3 + 8  # header + HMAC tag
SESSION_ID_MAX = 24


class TransportBridge(object):
    def __init__(self, mode, pty_link, psk=None, fec_k=0,
                 udp_board=None, udp_listen=0, stream_wire_fd=None,
                 session=False, board_id=b""):
        # mode: 'bch' (byte-stream re-framing) or 'datagram' (UDP envelope).
        # pty_link: symlink klippy opens as its serial port.
        # udp_board: (host, port) for datagram mode.
        # stream_wire_fd: an already-open fd for bch mode (serial device, or a
        #   socketpair end in tests). Required for bch mode.
        # session: datagram mode only — establish the DTLS-class session
        #   (session_sec) and use it instead of the static-PSK codec.
        self.mode = mode
        self.pty_link = pty_link
        self.psk = psk
        self.fec_k = fec_k
        self.udp_board = udp_board
        self.udp_listen = udp_listen
        self.use_session = session
        self.board_id = board_id
        self.master_fd = None
        self._stop = False
        self._thread = None
        self._wire_fd = stream_wire_fd     # bch: raw fd
        self._sock = None                  # datagram: UDP socket
        self._session = None
        self.session_established = False
        self.session_confirmed = False
        self.session_fin_retransmits = 0
        self._session_fin = None
        self.peer_id = b""
        self.host_bytes = 0
        self.wire_bytes = 0
        self.tx_datagrams = 0
        self.rx_datagrams = 0
        if mode == 'bch':
            self._codec = BchConsoleCodec()
            # bch starts in v1 PASS-THROUGH: the MCU's console accepts both
            # framings at all times, so identify happens in plain v1 and the
            # caller upgrades with enable_v2() once the dictionary confirms
            # FRAMING_V2 (or never, for a stock board — graceful fallback).
            self.v2_active = False
        elif mode == 'datagram':
            self._codec = load_datagram_codec()(psk, fec_k)
            self.v2_active = True  # the datagram MCU console only speaks v2
            if session:
                if not psk:
                    raise FrameError("session mode requires a PSK")
                if not board_id:
                    raise FrameError("session mode requires an expected"
                                     " board_id")
                if len(board_id) > SESSION_ID_MAX:
                    raise FrameError("session board_id exceeds the 24-byte"
                                     " protocol limit")
                ip = _load_intentproto()
                # The session carries per-peer identities both ways: the
                # host advertises itself as "klippy-host"; board_id is the
                # identity we REQUIRE the board to present (verified after
                # the handshake in _session_handshake).
                self._session = ip.SecureSession(True, psk, b"klippy-host")
        else:
            raise FrameError("unknown transport mode %r" % (mode,))

    def enable_v2(self):
        # Upgrade a bch link from pass-through to the v2 transform. Safe at
        # any time: the MCU accepts both framings, per-frame; if the switch
        # splits a frame mid-stream the CRC/BCH resync + v1 ARQ recover it.
        self.v2_active = True

    def stats(self):
        c = self._codec
        if self.mode == 'bch':
            return {'mode': 'bch', 'v2_active': self.v2_active,
                    'tx_frames': c.tx_frames, 'rx_frames': c.rx_frames,
                    'rx_uncorrectable': c.rx_uncorrectable}
        st = {'mode': 'datagram', 'v2_active': self.v2_active,
              'session': self.use_session,
              'session_established': self.session_established,
              'session_confirmed': self.session_confirmed,
              'session_fin_retransmits': self.session_fin_retransmits,
              'host_bytes': self.host_bytes, 'wire_bytes': self.wire_bytes,
              'tx_datagrams': self.tx_datagrams,
              'rx_datagrams': self.rx_datagrams,
              'rx_lost': c.rx_lost, 'rx_reordered': c.rx_reordered,
              'auth_failures': c.auth_failures}
        if self.session_established:
            # In session mode the static codec is bypassed; report the
            # session's own health counters (and the verified peer).
            st['peer_id'] = self.peer_id.decode('utf-8', 'replace')
            st.update(self._session.diag())
        return st

    def open(self):
        import pty
        import tty
        master_fd, slave_fd = pty.openpty()
        tty.setraw(master_fd)
        os.set_blocking(master_fd, False)
        slave_name = os.ttyname(slave_fd)
        try:
            os.unlink(self.pty_link)
        except FileNotFoundError:
            pass
        os.symlink(slave_name, self.pty_link)
        os.chmod(slave_name, 0o660)
        self.master_fd = master_fd
        if self.mode == 'datagram':
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setblocking(False)
            self._sock.bind(("0.0.0.0", self.udp_listen))
            if self.use_session:
                # Run the DTLS-class handshake to completion BEFORE the pump
                # starts, so the ClientHello/ServerHello/ClientFin exchange is
                # never mistaken for session data. Once established, every
                # datagram both ways is a session datagram.
                self._session_handshake()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _session_handshake(self, tries=10, timeout=0.5):
        # Host is the session initiator: send ClientHello, await ServerHello,
        # reply ClientFin. Retransmit ClientHello until a ServerHello lands
        # (the responder is idempotent on a repeated ClientHello) or we give
        # up. Blocks the opener; the pump has not started yet.
        if self.udp_board is None:
            raise FrameError("datagram session mode requires a board address")
        hello = self._session.start()
        for _ in range(tries):
            self._sock.sendto(hello, self.udp_board)
            r, _, _ = select.select([self._sock], [], [], timeout)
            if not r:
                continue
            try:
                data, _addr = self._sock.recvfrom(DATAGRAM_MAX)
            except OSError:
                continue
            fin = self._session.on_handshake(data)
            if fin:
                # There is no fourth handshake acknowledgement. Retain the
                # exact ClientFin and keep retransmitting it from the pump
                # until the first authenticated session response proves that
                # the responder adopted the new keys. This is essential on a
                # reconnect: losing the final packet leaves the board on its
                # previous live session while the host believes the new one
                # is ready.
                self._session_fin = fin
                self._sock.sendto(fin, self.udp_board)
            if self._session.established:
                # Enforce the configured board identity: the ServerHello's
                # id rides under the handshake's Finished MAC. This binds
                # the configured name to the PSK; deployments must use a
                # distinct PSK per board to prevent cross-board claims.
                peer = self._session.peer_id()
                if peer != self.board_id:
                    raise FrameError(
                        "session peer identity mismatch: board presented"
                        " %r, expected %r" % (peer, self.board_id))
                self.peer_id = peer
                self.session_established = True
                return
            if self._session.failed:
                raise FrameError("session handshake rejected by the board")
        raise FrameError("session handshake timed out")

    def close(self):
        self._stop = True
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        for fd in (self.master_fd,):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if self._sock is not None:
            self._sock.close()
        try:
            os.unlink(self.pty_link)
        except OSError:
            pass

    def _wire_readfd(self):
        return self._sock.fileno() if self.mode == 'datagram' else self._wire_fd

    def _pump(self):
        fds = [self.master_fd, self._wire_readfd()]
        next_fin = time.monotonic() + 0.050
        while not self._stop:
            timeout = 0.050 if (self.session_established
                                and not self.session_confirmed) else 0.2
            try:
                r, _, _ = select.select(fds, [], [], timeout)
            except (OSError, ValueError):
                break
            if self.master_fd in r:
                self._host_to_wire()
            if self._wire_readfd() in r:
                self._wire_to_host()
            if (self.session_established and not self.session_confirmed
                    and self._session_fin is not None
                    and time.monotonic() >= next_fin):
                # Duplicate ClientFin is idempotent before adoption and is
                # ignored after adoption. Spread retries across time instead
                # of sending one correlated burst.
                try:
                    self._sock.sendto(self._session_fin, self.udp_board)
                    self.session_fin_retransmits += 1
                except OSError:
                    pass
                next_fin = time.monotonic() + 0.100

    def _read(self, fd, n=4096):
        try:
            return os.read(fd, n)
        except (BlockingIOError, OSError) as e:
            if getattr(e, 'errno', None) in (errno.EAGAIN, errno.EWOULDBLOCK):
                return b""
            return b""

    def _host_to_wire(self):
        data = self._read(self.master_fd)
        if not data:
            return
        self.host_bytes += len(data)
        if self.mode == 'bch':
            if not self.v2_active:
                self._write_stream(data)  # v1 pass-through
                return
            wire = self._codec.to_wire(data)
            if wire:
                self._write_stream(wire)
        else:  # datagram
            limit = DATAGRAM_MAX - DATAGRAM_OVERHEAD
            while data:
                chunk, data = data[:limit], data[limit:]
                if self.session_established:
                    # Session datagrams carry the rotating-key seal in place of
                    # the static HMAC; no erasure parity in session mode.
                    self._sock.sendto(self._session.encode(chunk),
                                      self.udp_board)
                    self.tx_datagrams += 1
                    continue
                self._sock.sendto(self._codec.encode(chunk), self.udp_board)
                self.tx_datagrams += 1
                parity = self._codec.parity_flush()
                if parity is not None:
                    self._sock.sendto(parity, self.udp_board)
                    self.tx_datagrams += 1

    def _wire_to_host(self):
        if self.mode == 'bch':
            data = self._read(self._wire_fd)
            if not data:
                return
            if not self.v2_active:
                self._write_master(data)  # v1 pass-through
                return
            v1 = self._codec.from_wire(data)
            if v1:
                self._write_master(v1)
        else:  # datagram
            try:
                data, addr = self._sock.recvfrom(DATAGRAM_MAX)
            except (BlockingIOError, OSError):
                return
            self.rx_datagrams += 1
            self.wire_bytes += len(data)
            if self.udp_board is None:
                self.udp_board = addr
            if self.session_established:
                try:
                    payload, _cls = self._session.decode(data)
                except ValueError:
                    # Auth failure / malformed / replay: drop; v1 ARQ
                    # recovers.
                    return
                self.session_confirmed = True
                self._session_fin = None
                if payload:
                    self._write_master(payload)
                return
            for payload in self._codec.decode(data):
                if payload:
                    self._write_master(payload)

    def _write_stream(self, data):
        try:
            os.write(self._wire_fd, data)
        except OSError:
            pass

    def _write_master(self, data):
        try:
            os.write(self.master_fd, data)
        except OSError:
            pass
