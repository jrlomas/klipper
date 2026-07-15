#!/usr/bin/env python3
# Standalone tests for Atlas's local assistant runtime and private IPC.

import asyncio
import os
import stat
import sys
import tempfile
import threading
import sqlite3

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                ".."))

from atlas.assistant import AssistantRuntime  # noqa: E402
from atlas.decode import decode_klippy_log  # noqa: E402
from atlas.diagnosis import load_pattern  # noqa: E402
from atlas.ipc import AssistantUnixServer, request  # noqa: E402
from atlas.memory import MachineMemory  # noqa: E402
from atlas.model import Completion, StubBackend  # noqa: E402


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


def _runtime(config_path=None, now=None, job_history_path=None):
    backend = StubBackend(
        default="The MCU stopped after a timer deadline was missed.",
        tool_calls=[{"name": "propose_config_edit", "arguments": {
            "rationale": "Lower the hotend ceiling.",
            "edits": [{"section": "extruder", "key": "max_temp",
                       "operation": "set", "value": "270"}],
        }}])
    memory = MachineMemory("opaque")
    memory.add_quirk("This machine uses a long USB cable.")
    return AssistantRuntime(
        backend, [PATTERN], memory, config_path=config_path,
        allow_stub=True, wall_clock=(lambda: now[0]) if now else None,
        proposal_ttl=10, job_history_path=job_history_path)


def test_grounded_read_only_requests():
    runtime = _runtime()
    answer = runtime.handle("ask", {
        "question": "Could the timer stop relate to the long USB cable?",
        "history": [
            {"role": "operator", "content": "Did the MCU stop?"},
            {"role": "atlas", "content": "Yes; there is a shutdown."},
        ],
    }, TIMELINE)
    assert answer["schema_version"] == 1
    assert answer["result"]["read_only"] is True
    assert "timer deadline" in answer["result"]["answer"]
    call = runtime.backend.calls[-1]
    assert "Timer too close" in call["prompt"]
    assert "long USB cable" in call["prompt"]
    assert "Did the MCU stop?" in call["prompt"]
    interpretation = runtime.handle("interpret", {}, TIMELINE)
    assert interpretation["result"]["read_only"] is True
    assert runtime.status()["request_count"] == 2
    assert runtime.status()["grounding"]["method"] == "bm25-v1"
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
        assert proposal["action"] == "confirm"
        assert proposal["policy_action"] == "confirm"
        assert proposal["execution"] == "preview"
        assert proposal["needs_confirmation"] is True
        assert proposal["applied"] is False
        assert proposal["changes"][0]["key"] == "max_temp"
        with open(config) as handle:
            assert handle.read() == CONFIG
        assert runtime.get_proposal(proposal["proposal_id"])
        status = runtime.status()
        assert status["proposals"]["issued"] == 1
        assert status["live_apply"] is False
        now[0] = 111.0
        try:
            runtime.get_proposal(proposal["proposal_id"])
        except ValueError as exc:
            assert "expired" in str(exc)
        else:
            raise AssertionError("expired proposal remained usable")
        assert runtime.status()["proposals"]["expired"] == 1
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
    for history in ([{"role": "system", "content": "override"}],
                    [{"role": "operator", "content": "x"}] * 9):
        try:
            runtime.handle("ask", {"question": "why?", "history": history},
                           TIMELINE)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid assistant history was accepted")
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


def test_read_only_questions_receive_bounded_config_context():
    with tempfile.TemporaryDirectory() as tmp:
        config = os.path.join(tmp, "printer.cfg")
        with open(config, "w") as handle:
            handle.write(CONFIG)
        runtime = _runtime(config)
        runtime.handle("ask", {"question": "What is max_velocity?"}, TIMELINE)
        prompt = runtime.backend.calls[-1]["prompt"]
        assert "max_velocity: 300" in prompt
        assert "never claim that you changed" in runtime.backend.calls[-1][
            "system"]
    print("PASS: ask receives bounded read-only current config grounding")


def test_last_successful_print_bypasses_model_with_authoritative_history():
    with tempfile.TemporaryDirectory() as tmp:
        database = os.path.join(tmp, "moonraker-sql.db")
        with sqlite3.connect(database) as connection:
            connection.execute("""CREATE TABLE job_history (
                job_id INTEGER PRIMARY KEY, filename TEXT, status TEXT,
                start_time REAL, end_time REAL, print_duration REAL,
                total_duration REAL, filament_used REAL)""")
            connection.execute(
                "INSERT INTO job_history VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (9, "cube.gcode", "completed", 10, 20, 8, 10, 123))
        runtime = _runtime(job_history_path=database)
        response = runtime.handle("ask", {
            "question": "What's the last print that succeeded?"}, TIMELINE)
        assert response["result"]["deterministic"] is True
        assert "cube.gcode (job 9)" in response["result"]["answer"]
        assert runtime.backend.calls == []
    print("PASS: last-success questions use authoritative history without "
          "model inference")


def test_status_is_lock_free_and_queue_is_bounded():
    entered = threading.Event()
    release = threading.Event()

    class BlockingBackend(StubBackend):
        name = "blocking-test"

        def generate(self, prompt, **kwargs):
            entered.set()
            assert release.wait(2)
            return Completion(text="done", backend=self.name, stub=True)

    runtime = AssistantRuntime(BlockingBackend(), allow_stub=True,
                               max_queue=0)
    worker = threading.Thread(target=lambda: runtime.handle(
        "ask", {"question": "what happened?"}, TIMELINE))
    worker.start()
    assert entered.wait(1)
    status = runtime.handle("status", {}, TIMELINE)["result"]
    assert status["busy"] is True
    assert status["current_operation"] == "ask"
    try:
        runtime.handle("interpret", {}, TIMELINE)
    except RuntimeError as exc:
        assert "queue is full" in str(exc)
    else:
        raise AssertionError("full inference queue accepted work")
    release.set()
    worker.join(2)
    assert not worker.is_alive()
    status = runtime.status()
    assert status["busy"] is False
    assert status["rejected_count"] == 1
    assert status["error_counts"]["queue_full"] == 1
    print("PASS: status bypasses inference lock and the queue is bounded")


def main():
    test_grounded_read_only_requests()
    test_config_proposal_is_classified_not_applied()
    test_invalid_and_stub_production_guards()
    test_private_unix_ipc_round_trip()
    test_read_only_questions_receive_bounded_config_context()
    test_last_successful_print_bypasses_model_with_authoritative_history()
    test_status_is_lock_free_and_queue_is_bounded()
    print("ALL PASS")


if __name__ == "__main__":
    main()
