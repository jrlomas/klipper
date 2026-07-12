#!/usr/bin/env python3
# UDP transport bridge for intentproto datagrams (RFC 0001 doc 07).
#
# Bridges klippy's existing serial transport to a UDP/WiFi/Ethernet
# board: creates a PTY that klippy opens as its serial port, and
# forwards the byte stream as authenticated datagrams — 16-bit
# sequencing, traffic-class tag, truncated HMAC-SHA256 — matching
# lib/intentproto's C datagram layer byte for byte. This makes
# network boards usable with today's host without touching
# serialqueue; the native asyncio transport replaces it as klippy
# host modernization lands (RFC 0001 doc 05).
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
# Batch serial bytes briefly to amortize per-datagram overhead
BATCH_DELAY = 0.002


def make_tag(psk, data):
    return hmac_mod.new(psk, data, hashlib.sha256).digest()[:DATAGRAM_TAG]


class DatagramCodec:
    def __init__(self, psk):
        self.psk = psk
        self.tx_seq = 0
        self.rx_seq = None
        self.rx_lost = self.rx_reordered = self.auth_failures = 0

    def encode(self, payload, cls=0):
        seq = self.tx_seq & 0xffff
        self.tx_seq += 1
        flags = cls & DGF_CLASS_MASK
        if self.psk:
            flags |= DGF_AUTH
        head = bytes([(seq >> 8) & 0xff, seq & 0xff, flags])
        body = head + payload
        if self.psk:
            body += make_tag(self.psk, body)
        return body

    def decode(self, data):
        if len(data) < DATAGRAM_HEADER:
            return None
        flags = data[2]
        if self.psk:
            if not (flags & DGF_AUTH) \
               or len(data) < DATAGRAM_HEADER + DATAGRAM_TAG:
                self.auth_failures += 1
                return None
            body, tag = data[:-DATAGRAM_TAG], data[-DATAGRAM_TAG:]
            if not hmac_mod.compare_digest(make_tag(self.psk, body), tag):
                self.auth_failures += 1
                return None
            data = body
        seq = (data[0] << 8) | data[1]
        if self.rx_seq is not None:
            delta = (seq - self.rx_seq) & 0xffff
            if delta == 0 or delta > 0x8000:
                self.rx_reordered += 1
                return None
            if delta > 1:
                self.rx_lost += delta - 1
        self.rx_seq = seq
        if flags & DGF_PARITY:
            return None  # parity handling is a future refinement
        return data[DATAGRAM_HEADER:]


class Bridge:
    def __init__(self, board_addr, psk, pty_link):
        self.board_addr = board_addr
        self.codec = DatagramCodec(psk)
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

    # UDP -> serial
    def datagram_received(self, data, addr):
        payload = self.codec.decode(data)
        if payload:
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
    bridge = Bridge((host, int(port)), psk, args.pty)
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
