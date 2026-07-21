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
#   * reactor -> asyncio: loop.call_soon_threadsafe() hands an awaitable
#     factory to the loop thread; ensure_future() starts it there and its
#     done-callback relays the outcome with reactor.async_complete() onto a
#     ReactorCompletion the reactor greenlet waits on.
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

try:
    import asyncio
except ImportError:
    asyncio = None
import logging
import os
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
        self._wakeup_confirmed = threading.Event()
        self._wake_r = self._wake_w = None

    # ---- lifecycle ----
    def start(self):
        if self._running:
            return
        if asyncio is None:
            raise BridgeError("asyncio bridge requires Python 3")
        # The worker creates as well as runs the loop.  In particular, the
        # selector loop's cross-thread wakeup pipe must belong to the thread
        # that dispatches it; creating the loop here and moving it to the
        # worker can leave call_soon_threadsafe() unable to wake Python 3.12.
        self.loop = None
        self._ready.clear()
        self._wakeup_confirmed.clear()
        self._running = True
        self._thread = threading.Thread(target=self._thread_main,
                                        name=self._name)
        self._thread.daemon = True
        self._thread.start()
        if not self._ready.wait(self._start_timeout):
            raise BridgeError("asyncio bridge failed to start")
        # Prove that the loop has processed a callback submitted through its
        # cross-thread wakeup path before exposing it to callers.
        self.loop.call_soon_threadsafe(self._wakeup_confirmed.set)
        self._wake_loop()
        if not self._wakeup_confirmed.wait(self._start_timeout):
            raise BridgeError("asyncio bridge wakeup path failed to start")
        logging.info("asyncio_bridge: event loop thread '%s' started",
                     self._name)

    def _thread_main(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_r, False)
        os.set_blocking(self._wake_w, False)
        loop.add_reader(self._wake_r, self._drain_loop_wake)
        loop.call_soon(self._ready.set)
        try:
            loop.run_forever()
        finally:
            # Cancel anything still pending, then close cleanly.
            try:
                pending = asyncio.all_tasks(loop)
            except RuntimeError:
                pending = set()
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True))
            loop.remove_reader(self._wake_r)
            os.close(self._wake_r)
            os.close(self._wake_w)
            self._wake_r = self._wake_w = None
            loop.close()

    def _wake_loop(self):
        # Do not rely solely on BaseEventLoop's private socketpair.  An
        # explicit selector fd makes every cross-thread submission visible
        # even when Python loses an immediate startup wakeup.
        wake_w = self._wake_w
        if wake_w is None:
            return
        try:
            os.write(wake_w, b'.')
        except (BlockingIOError, OSError):
            pass

    def _drain_loop_wake(self):
        try:
            os.read(self._wake_r, 4096)
        except (BlockingIOError, OSError):
            pass

    def stop(self):
        if not self._running:
            return
        self._running = False
        loop, thread = self.loop, self._thread
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
            self._wake_loop()
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
    def run_coro_factory(self, factory):
        # Run factory() on the bridge loop and schedule the awaitable it
        # returns.  Creating asyncio Futures on their owning loop thread is
        # important for factories that call call_reactor().
        # Returns a ReactorCompletion the reactor greenlet can wait()
        # on; its result is a (ok, value) pair - (True, awaitable_result)
        # or (False, exception). Never raises across the thread
        # boundary; use run_coro_factory_wait() for the raise-on-error form.
        completion = self.reactor.completion()
        if not self._running or self.loop is None:
            completion.complete((False, BridgeError("bridge not running")))
            return completion

        def _relay(fut, completion=completion):
            try:
                self.reactor.async_complete(completion, (True, fut.result()))
            except BaseException as e:  # relay, do not swallow
                self.reactor.async_complete(completion, (False, e))

        def _start():
            try:
                awaitable = factory()
                task = asyncio.ensure_future(awaitable)
                task.add_done_callback(_relay)
            except BaseException as e:
                self.reactor.async_complete(completion, (False, e))
        try:
            self.loop.call_soon_threadsafe(_start)
            self._wake_loop()
        except RuntimeError as e:
            completion.complete((False, BridgeError(str(e))))
            return completion
        return completion

    def run_coro(self, coro):
        # Schedule an already-created coroutine or Future on the loop.
        return self.run_coro_factory(lambda: coro)

    def run_coro_factory_wait(self, factory, waketime=None):
        completion = self.run_coro_factory(factory)
        return self._wait_completion(completion, waketime)

    def run_coro_wait(self, coro, waketime=None):
        # Blocking (reactor-greenlet) convenience: run the coroutine on
        # the bridge and return its result on the reactor, re-raising
        # any exception it raised. Must be called from a reactor
        # greenlet (it pauses via ReactorCompletion.wait()).
        completion = self.run_coro(coro)
        return self._wait_completion(completion, waketime)

    def _wait_completion(self, completion, waketime):
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
                self._wake_loop()
            except BaseException as e:
                loop.call_soon_threadsafe(_set, False, e)
                self._wake_loop()
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

    def run_coro_factory(self, factory):
        return self.bridge.run_coro_factory(factory)

    def run_coro_wait(self, coro, waketime=None):
        return self.bridge.run_coro_wait(coro, waketime)

    def run_coro_factory_wait(self, factory, waketime=None):
        return self.bridge.run_coro_factory_wait(factory, waketime)

    def call_reactor(self, fn):
        return self.bridge.call_reactor(fn)

    @property
    def running(self):
        return self.bridge.running

    def get_loop(self):
        return self.bridge.loop


def load_config(config):
    return PrinterAsyncioBridge(config)
