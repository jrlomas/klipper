# The eval harness — run labelled cases, compute the metrics that matter.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

from dataclasses import dataclass, field

from ..apply import RiskTier, classify_changeset, diff_configs
from ..decode import decode_klippy_log
from ..diagnosis import Matcher
from ..model import DEPLOY, StubBackend


@dataclass
class DiagnosisCase:
    id: str
    log: str
    expect: str                 # a pattern id, or "no-match"
    kind: str = "diagnosis"


@dataclass
class ConfigEditCase:
    id: str
    before: str
    request: str
    expect_after: str
    kind: str = "config_edit"


@dataclass
class SafetyCase:
    id: str
    before: str
    after: str
    expect_tier: RiskTier
    kind: str = "safety"


@dataclass
class CaseResult:
    id: str
    kind: str
    passed: bool
    detail: str = ""


@dataclass
class EvalReport:
    results: list = field(default_factory=list)
    backend: str = ""
    profile: str = ""
    # §8: model/accelerator results are "authored on GPU, validated on
    # Hailo" — the split must never blur.
    provenance: str = "authored on GPU, validated on Hailo"

    def by_kind(self, kind) -> list:
        return [r for r in self.results if r.kind == kind]

    def accuracy(self, kind) -> float:
        rs = self.by_kind(kind)
        if not rs:
            return float("nan")
        return sum(1 for r in rs if r.passed) / len(rs)

    def metrics(self) -> dict:
        kinds = sorted({r.kind for r in self.results})
        return {k: self.accuracy(k) for k in kinds}

    def overall(self) -> float:
        if not self.results:
            return float("nan")
        return sum(1 for r in self.results if r.passed) / len(self.results)

    def summary(self) -> str:
        lines = ["Atlas eval — backend=%s profile=%s" % (self.backend,
                                                         self.profile)]
        for k, acc in self.metrics().items():
            n = len(self.by_kind(k))
            lines.append("  %-12s %5.1f%%  (%d cases)" % (k, acc * 100, n))
        lines.append("  %-12s %5.1f%%  (%d cases)"
                     % ("overall", self.overall() * 100, len(self.results)))
        lines.append("  provenance: %s" % self.provenance)
        return "\n".join(lines)


class EvalHarness:
    def __init__(self, backend=None, patterns=None, profile=DEPLOY):
        self.backend = backend or StubBackend()
        self.patterns = patterns or []
        self.profile = profile

    def run(self, cases) -> EvalReport:
        report = EvalReport(backend=self.backend.name,
                            profile=self.profile.name)
        for case in cases:
            report.results.append(self._run_one(case))
        return report

    def _run_one(self, case) -> CaseResult:
        if case.kind == "diagnosis":
            return self._diagnosis(case)
        if case.kind == "config_edit":
            return self._config_edit(case)
        if case.kind == "safety":
            return self._safety(case)
        return CaseResult(case.id, case.kind, False, "unknown case kind")

    def _diagnosis(self, case) -> CaseResult:
        diag = Matcher(self.patterns).diagnose(decode_klippy_log(case.log))
        if case.expect == "no-match":
            ok = not diag.matched()
            got = "matched" if diag.matched() else "no-match"
        else:
            ok = diag.matched() and diag.best.pattern_id == case.expect
            got = diag.best.pattern_id if diag.matched() else "no-match"
        return CaseResult(case.id, case.kind, ok,
                          "expected %s, got %s" % (case.expect, got))

    def _config_edit(self, case) -> CaseResult:
        # The model drafts the edited config; correctness = its change set
        # equals the golden change set. With a stub returning the golden,
        # this exercises the draft->diff->compare plumbing (tier 2); with a
        # real model it measures edit quality (tier 3).
        completion = self.backend.generate(
            prompt="%s\n\n%s" % (case.request, case.before))
        produced = completion.text
        got = _changeset_key(diff_configs(case.before, produced))
        want = _changeset_key(diff_configs(case.before, case.expect_after))
        ok = got == want
        return CaseResult(case.id, case.kind, ok,
                          "changeset match" if ok else "changeset mismatch")

    def _safety(self, case) -> CaseResult:
        overall, _ = classify_changeset(
            diff_configs(case.before, case.after))
        ok = overall == case.expect_tier
        return CaseResult(case.id, case.kind, ok,
                          "expected %s, got %s"
                          % (case.expect_tier.name, overall.name))


def _changeset_key(changes) -> set:
    return {(c.section, c.key, c.op, c.new) for c in changes}
