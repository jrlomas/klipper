#!/usr/bin/env python3
# Standalone unit test for the Atlas model tier (FD-0002 §2; Milestone C
# prep). Checks the ModelBackend abstraction (stub always available;
# selection falls back safely) and — the load-bearing part — the deploy
# profile budget guard that keeps development honest to the ~6 GB /
# Qwen3-4B deploy budget even on a big dev card.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                ".."))

from atlas.model import (DEPLOY, DEV, BudgetError, ModelPinError,  # noqa
                         StubBackend, estimate_memory_mb, select_backend)


def test_deploy_pins_qwen3_4b():
    est = DEPLOY.check("Qwen3-4B", 4.0, "Q4_K_M")
    assert est <= DEPLOY.mem_ceiling_mb
    print("PASS: the pinned Qwen3-4B Q4_K_M fits the deploy ceiling (%d MB)"
          % est)


def test_deploy_refuses_bigger_model():
    # A 14B model is what a 16 GB dev card tempts you into — deploy refuses.
    try:
        DEPLOY.check("Qwen3-14B", 14.0, "Q4_K_M")
    except (BudgetError, ModelPinError):
        print("PASS: deploy profile refuses a 14B model")
        return
    raise AssertionError("expected the deploy guard to refuse 14B")


def test_deploy_strict_pins_model_and_quant():
    for model, quant, why in [("Qwen3-8B", "Q4_K_M", "wrong model"),
                              ("Qwen3-4B", "Q8_0", "wrong quant")]:
        try:
            DEPLOY.check(model, 4.0, quant)
        except ModelPinError:
            continue
        raise AssertionError("expected ModelPinError for %s" % why)
    print("PASS: strict deploy profile pins both model and quant")


def test_dev_profile_allows_bigger_but_unpinned():
    # DEV is looser (bigger card) and not strict, but an 8B still fits its
    # ceiling while a 14B may or may not — either way DEV never pretends to
    # be the deploy budget.
    est8 = DEV.check("Qwen3-8B", 8.0, "Q4_K_M")   # not strict -> allowed
    assert est8 <= DEV.mem_ceiling_mb
    assert DEV.mem_ceiling_mb > DEPLOY.mem_ceiling_mb
    print("PASS: dev profile is looser but distinct from deploy")


def test_memory_estimate_orders_by_size_and_quant():
    # More params -> more memory; heavier quant -> more memory.
    assert estimate_memory_mb(4.0, "Q4_K_M") < estimate_memory_mb(8.0, "Q4_K_M")
    assert estimate_memory_mb(4.0, "Q4_K_M") < estimate_memory_mb(4.0, "Q8_0")
    # Qwen3-4B Q4_K_M lands in the documented ~2.5-3.5 GB range.
    mb = estimate_memory_mb(4.0, "Q4_K_M")
    assert 2000 < mb < 4000, mb
    print("PASS: memory estimate monotonic and in the documented range")


def test_stub_backend_is_deterministic():
    b = StubBackend(responses={"diagnose": "it is a timer fault"},
                    default="unknown")
    r1 = b.generate("please diagnose this")
    r2 = b.generate("something else")
    assert r1.text == "it is a timer fault" and r1.stub
    assert r2.text == "unknown"
    assert len(b.calls) == 2                 # calls recorded
    print("PASS: stub backend returns scripted, deterministic completions")


def test_select_backend_falls_back_to_stub():
    # With no real accelerator installed, selection must still return a
    # usable (stub) backend rather than failing.
    backend = select_backend()
    assert backend.available()
    # In this environment (no llama_cpp/hailo) that resolves to the stub.
    assert backend.name in ("stub", "llama.cpp", "hailo")
    print("PASS: backend selection always yields an available backend (%s)"
          % backend.name)


def test_select_backend_prefers_stub_when_asked():
    backend = select_backend(prefer=["stub"])
    assert backend.name == "stub"
    print("PASS: backend selection honours an explicit preference")


def main():
    test_deploy_pins_qwen3_4b()
    test_deploy_refuses_bigger_model()
    test_deploy_strict_pins_model_and_quant()
    test_dev_profile_allows_bigger_but_unpinned()
    test_memory_estimate_orders_by_size_and_quant()
    test_stub_backend_is_deterministic()
    test_select_backend_falls_back_to_stub()
    test_select_backend_prefers_stub_when_asked()
    print("ALL PASS")


if __name__ == "__main__":
    main()
