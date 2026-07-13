# RAG index over the KB + the per-machine memory (FD-0002 §7).
#
# The model interprets grounded by retrieval over the failure-pattern
# catalog and the machine's own memory, so answers are about *this*
# machine, not the internet's average printer. The index format and the
# retrieval are deterministic and testable. TokenHashEmbedder uses stable
# token hashing without separate weights: a small, auditable retriever that
# remains available on the base tier while the LLM interprets its results.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import hashlib
import math
import re
from dataclasses import dataclass, field

_RE_TOK = re.compile(r"[a-z0-9_]+")


@dataclass
class RagDocument:
    id: str
    text: str
    source: str = ""              # 'pattern' | 'memory' | ...
    metadata: dict = field(default_factory=dict)


class TokenHashEmbedder:
    """Deterministic bag-of-hashed-tokens embedder (no weights).

    Stable across processes (hashlib, not the salted builtin hash), so an
    index built now retrieves identically later and remains available when
    the intelligence-tier accelerator is absent.
    """

    name = "token-hash-v1"

    def __init__(self, dim: int = 256):
        self.dim = dim

    def _tokens(self, text: str) -> list:
        return _RE_TOK.findall(text.lower())

    def embed(self, text: str) -> list:
        vec = [0.0] * self.dim
        for tok in self._tokens(text):
            h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
            vec[h % self.dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vec))
        if norm:
            vec = [v / norm for v in vec]
        return vec


def _cosine(a, b) -> float:
    # Both vectors are L2-normalized, so cosine is just the dot product.
    return sum(x * y for x, y in zip(a, b))


class RagIndex:
    """An embedded document store with top-k cosine retrieval."""

    def __init__(self, embedder=None):
        self.embedder = embedder or TokenHashEmbedder()
        self.docs: list = []
        self._vectors: list = []

    def add(self, doc: RagDocument) -> None:
        self.docs.append(doc)
        self._vectors.append(self.embedder.embed(doc.text))

    def build(self, docs) -> "RagIndex":
        for d in docs:
            self.add(d)
        return self

    def query(self, text: str, k: int = 3) -> list:
        """Return up to k (RagDocument, score) pairs, best first."""
        q = self.embedder.embed(text)
        scored = [(doc, _cosine(q, vec))
                  for doc, vec in zip(self.docs, self._vectors)]
        scored.sort(key=lambda t: (-t[1], t[0].id))
        return [(d, s) for d, s in scored[:k] if s > 0.0]

    def __len__(self) -> int:
        return len(self.docs)


def kb_documents(patterns=None, memory=None) -> list:
    """Build the grounding corpus from KB patterns + machine memory."""
    docs = []
    for p in (patterns or []):
        docs.append(RagDocument(
            id="pattern:%s" % p.id, source="pattern",
            text="%s. cause: %s fix: %s" % (p.id.replace("-", " "),
                                            p.cause, p.fix),
            metadata={"confidence": p.confidence}))
    if memory is not None:
        for q in memory.quirks:
            docs.append(RagDocument(
                id="quirk:%d" % len(docs), source="memory", text=q))
        for d in memory.diagnoses:
            docs.append(RagDocument(
                id="diag:%s" % d.get("case_hash", len(docs)),
                source="memory",
                text="past incident: %s" % d.get("summary", ""),
                metadata={"case_hash": d.get("case_hash", "")}))
        for name, baseline in sorted(memory.baselines.items()):
            docs.append(RagDocument(
                id="baseline:%s" % name, source="memory",
                text="machine baseline %s: %s" % (
                    name, _baseline_text(baseline))))
    return docs


def _baseline_text(value):
    if isinstance(value, dict):
        return "; ".join("%s=%s" % (key, _baseline_text(item))
                         for key, item in sorted(value.items()))
    if isinstance(value, list):
        return ", ".join(_baseline_text(item) for item in value)
    return str(value)


# Compatibility for callers written while the contract was stub-first.
StubEmbedder = TokenHashEmbedder
