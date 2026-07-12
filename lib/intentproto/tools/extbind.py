#!/usr/bin/env python3
# intentproto extension self-description: host-side reference binding.
#
# A v2 peer needs no zlib dictionary round-trip. The device serves its
# registry as data over two library-owned meta-commands
# (list_extensions / list_constants, see include/intentproto/proto.hpp
# and core_ids.hpp). This module is the reference implementation of the
# host end: it drives those meta-commands over a duck-typed transport,
# parses the streamed descriptors into typed command encoders and
# response parsers, and exposes them as ext.commands[name](**kwargs)
# and ext.parse_response(payload). It is the blueprint for the future
# klippy v2 connect path; it depends only on the standard library.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import re

# ---- fixed core ids (mirror include/intentproto/core_ids.hpp) ----
MSGID_LIST_EXTENSIONS = 32
MSGID_EXTENSION_DESC = 33
MSGID_LIST_CONSTANTS = 34
MSGID_CONSTANT_DESC = 35
MSGID_EXTENSION_DONE = 36
EXTDESC_COUNT_MAX = 8

EXTDESC_KIND_COMMAND = 0
EXTDESC_KIND_RESPONSE = 1
CONSTDESC_KIND_INT = 0
CONSTDESC_KIND_STR = 1
CONSTDESC_KIND_ENUM = 2


# ---- VLQ codec (byte-identical to src/proto.cpp) ----

def vlq_encode(v):
    v &= 0xffffffff
    sv = v - (1 << 32) if v >= (1 << 31) else v
    if -(1 << 5) <= sv < (3 << 5):
        length = 1
    elif -(1 << 12) <= sv < (3 << 12):
        length = 2
    elif -(1 << 19) <= sv < (3 << 19):
        length = 3
    elif -(1 << 26) <= sv < (3 << 26):
        length = 4
    else:
        length = 5
    out = bytearray()
    for i in range(length - 1, 0, -1):
        out.append(((v >> (7 * i)) & 0x7f) | 0x80)
    out.append(v & 0x7f)
    return bytes(out)


def vlq_decode(buf, pos):
    c = buf[pos]
    pos += 1
    v = c & 0x7f
    if (c & 0x60) == 0x60:
        v |= 0xffffffe0  # sign-extend a negative leading group (-0x20)
    while c & 0x80:
        c = buf[pos]
        pos += 1
        v = ((v << 7) | (c & 0x7f)) & 0xffffffff
    return v, pos


# ---- message format grammar ("name p1=%c p2=%u ...") ----
# The same grammar klippy's msgproto uses; %.*s is a length-prefixed
# byte buffer, every other specifier is a VLQ integer differing only
# in how the receiver interprets width/sign.

_PARAM_RE = re.compile(r'(\w+)=(%[^ ]+)')


class Message:
    def __init__(self, name, params, msgid=None):
        self.name = name
        self.params = params      # [(pname, fmt)]
        self.msgid = msgid

    @classmethod
    def parse_desc(cls, desc, msgid=None):
        parts = desc.split(' ', 1)
        name = parts[0]
        params = []
        if len(parts) > 1:
            for m in _PARAM_RE.finditer(parts[1]):
                params.append((m.group(1), m.group(2)))
        return cls(name, params, msgid)

    def encode(self, *args, **kwargs):
        # Positional or keyword; keyword matches declared param names.
        if args and kwargs:
            raise TypeError("give positional OR keyword args, not both")
        if args:
            if len(args) != len(self.params):
                raise TypeError("%s takes %d args, got %d"
                                % (self.name, len(self.params), len(args)))
            values = list(args)
        else:
            values = [kwargs[p[0]] for p in self.params]
        out = bytearray(vlq_encode(self.msgid))
        for (pname, fmt), val in zip(self.params, values):
            if fmt == '%.*s':
                data = bytes(val)
                out += vlq_encode(len(data))
                out += data
            else:
                out += vlq_encode(int(val) & 0xffffffff)
        return bytes(out)

    def parse(self, payload, pos=0):
        # payload includes the leading msgid; skip it.
        _id, pos = vlq_decode(payload, pos)
        result = {}
        for pname, fmt in self.params:
            if fmt == '%.*s':
                n, pos = vlq_decode(payload, pos)
                result[pname] = bytes(payload[pos:pos + n])
                pos += n
            else:
                v, pos = vlq_decode(payload, pos)
                result[pname] = _interpret(fmt, v)
        return result, pos


