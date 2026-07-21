#!/usr/bin/env python3
"""Deterministic fault/conservation tests for the unified gateway."""

import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'klippy'))

from helix_gateway import (Ack, CanFrame, Delivery, DeliveryLedger,
                           Packet, PacketWindow, Record, Runtime,
                           GatewayProtocolError, CAN_FRAME, SERIAL_DATA,
                           SERVICE_CAN, SERVICE_SERIAL, PACKET_RESET,
                           DELIVERY_ADMITTED, DELIVERY_SUBMITTED,
                           DELIVERY_COMPLETED, DELIVERY_FAILED,
                           DELIVERY_UNKNOWN)


class Clock:
    def __init__(self):
        self.now = 0.

    def __call__(self):
        return self.now


def test_ack_window_policy():
    clock = Clock()
    window = PacketWindow(capacity=3, retry_after=1., max_attempts=2,
                          clock=clock)
    unsafe = Packet(1, 1, (Record(SERVICE_CAN, CAN_FRAME, cookie=11,
                                  data=CanFrame(1, b'x').encode()),),
                    PACKET_RESET)
    safe = Packet(1, 2, (), 0)
    window.track(unsafe, unsafe.encode(), replay_safe=False)
    window.track(safe, safe.encode(), replay_safe=True)
    clock.now = 1.1
    retries, unknown = window.due()
    assert retries == [safe.encode()]
    assert [item['packet'].sequence for item in unknown] == [1]
    assert window.acknowledge(Ack(1, 2, 1))[0]['packet'] == safe
    assert window.stats == {'tracked': 2, 'acked': 1, 'retransmitted': 1,
                            'unknown': 1, 'overflow': 0}


def test_delivery_conservation_randomized():
    rng = random.Random(0x48454c49)
    for _ in range(100):
        ledger = DeliveryLedger(capacity=512)
        cookies = list(range(1, rng.randrange(2, 200)))
        rng.shuffle(cookies)
        for cookie in cookies:
            ledger.update(Delivery(DELIVERY_ADMITTED, cookie))
            submitted = bool(rng.randrange(4))
            if submitted:
                ledger.update(Delivery(DELIVERY_SUBMITTED, cookie))
            choices = ((DELIVERY_COMPLETED, DELIVERY_FAILED,
                        DELIVERY_UNKNOWN) if submitted else
                       (DELIVERY_FAILED, DELIVERY_UNKNOWN))
            terminal = rng.choice(choices)
            ledger.update(Delivery(terminal, cookie))
        snap = ledger.snapshot()
        assert snap['residual'] == 0
        assert snap['admitted'] == snap['terminal']
        assert snap['terminal'] == (snap['completed'] + snap['failed']
                                    + snap['unknown'])


def test_reset_marks_inflight_unknown():
    ledger = DeliveryLedger()
    ledger.update(Delivery(DELIVERY_ADMITTED, 1))
    ledger.update(Delivery(DELIVERY_ADMITTED, 2))
    ledger.update(Delivery(DELIVERY_SUBMITTED, 2))
    ledger.update(Delivery(DELIVERY_ADMITTED, 3))
    ledger.update(Delivery(DELIVERY_SUBMITTED, 3))
    ledger.update(Delivery(DELIVERY_COMPLETED, 3))
    assert ledger.mark_nonterminal_unknown() == [1, 2]
    snap = ledger.snapshot()
    assert snap['unknown'] == 2 and snap['completed'] == 1
    assert snap['residual'] == 0


def test_queue_busoff_and_lost_event_contracts():
    # Queue admission is bounded and fails before an untracked cookie enters
    # the conservation identity.
    bounded = DeliveryLedger(capacity=1)
    bounded.update(Delivery(DELIVERY_ADMITTED, 10))
    try:
        bounded.update(Delivery(DELIVERY_ADMITTED, 11))
    except GatewayProtocolError as exc:
        assert 'full' in str(exc)
    else:
        raise AssertionError('delivery ledger exceeded its fixed capacity')
    assert bounded.snapshot()['admitted'] == 1

    # Bus-off and a missing Tx-event have the same fail-closed accounting
    # result: completion is unknowable, so the frame is never blindly replayed.
    for fault in ('bus_off', 'tx_event_lost'):
        ledger = DeliveryLedger()
        ledger.update(Delivery(DELIVERY_ADMITTED, 20))
        ledger.update(Delivery(DELIVERY_SUBMITTED, 20))
        assert ledger.mark_nonterminal_unknown() == [20], fault
        snap = ledger.snapshot()
        assert snap['unknown'] == 1 and snap['residual'] == 0, fault


def test_loss_duplicate_reorder_and_credit_faults():
    seen = []
    runtime = Runtime(credits=8)
    runtime.register(SERVICE_SERIAL, lambda record: seen.append(record.cookie))
    packets = [Packet(7, sequence, (
        Record(SERVICE_SERIAL, SERIAL_DATA, cookie=sequence, data=b'x'),),
        PACKET_RESET if sequence == 1 else 0)
        for sequence in range(1, 7)]
    runtime.dispatch(packets[0].encode())
    runtime.dispatch(packets[0].encode())       # duplicate: no re-actuation
    runtime.dispatch(packets[2].encode())       # deliberate gap (loss)
    try:
        runtime.dispatch(packets[1].encode())   # late reorder rejected
    except GatewayProtocolError:
        pass
    else:
        raise AssertionError('late reordered packet was accepted')
    runtime.dispatch(packets[3].encode())
    assert seen == [1, 3, 4]
    assert runtime.stats['duplicates'] == 1
    assert runtime.stats['stale_epochs'] == 1

    exhausted = Runtime(credits=1)
    exhausted.register(SERVICE_SERIAL, lambda record: None)
    two = Packet(9, 1, (
        Record(SERVICE_SERIAL, SERIAL_DATA, data=b'a'),
        Record(SERVICE_SERIAL, SERIAL_DATA, data=b'b')), PACKET_RESET)
    try:
        exhausted.dispatch(two.encode())
    except GatewayProtocolError:
        pass
    else:
        raise AssertionError('credit-exhausted packet was accepted')
    assert exhausted.stats['records'] == 0


if __name__ == '__main__':
    test_ack_window_policy()
    test_delivery_conservation_randomized()
    test_reset_marks_inflight_unknown()
    test_queue_busoff_and_lost_event_contracts()
    test_loss_duplicate_reorder_and_credit_faults()
    print('helix_gateway_fault_test: PASS')
