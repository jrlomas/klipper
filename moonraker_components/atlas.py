# Atlas snapshot bridge for Moonraker (FD-0002 section 7).
#
# Install this file as moonraker/components/atlas.py.  It intentionally does
# not import the Atlas package: the standalone daemon owns facts, while this
# component only validates and exposes its atomic, versioned snapshot.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import time
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

from ..common import RequestType

if TYPE_CHECKING:
    from ..common import WebRequest
    from ..confighelper import ConfigHelper
    from ..eventloop import FlexTimer


SUPPORTED_SCHEMA_VERSION = 1
DEFAULT_MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_ASSISTANT_RESPONSE_BYTES = 4 * 1024 * 1024
ASSISTANT_IPC_SCHEMA_VERSION = 1


def _validate_snapshot(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("snapshot root must be an object")
    if value.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
        raise ValueError(
            "unsupported schema_version %r (expected %d)"
            % (value.get("schema_version"), SUPPORTED_SCHEMA_VERSION))
    timeline = value.get("timeline")
    diagnosis = value.get("diagnosis")
    service = value.get("service")
    if not isinstance(timeline, dict):
        raise ValueError("timeline must be an object")
    if not isinstance(timeline.get("events"), list):
        raise ValueError("timeline.events must be an array")
    if not isinstance(timeline.get("notes"), list):
        raise ValueError("timeline.notes must be an array")
    if not isinstance(diagnosis, dict):
        raise ValueError("diagnosis must be an object")
    if not isinstance(service, dict):
        raise ValueError("service must be an object")
    if not isinstance(service.get("generation"), int):
        raise ValueError("service.generation must be an integer")
    if not isinstance(service.get("updated_at"), (int, float)):
        raise ValueError("service.updated_at must be numeric")
    assistant = value.get("assistant")
    if assistant is not None and not isinstance(assistant, dict):
        raise ValueError("assistant must be an object")
    return value


class AssistantClient:
    """Bounded relay to the daemon-owned local model runtime."""

    def __init__(self, path: pathlib.Path, timeout: float,
                 max_bytes: int = DEFAULT_MAX_ASSISTANT_RESPONSE_BYTES):
        self.path = path
        self.timeout = timeout
        self.max_bytes = max_bytes

    async def request(self, operation: str,
                      params: Dict[str, Any]) -> Dict[str, Any]:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(
                str(self.path), limit=self.max_bytes + 1), self.timeout)
        payload = {
            "schema_version": ASSISTANT_IPC_SCHEMA_VERSION,
            "operation": operation,
            "params": params,
        }
        writer.write((json.dumps(payload, separators=(",", ":"))
                      + "\n").encode("utf-8"))
        await writer.drain()
        try:
            raw = await asyncio.wait_for(reader.readline(), self.timeout)
            if len(raw) > self.max_bytes or not raw.endswith(b"\n"):
                raise RuntimeError("assistant response is missing or too large")
            response = json.loads(raw)
        finally:
            writer.close()
            await writer.wait_closed()
        if not response.get("ok"):
            error = response.get("error", {})
            raise RuntimeError("%s: %s" % (
                error.get("type", "assistant error"),
                error.get("message", "unknown failure")))
        value = response.get("response")
        if not isinstance(value, dict):
            raise RuntimeError("assistant returned an invalid response")
        return value


class SnapshotReader:
    """Size-bound reader that retains the last valid Atlas snapshot."""

    def __init__(self, path: pathlib.Path, max_bytes: int,
                 wall_clock=time.time) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.wall_clock = wall_clock
        self.state: Optional[Dict[str, Any]] = None
        self.signature: Optional[Tuple[int, int, int]] = None
        self.last_read_at: Optional[float] = None
        self.last_error = "snapshot has not been read"

    def read(self) -> bool:
        """Read a changed file. Return True when public bridge state changed."""
        try:
            stat = self.path.stat()
            signature = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
            if signature == self.signature:
                return False
            if stat.st_size > self.max_bytes:
                raise ValueError(
                    "snapshot is %d bytes (limit %d)"
                    % (stat.st_size, self.max_bytes))
            with self.path.open("r", encoding="utf-8") as handle:
                state = _validate_snapshot(json.load(handle))
        except Exception as exc:
            error = "%s: %s" % (type(exc).__name__, exc)
            changed = error != self.last_error
            self.last_error = error
            return changed
        self.state = state
        self.signature = signature
        self.last_read_at = self.wall_clock()
        self.last_error = ""
        return True

    def health(self, stale_after: float) -> Dict[str, Any]:
        now = self.wall_clock()
        updated_at = None
        generation = None
        daemon_state = "unavailable"
        age = None
        if self.state is not None:
            service = self.state["service"]
            updated_at = float(service["updated_at"])
            generation = service["generation"]
            daemon_state = service.get("state", "unknown")
            age = max(0.0, now - updated_at)
        stale = age is None or age > stale_after
        return {
            "available": self.state is not None,
            "healthy": self.state is not None and not stale and not self.last_error,
            "stale": stale,
            "age": age,
            "stale_after": stale_after,
            "generation": generation,
            "daemon_state": daemon_state,
            "last_read_at": self.last_read_at,
            "last_error": self.last_error,
            "state_file": str(self.path),
            "schema_version": SUPPORTED_SCHEMA_VERSION,
        }


