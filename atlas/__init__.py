# Atlas — the HELIX companion system (FD-0002).
#
# This package is the deterministic floor of Atlas: the code that turns
# the honest data HELIX produces into facts (a merged, machine-time
# ordered timeline; deterministic diagnosis; provisioning; fleet
# coherence).  It is ordinary CPU code with no accelerator dependency and
# must stay that way — the intelligence tier (the local model) sits
# *above* this floor and only ever interprets what the floor computes.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

__version__ = "0.0.1-milestone-a"

# Design canon: docs/founding/0002-companion-system/README.md
# Development handoff: docs/founding/0002-companion-system/HANDOFF.md
