#!/usr/bin/env python3
# Connect-time extension binding, packaged-API test (FD-0001 doc 10).
#
# Drives intentproto.bind_host_session() over a real HostSession <-> Device
# loopback: the host enumerates the in-process device's registry entirely
# through list_extensions / list_constants, and the resulting ExtBinding
# must expose the device's commands as encoders and its constants/enums as
# data. Proves the packaged binding (which reuses the C-backed VLQ) agrees
# with the device's own registry and produces byte-correct payloads.
#
# Skips gracefully (exit 0) without cffi, like the other capi tests.
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
        print("test_extbind_py: cffi not installed - skipping")
        return 0

    import intentproto as ip

    h2d = bytearray()
    d2h = bytearray()
    responses = []

    device = ip.Device(on_write=lambda b: d2h.extend(b),
                       version="extbind-py-test")
    host = ip.HostSession(on_write=lambda b: h2d.extend(b),
                          on_response=lambda b: responses.append(b))

    def pump():
        # host -> device, then device -> host (drains the ARQ ack and the
        # descriptor responses for the command just sent).
        device.rx(bytes(h2d))
        del h2d[:]
        host.on_rx(bytes(d2h))
        del d2h[:]

    # This build assigns the meta-command ids via init() (legacy-style),
    # not the fixed 32..36 v2 allocation, so learn them from the device's
    # own registry and hand them to the binding.
    resp_by_name = {name: rid for (_i, rid, name, _k) in device.responses()}
    ids = {
        'list_extensions_id': device.command_id("list_extensions"),
        'list_constants_id': device.command_id("list_constants"),
        'extension_desc_id': resp_by_name["extension_desc"],
        'constant_desc_id': resp_by_name["constant_desc"],
        'extension_done_id': resp_by_name["extension_done"],
    }

    ext = ip.bind_host_session(host, responses, pump, ids=ids)

    # The device self-describes all the way down: the meta-commands appear
    # as bound commands, and FRAMING_V2 as a constant.
    assert "list_extensions" in ext.commands, sorted(ext.commands)
    assert "list_constants" in ext.commands, sorted(ext.commands)
    assert ext.constants.get("FRAMING_V2") == 1, ext.constants

    # The bound command's id matches the device registry.
    le_id = device.command_id("list_extensions")
    assert ext.commands["list_extensions"].msgid == le_id

    # encode_command must produce the exact wire payload the device accepts.
    want = (ip.vlq_encode(le_id) + ip.vlq_encode(0) + ip.vlq_encode(8))
    got = ext.encode_command("list_extensions", start=0, count=8)
    assert got == want, (got.hex(), want.hex())
    # positional form agrees with keyword form
    assert ext.encode_command("list_extensions", 0, 8) == want

    # parse_response round-trips a response the binding itself built: hand
    # a constant_desc-shaped payload back through parse_response.
    cd_id = resp_by_name["constant_desc"]
    if cd_id in ext.responses:
        name, fields = ext.parse_response(
            ext.responses[cd_id].encode(kind=0, desc=b"X=1"))
        assert name == "constant_desc" and fields["desc"] == b"X=1", fields

    host.close()
    print("test_extbind_py: HostSession extension binding round-trip ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
