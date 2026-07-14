# Mode-private JSON-lines IPC between the Atlas daemon and Moonraker.

import asyncio
import json
import os
import socket
import stat
import struct


IPC_SCHEMA_VERSION = 1
DEFAULT_MAX_REQUEST_BYTES = 64 * 1024
DEFAULT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class AssistantUnixServer:
    def __init__(self, path, handler,
                 max_request_bytes=DEFAULT_MAX_REQUEST_BYTES,
                 expected_uid=None):
        self.path = os.path.abspath(os.path.expanduser(path))
        self.handler = handler
        self.max_request_bytes = max_request_bytes
        self.expected_uid = (os.geteuid() if expected_uid is None
                             else expected_uid)
        self.server = None

    async def start(self):
        directory = os.path.dirname(self.path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        try:
            mode = os.stat(self.path).st_mode
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(mode):
                raise RuntimeError("refusing to replace non-socket %s"
                                   % self.path)
            os.unlink(self.path)
        self.server = await asyncio.start_unix_server(
            self._handle_client, path=self.path)
        os.chmod(self.path, 0o600)

    async def _handle_client(self, reader, writer):
        response = None
        try:
            peer_socket = writer.get_extra_info("socket")
            if (peer_socket is not None and hasattr(socket, "SO_PEERCRED")
                    and self.expected_uid is not None):
                credentials = peer_socket.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED,
                    struct.calcsize("3i"))
                _pid, uid, _gid = struct.unpack("3i", credentials)
                if uid != self.expected_uid:
                    raise PermissionError("assistant IPC peer uid is not allowed")
            raw = await reader.readline()
            if not raw:
                raise ValueError("empty request")
            if len(raw) > self.max_request_bytes or not raw.endswith(b"\n"):
                raise ValueError("request exceeds %d bytes"
                                 % self.max_request_bytes)
            request = json.loads(raw)
            if request.get("schema_version") != IPC_SCHEMA_VERSION:
                raise ValueError("unsupported IPC schema_version")
            operation = request.get("operation")
            if not isinstance(operation, str):
                raise ValueError("operation must be a string")
            result = await asyncio.to_thread(
                self.handler, operation, request.get("params", {}))
            response = {"ok": True, "response": result}
        except Exception as exc:
            response = {"ok": False, "error": {
                "type": type(exc).__name__, "message": str(exc)}}
        try:
            writer.write((json.dumps(response, separators=(",", ":"))
                          + "\n").encode("utf-8"))
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def close(self):
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


async def request(path, operation, params=None, timeout=300,
                  max_response_bytes=DEFAULT_MAX_RESPONSE_BYTES):
    reader, writer = await asyncio.wait_for(
        asyncio.open_unix_connection(os.path.expanduser(path)), timeout)
    payload = {"schema_version": IPC_SCHEMA_VERSION,
               "operation": operation, "params": params or {}}
    writer.write((json.dumps(payload, separators=(",", ":"))
                  + "\n").encode("utf-8"))
    await writer.drain()
    try:
        raw = await asyncio.wait_for(reader.readline(), timeout)
        if len(raw) > max_response_bytes or not raw.endswith(b"\n"):
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
    return response["response"]
