#!/usr/bin/env python3
# Standalone unit test for the Atlas memory + RAG formats (FD-0002 §6,§7;
# Milestone C). Checks the per-machine memory file round-trips
# losslessly and journals applied changes, and that the RAG index (with a
# deterministic token-hash retriever) retrieves relevant grounding.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
import stat
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                ".."))

from atlas.apply import ApplyPipeline, Proposal  # noqa: E402
from atlas.diagnosis import load_patterns  # noqa: E402
from atlas.memory import (MachineMemory, MachineMemoryStore, RagIndex,  # noqa
                          TokenHashEmbedder, kb_documents)

CFG = ("[printer]\nmax_velocity: 300\n\n"
       "[gcode_macro X]\ndescription: hi\n")


def test_memory_round_trip():
    m = MachineMemory(machine_id="opaque-token-abc", created="2026-07-12")
    m.add_quirk("bed is 3mm low at the front-left")
    m.set_baseline("idle", {"crc_rate": 0.0, "sync_err_us": 2})
    m.record_diagnosis("da8c80e8f3de1166", "heater not heating")
    back = MachineMemory.from_json(m.to_json())
    assert back.to_dict() == m.to_dict()          # lossless
    assert back.machine_id == "opaque-token-abc"
    assert back.baselines["idle"]["sync_err_us"] == 2
    print("PASS: machine memory round-trips losslessly through JSON")


def test_memory_records_applied_change():
    m = MachineMemory(machine_id="x")
    pipe = ApplyPipeline()
    after = CFG.replace("max_velocity: 300", "max_velocity: 250")
    res = pipe.process(Proposal(CFG, after))       # consequential, applied
    assert res.applied
    rec = m.record_change(res.entry)
    assert m.changes and rec["action"] == res.action
    assert rec["changes"][0]["section"] == "printer"
    # And it survives serialization.
    assert MachineMemory.from_json(m.to_json()).changes == m.changes
    print("PASS: an applied change is journaled into memory and persists")


def test_add_quirk_is_idempotent():
    m = MachineMemory(machine_id="x")
    m.add_quirk("q")
    m.add_quirk("q")
    assert m.quirks == ["q"]
    print("PASS: duplicate quirks are not double-recorded")


def test_private_atomic_memory_store():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "memory.json")
        store = MachineMemoryStore(path, wall_clock=lambda: 100.0)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        machine_id = store.memory.machine_id
        diagnosis = SimpleNamespace(
            best=SimpleNamespace(pattern_id="timer", cause="missed deadline"),
            case=None)
        assert store.record_diagnosis(diagnosis) is True
        assert store.record_diagnosis(diagnosis) is False
        assert store.sync_baselines({"mcu.error_us": {
            "count": 5, "mean": 2.0, "m2": 0.5}}) is True
        reopened = MachineMemoryStore(path)
        assert reopened.memory.machine_id == machine_id
        assert len(reopened.memory.diagnoses) == 1
        assert reopened.memory.baselines["monitor"][
            "mcu.error_us"]["count"] == 5
        assert not [name for name in os.listdir(tmp) if name.endswith(".tmp")]
    print("PASS: daemon memory is private, atomic, durable, and deduplicated")


def test_token_hash_embedder_is_deterministic():
    e = TokenHashEmbedder()
    v1, v2 = e.embed("timer too close"), e.embed("timer too close")
    assert v1 == v2                                # stable across calls
    assert abs(sum(x * x for x in v1) - 1.0) < 1e-9  # L2-normalized
    assert e.name == "token-hash-v1"
    print("PASS: token-hash retriever is deterministic and normalized")


def test_rag_retrieves_relevant_pattern():
    patterns = load_patterns([
        {"id": "mcu-timer-too-close",
         "signature": {"fault_class": ["timer_too_close"]},
         "cause": "host overload — a timer deadline passed",
         "fix": "reduce host CPU load"},
        {"id": "flaky-wire-retransmits",
         "signature": {"event_kind": ["stats"]},
         "cause": "marginal comms link, high retransmits",
         "fix": "reseat the CAN cable"},
    ])
    index = RagIndex().build(kb_documents(patterns=patterns))
    assert len(index) == 2
    hits = index.query("why did I get a timer too close error", k=1)
    assert hits and hits[0][0].id == "pattern:mcu-timer-too-close"
    print("PASS: RAG retrieves the timer pattern for a timer question")


def test_rag_grounds_on_machine_memory():
    m = MachineMemory(machine_id="x")
    m.add_quirk("the CAN cable to the toolhead is marginal and flaky")
    index = RagIndex().build(kb_documents(memory=m))
    hits = index.query("flaky cable problems", k=1)
    assert hits and hits[0][0].source == "memory"
    m.set_baseline("monitor", {"mcu.error_us": {"mean": 22.0}})
    index = RagIndex().build(kb_documents(memory=m))
    hits = index.query("machine baseline error_us", k=2)
    assert any(hit.id == "baseline:monitor" for hit, _ in hits)
    print("PASS: RAG grounds on the machine's own memory (quirks)")


def test_rag_query_orders_by_relevance():
    patterns = load_patterns([
        {"id": "thermal-runaway",
         "signature": {"event_kind": ["heater_fault"]},
         "cause": "heater not reaching target temperature",
         "fix": "check thermistor and heater cartridge"},
        {"id": "endstop-bounce",
         "signature": {"event_kind": ["stats"]},
         "cause": "endstop switch bounces during homing",
         "fix": "add a homing retract"},
    ])
    index = RagIndex().build(kb_documents(patterns=patterns))
    hits = index.query("heater temperature thermistor problem", k=2)
    assert hits[0][0].id == "pattern:thermal-runaway"
    assert hits[0][1] >= hits[-1][1]               # sorted by score
    print("PASS: RAG orders results by relevance score")


def main():
    test_memory_round_trip()
    test_memory_records_applied_change()
    test_add_quirk_is_idempotent()
    test_private_atomic_memory_store()
    test_token_hash_embedder_is_deterministic()
    test_rag_retrieves_relevant_pattern()
    test_rag_grounds_on_machine_memory()
    test_rag_query_orders_by_relevance()
    print("ALL PASS")


if __name__ == "__main__":
    main()