class Atlas:
    def __init__(self, config: "ConfigHelper") -> None:
        self.server = config.get_server()
        self.eventloop = self.server.get_event_loop()
        default_path = pathlib.Path("~/.local/state/atlas/status.json")
        state_path = config.getpath("state_file", default_path)
        max_bytes = config.getint(
            "max_snapshot_bytes", DEFAULT_MAX_SNAPSHOT_BYTES,
            above=1024)
        self.poll_interval = config.getfloat(
            "poll_interval", 0.5, minval=0.1)
        self.stale_after = config.getfloat(
            "stale_after", 15.0, above=self.poll_interval)
        self.reader = SnapshotReader(state_path, max_bytes)
        default_socket = state_path.parent / "assistant.sock"
        assistant_socket = config.getpath("assistant_socket", default_socket)
        assistant_timeout = config.getfloat(
            "assistant_timeout", 300.0, above=0.0)
        assistant_max_bytes = config.getint(
            "max_assistant_response_bytes",
            DEFAULT_MAX_ASSISTANT_RESPONSE_BYTES, above=1024)
        self.assistant = AssistantClient(
            assistant_socket, assistant_timeout, assistant_max_bytes)
        self._last_health_key: Optional[Tuple[Any, ...]] = None
        self.poll_timer: "FlexTimer" = self.eventloop.register_timer(
            self._handle_poll)

        self.server.register_endpoint(
            "/server/atlas/status", RequestType.GET,
            self._handle_status)
        self.server.register_endpoint(
            "/server/atlas/incidents", RequestType.GET,
            self._handle_incidents)
        self.server.register_endpoint(
            "/server/atlas/health", RequestType.GET,
            self._handle_health)
        self.server.register_endpoint(
            "/server/atlas/assistant/ask", RequestType.POST,
            self._handle_assistant_ask)
        self.server.register_endpoint(
            "/server/atlas/assistant/interpret", RequestType.POST,
            self._handle_assistant_interpret)
        self.server.register_endpoint(
            "/server/atlas/assistant/propose", RequestType.POST,
            self._handle_assistant_propose)
        self.server.register_notification(
            "atlas:status_update", "atlas_status_update")

    async def component_init(self) -> None:
        await self._poll_and_notify(force=True)
        self.poll_timer.start(self.poll_interval)

    def _health_key(self, health: Dict[str, Any]) -> Tuple[Any, ...]:
        return (
            health["available"], health["healthy"], health["stale"],
            health["generation"], health["last_error"])

    async def _poll_and_notify(self, force: bool = False) -> None:
        changed = await self.eventloop.run_in_thread(self.reader.read)
        health = self.reader.health(self.stale_after)
        health_key = self._health_key(health)
        if force or changed or health_key != self._last_health_key:
            self._last_health_key = health_key
            self.server.send_event(
                "atlas:status_update", self._status_response(health))

    async def _handle_poll(self, eventtime: float) -> float:
        try:
            await self._poll_and_notify()
        except Exception:
            logging.exception("Atlas snapshot poll failed")
        return eventtime + self.poll_interval

    def _status_response(self, health: Optional[Dict[str, Any]] = None
                         ) -> Dict[str, Any]:
        return {
            "status": self.reader.state,
            "bridge": health or self.reader.health(self.stale_after),
        }

    async def _handle_status(self, web_request: "WebRequest"
                             ) -> Dict[str, Any]:
        return self._status_response()

    async def _handle_incidents(self, web_request: "WebRequest"
                                ) -> Dict[str, Any]:
        diagnosis = None
        if self.reader.state is not None:
            diagnosis = self.reader.state.get("diagnosis")
        return {
            "diagnosis": diagnosis,
            "bridge": self.reader.health(self.stale_after),
        }

    async def _handle_health(self, web_request: "WebRequest"
                             ) -> Dict[str, Any]:
        return self.reader.health(self.stale_after)

    async def _handle_assistant_ask(self, web_request: "WebRequest"
                                    ) -> Dict[str, Any]:
        return await self.assistant.request(
            "ask", {"question": web_request.get_str("question")})

    async def _handle_assistant_interpret(self, web_request: "WebRequest"
                                          ) -> Dict[str, Any]:
        return await self.assistant.request(
            "interpret", {"structured": web_request.get_boolean(
                "structured", False)})

    async def _handle_assistant_propose(self, web_request: "WebRequest"
                                        ) -> Dict[str, Any]:
        return await self.assistant.request(
            "propose_config", {"request": web_request.get_str("request")})

    async def close(self) -> None:
        self.poll_timer.stop()
        await self.poll_timer.wait_timer_done()


def load_component(config: "ConfigHelper") -> Atlas:
    return Atlas(config)
