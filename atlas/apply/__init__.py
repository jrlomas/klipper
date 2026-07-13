# The apply layer — draft -> validate -> classify -> apply (FD-0002 §7).
#
# The LLM (Milestone C) drafts a concrete config diff; this deterministic
# layer decides what happens to it. A non-LLM classifier sets the risk
# tier from the diff, so the safety gate never depends on the model's
# judgement: safety-affecting changes always confirm; consequential ones
# auto-apply with undo + audit; cosmetic ones auto-apply. Everything is
# journaled and reversible. Built now (CPU-only, testable) so dropping a
# model in later is a plug-in, not an integration.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

from .config_diff import Change, parse_config, diff_configs
from .classify import (RiskTier, classify_change, classify_changeset,
                       decision_for)
from .pipeline import (ApplyPipeline, ApplyResult, Journal, JournalEntry,
                       Proposal, Validation)
from .live import PersistentApplyPipeline, StaleConfigError

__all__ = [
    "Change", "parse_config", "diff_configs",
    "RiskTier", "classify_change", "classify_changeset", "decision_for",
    "ApplyPipeline", "ApplyResult", "Journal", "JournalEntry", "Proposal",
    "Validation",
    "PersistentApplyPipeline", "StaleConfigError",
]
