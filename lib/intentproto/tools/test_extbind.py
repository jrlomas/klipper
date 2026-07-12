#!/usr/bin/env python3
# Validate tools/extbind.py against the device bytes captured by the
# C++ test (tests/test_extdesc.cpp -> build/extdesc_wire.bin). The
# binding is built purely from the device's extension_desc /
# constant_desc stream, then used to encode command payloads that must
# match, byte for byte, what the device dispatched and accepted.
#
# Run: python3 tools/test_extbind.py  (after `make test` has produced
# the transcript, or it is run automatically by the Makefile).
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import extbind

HERE = os.path.dirname(os.path.abspath(__file__))
WIRE = os.path.join(HERE, "..", "build", "extdesc_wire.bin")

failures = 0


def check(cond, msg):
    global failures
    if not cond:
        print("FAIL: " + msg)
        failures += 1


def load_transcript(path):
    meta = {}
    rsp_payloads = []
    enc = {}      # name -> payload bytes the device accepted
    par = {}      # name -> response payload bytes to parse
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            tok = line.split()
            if tok[0] == 'meta':
                for kv in tok[1:]:
                    k, v = kv.split('=')
                    meta[k] = int(v)
            elif tok[0] == 'rsp':
                rsp_payloads.append(bytes.fromhex(tok[1]))
            elif tok[0] == 'cmd':
                pass  # the list_* command payloads; not needed here
            elif tok[0] == 'enc':
                enc[tok[1]] = bytes.fromhex(tok[2])
            elif tok[0] == 'par':
                par[tok[1]] = bytes.fromhex(tok[2])
    return meta, rsp_payloads, enc, par


def main():
    if not os.path.exists(WIRE):
        print("SKIP: %s not found (run `make test` first)" % WIRE)
        return 0

    meta, rsp_payloads, enc, par = load_transcript(WIRE)

    # This standalone loopback drives the device library, whose init()
    # assigns the meta-commands ids in registration order (the fixed
    # 32..36 of core_ids.hpp are the v2-link allocation, exercised on a
    # real v2 peer). The host learns the ids the same way it always
    # has — here from the transcript's meta line, on a live v2 link
    # from the fixed constants. Every id must be present and distinct.
    for key in ('list_extensions', 'list_constants', 'extension_desc',
                'constant_desc', 'extension_done'):
        check(key in meta, "meta id present: " + key)
    check(len(set(meta.values())) == len(meta), "meta ids distinct")

    # Build the binding purely from the device's streamed descriptors —
    # exactly what the live host does, replayed from captured bytes.
    ext = extbind.ExtBinding(
        list_extensions_id=meta['list_extensions'],
        list_constants_id=meta['list_constants'],
        extension_desc_id=meta['extension_desc'],
        constant_desc_id=meta['constant_desc'],
        extension_done_id=meta['extension_done'])
    for payload in rsp_payloads:
        ext.ingest_response(payload)

    # The device's own registered commands must have been described.
    for name in ('oams_cmd_load_spool', 'ext_cmd_trim', 'ext_cmd_blob',
                 'list_extensions', 'list_constants'):
        check(name in ext.commands, "command described: " + name)
    check(ext.commands['ext_cmd_trim'].params
          == [('trim', '%hi'), ('bias', '%i')], "trim signature")
    check(ext.commands['ext_cmd_blob'].params
          == [('oid', '%c'), ('data', '%.*s')], "blob signature")

    # Constants + enumerations round-tripped through the text encoding.
    check(ext.constants.get('CLOCK_FREQ') == 64000000, "int constant")
    check(ext.constants.get('MCU') == 'extdesc-test', "str constant")
    check(ext.enums.get('spi_bus', {}).get('spi1') == 1, "enum value")

    # The core assertion: bindings built from self-description encode
    # payloads byte-identical to what the device accepted.
    check(ext.encode_command('oams_cmd_load_spool', spool=3)
          == enc['load_spool'], "encode load_spool matches device")
    check(ext.encode_command('ext_cmd_trim', trim=-123, bias=-70000)
          == enc['trim'], "encode trim matches device")
    check(ext.encode_command('ext_cmd_blob', oid=9,
                             data=bytes([0xde, 0xad, 0x7e, 0x00]))
          == enc['blob'], "encode blob matches device")

    # And a device response parses back to its fields.
    if 'action_status' in par:
        name, fields = ext.parse_response(par['action_status'])
        check(name == 'oams_action_status', "parsed response name")
        check(fields.get('value') == 3, "parsed response field")

    if failures:
        print("%d FAILURE(S)" % failures)
        return 1
    print("extbind: all tests passed")
    return 0


if __name__ == '__main__':
    sys.exit(main())
