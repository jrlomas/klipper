#!/usr/bin/env python3
# intentproto cffi binding round-trip test (FD-0001 doc 10).
#
# Builds the API-mode extension from capi.h + the C++ core and drives a
# host-session loopback against the device singleton entirely through
# the Python surface — the Python counterpart of tests/test_capi.c.
# Skips gracefully (exit 0) when cffi is unavailable, so `make capi`
# stays green on a minimal toolchain.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))


def main():
    try:
        import cffi  # noqa: F401
    except ImportError:
        print("test_capi_py: cffi not installed - skipping")
        return 0

    import intentproto

    # ---- codecs and ABI ----
    assert (intentproto.abi_version() >> 16) == 1, "ABI major"
    print("abi_version = %#x (%s)"
          % (intentproto.abi_version(), intentproto.version_string()))
    for v in (0, 1, 95, 300, 0x7fffffff, 0xdeadbeef):
        enc = intentproto.vlq_encode(v)
        dec, n = intentproto.vlq_decode(enc)
        assert dec == (v & 0xffffffff) and n == len(enc), (v, enc.hex())
    assert intentproto.crc16_ccitt(b"\x01\x02\x03\x04") == \
        intentproto.crc16_ccitt(b"\x01\x02\x03\x04")

    # ---- host <-> device loopback ----
    h2d = bytearray()
    d2h = bytearray()
    responses = []

    device = intentproto.Device(on_write=lambda b: d2h.extend(b),
                                version="capi-py-test")
    host = intentproto.HostSession(
        on_write=lambda b: h2d.extend(b),
        on_response=lambda b: responses.append(b))

    # The device serves its own registry as data; enumerate its
    # constants over list_constants (only library-owned commands exist
    # in this build - self-describing all the way down).
    list_constants = device.command_id("list_constants")
    resp_by_id = {rid: name for (_i, rid, name, _k) in device.responses()}
    const_desc_id = next(rid for rid, n in resp_by_id.items()
                         if n == "constant_desc")
    done_id = next(rid for rid, n in resp_by_id.items()
                   if n == "extension_done")

    payload = (intentproto.vlq_encode(list_constants)
               + intentproto.vlq_encode(0)     # start
               + intentproto.vlq_encode(8))    # count
    assert host.send_command(payload, intentproto.CLASS_SCHEDULED)
    assert host.inflight == 1

    # pump host -> device, then device -> host
    device.rx(bytes(h2d))
    del h2d[:]
    host.on_rx(bytes(d2h))
    del d2h[:]

    assert host.inflight == 0, "ack should drain the window"
    assert len(responses) >= 2, responses

    saw_framing_v2 = saw_done = False
    for p in responses:
        msgid, pos = intentproto.vlq_decode(p)
        if msgid == const_desc_id:
            _kind, pos = intentproto.vlq_decode(p, pos)
            dlen, pos = intentproto.vlq_decode(p, pos)
            desc = p[pos:pos + dlen].decode("ascii")
            if desc == "FRAMING_V2=1":
                saw_framing_v2 = True
        elif msgid == done_id:
            saw_done = True
    assert saw_framing_v2, "device did not serve FRAMING_V2 constant"
    assert saw_done, "no extension_done terminator"

    assert not host.need_retransmit(1000000, 1)
    diag = host.diag()
    assert diag["retransmits"] == 0 and diag["naks"] == 0, diag
    stats = host.class_stats(intentproto.CLASS_SCHEDULED)
    assert stats["tx_msgs"] >= 1, stats

    host.close()
    print("test_capi_py: host-session loopback round-trip ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
