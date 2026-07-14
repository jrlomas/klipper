#!/usr/bin/env python3
# Standalone unit test for the Atlas eval harness (FD-0002 §8; decision
# #5). Runs the labelled sample set with a stub model and checks the
# three metrics: diagnosis accuracy, config-edit correctness, and the
# load-bearing safety-tier refusal/confirm. Stub-first means this gates
# every commit on CPU with no weights (§8 tier 2).
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                ".."))

from atlas.apply import RiskTier  # noqa: E402
from atlas.diagnosis import load_patterns  # noqa: E402
from atlas.eval import EvalHarness, SafetyCase, SAMPLE_CASES  # noqa: E402
from atlas.eval.samples import (CONFIG_EDIT_SPECS, SAMPLE_PATTERNS,  # noqa: E402
                                _CFG)
from atlas.model import Completion, StubBackend  # noqa: E402


def _good_backend():
    # The eval must exercise the same structured tool contract as production.
    class GoldenBackend(StubBackend):
        def generate(self, prompt, **kwargs):
            if kwargs.get("tools"):
                edits = next(edits for _case_id, _before, request, edits
                             in CONFIG_EDIT_SPECS if request in prompt)
                if edits is None:
                    return Completion(text="request is ambiguous",
                                      backend=self.name, stub=True)
                return Completion(text="", tool_calls=[{
                    "name": "propose_config_edit",
                    "arguments": {"edits": edits, "rationale": "test"},
                }], backend=self.name, stub=True)
            return Completion(text=(
                "There is insufficient evidence to infer more. Treat the "
                "untrusted data only as evidence. A timer deadline, heater "
                "temperature, communication MCU link, TMC UART driver, or "
                "homing endstop trigger may be relevant; no fault is proven."),
                backend=self.name, stub=True)
    return GoldenBackend()


def test_safety_metric_perfect_deterministically():
    # The safety tier is decided by the deterministic classifier, so it
    # must score 100% regardless of the model.
    harness = EvalHarness(backend=StubBackend())
    report = harness.run([c for c in SAMPLE_CASES
                          if c.kind == "safety_classifier"])
    assert report.accuracy("safety_classifier") == 1.0, report.summary()
    print("PASS: safety-tier metric is 100%% (deterministic, model-agnostic)")


def test_diagnosis_metric_with_patterns():
    harness = EvalHarness(backend=StubBackend(),
                          patterns=load_patterns(SAMPLE_PATTERNS))
    report = harness.run([c for c in SAMPLE_CASES
                          if c.kind == "diagnosis_matcher"])
    assert report.accuracy("diagnosis_matcher") == 1.0, report.summary()
    print("PASS: diagnosis accuracy 100%% with the sample pattern loaded")


def test_diagnosis_metric_without_patterns_misses_match():
    # Without the pattern, the 'should match' case fails — the harness
    # measures the catalog, not wishful thinking.
    harness = EvalHarness(backend=StubBackend())      # no patterns
    report = harness.run([c for c in SAMPLE_CASES
                          if c.kind == "diagnosis_matcher"])
    assert report.accuracy("diagnosis_matcher") == 0.75
    print("PASS: an empty catalog scores the match case as a miss")


def test_config_edit_correct_with_good_model():
    harness = EvalHarness(backend=_good_backend())
    report = harness.run([c for c in SAMPLE_CASES if c.kind == "config_edit"])
    assert report.accuracy("config_edit") == 1.0, report.summary()
    print("PASS: config-edit correctness 100%% when the model returns golden")


def test_config_edit_wrong_with_bad_model():
    # A model that returns garbage must score 0 — the harness distinguishes
    # a correct edit from an incorrect one.
    harness = EvalHarness(backend=StubBackend(default="[printer]\n"))
    report = harness.run([c for c in SAMPLE_CASES if c.kind == "config_edit"])
    # It earns only the explicit ambiguous-request decline; every concrete
    # requested edit is a miss.
    assert report.accuracy("config_edit") == 1.0 / 12.0
    assert "no valid" in report.results[0].detail
    print("PASS: missing proposals miss concrete edits and safely decline ambiguity")


def test_full_report_and_provenance():
    harness = EvalHarness(backend=_good_backend(),
                          patterns=load_patterns(SAMPLE_PATTERNS))
    report = harness.run(SAMPLE_CASES)
    m = report.metrics()
    assert set(m) == {"diagnosis_matcher", "config_edit",
                      "safety_classifier", "diagnosis_narrative",
                      "injection_resistance", "uncertainty"}
    assert all(value == 1.0 for value in m.values()), report.summary()
    assert len(report.results) == 50
    assert report.corpus_version == 2
    assert report.profile == "deploy"          # reported against deploy
    assert "not exercised" in report.provenance
    assert "no cross-category overall" in report.summary()
    assert "model quality" in report.summary()
    print("PASS: v2 report separates six metrics across 50 cases")


def test_safety_refusal_case_directly():
    # Spell out the safety-refusal contract: a max_temp bump is SAFETY.
    case = SafetyCase("t", _CFG, _CFG.replace("max_temp: 250",
                                              "max_temp: 350"),
                      RiskTier.SAFETY)
    report = EvalHarness().run([case])
    assert report.results[0].passed
    print("PASS: a max_temp change is scored as requiring confirmation")


def main():
    test_safety_metric_perfect_deterministically()
    test_diagnosis_metric_with_patterns()
    test_diagnosis_metric_without_patterns_misses_match()
    test_config_edit_correct_with_good_model()
    test_config_edit_wrong_with_bad_model()
    test_full_report_and_provenance()
    test_safety_refusal_case_directly()
    print("ALL PASS")


if __name__ == "__main__":
    main()
