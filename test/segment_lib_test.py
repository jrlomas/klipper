#!/usr/bin/env python3
# Bit-identity guard for the intentproto LIBRARY segment codec
# (lib/intentproto, FD-0001 doc 02) against klippy's reference emitter.
#
# A third-party trajectory peer that speaks the motion-intent protocol
# uses the library's segment_end_delta()/segment_quantize() instead of
# klippy's segfit.c/trajectory_queuing.py. Those MUST land on the exact
# same integers, or a peer's chained position would diverge from where the
# MCU integrates the segment to. This test asserts:
#   * intentproto.segment_end_delta == segfit_end_delta_ho == py_end_delta_ho
#     across the physically reachable cubic/quintic coefficient space;
#   * intentproto.segment_quantize matches trajectory_queuing's
#     traj_round()+_clamp_i32() scaling for every derivative order; and
#   * the queue_traj_segment / traj_hold payload codec round-trips and its
#     bytes match a hand-built VLQ payload.
#
# Skips politely when cffi / the C toolchain is unavailable. Exits 0 on
# success. Run: python3 test/segment_lib_test.py
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
import random
import sys

ROOT = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..")
KDIR = os.path.join(ROOT, "klippy")
sys.path.insert(0, KDIR)
sys.path.insert(0, os.path.join(KDIR, "extras"))
sys.path.insert(0, os.path.join(ROOT, "lib", "intentproto", "python"))

INT64_SAFE = 1 << 60


def test_end_delta_bit_exact(ip, lib, tqm):
    # The library's end_delta must equal BOTH klippy references bit-for-bit
    # over the same physically reachable space test/traj_higher_order_test
    # exercises.
    random.seed(20260712)
    tested = 0
    for _ in range(200000):
        d = random.randint(1, 1 << 20)
        v = random.randint(-(1 << 18), 1 << 18)
        a = random.randint(-(1 << 26), 1 << 26)
        if random.random() < 0.5:
            j = random.randint(-(1 << 15), 1 << 15)
            s = c = 0
        else:
            j = random.randint(-(1 << 14), 1 << 14)
            s = random.randint(-(1 << 12), 1 << 12)
            c = random.randint(-(1 << 9), 1 << 9)
        pval = tqm.py_end_delta_ho(d, v, a, j, s, c)
        if abs(pval) >= INT64_SAFE:
            continue
        libval = ip.segment_end_delta(d, v, a, j, s, c)
        cval = int(lib.segfit_end_delta_ho(d, v, a, j, s, c))
        tested += 1
        if not (libval == cval == pval):
            raise AssertionError(
                "end_delta mismatch seg=%r lib=%d C=%d py=%d"
                % ((d, v, a, j, s, c), libval, cval, pval))
    if tested < 60000:
        raise AssertionError("too few physical cases tested (%d)" % tested)
    print("  library end_delta == segfit.c == py over %d cases: OK" % tested)


def test_quantize_matches_reference(ip, tqm):
    # segment_quantize(true, k) must equal traj_round(true * 2^(16k)) then
    # _clamp_i32, for every derivative order.
    random.seed(99)
    for _ in range(200000):
        k = random.randint(1, 5)
        # pick a magnitude that lands both inside and (rarely) over the rail
        mag = random.choice([1e-9, 1e-6, 1e-3, 0.4, 1.0, 3.0]) * (
            2.0 ** (-16 * k))
        true_v = random.uniform(-1, 1) * mag * (2.0 ** (16 * k))
        want = tqm._clamp_i32(tqm.traj_round(true_v * (2.0 ** (16 * k))))
        got = ip.segment_quantize(true_v, k)
        if got != want:
            raise AssertionError(
                "quantize mismatch true=%r k=%d lib=%d ref=%d"
                % (true_v, k, got, want))
    # explicit rail cases
    assert ip.segment_quantize(1.0, 2) == 2147483647
    assert ip.segment_quantize(-1.0, 2) == -2147483648
    print("  library quantize == traj_round+clamp for orders 1..5: OK")


