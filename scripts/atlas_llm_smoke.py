#!/usr/bin/env python3
# Real-model smoke test for the Atlas LLM integration (FD-0002 §7).
#
# Unlike test/atlas_llm_test.py (which mocks the model), this loads a real
# GGUF model through the LlamaCppBackend and exercises interpret_incident
# and propose_config_edit end to end. It needs llama-cpp-python and a GGUF
# file, so it is NOT part of the standard CPU test suite — run it in a
# venv with a model:
#
#   python3 scripts/atlas_llm_smoke.py /path/to/model.gguf
#
# It validates the *plumbing* on real weights (the tiny dev model is not
# expected to give production-grade answers); quality is measured by the
# eval harness against the pinned deploy model.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                ".."))

from atlas.apply import ApplyPipeline, RiskTier  # noqa: E402
from atlas.decode import decode_klippy_log  # noqa: E402
from atlas.model import (LlamaCppBackend, interpret_incident,  # noqa: E402
                         propose_config_edit)

CFG = ("[printer]\nkinematics: corexy\nmax_velocity: 300\nmax_accel: 3000\n\n"
       "[gcode_macro START]\ndescription: Start a print\n")


def main(model_path):
    print("Loading %s ..." % model_path)
    backend = LlamaCppBackend(model_path=model_path, accelerator="cpu",
                              n_ctx=4096)
    assert backend.available(), "llama_cpp not importable"

    # 1) interpret a real decoded incident
    tl = decode_klippy_log(
        "Start printer at X (100.0 5.0)\n"
        "MCU 'mcu' shutdown: Timer too close\n")
    print("\n[interpret_incident] ->")
    text = interpret_incident(backend, tl)
    print(text.strip()[:600])
    assert text.strip(), "empty interpretation"

    # 2) propose a config edit via the tool, then run it through the gate
    print("\n[propose_config_edit] 'lower max_accel to 2000' ->")
    proposal = propose_config_edit(backend, "lower max_accel to 2000", CFG)
    if proposal is None:
        print("  (model did not call the tool — plumbing ok, tiny model "
              "may not tool-call reliably)")
    else:
        print("  rationale:", proposal.rationale[:200])
        res = ApplyPipeline().process(proposal)
        print("  tier=%s applied=%s needs_confirmation=%s"
              % (res.tier.name, res.applied, res.needs_confirmation))
        # If it happened to touch a safety key, it must be gated.
        if res.tier == RiskTier.SAFETY:
            assert res.needs_confirmation and not res.applied

    print("\nSMOKE OK: real model loaded and drove the Atlas contracts.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: atlas_llm_smoke.py /path/to/model.gguf")
        sys.exit(2)
    main(sys.argv[1])
