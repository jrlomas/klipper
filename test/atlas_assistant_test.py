#!/usr/bin/env python3
# Standalone tests for Atlas's local assistant runtime and private IPC.

import asyncio
import os
import stat
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                ".."))

from atlas.assistant import AssistantRuntime  # noqa: E402
from atlas.decode import decode_klippy_log  # noqa: E402
from atlas.diagnosis import load_pattern  # noqa: E402
from atlas.ipc import AssistantUnixServer, request  # noqa: E402
from atlas.memory import MachineMemory  # noqa: E402
from atlas.model import StubBackend  # noqa: E402


PATTERN = load_pattern({
    "id": "timer-too-close",
    "signature": {"fault_class": ["timer_too_close"]},
    "cause": "The host missed a timer deadline.",
    "fix": "Check host load and transport latency.",
    "confidence": 0.9,
})
TIMELINE = decode_klippy_log(
    "Start printer at X (100.0 5.0)\n"
    "MCU 'mcu' shutdown: Timer too close\n")
CONFIG = ("[printer]\nkinematics: corexy\nmax_velocity: 300\n\n"
          "[extruder]\nmax_temp: 280\n")


def _runtime(config_path=None, now=None):
    backend = StubBackend(
        default="The MCU stopped after a timer deadline was missed.",
        tool_calls=[{"name": "propose_config_edit", "arguments": {
            "rationale": "Lower the hotend ceiling.",
            "after_config": CONFIG.replace("max_temp: 280", "max_temp: 270"),
        }}])
    memory = MachineMemory("opaque")
    memory.add_quirk("This machine uses a long USB cable.")
    return AssistantRuntime(
        backend, [PATTERN], memory, config_path=config_path,
        allow_stub=True, wall_clock=(lambda: now[0]) if now else None,
        proposal_ttl=10)


def test_grounded_read_only_requests():
    runtime = _runtime()
    answer = runtime.handle("ask", {"question": "Why did it stop?"},
                            TIMELINE)
    assert answer["schema_version"] == 1
    assert answer["result"]["read_only"] is True
    assert "timer deadline" in answer["result"]["answer"]
    call = runtime.backend.calls[-1]
    assert "Timer too close" in call["prompt"]
    assert "long USB cable" in call["prompt"]
    interpretation = runtime.handle("interpret", {}, TIMELINE)
    assert interpretation["result"]["read_only"] is True
    assert runtime.status()["request_count"] == 2
    print("PASS: questions and interpretations are read-only and grounded")


def test_config_proposal_is_classified_not_applied():
    with tempfile.TemporaryDirectory() as tmp:
        config = os.path.join(tmp, "printer.cfg")
        with open(config, "w") as handle:
            handle.write(CONFIG)
        now = [100.0]
        runtime = _runtime(config, now)
        response = runtime.handle(
            "propose_config", {"request": "lower max_temp to 270"},
            TIMELINE)
        proposal = response["result"]["proposal"]
        assert proposal["tier"] == "safety"
        assert proposal["needs_confirmation"] is True
        assert proposal["applied"] is False
        assert proposal["changes"][0]["key"] == "max_temp"
        with open(config) as handle:
            assert handle.read() == CONFIG
        assert runtime.get_proposal(proposal["proposal_id"])
        now[0] = 111.0
        try:
            runtime.get_proposal(proposal["proposal_id"])
        except ValueError as exc:
            assert "expired" in str(exc)
        else:
            raise AssertionError("expired proposal remained usable")
        print("PASS: model edits become expiring deterministic previews")


def test_invalid_and_stub_production_guards():
    try:
        AssistantRuntime(StubBackend())
    except RuntimeError as exc:
        assert "stub" in str(exc)
    else:
        raise AssertionError("production runtime accepted a stub")
    runtime = _runtime()
    for params in ({"question": ""}, {"question": "x" * 4097}):
        try:
            runtime.handle("ask", params, TIMELINE)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid assistant question was accepted")
    print("PASS: production stub and unbounded prompt inputs are refused")


def test_private_unix_ipc_round_trip():
    async def exercise():
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "assistant.sock")
            runtime = _runtime()
            server = AssistantUnixServer(
                path, lambda op, params: runtime.handle(op, params, TIMELINE))
            await server.start()
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
            response = await request(
                path, "ask", {"question": "What happened?"}, timeout=2)
            assert response["operation"] == "ask"
            assert response["result"]["read_only"] is True
            try:
                await request(path, "delete_printer", {}, timeout=2)
            except RuntimeError as exc:
                assert "unsupported operation" in str(exc)
            else:
                raise AssertionError("unsupported IPC operation succeeded")
            await server.close()
            assert not os.path.exists(path)
    asyncio.run(exercise())
    print("PASS: mode-private bounded IPC relays success and safe errors")


def main():
    test_grounded_read_only_requests()
    test_config_proposal_is_classified_not_applied()
    test_invalid_and_stub_production_guards()
    test_private_unix_ipc_round_trip()
    print("ALL PASS")


if __name__ == "__main__":
    main()