def test_bezier_quantize_agrees(ip, tqm):
    # The library quantizer, applied to trajectory_queuing's own true
    # derivatives, reproduces bezier_to_wire's coefficients exactly.
    random.seed(7)
    keys = ['v', 'a', 'j', 's', 'c']
    for _ in range(20000):
        n = random.choice([3, 5])
        ctrl = [random.uniform(-1e5, 1e5) for _ in range(n + 1)]
        dur = random.randint(64, 1 << 20)
        order, ref = tqm.bezier_to_wire(ctrl, dur)
        derivs = tqm.bezier_power_derivatives(ctrl)
        D = float(dur)
        for k in range(1, n + 1):
            true_k = derivs[k - 1] / (D ** k)
            got = ip.segment_quantize(true_k, k)
            if got != ref[keys[k - 1]]:
                raise AssertionError(
                    "bezier quantize mismatch key=%s lib=%d ref=%d"
                    % (keys[k - 1], got, ref[keys[k - 1]]))
    print("  library quantize reproduces bezier_to_wire coefficients: OK")


def test_payload_codec(ip):
    # Round-trip queue_traj_segment (all orders) and traj_hold, and match a
    # hand-built VLQ payload for the quadratic case.
    MSGID_QUEUE = 12
    frame = ip.segment_encode(3, ip.SEG_POLY_QUADRATIC, 48000, 123456, -789)
    want = (ip.vlq_encode(MSGID_QUEUE) + ip.vlq_encode(3)
            + ip.vlq_encode(ip.SEG_POLY_QUADRATIC) + ip.vlq_encode(48000)
            + ip.vlq_encode(123456 & 0xffffffff)
            + ip.vlq_encode((-789) & 0xffffffff))
    if frame != want:
        raise AssertionError("segment payload bytes mismatch %r != %r"
                             % (frame, want))
    seg = ip.segment_decode(frame)
    assert seg['kind'] == ip.SEG_KIND_SEGMENT
    assert seg['oid'] == 3 and seg['duration'] == 48000
    assert seg['velocity'] == 123456 and seg['accel'] == -789

    fr = ip.segment_encode(7, ip.SEG_POLY_QUINTIC | ip.SEG_HOLD_AT_END,
                           9000, 10, -20, 30, -40, 50)
    seg = ip.segment_decode(fr)
    assert (seg['flags'] & ip.SEG_POLY_MASK) == ip.SEG_POLY_QUINTIC
    assert seg['crackle'] == 50 and seg['snap'] == -40

    h = ip.segment_encode_hold(2, 250000)
    seg = ip.segment_decode(h)
    assert seg['kind'] == ip.SEG_KIND_HOLD
    assert seg['oid'] == 2 and seg['duration'] == 250000

    assert ip.segment_decode(ip.vlq_encode(99)) is None  # foreign msgid
    print("  payload codec round-trips + matches hand-built VLQ: OK")


def main():
    try:
        import cffi  # noqa: F401
    except ImportError:
        print("SKIP: cffi not available")
        return 0
    try:
        import intentproto as ip
        import chelper
        import trajectory_queuing as tqm
    except Exception as e:  # pragma: no cover - toolchain/env dependent
        print("SKIP: cannot import (%s)" % (e,))
        return 0
    try:
        ip.build()
        _, lib = chelper.get_ffi()
    except Exception as e:  # pragma: no cover
        print("SKIP: cannot build native (%s)" % (e,))
        return 0
    test_end_delta_bit_exact(ip, lib, tqm)
    test_quantize_matches_reference(ip, tqm)
    test_bezier_quantize_agrees(ip, tqm)
    test_payload_codec(ip)
    print("segment_lib_test: all OK")
    return 0


if __name__ == '__main__':
    sys.exit(main())
