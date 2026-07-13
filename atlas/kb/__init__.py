# A8 knowledge-base framework — bundle format, redaction, and the
# GitHub-Issues intake for the KB lifecycle (FD-0002 §6, §6a).
#
# Milestone A ships the *rails*, not the intelligence: assemble a
# redacted blackbox bundle from a decoded timeline + diagnosis, and shape
# it into the structured GitHub Issue the §6a state machine runs on. The
# redaction pass implements the settled policy — numeric diagnostics
# shared raw, every string transformed, secrets never shareable.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

from .redact import (redact_fields, redact_event, redact_value,
                     RedactionPolicy, DEFAULT_POLICY)
from .bundle import BlackboxBundle, assemble_bundle
from .issue import (render_issue, STATE_LABELS, ACCEPT_REASONS,
                    REJECT_REASONS, ALL_LABELS)
from .store import ConsentError, KnowledgeOutbox, SignedCatalogInstaller

__all__ = [
    "redact_fields", "redact_event", "redact_value", "RedactionPolicy",
    "DEFAULT_POLICY", "BlackboxBundle", "assemble_bundle", "render_issue",
    "STATE_LABELS", "ACCEPT_REASONS", "REJECT_REASONS", "ALL_LABELS",
    "ConsentError", "KnowledgeOutbox", "SignedCatalogInstaller",
]
