#!/usr/bin/env python3
# Standalone proof of the asyncio<->reactor bridge seam (FD-0001
# doc 05). Drives klippy's real greenlet reactor plus a live
# AsyncioBridge and exercises the two-way handoff end to end:
#
#   * reactor context -> asyncio: run_coro_wait() runs a coroutine on
#     the bridge loop and returns its result on the reactor greenlet;
#   * asyncio context -> reactor: a coroutine calls call_reactor() and
#     awaits a value computed back in reactor context - proving the
#     round trip closes in both directions;
#   * exceptions propagate across the boundary;
#   * the callbacks actually run on the two different threads.
#
# Run: python3 test/asyncio_bridge_test.py   (needs klippy's chelper
# build; exits 0 on success, non-zero on failure).
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import asyncio
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                "..", "klippy"))

import reactor as reactor_mod  # noqa: E402
from extras.asyncio_bridge import AsyncioBridge  # noqa: E402


def main():
    reactor = reactor_mod.Reactor()
    bridge = AsyncioBridge(reactor, name="bridge-test")
    bridge.start()

    results = {}
    main_thread = threading.current_thread().name

    def test_body(eventtime):
        try:
            # 1) reactor -> asyncio: a coroutine result comes back on
            #    the reactor greenlet.
            async def add(a, b):
                await asyncio.sleep(0)
                results['coro_thread'] = threading.current_thread().name
                return a + b
            assert bridge.run_coro_wait(add(2, 3)) == 5

            # 2) reactor -> asyncio -> reactor: the coroutine hops back
            #    into reactor context via call_reactor and awaits the
            #    reactor-computed value.
            async def uses_reactor():
                def on_reactor(et):
                    results['reactor_cb_thread'] = \
                        threading.current_thread().name
                    return 41
                val = await bridge.call_reactor(on_reactor)
                return val + 1
            assert bridge.run_coro_wait(uses_reactor()) == 42

            # 3) exception propagation across the seam.
            async def boom():
                raise ValueError("kaboom")
            raised = None
            try:
                bridge.run_coro_wait(boom())
            except ValueError as e:
                raised = str(e)
            assert raised == "kaboom", raised

            # 4) many round trips, to shake out any wake/queue races.
            async def spin(n):
                total = 0
                for i in range(n):
                    total += await bridge.call_reactor(lambda et, i=i: i)
                return total
            assert bridge.run_coro_wait(spin(20)) == sum(range(20))

            # 5) An awaitable factory is invoked on the loop thread before it
            #    creates a Future.  failure_recovery uses this exact path to
            #    enter asyncio and then drain execution logs on the reactor.
            def reactor_future():
                results['factory_thread'] = threading.current_thread().name
                return bridge.call_reactor(lambda et: 73)
            assert bridge.run_coro_factory_wait(reactor_future) == 73

            results['ok'] = True
        except BaseException as e:  # record and stop the reactor
            results['error'] = repr(e)
        reactor.end()
        return reactor.NEVER

    reactor.register_callback(test_body)
    reactor.run()
    bridge.stop()

    if not results.get('ok'):
        print("FAIL:", results.get('error', 'unknown'))
        return 1
    # The coroutine and the reactor callback must have run on different
    # threads - proof the loop really lives off the reactor thread.
    assert results['coro_thread'] != main_thread, results
    assert results['factory_thread'] == results['coro_thread'], results
    assert results['reactor_cb_thread'] == main_thread, results
    print("asyncio_bridge_test: two-way handoff ok"
          " (coro on %s, reactor cb on %s)"
          % (results['coro_thread'], results['reactor_cb_thread']))
    return 0


if __name__ == "__main__":
    sys.exit(main())
