#!/usr/bin/env python3
# Standalone tests for the live Klippy -> Atlas trace JSONL seam.

import json
import os
import pathlib
import sys
import tempfile
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "klippy"))

fake_mcu_module = types.ModuleType("mcu")
fake_mcu_module.get_printer_mcu = (
    lambda printer, name: printer.mcus[name])
sys.modules["mcu"] = fake_mcu_module

from extras import atlas_trace  # noqa: E402

TRACE_DATA_RESPONSE = (
    "trace_data oid=%c seq=%u clock=%u event=%hu sub=%c level=%c data=%*s")
TRACE_STATUS_RESPONSE = (
    "trace_status oid=%c next_seq=%u oldest_seq=%u dropped=%u")


class FakeCommand:
    def __init__(self):
        self.sent = []

    def send(self, values):
        self.sent.append(values)


class FakeMcu:
    def __init__(self, name, trace=True):
        self.name = name
        self.trace = trace
        self.callbacks = []
        self.responses = {}
        self.config_cmds = []
        self.commands = {}

    def create_oid(self):
        return 7

    def get_name(self):
        return self.name

    def register_config_callback(self, callback):
        self.callbacks.append(callback)

    def try_lookup_command(self, message):
        if not self.trace:
            return None
        return self.commands.setdefault(message, FakeCommand())

    def add_config_cmd(self, command):
        self.config_cmds.append(command)

    def alloc_command_queue(self):
        return object()

    def lookup_command(self, message, cq=None):
        command = self.commands.setdefault(message, FakeCommand())
        return command

    def register_serial_response(self, callback, message, oid=None):
        self.responses[(message, oid)] = callback

    def get_enumerations(self):
        return {
            "trace_sub": {"core": 0, "motion": 1},
            "trace_event": {"step_underrun": 1},
        }

    def get_constants(self):
        return {
            "trace_fmt_step_underrun":
                "horizon_us=%u queue_depth=%u",
        }

    def is_fileoutput(self):
        return False

    def clock32_to_clock64(self, clock):
        return clock

    def clock_to_print_time(self, clock):
        return 10. + clock / 1_000_000.


class FakeReactor:
    NOW = 0.
    NEVER = 1e30

    def __init__(self):
        self.timer = None
        self.updated = []

    def register_timer(self, callback):
        self.timer = callback
        return callback

    def update_timer(self, timer, when):
        self.updated.append(when)


class FakeGcode:
    def __init__(self):
        self.commands = {}

    def register_command(self, name, callback, desc=None):
        self.commands[name] = callback


class FakePrinter:
    def __init__(self, mcus):
        self.mcus = mcus
        self.reactor = FakeReactor()
        self.gcode = FakeGcode()
        self.events = {}

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name):
        if name == "gcode":
            return self.gcode
        if name == "mcu":
            return self.mcus["mcu"]
        return self.mcus[name.split(" ", 1)[1]]

    def register_event_handler(self, name, callback):
        self.events[name] = callback


class FakeConfig:
    def __init__(self, printer, output):
        self.printer = printer
        self.values = {
            "output": output,
            "mcus": ("mcu", "ebb36"),
            "motion_level": "info",
        }

    def get_printer(self):
        return self.printer

    def get(self, name, default=None):
        return self.values.get(name, default)

    def getlist(self, name, default=None):
        return self.values.get(name, default)

    def getint(self, name, default=None, minval=None, maxval=None):
        return int(self.values.get(name, default))

    def getfloat(self, name, default=None, above=None):
        return float(self.values.get(name, default))

    def getchoice(self, name, choices, default=None):
        return choices[self.values.get(name, default)]

    def error(self, message):
        return ValueError(message)


def _setup(path, ebb_trace=True):
    mcus = {"mcu": FakeMcu("mcu"),
            "ebb36": FakeMcu("ebb36", trace=ebb_trace)}
    printer = FakePrinter(mcus)
    old_get = atlas_trace.mcu.get_printer_mcu
    atlas_trace.mcu.get_printer_mcu = (
        lambda printer, name: printer.mcus[name])
    try:
        manager = atlas_trace.AtlasTrace(FakeConfig(printer, path))
    finally:
        atlas_trace.mcu.get_printer_mcu = old_get
    for chip in mcus.values():
        for callback in chip.callbacks:
            callback()
    return manager, printer, mcus


