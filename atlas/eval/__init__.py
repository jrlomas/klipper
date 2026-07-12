# The eval harness (FD-0002 §8 tier 3; decision 2026-07-12 #5).
#
# Measure Atlas on the tasks that matter so model swaps are decisions, not
# vibes: diagnosis accuracy against a labelled case set, config-edit
# correctness against golden diffs, and — the load-bearing one — correct
# refusal/confirm on the safety tier. Runs stub-model-first (no weights)
# so it gates every commit on CPU (§8 tier 2); the same suite runs on the
# GPU (quality) and on the Hailo (deploy), always reported against the
# deploy profile so a passing dev run is never mistaken for a deploy pass.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

from .harness import (DiagnosisCase, ConfigEditCase, SafetyCase,
                      CaseResult, EvalReport, EvalHarness)
from .samples import SAMPLE_CASES

__all__ = [
    "DiagnosisCase", "ConfigEditCase", "SafetyCase", "CaseResult",
    "EvalReport", "EvalHarness", "SAMPLE_CASES",
]
