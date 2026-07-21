#!/usr/bin/env python3

import os
import random
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
    expect_error(lambda: CanConfig.decode(b'\0' * 15))
    expect_error(lambda: Delivery.decode(b'\0' * 16))
    broken = bytearray(good)
    broken[3] = 0x80
    expect_error(lambda: Packet.decode(broken))
    broken = bytearray(good)
    broken[HEADER.size + 4] = 0x80
    expect_error(lambda: Packet.decode(broken))

    # Invalid Classical/FD combinations fail at the authenticated boundary.
    expect_error(lambda: CanFrame.decode(
        CAN_HEADER.pack(1, 0, 9, 0, 0) + b'x' * 9))
    expect_error(lambda: CanFrame.decode(
        CAN_HEADER.pack(1, 0, 1, 2, 0) + b'x'))
    expect_error(lambda: CanFrame.decode(
        CAN_HEADER.pack(0x20000001, 0, 1, 0, 0) + b'x'))
    expect_error(lambda: CanConfig.decode(
        CAN_CONFIG_FORMAT.pack(CAN_CONFIG_PREPARE, 2, 0,
                               1, 1000000, 8000000)))


def test_control_records():
    config = CanConfig(CAN_CONFIG_PREPARE, 0x12345678, 1000000, 8000000,
                       True, True)
    assert CanConfig.decode(config.encode()) == config
    delivery = Delivery(DELIVERY_COMPLETED, 0xfeedbeef, 12345, 7)
    assert Delivery.decode(delivery.encode()) == delivery


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

    # Sequence comparison is modulo 32 bits, not ordinary integer ordering.
    wrapped = Runtime()
    wrapped.register(SERVICE_SERIAL, lambda record: None)
    wrapped.dispatch(Packet(9, 0xffffffff, (), PACKET_RESET).encode())
    wrapped.dispatch(Packet(9, 0, ()).encode())

    # Whole-packet structural validation precedes all service callbacks.
    atomic_seen = []
    atomic = Runtime()
    atomic.register(SERVICE_SERIAL, atomic_seen.append)
    two = bytearray(Packet(10, 1, (
        Record(SERVICE_SERIAL, SERIAL_DATA, data=b'first'),
        Record(SERVICE_SERIAL, SERIAL_DATA, data=b'second')),
        PACKET_RESET).encode())
    # Corrupt the second record's length after leaving the first valid.
    second = HEADER.size + RECORD_HEADER.size + len(b'first')
    two[second + 6:second + 8] = (127).to_bytes(2, 'little')
    expect_error(lambda: atomic.dispatch(two))
    assert not atomic_seen


def test_adversarial_decode():
    raw = Packet(1, 1, (Record(SERVICE_CAN, CAN_FRAME,
                                data=CanFrame(1, b'abc').encode()),),
                 PACKET_RESET).encode()
    rng = random.Random(0x4847)
    for _ in range(1000):
        candidate = bytearray(raw)
        for _ in range(rng.randrange(1, 5)):
            index = rng.randrange(len(candidate))
            candidate[index] ^= 1 << rng.randrange(8)
        try:
            Packet.decode(candidate)
        except GatewayProtocolError:
            pass

    tools = os.path.join(ROOT, 'lib', 'intentproto', 'tools')
    sys.path.insert(0, tools)
    from udp_bridge import DatagramCodec
    sender = DatagramCodec(b'correct key')
    receiver = DatagramCodec(b'wrong key')
    assert receiver.decode(sender.encode(raw)) == []
    assert receiver.auth_failures == 1


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
  struct helix_gateway_can_frame invalid;
  can_wire[9] = 2; /* BRS without FD */
  assert(helix_gateway_can_decode(&invalid, can_wire, cn) < 0);
  can_wire[9] = 3;
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
  struct helix_gateway_can_config cfg = {1, 1, 1, 9, 1000000, 8000000}, cfg2;
  uint8_t cfgwire[16];
  assert(helix_gateway_can_config_encode(cfgwire, 16, &cfg) == 16);
  assert(helix_gateway_can_config_decode(&cfg2, cfgwire, 16) == 16);
  assert(cfg2.epoch == 9 && cfg2.data_bitrate == 8000000
         && cfg2.fd && cfg2.brs);
  struct helix_gateway_delivery dl = {3, 0, 77, 88, 99}, dl2;
  assert(helix_gateway_delivery_encode(cfgwire, 16, &dl) == 16);
  assert(helix_gateway_delivery_decode(&dl2, cfgwire, 16) == 16);
  assert(dl2.cookie == 77 && dl2.hw_clock == 88 && dl2.detail == 99);
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
    test_control_records()
    test_runtime()
    test_adversarial_decode()
    test_c_codec()
    print('helix_gateway_test: PASS')
