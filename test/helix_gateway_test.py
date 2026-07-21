#!/usr/bin/env python3

import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'klippy'))

from helix_gateway import *


def expect_error(fn):
    try:
        fn()
    except GatewayProtocolError:
        return
    raise AssertionError('expected GatewayProtocolError')


def test_vectors():
    can = CanFrame(0x123, bytes(range(64)), flags=3, hw_clock=0x11223344)
    records = (
        Record(SERVICE_CAN, CAN_FRAME, channel=2, cookie=99,
               flags=RECORD_TIMESTAMP_VALID, data=can.encode()),
        Record(SERVICE_SERIAL, SERIAL_DATA, channel=7, data=b'abc'),
    )
    raw = Packet(0x10203040, 5, records, PACKET_RESET).encode()
    decoded = Packet.decode(raw)
    assert decoded == Packet(0x10203040, 5, records, PACKET_RESET)
    assert CanFrame.decode(decoded.records[0].data) == can
    assert raw.hex() == (
        '48470101403020100500000002006700'
        '0101020008004c0063000000'
        '230100004433221140030000' + bytes(range(64)).hex() +
        '020107000000030000000000616263')


def test_malformed():
    good = Packet(1, 1, (Record(SERVICE_SERIAL, SERIAL_DATA,
                                data=b'x'),)).encode()
    expect_error(lambda: Packet.decode(good[:-1]))
    expect_error(lambda: Packet.decode(good + b'x'))
    broken = bytearray(good)
    broken[0] = 0
    expect_error(lambda: Packet.decode(broken))
    expect_error(lambda: CanFrame.decode(b'\0' * 11))


def test_runtime():
    seen = []
    runtime = Runtime(credits=2)
    runtime.register(SERVICE_SERIAL, seen.append, credits=1)
    p1 = Packet(7, 1, (Record(SERVICE_SERIAL, SERIAL_DATA,
                              data=b'a'),), PACKET_RESET)
    runtime.dispatch(p1.encode())
    assert [r.data for r in seen] == [b'a']
    expect_error(lambda: runtime.dispatch(Packet(7, 2, (
        Record(SERVICE_SERIAL, SERIAL_DATA, data=b'b'),)).encode()))
    runtime.add_credits(SERVICE_SERIAL, 1)
    runtime.dispatch(Packet(7, 3, (Record(SERVICE_SERIAL, SERIAL_DATA,
                                       data=b'c'),)).encode())
    expect_error(lambda: runtime.dispatch(Packet(7, 3, ()).encode()))
    expect_error(lambda: runtime.dispatch(Packet(8, 4, ()).encode()))
    assert runtime.stats['credit_stalls'] == 1
    assert runtime.stats['stale_epochs'] == 2


def test_c_codec():
    source = r'''
#include <assert.h>
#include <stdint.h>
#include <string.h>
#include "generic/gateway_protocol.h"
#include "generic/gateway_runtime.h"
static int seen;
static int submit(void *ctx, const struct helix_gateway_record *r) {
  (void)ctx; seen += r->length; return 0;
}
int main(void) {
  uint8_t can_data[64], can_wire[76], rec_wire[140], wire[160];
  for (int i=0; i<64; i++) can_data[i] = i;
  struct helix_gateway_can_frame cf = {0x123, 0x11223344, 64, 3, can_data};
  int cn = helix_gateway_can_encode(can_wire, sizeof(can_wire), &cf);
  assert(cn == 76);
  struct helix_gateway_record r = {1, 1, 2, 8, cn, 99, can_wire};
  int rn = helix_gateway_record_encode(rec_wire, sizeof(rec_wire), &r);
  struct helix_gateway_packet p = {1, 0x10203040, 5, 1, rn};
  assert(helix_gateway_packet_encode(wire, sizeof(wire), &p) == 16);
  memcpy(wire + 16, rec_wire, rn);
  struct helix_gateway_runtime rt;
  helix_gateway_runtime_init(&rt);
  static const struct helix_gateway_service_ops ops = {submit, 0};
  assert(!helix_gateway_runtime_register(&rt, 1, &ops, 0, 1));
  assert(helix_gateway_runtime_dispatch(&rt, wire, 16 + rn) == 1);
  assert(seen == 76 && rt.stats.records == 1);
  assert(helix_gateway_runtime_dispatch(&rt, wire, 16 + rn) < 0);
  return 0;
}
'''
    with tempfile.TemporaryDirectory() as td:
        cfile = os.path.join(td, 'test.c')
        exe = os.path.join(td, 'test')
        with open(cfile, 'w') as f:
            f.write(source)
        subprocess.check_call([
            'cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
            '-I' + os.path.join(ROOT, 'src'), cfile,
            os.path.join(ROOT, 'src/generic/gateway_protocol.c'),
            os.path.join(ROOT, 'src/generic/gateway_runtime.c'), '-o', exe])
        subprocess.check_call([exe])


if __name__ == '__main__':
    test_vectors()
    test_malformed()
    test_runtime()
    test_c_codec()
    print('helix_gateway_test: PASS')
