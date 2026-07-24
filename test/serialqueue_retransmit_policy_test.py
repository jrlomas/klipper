#!/usr/bin/env python3
"""Exercise urgent, buffered, and deadline-limited serialqueue retries."""

import os
import select
import sys
import time
import tty

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'klippy'))
import chelper  # noqa: E402


def _new_queue(name):
    ffi, lib = chelper.get_ffi()
    master, slave = os.openpty()
    tty.setraw(slave)
    sq = ffi.gc(
        lib.serialqueue_alloc(slave, b'u', 0, name),
        lib.serialqueue_free)
    cq = ffi.gc(lib.serialqueue_alloc_commandqueue(),
                lib.serialqueue_free_commandqueue)
    lib.serialqueue_set_retransmit_policy(sq, .025, .100, .020)
    now = lib.get_monotonic()
    lib.serialqueue_set_clock_est(sq, 1_000_000., now, 0, 0)
    return ffi, lib, master, slave, sq, cq


def _read(master, timeout):
    readable, _, _ = select.select([master], [], [], timeout)
    return os.read(master, 4096) if readable else b''


def _stats(ffi, lib, sq):
    buf = ffi.new('char[]', 4096)
    lib.serialqueue_get_stats(sq, buf, 4096)
    return ffi.string(buf).decode()


def _close(lib, master, slave, sq):
    lib.serialqueue_exit(sq)
    os.close(master)
    os.close(slave)


def test_buffered_floor():
    ffi, lib, master, slave, sq, cq = _new_queue(b'buffered-rto')
    try:
        payload = ffi.new('uint8_t[]', b'\x01')
        lib.serialqueue_send_class(
            sq, cq, payload, 1, 0, 0, 0, 1, 500_000)
        assert _read(master, .100), "buffered command was not transmitted"
        assert not _read(master, .060), (
            "buffered command used the legacy 25ms retransmit floor")
        assert _read(master, .100), "buffered command was not retried"
        stats = _stats(ffi, lib, sq)
        assert "retransmit_timeout=1" in stats, stats
        assert "retransmit_buffered=1" in stats, stats
        assert "retransmit_urgent=0" in stats, stats
        assert "buffered_rto=0.100" in stats, stats
        assert " rto=0.025 " in stats, (
            "buffered timeout inflated the urgent adaptive RTO: %s" % stats)
    finally:
        _close(lib, master, slave, sq)


def test_urgent_floor():
    ffi, lib, master, slave, sq, cq = _new_queue(b'urgent-rto')
    try:
        payload = ffi.new('uint8_t[]', b'\x02')
        lib.serialqueue_send(sq, cq, payload, 1, 0, 0, 0)
        assert _read(master, .100), "urgent command was not transmitted"
        started = time.monotonic()
        assert _read(master, .090), "urgent command did not retry promptly"
        assert time.monotonic() - started < .095
        stats = _stats(ffi, lib, sq)
        assert "retransmit_timeout=1" in stats, stats
        assert "retransmit_urgent=1" in stats, stats
        assert "retransmit_buffered=0" in stats, stats
    finally:
        _close(lib, master, slave, sq)


def test_deadline_limits_buffered_floor():
    ffi, lib, master, slave, sq, cq = _new_queue(b'deadline-rto')
    try:
        payload = ffi.new('uint8_t[]', b'\x03')
        # The command is due 60ms from the clock anchor. With a 20ms recovery
        # margin its 100ms buffered floor must be clipped to about 40ms.
        lib.serialqueue_send_class(
            sq, cq, payload, 1, 0, 0, 0, 1, 60_000)
        assert _read(master, .100), "deadline command was not transmitted"
        started = time.monotonic()
        assert _read(master, .085), "deadline did not advance buffered retry"
        assert time.monotonic() - started < .090
    finally:
        _close(lib, master, slave, sq)


def test_urgent_and_buffered_blocks_do_not_mix():
    ffi, lib, master, slave, sq, buffered_cq = _new_queue(b'class-blocks')
    urgent_cq = ffi.gc(lib.serialqueue_alloc_commandqueue(),
                       lib.serialqueue_free_commandqueue)
    try:
        buffered = ffi.new('uint8_t[]', b'\x04')
        urgent = ffi.new('uint8_t[]', b'\x05')
        lib.serialqueue_send_class(
            sq, buffered_cq, buffered, 1, 0, 500_000, 0, 1, 500_000)
        lib.serialqueue_send(
            sq, urgent_cq, urgent, 1, 0, 500_000, 0)
        assert not _read(master, .050), "timed commands released too early"
        lib.serialqueue_set_send_ahead(sq, 1.0)
        stream = _read(master, .100)
        assert len(stream) == 12, stream
        assert stream[0] == 6 and stream[6] == 6, stream
        started = time.monotonic()
        assert _read(master, .090), (
            "urgent block did not pull the cumulative retry window forward")
        assert time.monotonic() - started < .095
        stats = _stats(ffi, lib, sq)
        assert "retransmit_urgent=1" in stats, stats
        assert "retransmit_buffered=0" in stats, stats
    finally:
        _close(lib, master, slave, sq)


def main():
    test_buffered_floor()
    test_urgent_floor()
    test_deadline_limits_buffered_floor()
    test_urgent_and_buffered_blocks_do_not_mix()
    print("PASS: serialqueue retries follow urgency and execution slack")


if __name__ == '__main__':
    main()
