#!/usr/bin/env python3
# UDP transport bridge for intentproto datagrams (FD-0001 doc 07).
#
# Bridges klippy's existing serial transport to a UDP/WiFi/Ethernet
# board: creates a PTY that klippy opens as its serial port, and
# forwards the byte stream as authenticated datagrams — 16-bit
# sequencing, traffic-class tag, truncated HMAC-SHA256 — matching
# lib/intentproto's C datagram layer byte for byte. This makes
# network boards usable with today's host without touching
# serialqueue; the native asyncio transport replaces it as klippy
# host modernization lands (FD-0001 doc 05).
#
# Usage:
#   udp_bridge.py --board 192.168.1.50:41414 --psk-file /path/psk \
#                 --pty /tmp/intentproto-toolhead
#   ... then in the printer config:  [mcu toolhead] serial: /tmp/...
#
# An unauthenticated link requires the explicit --trust-network
# confession, mirroring the C layer.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import argparse
import asyncio
import hashlib
import hmac as hmac_mod
import logging
import os
import pty
import sys
import tty

DATAGRAM_HEADER = 3
DATAGRAM_TAG = 8
DATAGRAM_MAX = 1472
DGF_CLASS_MASK = 0x03
DGF_PARITY = 0x04
DGF_AUTH = 0x08
DGF_PARITY_LENGTHS = 0x20
DGF_FEC_DATA = 0x40
DGF_FEC_START = 0x80
DATAGRAM_FEC_MAX_BODY = DATAGRAM_MAX - DATAGRAM_HEADER - 2 - DATAGRAM_TAG
# Batch serial bytes briefly to amortize per-datagram overhead
BATCH_DELAY = 0.002


def make_tag(psk, data):
    return hmac_mod.new(psk, data, hashlib.sha256).digest()[:DATAGRAM_TAG]


def _xor_into(acc, src):
    # acc (bytearray) ^= src, zero-extending acc to len(src) - mirrors
    # the C library's xor_into (max-length XOR fold).
    if len(src) > len(acc):
        acc.extend(b"\x00" * (len(src) - len(acc)))
    for i in range(len(src)):
        acc[i] ^= src[i]


