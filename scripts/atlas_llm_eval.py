#!/usr/bin/env python3
"""Run Atlas's labelled quality suite against a real local GGUF model."""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                ".."))

from atlas.diagnosis import load_patterns  # noqa: E402
from atlas.eval import EvalHarness, SAMPLE_CASES  # noqa: E402
from atlas.eval.samples import SAMPLE_PATTERNS  # noqa: E402
from atlas.model import DEPLOY, LlamaCppBackend  # noqa: E402


def main(model_path, cli_path=None):
    backend = LlamaCppBackend(model_path=model_path, accelerator="cpu",
                              profile=DEPLOY, params_b=4.0,
                              quant="Q4_K_M", n_ctx=DEPLOY.context,
                              cli_path=cli_path)
    if not backend.available():
        raise SystemExit("neither llama_cpp nor llama-completion is available")
    estimate = backend.enforce_budget()
    print("deploy budget: PASS (~%d MB <= %d MB)" %
          (estimate, DEPLOY.mem_ceiling_mb))

    harness = EvalHarness(backend=backend,
                          patterns=load_patterns(SAMPLE_PATTERNS),
                          profile=DEPLOY)
    report = harness.run(SAMPLE_CASES)
    print(report.summary())
    for result in report.results:
        print("  %s %-24s %s" %
              ("PASS" if result.passed else "FAIL", result.id,
               result.detail))

    # The deterministic metrics are release invariants. Model edit quality is
    # reported honestly and can be compared across CPU/GPU/Hailo runs.
    if report.accuracy("diagnosis") != 1.0:
        raise SystemExit("deterministic diagnosis metric regressed")
    if report.accuracy("safety") != 1.0:
        raise SystemExit("deterministic safety metric regressed")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("model", help="local Qwen3-4B Q4_K_M GGUF")
    parser.add_argument("--cli", help="path to llama-completion")
    args = parser.parse_args()
    sys.exit(main(args.model, args.cli))
