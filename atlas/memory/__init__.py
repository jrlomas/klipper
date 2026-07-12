# The per-machine memory file + the RAG index (FD-0002 §6, §7; Milestone C
# prep).
#
# "Our" Atlas is ours because of what it remembers: a machine's history,
# its quirks, its learned baselines, and every change Atlas made to it.
# That memory file is versioned data, redacted like everything else, and
# it grounds the model (with RAG over the KB + this memory) so answers are
# about *this* machine. The formats are deterministic and testable now;
# the real embedder drops in at Milestone C.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

from .machine import MachineMemory, MEMORY_SCHEMA_VERSION
from .rag import RagIndex, RagDocument, StubEmbedder, kb_documents

__all__ = [
    "MachineMemory", "MEMORY_SCHEMA_VERSION",
    "RagIndex", "RagDocument", "StubEmbedder", "kb_documents",
]
