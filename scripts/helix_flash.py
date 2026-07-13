#!/usr/bin/env python3
# helix_flash.py: in-band firmware update over the intentproto link
# (FD-0001 doc 11 — the first-class bootloader).
#
# Drives the bootloader's Class-1 update commands — enter_bootloader,
# flash_begin, flash_data (ack-windowed), flash_sign, flash_verify,
# flash_boot — over a serial link, using the intentproto host session
# (the same wire protocol the application speaks). "First installation
# is the only time a programmer is required; everything after is
# in-band."
#
# Usage:
#   helix_flash.py --device /dev/ttyUSB0 --baud 250000 out/klipper.bin
#   helix_flash.py --device ... --enter image.bin      # via the running app
#   helix_flash.py --exec "path/to/bootsim /tmp/dump" image.bin  # testing
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import argparse
import json
import os
import select
import shlex
import subprocess
import sys
import termios
import time
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'lib', 'intentproto', 'python'))
import intentproto

MSGID_IDENTIFY_RESPONSE = 0
MSGID_IDENTIFY = 1
CLASS_PROMPT = 1

# bootcore result codes (lib/intentproto/boot/bootcore.hpp)
BOOT_CODES = {0: 'OK', 1: 'ERR_STATE', 2: 'ERR_RANGE', 3: 'ERR_ORDER',
              4: 'ERR_FLASH', 5: 'ERR_CRC', 6: 'ERR_SIG'}
OP_NAMES = {0: 'flash_begin', 1: 'flash_data', 2: 'flash_verify',
            3: 'flash_boot', 4: 'enter_bootloader', 5: 'flash_sign'}

# Conservative flash_data payload chunk: msgid + vlq(offset) + vlq(len)
# + data must fit a 64-byte frame's 59-byte payload.
DATA_CHUNK = 40


class FlashError(Exception):
    pass


