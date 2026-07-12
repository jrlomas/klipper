# The asyncio <-> greenlet-reactor bridge seam (FD-0001 doc 05).
#
# Doc 05 is explicit that klippy's bespoke greenlet reactor is NOT
# rewritten up front: the legacy motion path keeps running on it, and
# new asyncio-native components (the segment emitter's owner, the link
# transport of doc 07, the failure-recovery orchestration of doc 08)
# bridge to it "at a single documented seam." This module IS that seam.
#
# It runs one asyncio event loop in a dedicated daemon thread alongside
# the reactor and exposes a small, thread-safe, two-way handoff:
#
#   reactor  --run_coro(coro)-->  asyncio        (schedule a coroutine
#       from reactor context; get its result back on the reactor)
#   asyncio  --call_reactor(fn)-->  reactor       (run a reactor
#       callback from asyncio context; await its result on the loop)
#
# Both directions use only documented primitives:
#   * reactor -> asyncio: asyncio.run_coroutine_threadsafe() hands the
#     coroutine to the loop thread; its concurrent.futures.Future
#     done-callback relays the outcome back with reactor.async_complete()
#     onto a ReactorCompletion the reactor greenlet waits on.
#   * asyncio -> reactor: reactor.register_async_callback() (klippy's
#     own cross-thread wake, pipe + async queue) runs fn in reactor
#     context; loop.call_soon_threadsafe() resolves the asyncio Future
#     with the result back on the loop thread.
#
# Nothing here touches reactor.py or its greenlet machinery; the reactor
# is used exactly as a well-behaved external caller would. The seam is
# meant to shrink the reactor over time (doc 05, "strangling it is how
# they ship"), not to replace it in place.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import asyncio
import logging
import threading


class BridgeError(Exception):
    pass


class AsyncioBridge:
    # The transport-agnostic core: a reactor plus a background asyncio
    # loop, and the two-way handoff between them. Constructed with just
    # a reactor so it is unit-testable without a full printer (see
    # test_asyncio_bridge.py); the klippy component below wraps it and
    # ties its lifecycle to klippy:connect / klippy:disconnect.
    def __init__(self, reactor, name="asyncio_bridge",
                 start_timeout=5., stop_timeout=5.):
        self.reactor = reactor
        self._name = name
        self._start_timeout = start_timeout
        self._stop_timeout = stop_timeout
        self.loop = None
        self._thread = None
        self._running = False
        self._ready = threading.Event()

    # ---- lifecycle ----
    def start(self):
        if self._running:
            return
        # Create the loop in this (reactor/main) thread but run it only
        # in the worker thread - a loop may be created anywhere and run
        # in exactly one thread.
        self.loop = asyncio.new_event_loop()
        self._ready.clear()
        self._running = True
        self._thread = threading.Thread(target=self._thread_main,
                                        name=self._name, daemon=True)
        self._thread.start()
        if not self._ready.wait(self._start_timeout):
            raise BridgeError("asyncio bridge failed to start")
        logging.info("asyncio_bridge: event loop thread '%s' started",
                     self._name)

    def _thread_main(self):
        asyncio.set_event_loop(self.loop)
        self.loop.call_soon(self._ready.set)
        try:
            self.loop.run_forever()
        finally:
            # Cancel anything still pending, then close cleanly.
            try:
                pending = asyncio.all_tasks(self.loop)
            except RuntimeError:
                pending = set()
            for task in pending:
                task.cancel()
            if pending:
                self.loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True))
            self.loop.close()

    def stop(self):
        if not self._running:
            return
        self._running = False
        loop, thread = self.loop, self._thread
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(self._stop_timeout)
            if thread.is_alive():
                logging.warning("asyncio_bridge: loop thread did not exit")
        self._thread = None
        logging.info("asyncio_bridge: event loop thread '%s' stopped",
                     self._name)

    @property
    def running(self):
        return self._running

    # ---- reactor context -> asyncio ----
    def run_coro(self, coro):
        # Schedule `coro` on the bridge loop from reactor context.
        # Returns a ReactorCompletion the reactor greenlet can wait()
        # on; its result is a (ok, value) pair - (True, coro_result)
        # or (False, exception). Never raises across the thread
        # boundary; use run_coro_wait() for the raise-on-error form.
        completion = self.reactor.completion()
        if not self._running or self.loop is None:
            completion.complete((False, BridgeError("bridge not running")))
            return completion

        def _relay(fut, completion=completion):
            try:
                self.reactor.async_complete(completion, (True, fut.result()))
            except BaseException as e:  # relay, do not swallow
                self.reactor.async_complete(completion, (False, e))
        try:
            cfut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        except RuntimeError as e:
            completion.complete((False, BridgeError(str(e))))
            return completion
        cfut.add_done_callback(_relay)
        return completion

    def run_coro_wait(self, coro, waketime=None):
        # Blocking (reactor-greenlet) convenience: run the coroutine on
        # the bridge and return its result on the reactor, re-raising
        # any exception it raised. Must be called from a reactor
        # greenlet (it pauses via ReactorCompletion.wait()).
        completion = self.run_coro(coro)
        if waketime is None:
            outcome = completion.wait()
        else:
            outcome = completion.wait(waketime, (False, BridgeError("timeout")))
        ok, value = outcome
        if not ok:
            if isinstance(value, BaseException):
                raise value
            raise BridgeError(str(value))
        return value

    # ---- asyncio context -> reactor ----
    def call_reactor(self, fn):
        # Run reactor callback fn(eventtime) in reactor context from an
        # asyncio coroutine, and return an asyncio.Future (resolved on
        # the loop thread) with fn's return value. Call this from the
        # bridge loop thread (i.e. inside a coroutine).
        loop = self.loop
        afut = loop.create_future()

        def _set(ok, value):
            if afut.done():
                return
            if ok:
                afut.set_result(value)
            else:
                afut.set_exception(value)

        def _on_reactor(eventtime):
            try:
                res = fn(eventtime)
                loop.call_soon_threadsafe(_set, True, res)
            except BaseException as e:
                loop.call_soon_threadsafe(_set, False, e)
            # This runs inside a one-shot ReactorCallback; its return
            # value feeds an internal completion nobody waits on.
            return self.reactor.NEVER

        self.reactor.register_async_callback(_on_reactor)
        return afut


class PrinterAsyncioBridge:
    # klippy component wrapper: owns one AsyncioBridge and ties it to
    # the printer lifecycle. Other extras get it with
    #   bridge = self.printer.load_object(config, 'asyncio_bridge')
    # (or lookup_object) and use run_coro / run_coro_wait / call_reactor.
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        start_timeout = config.getfloat('start_timeout', 5., above=0.)
        stop_timeout = config.getfloat('stop_timeout', 5., above=0.)
        self.bridge = AsyncioBridge(self.reactor, start_timeout=start_timeout,
                                    stop_timeout=stop_timeout)
        self.printer.register_event_handler("klippy:connect",
                                            self._handle_connect)
        self.printer.register_event_handler("klippy:disconnect",
                                            self._handle_disconnect)

    def _handle_connect(self):
        self.bridge.start()

    def _handle_disconnect(self):
        self.bridge.stop()

    # ---- delegated public API ----
    def run_coro(self, coro):
        return self.bridge.run_coro(coro)

    def run_coro_wait(self, coro, waketime=None):
        return self.bridge.run_coro_wait(coro, waketime)

    def call_reactor(self, fn):
        return self.bridge.call_reactor(fn)

    @property
    def running(self):
        return self.bridge.running

    def get_loop(self):
        return self.bridge.loop


def load_config(config):
    return PrinterAsyncioBridge(config)
