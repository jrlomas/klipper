# The apply pipeline — draft -> validate -> classify -> apply/journal
# (FD-0002 §7).
#
# A model (Milestone C) produces a Proposal: a concrete before/after
# config diff. This pipeline is deterministic and model-agnostic:
#   1. validate  - does the result parse? is the diff non-empty?
#   2. classify  - the non-LLM risk classifier sets the tier
#   3. decide    - safety -> confirm; consequential -> auto+undo; cosmetic
#                  -> auto
#   4. apply     - journal the diff (so undo and "what did Atlas change?"
#                  are always answerable), unless confirmation is required
#                  and not yet given.
#
# This in-memory contract remains useful for drafting/tests. Real config files
# use live.PersistentApplyPipeline: compare-and-swap, fsynced atomic writes,
# durable audit, reload rollback, and restart-safe undo.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

from dataclasses import dataclass, field

from .classify import RiskTier, classify_changeset, decision_for
from .config_diff import diff_configs, parse_config


@dataclass
class Proposal:
    before: str
    after: str
    rationale: str = ""
    source: str = "model"        # 'model' | 'user' | 'rule'


@dataclass
class Validation:
    ok: bool
    errors: list = field(default_factory=list)


@dataclass
class JournalEntry:
    seq: int
    tier: RiskTier
    action: str
    before: str
    after: str
    changes: list
    reverted: bool = False


@dataclass
class ApplyResult:
    validation: Validation
    tier: RiskTier = RiskTier.COSMETIC
    action: str = ""
    needs_confirmation: bool = False
    applied: bool = False
    changes: list = field(default_factory=list)
    entry: "JournalEntry | None" = None


class Journal:
    """Ordered, reversible record of applied changes."""

    def __init__(self):
        self.entries: list = []

    def append(self, tier, action, before, after, changes) -> JournalEntry:
        entry = JournalEntry(seq=len(self.entries), tier=tier, action=action,
                             before=before, after=after, changes=list(changes))
        self.entries.append(entry)
        return entry

    def undo(self, entry: JournalEntry = None) -> str:
        """Revert an entry (default: the last un-reverted one).

        Returns the config text to restore (the entry's `before`).
        """
        if entry is None:
            entry = next((e for e in reversed(self.entries)
                          if not e.reverted), None)
        if entry is None:
            raise ValueError("nothing to undo")
        entry.reverted = True
        return entry.before


class ApplyPipeline:
    def __init__(self, journal: Journal = None):
        self.journal = journal or Journal()

    def validate(self, proposal: Proposal) -> Validation:
        errors = []
        try:
            parse_config(proposal.after)
        except Exception as exc:            # pragma: no cover - defensive
            errors.append("after config does not parse: %s" % exc)
        if proposal.before == proposal.after:
            errors.append("no-op: before and after are identical")
        return Validation(ok=not errors, errors=errors)

    def process(self, proposal: Proposal, confirmed: bool = False
                ) -> ApplyResult:
        validation = self.validate(proposal)
        if not validation.ok:
            return ApplyResult(validation=validation)

        changes = diff_configs(proposal.before, proposal.after)
        tier, _ = classify_changeset(changes)
        action, needs_conf = decision_for(tier)

        # Safety tier: never auto-apply. Requires explicit confirmation.
        if needs_conf and not confirmed:
            return ApplyResult(validation=validation, tier=tier,
                               action=action, needs_confirmation=True,
                               applied=False, changes=changes)

        entry = self.journal.append(tier, action, proposal.before,
                                    proposal.after, changes)
        return ApplyResult(validation=validation, tier=tier, action=action,
                           needs_confirmation=needs_conf, applied=True,
                           changes=changes, entry=entry)

    def preview(self, proposal: Proposal) -> ApplyResult:
        """Validate/classify without applying or creating an audit entry."""
        validation = self.validate(proposal)
        if not validation.ok:
            return ApplyResult(validation=validation)
        changes = diff_configs(proposal.before, proposal.after)
        tier, _ = classify_changeset(changes)
        action, needs_conf = decision_for(tier)
        return ApplyResult(validation=validation, tier=tier, action=action,
                           needs_confirmation=needs_conf, applied=False,
                           changes=changes)

    def undo_last(self) -> str:
        return self.journal.undo()