def _interpret(fmt, v):
    if fmt == '%c':
        return v & 0xff
    if fmt == '%hu':
        return v & 0xffff
    if fmt == '%hi':
        v &= 0xffff
        return v - (1 << 16) if v >= (1 << 15) else v
    if fmt == '%u':
        return v
    if fmt == '%i':
        return v - (1 << 32) if v >= (1 << 31) else v
    return v


# ---- the binding ----

class ExtBinding:
    # The meta-command ids default to the fixed v2 core allocation
    # (core_ids.hpp). On a legacy link the same messages carry
    # init()-assigned ids instead, so a caller that learned them from
    # the dictionary (or a test transcript) passes them in.
    def __init__(self, list_extensions_id=MSGID_LIST_EXTENSIONS,
                 list_constants_id=MSGID_LIST_CONSTANTS,
                 extension_desc_id=MSGID_EXTENSION_DESC,
                 constant_desc_id=MSGID_CONSTANT_DESC,
                 extension_done_id=MSGID_EXTENSION_DONE):
        self.list_extensions_id = list_extensions_id
        self.list_constants_id = list_constants_id
        self.extension_desc_id = extension_desc_id
        self.constant_desc_id = constant_desc_id
        self.extension_done_id = extension_done_id
        self.commands = {}        # name -> Message
        self.responses = {}       # msgid -> Message
        self.constants = {}       # NAME -> int|str
        self.enums = {}           # enum_name -> {value_name: int}
        self._done_total = None

    # Feed one decoded extension_desc / constant_desc record. These are
    # the exact per-record semantics of the live protocol, factored out
    # so a captured transcript and a live transport share one path.
    def ingest_extension_desc(self, kind, msgid, desc):
        msg = Message.parse_desc(desc, msgid)
        if kind == EXTDESC_KIND_COMMAND:
            self.commands[msg.name] = msg
        else:
            self.responses[msgid] = msg

    def ingest_constant_desc(self, kind, desc):
        if kind == CONSTDESC_KIND_ENUM:
            head, value = desc.split('=', 1)
            enum_name, value_name = head.split('.', 1)
            self.enums.setdefault(enum_name, {})[value_name] = int(value)
        else:
            name, value = desc.split('=', 1)
            self.constants[name] = (int(value)
                                    if kind == CONSTDESC_KIND_INT else value)

    # Consume a whole response payload (leading msgid decides its type).
    # Returns True once extension_done has been seen for the range.
    def ingest_response(self, payload):
        msgid, pos = vlq_decode(payload, 0)
        if msgid == self.extension_desc_id:
            kind = payload[pos]
            pos += 1
            eid, pos = vlq_decode(payload, pos)
            n, pos = vlq_decode(payload, pos)
            desc = payload[pos:pos + n].decode('ascii')
            self.ingest_extension_desc(kind, eid, desc)
            return False
        if msgid == self.constant_desc_id:
            kind = payload[pos]
            pos += 1
            n, pos = vlq_decode(payload, pos)
            desc = payload[pos:pos + n].decode('ascii')
            self.ingest_constant_desc(kind, desc)
            return False
        if msgid == self.extension_done_id:
            self._done_total, pos = vlq_decode(payload, pos)
            return True
        return False

    def encode_command(self, name, *args, **kwargs):
        return self.commands[name].encode(*args, **kwargs)

    def parse_response(self, payload):
        msgid, _ = vlq_decode(payload, 0)
        msg = self.responses.get(msgid)
        if msg is None:
            return None, {'msgid': msgid}
        fields, _ = msg.parse(payload)
        return msg.name, fields

    # ---- live enumeration over a duck-typed transport ----
    # transport.send(payload_bytes) sends one command; transport.poll()
    # yields received response payloads (bytes) until none remain. This
    # is the reference the klippy v2 connect path follows.
    def query(self, transport):
        for list_id in (self.list_extensions_id, self.list_constants_id):
            start = 0
            while True:
                transport.send(vlq_encode(list_id) + vlq_encode(start)
                               + vlq_encode(EXTDESC_COUNT_MAX))
                done = False
                for payload in transport.poll():
                    if self.ingest_response(payload):
                        done = True
                if done:
                    break
                start += EXTDESC_COUNT_MAX
        return self


if __name__ == '__main__':
    # Tiny self-check of the codec against known VLQ boundaries.
    for v in (0, 1, 95, 96, -32 & 0xffffffff, -33 & 0xffffffff, 300,
              0x7fffffff, 0xdeadbeef):
        b = vlq_encode(v)
        d, n = vlq_decode(b, 0)
        assert n == len(b) and d == (v & 0xffffffff), (v, b.hex())
    print("extbind vlq self-check ok")
