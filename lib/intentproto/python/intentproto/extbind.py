# Connect-time extension binding (FD-0001 doc 10), packaged API.
#
# A v2 peer serves its command/response/constant registry as DATA over two
# library-owned meta-commands (list_extensions / list_constants). This
# module turns that stream into typed command encoders and response
# parsers bound at connect: after query(), ext.encode_command(name, ...)
# builds a payload and ext.parse_response(payload) decodes one. No zlib
# dictionary round-trip, no source scraping.
#
# This is the packaged version of tools/extbind.py: it reuses the
# package's C-backed VLQ codec (the single source of truth for the wire
# integer format) instead of a second pure-Python copy, and adds a
# HostSession adapter so the enumeration drives over the real session.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import re

from . import vlq_encode, vlq_decode, CLASS_SCHEDULED

# ---- fixed core ids (mirror include/intentproto/core_ids.hpp v2::) ----
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

# ---- message format grammar ("name p1=%c p2=%u ...") ----
# The same grammar klippy's msgproto uses; %.*s is a length-prefixed byte
# buffer, every other specifier is a VLQ integer differing only in how the
# receiver interprets width/sign.
_PARAM_RE = re.compile(r'(\w+)=(%[^ ]+)')


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


class Message:
    """A bound command/response descriptor: name, msgid, and typed params.

    encode() builds a wire payload (leading msgid + args); parse() decodes
    one back into a {name: value} dict, applying each specifier's
    width/sign interpretation."""
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
        _id, pos = vlq_decode(payload, pos)  # leading msgid
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


class ExtBinding:
    """The live binding: enumerate a peer's registry, then encode commands
    and parse responses by name.

    The meta-command ids default to the fixed v2 core allocation
    (core_ids.hpp v2::). On a legacy link the same messages carry
    init()-assigned ids instead, so a caller that learned them from the
    dictionary (or Device introspection) passes them in."""
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

    # Feed one decoded extension_desc / constant_desc record.
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
            desc = bytes(payload[pos:pos + n]).decode('ascii')
            self.ingest_extension_desc(kind, eid, desc)
            return False
        if msgid == self.constant_desc_id:
            kind = payload[pos]
            pos += 1
            n, pos = vlq_decode(payload, pos)
            desc = bytes(payload[pos:pos + n]).decode('ascii')
            self.ingest_constant_desc(kind, desc)
            return False
        if msgid == self.extension_done_id:
            self._done_total, pos = vlq_decode(payload, pos)
            return True
        return False

    def encode_command(self, name, *args, **kwargs):
        """Build the wire payload for a bound command by name."""
        return self.commands[name].encode(*args, **kwargs)

    def parse_response(self, payload):
        """Decode a response payload into (name, fields), or
        (None, {'msgid': id}) for an unbound msgid."""
        msgid, _ = vlq_decode(payload, 0)
        msg = self.responses.get(msgid)
        if msg is None:
            return None, {'msgid': msgid}
        fields, _ = msg.parse(payload)
        return msg.name, fields

    # ---- live enumeration over a duck-typed transport ----
    # transport.send(payload_bytes) sends one command; transport.poll()
    # yields received response payloads (bytes) until none remain. Use
    # HostSessionTransport to drive this over a real HostSession.
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


class HostSessionTransport:
    """Adapt a HostSession to ExtBinding.query()'s send/poll duck type.

    responses: the list the HostSession's on_response callback appends to
      (wire on_response=responses.append when constructing the session).
    pump: a callable moving bytes between host and peer AND back after a
      send - for an in-process Device loopback that is device.rx(...)
      then host.on_rx(...); for a live link it flushes the transport both
      directions. query() drains poll() after each send, so pump must
      deliver all responses the peer produced for that command."""
    def __init__(self, host, responses, pump, cls=CLASS_SCHEDULED):
        self.host = host
        self.responses = responses
        self.pump = pump
        self.cls = cls

    def send(self, payload):
        if not self.host.send_command(payload, self.cls):
            raise RuntimeError("host session refused command (window full?)")
        self.pump()

    def poll(self):
        out = list(self.responses)
        del self.responses[:]
        return out


def bind_host_session(host, responses, pump, cls=CLASS_SCHEDULED, ids=None):
    """Convenience: enumerate a peer over a HostSession and return the
    bound ExtBinding. `ids` optionally overrides the five meta-command ids
    (dict with keys list_extensions/list_constants/extension_desc/
    constant_desc/extension_done) for a legacy link whose ids differ from
    the v2 core allocation."""
    ext = ExtBinding(**(ids or {}))
    ext.query(HostSessionTransport(host, responses, pump, cls))
    return ext
