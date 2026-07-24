#!/usr/bin/env python3
"""Prove a per-link send horizon releases timed frames early."""

import os
import select
import sys
import tty

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'klippy'))
import chelper  # noqa: E402


def main():
    ffi, lib = chelper.get_ffi()
    master, slave = os.openpty()
    tty.setraw(slave)
    sq = ffi.gc(
        lib.serialqueue_alloc(slave, b'u', 0, b'send-ahead-test'),
        lib.serialqueue_free)
    cq = ffi.gc(lib.serialqueue_alloc_commandqueue(),
                lib.serialqueue_free_commandqueue)
    try:
        now = lib.get_monotonic()
        frequency = 1_000_000.
        lib.serialqueue_set_clock_est(sq, frequency, now, 0, 0)
        payload = ffi.new('uint8_t[]', b'\x01')
        # The command is due 500ms from the estimate anchor.  Stock's 100ms
        # window must keep it off the wire at first.
        lib.serialqueue_send(
            sq, cq, payload, 1, 0, int(.500 * frequency), 0)
        readable, _, _ = select.select([master], [], [], .050)
        if readable:
            early = os.read(master, 64)
            raise AssertionError(
                "default serial horizon released frame early: %r" % (early,))
        # Expanding this link to one second must wake serialqueue and make the
        # already queued command eligible immediately.
        lib.serialqueue_set_send_ahead(sq, 1.0)
        readable, _, _ = select.select([master], [], [], .250)
        assert readable, "expanded send horizon did not release timed frame"
        frame = os.read(master, 64)
        assert frame[2] == 1
    finally:
        lib.serialqueue_exit(sq)
        os.close(master)
        os.close(slave)
    print("PASS: per-link send horizon physically releases timed work")


if __name__ == '__main__':
    main()
