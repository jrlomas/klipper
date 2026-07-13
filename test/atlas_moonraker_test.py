#!/usr/bin/env python3
# Standalone tests for Atlas's Moonraker snapshot bridge.

import asyncio
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import types
from enum import IntFlag


ROOT = pathlib.Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "moonraker_components" / "atlas.py"


class RequestType(IntFlag):
    GET = 1
    POST = 2
    DELETE = 4


def _load_component_module():
    moonraker = types.ModuleType("moonraker")
    moonraker.__path__ = []
    components = types.ModuleType("moonraker.components")
    components.__path__ = []
    common = types.ModuleType("moonraker.common")
    common.RequestType = RequestType
    sys.modules["moonraker"] = moonraker
    sys.modules["moonraker.components"] = components
    sys.modules["moonraker.common"] = common
    spec = importlib.util.spec_from_file_location(
        "moonraker.components.atlas", COMPONENT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


atlas_component = _load_component_module()


def _state(generation=1, updated_at=100.0):
    return {
        "schema_version": 1,
        "timeline": {"events": [], "notes": [], "versions": {}},
        "diagnosis": {
            "matched": False, "matches": [], "case": None, "notes": []},
        "service": {
            "state": "running", "generation": generation,
            "updated_at": updated_at},
    }


def _write(path, value):
    with open(path, "w") as handle:
        json.dump(value, handle)


def test_snapshot_validation_and_staleness():
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "status.json"
        now = [105.0]
        _write(path, _state())
        reader = atlas_component.SnapshotReader(
            path, 1024 * 1024, wall_clock=lambda: now[0])
        assert reader.read() is True
        assert reader.read() is False
        health = reader.health(15.0)
        assert health["healthy"] is True
        assert health["age"] == 5.0
        now[0] = 116.0
        health = reader.health(15.0)
        assert health["stale"] is True
        assert health["healthy"] is False
        print("PASS: bridge validates a snapshot and reports daemon staleness")


def test_bad_update_retains_last_good_state():
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "status.json"
        _write(path, _state(generation=7))
        reader = atlas_component.SnapshotReader(path, 1024 * 1024)
        reader.read()
        bad = _state(generation=8)
        bad["schema_version"] = 99
        _write(path, bad)
        assert reader.read() is True
        assert reader.state["service"]["generation"] == 7
        assert "unsupported schema_version" in reader.last_error
        assert reader.health(15.0)["healthy"] is False
        print("PASS: corrupt/incompatible updates never replace "
              "last-good facts")


def test_size_limit():
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "status.json"
        path.write_text("x" * 2048)
        reader = atlas_component.SnapshotReader(path, 1024)
        assert reader.read() is True
        assert reader.state is None
        assert "limit 1024" in reader.last_error
        print("PASS: bridge bounds untrusted snapshot size")


class FakeTimer:
    def __init__(self, callback):
        self.callback = callback
        self.started = None
        self.stopped = False

    def start(self, delay=0.0):
        self.started = delay

    def stop(self):
        self.stopped = True

    async def wait_timer_done(self):
        pass


class FakeEventLoop:
    def __init__(self):
        self.timer = None

    def register_timer(self, callback):
        self.timer = FakeTimer(callback)
        return self.timer

    async def run_in_thread(self, callback, *args):
        return callback(*args)


class FakeServer:
    def __init__(self):
        self.loop = FakeEventLoop()
        self.endpoints = {}
        self.notifications = []
        self.events = []

    def get_event_loop(self):
        return self.loop

    def register_endpoint(self, path, method, callback):
        self.endpoints[path] = (method, callback)

    def register_notification(self, event, name):
        self.notifications.append((event, name))

    def send_event(self, event, value):
        self.events.append((event, value))


class FakeConfig:
    def __init__(self, server, path):
        self.server = server
        self.path = pathlib.Path(path)

    def get_server(self):
        return self.server

    def getpath(self, option, default):
        return self.path

    def getint(self, option, default, **kwargs):
        return default

    def getfloat(self, option, default, **kwargs):
        return default


class FakeWebRequest:
    def __init__(self, values):
        self.values = values

    def get_str(self, name):
        return self.values[name]

    def get_boolean(self, name, default=False):
        return self.values.get(name, default)


def test_component_api_and_notifications():
    async def exercise():
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "status.json"
            _write(path, _state())
            server = FakeServer()
            component = atlas_component.Atlas(FakeConfig(server, path))
            now = [105.0]
            component.reader.wall_clock = lambda: now[0]
            assert set(server.endpoints) == {
                "/server/atlas/status", "/server/atlas/incidents",
                "/server/atlas/health", "/server/atlas/assistant/ask",
                "/server/atlas/assistant/interpret",
                "/server/atlas/assistant/propose"}
            assert server.notifications == [
                ("atlas:status_update", "atlas_status_update")]
            await component.component_init()
            assert server.loop.timer.started == 0.5
            assert len(server.events) == 1
            payload = await server.endpoints[
                "/server/atlas/status"][1](None)
            assert payload["status"]["schema_version"] == 1
            assert payload["bridge"]["healthy"] is True
            # An unchanged, healthy poll does not spam websocket clients.
            await component._handle_poll(10.0)
            assert len(server.events) == 1
            # Crossing the stale boundary is a meaningful status transition.
            now[0] = 116.0
            assert await component._handle_poll(11.0) == 11.5
            assert len(server.events) == 2
            assert server.events[-1][1]["bridge"]["stale"] is True
            incidents = await server.endpoints[
                "/server/atlas/incidents"][1](None)
            assert incidents["diagnosis"]["matched"] is False
            await component.close()
            assert server.loop.timer.stopped is True
    asyncio.run(exercise())
    print("PASS: Moonraker endpoints and websocket transitions are stable")


def test_assistant_endpoints_are_thin_relays():
    async def exercise():
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "status.json"
            server = FakeServer()
            component = atlas_component.Atlas(FakeConfig(server, path))
            calls = []

            async def relay(operation, params):
                calls.append((operation, params))
                return {"operation": operation, "result": {}}

            component.assistant.request = relay
            ask = await server.endpoints[
                "/server/atlas/assistant/ask"][1](
                    FakeWebRequest({"question": "why?"}))
            assert ask["operation"] == "ask"
            await server.endpoints[
                "/server/atlas/assistant/interpret"][1](
                    FakeWebRequest({"structured": True}))
            await server.endpoints[
                "/server/atlas/assistant/propose"][1](
                    FakeWebRequest({"request": "rename a macro"}))
            assert calls == [
                ("ask", {"question": "why?"}),
                ("interpret", {"structured": True}),
                ("propose_config", {"request": "rename a macro"}),
            ]
    asyncio.run(exercise())
    print("PASS: assistant APIs relay only typed requests to the daemon")


def main():
    test_snapshot_validation_and_staleness()
    test_bad_update_retains_last_good_state()
    test_size_limit()
    test_component_api_and_notifications()
    test_assistant_endpoints_are_thin_relays()
    print("ALL PASS")


if __name__ == "__main__":
    main()
