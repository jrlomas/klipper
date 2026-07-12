#!/usr/bin/env python3
# Focused test for the udp_bridge.py XOR erasure layer (RFC 0001
# doc 07, "two layers").  Drives DatagramCodec through
# encode -> drop exactly one datagram -> parity -> decode and asserts
# the lost datagram's frames are reconstructed without a retransmit.
#
# Also cross-checks wire identity against the C datagram library: if
# the sibling C++ glue test (test_udp_glue) has left its
# build/udp_glue_*.bin fixtures, this decodes C-produced survivor +
# parity datagrams with the Python rx codec and recovers the C-side
# payload - proving the host bridge and firmware share one byte layout.
#
# Run:  python3 tools/test_udp_bridge_fec.py

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import udp_bridge as ub

PSK = bytes(range(1, 17))
FAIL = 0


def check(cond, msg):
    global FAIL
    if not cond:
        print("FAIL: %s" % msg)
        FAIL += 1


def make_frames(k, size=8):
    # k distinct fixed-length "frame" payloads
    return [bytes([i + 1]) * size for i in range(k)]


def tx_block(psk, k, frames):
    # Encode k data datagrams then the block's parity, exactly as the
    # bridge tx path does (encode + parity_flush per datagram).
    tx = ub.DatagramCodec(psk, k)
    dgrams, parity = [], None
    for f in frames:
        dgrams.append(tx.encode(f))
        p = tx.parity_flush()
        if p is not None:
            check(parity is None, "one parity per block")
            parity = p
    check(parity is not None, "parity emitted after k datagrams")
    return dgrams, parity


def deliver(rx, dgram):
    return rx.decode(dgram)


def test_tail_loss_recovers(psk):
    for k in (2, 3, 4, 8):
        frames = make_frames(k)
        dgrams, parity = tx_block(psk, k, frames)
        rx = ub.DatagramCodec(psk, k)
        got = []
        # Deliver every data datagram except the last, then the parity.
        for d in dgrams[:-1]:
            got += deliver(rx, d)
        recovered = deliver(rx, parity)
        check(recovered == [frames[-1]],
              "k=%d tail loss: recovered %r != %r"
              % (k, recovered, [frames[-1]]))
        check(got == frames[:-1], "k=%d survivors delivered verbatim" % k)
        check(rx.rx_lost == 1, "k=%d exactly one loss accounted" % k)


def test_no_loss_no_phantom(psk):
    # A complete block must yield the k frames and NO phantom recovery.
    k = 4
    frames = make_frames(k)
    dgrams, parity = tx_block(psk, k, frames)
    rx = ub.DatagramCodec(psk, k)
    got = []
    for d in dgrams:
        got += deliver(rx, d)
    got += deliver(rx, parity)
    check(got == frames, "no-loss block delivers exactly k frames: %r" % got)
    check(rx.rx_lost == 0, "no-loss block: zero loss counted")


def test_midblock_loss_matches_library(psk):
    # The library reconstructs the tail-loss case; a mid-block loss is
    # detected (counted) but not reconstructed by the single-loss XOR
    # code.  Assert that documented behaviour so the bridge and the C
    # layer stay in lockstep rather than silently diverging.
    k = 4
    frames = make_frames(k)
    dgrams, parity = tx_block(psk, k, frames)
    rx = ub.DatagramCodec(psk, k)
    got = deliver(rx, dgrams[0])
    # drop dgrams[1]
    for d in dgrams[2:]:
        got += deliver(rx, d)
    recovered = deliver(rx, parity)
    check(recovered == [], "mid-block loss not reconstructed (library parity)")
    check(rx.rx_lost == 1, "mid-block loss still counted")


def test_two_losses_no_false_recovery(psk):
    k = 4
    frames = make_frames(k)
    dgrams, parity = tx_block(psk, k, frames)
    rx = ub.DatagramCodec(psk, k)
    got = deliver(rx, dgrams[0]) + deliver(rx, dgrams[1])
    # drop dgrams[2] and dgrams[3]
    recovered = deliver(rx, parity)
    check(recovered == [], "two losses: no false single-loss recovery")


def test_wire_identity_with_c():
    # Cross-check against fixtures emitted by the C++ glue test.
    here = os.path.dirname(os.path.abspath(__file__))
    d = os.path.join(here, "build")
    survivor = os.path.join(d, "udp_glue_survivor.bin")
    parity = os.path.join(d, "udp_glue_parity.bin")
    expect = os.path.join(d, "udp_glue_lost_frames.bin")
    if not (os.path.exists(survivor) and os.path.exists(parity)):
        print("== wire-identity vs C: skipped (no fixtures; run test_udp_glue)")
        return
    with open(survivor, "rb") as f:
        s = f.read()
    with open(parity, "rb") as f:
        p = f.read()
    with open(expect, "rb") as f:
        want = f.read()
    # The C fixtures are a k=2 block (PSK): one survivor + parity, the
    # other datagram dropped.  The Python rx must recover its frames.
    rx = ub.DatagramCodec(PSK, 2)
    got = rx.decode(s)
    check(got and got[0] == s[ub.DATAGRAM_HEADER:-ub.DATAGRAM_TAG],
          "C survivor decoded by Python rx")
    recovered = rx.decode(p)
    check(recovered == [want],
          "C-produced block recovered by Python rx: %r != %r"
          % (recovered, [want]))
    print("== wire-identity vs C: OK")


def main():
    for psk in (PSK, None):  # authenticated and trust-network modes
        label = "auth" if psk else "trust-network"
        print("== erasure recovery (%s)" % label)
        test_tail_loss_recovers(psk)
        test_no_loss_no_phantom(psk)
        test_midblock_loss_matches_library(psk)
        test_two_losses_no_false_recovery(psk)
    test_wire_identity_with_c()
    if FAIL:
        print("%d FAILURE(S)" % FAIL)
        return 1
    print("all tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