def test_configures_and_tolerates_rolling_firmware():
    with tempfile.TemporaryDirectory() as tmp:
        manager, printer, mcus = _setup(
            os.path.join(tmp, "trace.jsonl"), ebb_trace=False)
        manager._ready()
        assert mcus["mcu"].config_cmds == ["config_trace oid=7 size=64"]
        assert manager.links["mcu"].available
        assert not manager.links["ebb36"].available
        levels = mcus["mcu"].commands[
            "trace_set_level sub=%c level=%c"].sent
        assert [1, 2] in levels
        stream = mcus["mcu"].commands[
            "trace_stream oid=%c max_per_wake=%c"].sent
        assert stream == [[7, 4]]
        manager.links["mcu"].emit_test(256)
        assert mcus["mcu"].commands["trace_test count=%hu"].sent == [[256]]
        print("PASS: live collector configures trace and tolerates old MCU")


def test_trace_data_renders_and_writes_common_time():
    with tempfile.TemporaryDirectory() as tmp:
        output = os.path.join(tmp, "trace.jsonl")
        manager, printer, mcus = _setup(output)
        manager._ready()
        callback = mcus["ebb36"].responses[(TRACE_DATA_RESPONSE, 7)]
        callback({
            "oid": 7, "seq": 4, "clock": 2_000_000,
            "event": 1, "sub": 1, "level": 1,
            "data": (1200).to_bytes(4, "little") + (0).to_bytes(4, "little"),
        })
        record = json.loads(pathlib.Path(output).read_text())
        assert record["kind"] == "trace"
        assert record["session_id"] == manager.session_id
        assert record["machine_time"] == 12.
        assert record["source"] == "mcu/ebb36/motion"
        assert record["summary"] == (
            "step_underrun horizon_us=1200 queue_depth=0")
        assert record["severity"] == "warning"
        assert record["fields"]["time_basis"] == "klipper_print_time"
        print("PASS: live record renders onto the common JSONL time axis")


def test_sequence_gaps_and_firmware_drops_are_visible():
    with tempfile.TemporaryDirectory() as tmp:
        manager, printer, mcus = _setup(os.path.join(tmp, "trace.jsonl"))
        manager._ready()
        data_cb = mcus["mcu"].responses[(TRACE_DATA_RESPONSE, 7)]
        base = {"oid": 7, "clock": 1, "event": 1, "sub": 1,
                "level": 2, "data": b""}
        data_cb(dict(base, seq=1))
        data_cb(dict(base, seq=4))
        status_cb = mcus["mcu"].responses[(TRACE_STATUS_RESPONSE, 7)]
        status_cb({"next_seq": 10, "oldest_seq": 6, "dropped": 1})
        status = manager.links["mcu"].get_status()
        assert status["sequence_gaps"] == 2
        assert status["dropped"] == 1
        assert status["unaccounted_gaps"] == 1
        assert status["next_seq"] == 10 and status["oldest_seq"] == 6
        print("PASS: host sequence gaps and firmware drops remain explicit")


def test_execution_and_wire_intention_share_machine_time():
    with tempfile.TemporaryDirectory() as tmp:
        output = os.path.join(tmp, "trace.jsonl")
        manager, printer, mcus = _setup(output)
        manager.record_intention(mcus["mcu"], "stepper_x", 4, {
            "event": "segment", "start_clock": 2_000_000,
            "end_clock": 2_050_000, "duration": 50_000,
            "flags": 0, "velocity": 65536, "accel": 0,
            "start_position_su": 0, "end_position_su": 3276,
        })
        manager.record_execution(
            mcus["mcu"], (12, 1, 4, 2_050_000, 3276, 0))
        manager.record_execution(
            mcus["mcu"], (13, 9, 23, 2_060_000, 0, 0))
        records = [json.loads(line)
                   for line in pathlib.Path(output).read_text().splitlines()]
        assert [r["kind"] for r in records] == [
            "intention", "execution", "execution"]
        assert all(r["session_id"] == manager.session_id for r in records)
        assert records[0]["machine_time"] == 12.
        assert records[0]["fields"]["velocity"] == 65536
        assert records[1]["machine_time"] == 12.05
        assert records[1]["fields"]["event"] == "segment_done"
        assert records[1]["fields"]["position_su"] == 3276
        assert records[2]["fields"]["event"] == "edge_observed"
        assert records[2]["severity"] == "info"
        print("PASS: wire coefficients and execution records share one"
              " machine-time axis")


def main():
    test_configures_and_tolerates_rolling_firmware()
    test_trace_data_renders_and_writes_common_time()
    test_sequence_gaps_and_firmware_drops_are_visible()
    test_execution_and_wire_intention_share_machine_time()
    print("ALL PASS")


if __name__ == "__main__":
    main()