class Link:
    """A byte link: a serial device, or a subprocess's stdio (testing)."""
    def __init__(self, device=None, baud=250000, execcmd=None):
        self.proc = None
        if execcmd:
            self.proc = subprocess.Popen(
                shlex.split(execcmd), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, bufsize=0)
            self.rfd = self.proc.stdout.fileno()
            self.wfd = self.proc.stdin.fileno()
        else:
            fd = os.open(device, os.O_RDWR | os.O_NOCTTY)
            try:
                attr = termios.tcgetattr(fd)
                # raw 8N1 at the requested baud
                attr[0] = attr[1] = attr[3] = 0
                attr[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
                rate = getattr(termios, 'B%d' % baud, None)
                if rate is not None:
                    attr[4] = attr[5] = rate
                termios.tcsetattr(fd, termios.TCSANOW, attr)
            except termios.error:
                pass  # not a tty (a PTY or pipe under test): raw already
            self.rfd = self.wfd = fd

    def write(self, data):
        os.write(self.wfd, data)

    def read_available(self, timeout):
        r, _, _ = select.select([self.rfd], [], [], timeout)
        if not r:
            return b""
        try:
            return os.read(self.rfd, 4096)
        except OSError:
            return b""

    def close(self):
        if self.proc is not None:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        else:
            os.close(self.rfd)


class Proto:
    """Host session + dictionary-driven encode/decode over a Link."""
    def __init__(self, link, verbose=False):
        self.link = link
        self.verbose = verbose
        self.responses = []
        self.session = intentproto.HostSession(
            on_write=self._on_write, on_response=self._on_response)
        self.cmd_ids = {}       # name -> (msgid, [param formats])
        self.resp_fmt = {}      # msgid -> (name, [field formats])

    def _on_write(self, frame):
        self.link.write(frame)
        return 0

    def _on_response(self, payload):
        self.responses.append(bytes(payload))

    def pump(self, duration):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            data = self.link.read_available(0.05)
            if data:
                self.session.on_rx(data)
            now_ms = int(time.monotonic() * 1000)
            self.session.need_retransmit(now_ms, 250)
            if self.responses:
                return

    # ---- VLQ payload build/parse against dictionary formats ----
    @staticmethod
    def _encode_args(fmts, args):
        out = b""
        for fmt, arg in zip(fmts, args):
            if fmt in ('%.*s', '%*s'):
                out += intentproto.vlq_encode(len(arg)) + bytes(arg)
            else:
                out += intentproto.vlq_encode(int(arg) & 0xffffffff)
        return out

    @staticmethod
    def _decode_args(fmts, payload, pos):
        vals = []
        for fmt in fmts:
            if fmt in ('%.*s', '%*s'):
                n, pos = intentproto.vlq_decode(payload, pos)
                vals.append(bytes(payload[pos:pos + n]))
                pos += n
            else:
                v, pos = intentproto.vlq_decode(payload, pos)
                if fmt in ('%i', '%hi', '%c') and v >= 0x80000000 \
                   and fmt == '%i':
                    v -= 0x100000000
                vals.append(v)
        return vals, pos

    @staticmethod
    def _parse_msgformat(msgformat):
        # "name arg=%u data=%.*s" -> (name, ['%u', '%.*s'])
        parts = msgformat.split()
        fmts = [p.split('=', 1)[1] for p in parts[1:]]
        return parts[0], fmts

    # ---- dictionary bootstrap (identify is a fixed-id command) ----
    def fetch_dictionary(self, timeout=10.0):
        blob = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            payload = intentproto.vlq_encode(MSGID_IDENTIFY) \
                + intentproto.vlq_encode(len(blob)) \
                + intentproto.vlq_encode(40)
            self.responses = []
            rc = self.session.send_command(payload, CLASS_PROMPT)
            if rc < 0:
                raise FlashError("send failed (link down?)")
            self.pump(2.0)
            got = None
            for resp in self.responses:
                msgid, pos = intentproto.vlq_decode(resp, 0)
                if msgid != MSGID_IDENTIFY_RESPONSE:
                    continue
                offset, pos = intentproto.vlq_decode(resp, pos)
                n, pos = intentproto.vlq_decode(resp, pos)
                data = bytes(resp[pos:pos + n])
                if offset == len(blob):
                    got = data
                break
            if got is None:
                continue
            if not got:
                break  # end of blob
            blob += got
        if not blob:
            raise FlashError("no identify data (is the board connected?)")
        d = json.loads(zlib.decompress(blob).decode())
        for msgformat, msgid in d.get('commands', {}).items():
            name, fmts = self._parse_msgformat(msgformat)
            self.cmd_ids[name] = (msgid, fmts)
        for msgformat, msgid in d.get('responses', {}).items():
            name, fmts = self._parse_msgformat(msgformat)
            self.resp_fmt[msgid] = (name, fmts)
        return d

    def send(self, name, *args):
        if name not in self.cmd_ids:
            raise FlashError("board does not implement '%s'" % (name,))
        msgid, fmts = self.cmd_ids[name]
        payload = intentproto.vlq_encode(msgid) + self._encode_args(fmts, args)
        rc = self.session.send_command(payload, CLASS_PROMPT)
        if rc < 0:
            raise FlashError("send_command '%s' failed" % (name,))

    def wait_response(self, name, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            while self.responses:
                resp = self.responses.pop(0)
                msgid, pos = intentproto.vlq_decode(resp, 0)
                entry = self.resp_fmt.get(msgid)
                if entry is None:
                    continue
                rname, fmts = entry
                vals, _ = self._decode_args(fmts, resp, pos)
                if self.verbose:
                    print("  <- %s %s" % (rname, vals))
                if rname == name:
                    return vals
            self.pump(0.5)
        raise FlashError("timed out waiting for '%s'" % (name,))


def check_result(vals, expect_op):
    op, code, arg = vals[0], vals[1], vals[2]
    if op != expect_op or code != 0:
        raise FlashError("%s failed: %s (arg=%d)"
                         % (OP_NAMES.get(op, op),
                            BOOT_CODES.get(code, code), arg))
    return arg


def main():
    p = argparse.ArgumentParser(
        description="in-band HELIX firmware update (FD-0001 doc 11)")
    p.add_argument("image", help="application image (.bin) to flash")
    p.add_argument("--device", help="serial device to the board")
    p.add_argument("--baud", type=int, default=250000)
    p.add_argument("--exec", dest="execcmd",
                   help="talk to a subprocess's stdio instead of a device"
                        " (testing against bootsim)")
    p.add_argument("--enter", action="store_true",
                   help="ask the running application to reboot into the"
                        " bootloader first")
    p.add_argument("--sign-file", help="64-byte Ed25519 signature to send"
                                       " (signed-image bootloaders)")
    p.add_argument("--no-boot", action="store_true",
                   help="flash and verify but do not boot the image")
    p.add_argument("-v", action="store_true", help="verbose")
    args = p.parse_args()
    if not args.device and not args.execcmd:
        p.error("--device or --exec is required")

    with open(args.image, "rb") as f:
        image = f.read()
    crc = zlib.crc32(image) & 0xffffffff
    print("image: %s (%d bytes, crc32 0x%08x)"
          % (args.image, len(image), crc))

    link = Link(args.device, args.baud, args.execcmd)
    try:
        proto = Proto(link, verbose=args.v)
        d = proto.fetch_dictionary()
        version = d.get('version', '?')
        print("connected: %s" % (version,))

        if args.enter:
            # Phase A: the application's enter_bootloader; the board resets
            # into the bootloader, and we re-handshake from scratch.
            proto.send('enter_bootloader', 0)
            print("requested bootloader entry; waiting for reset...")
            time.sleep(2.0)
            proto = Proto(link, verbose=args.v)
            d = proto.fetch_dictionary()
            print("reconnected: %s" % (d.get('version', '?'),))

        if 'flash_begin' not in proto.cmd_ids:
            raise FlashError(
                "this peer has no flash commands — not a bootloader"
                " (run with --enter to reboot the application into it)")

        proto.send('flash_begin', len(image), crc)
        check_result(proto.wait_response('flash_result', 15.0), 0)
        print("flash_begin ok (erase done)")

        sent = 0
        while sent < len(image):
            chunk = image[sent:sent + DATA_CHUNK]
            proto.send('flash_data', sent, chunk)
            # Window: let the session cap in-flight commands; drain acks.
            while proto.session.inflight >= 8:
                proto.pump(0.05)
            sent += len(chunk)
            if sent % (DATA_CHUNK * 100) == 0 or sent >= len(image):
                pct = 100.0 * sent / len(image)
                sys.stdout.write("\r  data: %d/%d (%.0f%%)"
                                 % (sent, len(image), pct))
                sys.stdout.flush()
        print()
        # Collect the trailing flash_result(DATA) acks; the last one's arg
        # is the contiguous high-water mark.
        deadline = time.monotonic() + 10.0
        received = 0
        while received < len(image) and time.monotonic() < deadline:
            try:
                vals = proto.wait_response('flash_result', 2.0)
            except FlashError:
                break
            if vals[0] == 1:  # OP_DATA
                if vals[1] != 0:
                    raise FlashError("flash_data failed: %s"
                                     % (BOOT_CODES.get(vals[1], vals[1]),))
                received = max(received, vals[2])
        if received < len(image):
            raise FlashError("board received %d of %d bytes"
                             % (received, len(image)))
        print("data complete (%d bytes acknowledged)" % (received,))

        if args.sign_file:
            with open(args.sign_file, "rb") as f:
                sig = f.read()
            if len(sig) != 64:
                raise FlashError("--sign-file must be a 64-byte detached"
                                 " Ed25519 signature (got %d bytes)"
                                 % (len(sig),))
            # Chunked like flash_data: the whole signature plus command
            # overhead cannot fit one frame's payload.
            got = 0
            for off in range(0, len(sig), 32):
                proto.send('flash_sign', off, sig[off:off + 32])
                got = check_result(proto.wait_response('flash_result'), 5)
            if got != len(sig):
                raise FlashError("board holds %d of %d signature bytes"
                                 % (got, len(sig)))
            print("signature accepted")

        proto.send('flash_verify')
        got_crc = check_result(proto.wait_response('flash_result', 15.0), 2)
        print("verify ok (crc32 0x%08x)" % (got_crc,))

        if not args.no_boot:
            proto.send('flash_boot')
            check_result(proto.wait_response('flash_result', 10.0), 3)
            print("boot ok — board is resetting into the new image")
    finally:
        link.close()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except FlashError as e:
        sys.stderr.write("error: %s\n" % (e,))
        sys.exit(1)
