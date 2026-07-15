#!/usr/bin/env python3
"""Exercise an EOF/rebind without replacing the serialqueue or its CQs."""

import os
import select
import sys
import tty

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'klippy'))
import chelper


def main():
    ffi, lib = chelper.get_ffi()
    master, slave = os.openpty()
    tty.setraw(slave)
    sq = ffi.gc(lib.serialqueue_alloc(slave, b'u', 0, b'reconnect-test'),
                lib.serialqueue_free)
    assert sq != ffi.NULL
    cq = ffi.gc(lib.serialqueue_alloc_commandqueue(),
                lib.serialqueue_free_commandqueue)

    # Closing the peer produces EOF and stops the C worker.  Pull returns the
    # same sentinel used by SerialReader's Python consumer.
    os.close(master)
    response = ffi.new('struct pull_queue_message *')
    lib.serialqueue_pull(sq, response)
    assert response.len < 0

    # Work generated after EOF was never transmitted and must not burst into
    # the MCU after reconnect.  A waiting notification receives an explicit
    # local cancellation instead of hanging forever.
    stale = ffi.new('uint8_t[]', b'\x02')
    lib.serialqueue_send(sq, cq, stale, 1, 0, 0, 0)
    stale_query = ffi.new('uint8_t[]', b'\x03')
    lib.serialqueue_send(sq, cq, stale_query, 1, 0, 0, 42)

    # Rebind a fresh tty to the descriptor number retained by serialqueue.
    new_master, new_slave = os.openpty()
    tty.setraw(new_slave)
    os.dup2(new_slave, slave)
    os.close(new_slave)
    assert lib.serialqueue_reconnect(sq) == 1

    lib.serialqueue_pull(sq, response)
    assert response.len == 0 and response.notify_id == 42
    assert response.sent_time == 0. and response.receive_time == 0.

    # A command queue allocated before EOF must still drive the new endpoint.
    payload = ffi.new('uint8_t[]', b'\x01')
    lib.serialqueue_send(sq, cq, payload, 1, 0, 0, 0)
    readable, _, _ = select.select([new_master], [], [], 2.)
    assert readable, "re-armed serialqueue did not transmit"
    frame = os.read(new_master, 64)
    assert frame[2] == 1, "stale command escaped reconnect purge"

    lib.serialqueue_exit(sq)
    os.close(new_master)
    os.close(slave)
    print("PASS: reconnect retains command queues while purging stale work")


if __name__ == '__main__':
    main()