class DatagramCodec:
    # Wire-identical to lib/intentproto's C datagram layer, including
    # the XOR erasure layer: on tx a parity datagram is emitted after
    # every k data datagrams (parity_flush), on rx a single datagram
    # lost inside a protected block is reconstructed from the block's
    # survivors and parity. fec_k == 0 leaves the erasure layer off.
    def __init__(self, psk, fec_k=0):
        if fec_k not in (0, 2):
            raise ValueError("fec_k must be 0 (off) or 2 (pair blocks)")
        self.psk = psk
        self.tx_seq = 0
        self.expect_seq = None
        self.rx_lost = self.rx_reordered = self.auth_failures = 0
        self.k = fec_k
        # tx parity accumulator: XOR of the current block's datagrams
        # (each folded as [seq, flags, payload], pre-auth), and how many
        # data datagrams have been folded since the last parity flush.
        self.tx_parity = bytearray()
        self.tx_len_xor = 0
        self.sent_since_parity = 0
        # rx survivors accumulator: XOR of the datagrams received since
        # the last parity, plus whether a block is currently open.
        self.rx_held = bytearray()
        self.rx_len_xor = 0
        self.holding = False
        self.rx_block_received = 0
        self.rx_block_gap = False
        self.rx_deferred = None

    def encode(self, payload, cls=0):
        seq = self.tx_seq & 0xffff
        flags = cls & DGF_CLASS_MASK
        if self.k:
            flags |= DGF_FEC_DATA
            if self.sent_since_parity == 0:
                flags |= DGF_FEC_START
        if self.psk:
            flags |= DGF_AUTH
        head = bytes([(seq >> 8) & 0xff, seq & 0xff, flags])
        body = head + payload
        # Match the freestanding C codec, which reserves tag capacity even
        # in explicit trust-network mode so configuration does not alter MTU.
        if len(body) + DATAGRAM_TAG > DATAGRAM_MAX:
            raise ValueError("datagram payload exceeds UDP MTU")
        if self.k and len(body) > DATAGRAM_FEC_MAX_BODY:
            raise ValueError("datagram payload exceeds FEC parity capacity")
        self.tx_seq += 1
        if self.k:
            # Fold [seq, flags, payload] (pre-auth) into the block
            if self.sent_since_parity == 0:
                self.tx_parity = bytearray()
                self.tx_len_xor = 0
            _xor_into(self.tx_parity, body)
            self.tx_len_xor ^= len(body)
            self.sent_since_parity += 1
        if self.psk:
            body += make_tag(self.psk, body)
        return body

    def parity_flush(self):
        # Emit the block's parity datagram once k data datagrams have
        # been folded, else None. Call once after every encode().
        if not self.k or self.sent_since_parity < self.k:
            return None
        seq = self.tx_seq & 0xffff
        self.tx_seq += 1
        flags = (DGF_PARITY | DGF_PARITY_LENGTHS
                 | (self.sent_since_parity & DGF_CLASS_MASK))
        if self.psk:
            # Match the C seal(): the authenticated bit covers the
            # parity datagram too, and the tag spans it.
            flags |= DGF_AUTH
        plen = min(len(self.tx_parity),
                   DATAGRAM_MAX - DATAGRAM_HEADER - 2 - DATAGRAM_TAG)
        head = bytes([(seq >> 8) & 0xff, seq & 0xff, flags])
        lengths = bytes([self.tx_len_xor >> 8, self.tx_len_xor & 0xff])
        body = head + lengths + bytes(self.tx_parity[:plen])
        self.sent_since_parity = 0
        if self.psk:
            body += make_tag(self.psk, body)
        return body

    def decode(self, data):
        # Returns a list of frame payloads to deliver (0, 1, or - when a
        # parity reconstructs a lost datagram - 1 recovered payload).
        if len(data) < DATAGRAM_HEADER:
            return []
        flags = data[2]
        if self.psk:
            if not (flags & DGF_AUTH) \
               or len(data) < DATAGRAM_HEADER + DATAGRAM_TAG:
                self.auth_failures += 1
                return []
            body, tag = data[:-DATAGRAM_TAG], data[-DATAGRAM_TAG:]
            if not hmac_mod.compare_digest(make_tag(self.psk, body), tag):
                self.auth_failures += 1
                return []
            data = body
        seq = (data[0] << 8) | data[1]
        fec_data = bool(flags & DGF_FEC_DATA)
        fec_start = bool(flags & DGF_FEC_START)
        if self.expect_seq is None:
            self.expect_seq = ((seq - 1) & 0xffff
                               if fec_data and not fec_start else seq)
        delta = (seq - self.expect_seq) & 0xffff
        if delta > 0x8000:
            self.rx_reordered += 1  # stale duplicate / reorder
            return []
        if delta > 0:
            self.rx_lost += delta
        self.expect_seq = (seq + 1) & 0xffff

        if flags & DGF_PARITY:
            out = []
            if not (flags & DGF_PARITY_LENGTHS):
                self.holding = False
                self.rx_held = bytearray()
                self.rx_len_xor = 0
                self.rx_block_received = 0
                self.rx_block_gap = False
                self.rx_deferred = None
                return out
            protected_count = flags & DGF_CLASS_MASK
            if (self.holding and protected_count == 2
                    and self.rx_block_received == 1):
                if len(data) < DATAGRAM_HEADER + 2:
                    self.holding = False
                    self.rx_held = bytearray()
                    self.rx_len_xor = 0
                    self.rx_block_received = 0
                    self.rx_block_gap = False
                    self.rx_deferred = None
                    return []
                block_len_xor = ((data[DATAGRAM_HEADER] << 8)
                                 | data[DATAGRAM_HEADER + 1])
                parity = data[DATAGRAM_HEADER + 2:]
                lost_len = block_len_xor ^ self.rx_len_xor
                if not lost_len or lost_len > len(parity):
                    self.holding = False
                    self.rx_held = bytearray()
                    self.rx_len_xor = 0
                    self.rx_block_received = 0
                    self.rx_block_gap = False
                    self.rx_deferred = None
                    return []
                _xor_into(self.rx_held, parity[:lost_len])
                del self.rx_held[lost_len:]
                # rx_held now holds the missing datagram [seq,flags,pay]
                recovered = bytes(self.rx_held[DATAGRAM_HEADER:])
                if recovered:
                    out.append(recovered)
                if self.rx_deferred is not None:
                    out.append(self.rx_deferred[DATAGRAM_HEADER:])
            self.holding = False
            self.rx_held = bytearray()
            self.rx_len_xor = 0
            self.rx_block_received = 0
            self.rx_block_gap = False
            self.rx_deferred = None
            return out

        if not fec_data:
            self.holding = False
            self.rx_held = bytearray()
            self.rx_len_xor = 0
            self.rx_block_received = 0
            self.rx_block_gap = False
            self.rx_deferred = None
            return [data[DATAGRAM_HEADER:]]

        # Pair-block data: stream in-order packets immediately, but defer
        # the second survivor after a gap until parity reconstructs first.
        if fec_start:
            self.rx_held = bytearray()
            self.rx_len_xor = 0
            self.holding = True
            self.rx_block_received = 0
            self.rx_block_gap = False
            self.rx_deferred = None
        elif not self.holding:
            self.rx_held = bytearray()
            self.rx_len_xor = 0
            self.holding = True
            self.rx_block_received = 0
            self.rx_block_gap = True
            self.rx_deferred = None
        if delta > 0 and not fec_start:
            self.rx_block_gap = True
        _xor_into(self.rx_held, data)
        self.rx_len_xor ^= len(data)
        self.rx_block_received += 1
        if self.rx_block_gap:
            self.rx_deferred = bytes(data)
            return []
        return [data[DATAGRAM_HEADER:]]


class Bridge:
    def __init__(self, board_addr, psk, pty_link, fec_k=0):
        self.board_addr = board_addr
        self.codec = DatagramCodec(psk, fec_k)
        self.pty_link = pty_link
        self.transport = None
        self.master_fd = None
        self.pending = b""
        self.flush_handle = None

    def open_pty(self):
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
        logging.info("pty %s -> %s", self.pty_link, slave_name)

    # serial -> UDP
    def _pty_readable(self):
        try:
            data = os.read(self.master_fd, 4096)
        except (BlockingIOError, OSError):
            return
        if not data:
            return
        self.pending += data
        if self.flush_handle is None:
            loop = asyncio.get_running_loop()
            self.flush_handle = loop.call_later(BATCH_DELAY, self._flush)

    def _flush(self):
        self.flush_handle = None
        limit = DATAGRAM_MAX - DATAGRAM_HEADER - DATAGRAM_TAG
        while self.pending:
            chunk, self.pending = self.pending[:limit], self.pending[limit:]
            if self.transport is not None:
                self.transport.sendto(self.codec.encode(chunk),
                                      self.board_addr)
                # Emit this block's parity datagram when it just filled
                parity = self.codec.parity_flush()
                if parity is not None:
                    self.transport.sendto(parity, self.board_addr)

    # UDP -> serial
    def datagram_received(self, data, addr):
        # decode() yields the datagram's frames plus, when a parity
        # reconstructs a lost datagram, the recovered frames - all fed
        # onward to klippy in block order.
        for payload in self.codec.decode(data):
            if not payload:
                continue
            try:
                os.write(self.master_fd, payload)
            except OSError:
                logging.exception("pty write failed")

    def error_received(self, exc):
        logging.warning("udp error: %s", exc)

    def connection_made(self, transport):
        self.transport = transport

    def connection_lost(self, exc):
        logging.warning("udp closed: %s", exc)


async def amain(args):
    host, port = args.board.rsplit(":", 1)
    psk = None
    if args.psk_file:
        with open(args.psk_file, "rb") as f:
            psk = f.read().strip()
        if not psk:
            raise SystemExit("empty PSK file")
    elif not args.trust_network:
        raise SystemExit("authentication is mandatory: give --psk-file, or"
                         " confess --trust-network for an isolated segment")
    bridge = Bridge((host, int(port)), psk, args.pty, args.fec_k)
    bridge.open_pty()
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(
        lambda: bridge, local_addr=("0.0.0.0", args.listen_port))
    loop.add_reader(bridge.master_fd, bridge._pty_readable)
    logging.info("bridging %s <-> udp %s:%s (auth=%s)",
                 args.pty, host, port, "on" if psk else "TRUSTED NETWORK")
    await asyncio.Event().wait()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--board", required=True, help="board addr host:port")
    p.add_argument("--pty", default="/tmp/intentproto-udp",
                   help="pty symlink for klippy's serial config")
    p.add_argument("--psk-file", help="pre-shared key file (mandatory"
                   " unless --trust-network)")
    p.add_argument("--trust-network", action="store_true",
                   help="explicitly run unauthenticated")
    p.add_argument("--fec-k", type=int, choices=(0, 2), default=0,
                   help="XOR erasure pair blocks (2 = on, 0 = off; must"
                   " match the board's -f)")
    p.add_argument("--listen-port", type=int, default=41414)
    p.add_argument("-v", action="store_true", help="verbose")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.v else logging.INFO)
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
