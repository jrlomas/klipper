# RAG index over the KB + the per-machine memory (FD-0002 §7).
#
# The model interprets grounded by retrieval over the failure-pattern
# catalog and the machine's own memory, so answers are about *this*
# machine, not the internet's average printer. Production retrieval is
# deterministic BM25 over real terms, so collisions cannot create false
# relevance and every score is inspectable.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import hashlib
import math
import re
from collections import Counter
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
    """A deterministic BM25 document store with observable retrieval."""

    def __init__(self, embedder=None, min_score=0.05, k1=1.5, b=0.75):
        # Supplying an embedder opts into the legacy cosine compatibility
        # path. The production default is collision-free BM25.
        self.embedder = embedder
        self.min_score = min_score
        self.k1 = k1
        self.b = b
        self.docs: list = []
        self._vectors: list = []
        self._terms: list = []
        self._doc_freq = Counter()
        self._total_terms = 0
        self.last_query = {"query_sha256": "", "term_count": 0,
                           "scores": [], "weak": True}

    def add(self, doc: RagDocument) -> None:
        self.docs.append(doc)
        if self.embedder is not None:
            self._vectors.append(self.embedder.embed(doc.text))
            return
        terms = Counter(_tokens(doc.text))
        self._terms.append(terms)
        self._total_terms += sum(terms.values())
        self._doc_freq.update(terms.keys())

    def build(self, docs) -> "RagIndex":
        for d in docs:
            self.add(d)
        return self

    def query(self, text: str, k: int = 3) -> list:
        """Return up to k (RagDocument, score) pairs, best first."""
        if self.embedder is not None:
            q = self.embedder.embed(text)
            scored = [(doc, _cosine(q, vec))
                      for doc, vec in zip(self.docs, self._vectors)]
        else:
            query_terms = Counter(_tokens(text))
            average = (self._total_terms / len(self.docs)
                       if self.docs else 0.0)
            scored = []
            for doc, terms in zip(self.docs, self._terms):
                length = sum(terms.values())
                score = 0.0
                for term, qtf in query_terms.items():
                    tf = terms.get(term, 0)
                    if not tf:
                        continue
                    df = self._doc_freq[term]
                    idf = math.log(1.0 + (len(self.docs) - df + 0.5)
                                   / (df + 0.5))
                    norm = tf + self.k1 * (
                        1.0 - self.b + self.b * length / (average or 1.0))
                    score += idf * tf * (self.k1 + 1.0) / norm * qtf
                scored.append((doc, score))
        scored.sort(key=lambda t: (-t[1], t[0].id))
        hits = [(d, s) for d, s in scored[:k] if s >= self.min_score]
        self.last_query = {
            "query_sha256": hashlib.sha256(
                text.encode("utf-8")).hexdigest()[:16],
            "term_count": len(_RE_TOK.findall(text.lower())),
            "scores": [{"document": doc.id, "source": doc.source,
                        "score": round(score, 6)}
                       for doc, score in scored[:k]],
            "weak": not hits,
        }
        return hits

    def status(self) -> dict:
        return {
            "method": (getattr(self.embedder, "name", "legacy-cosine")
                       if self.embedder is not None else "bm25-v1"),
            "documents": len(self.docs),
            "minimum_score": self.min_score,
            "last_query": self.last_query,
        }

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


_TERM_EXPANSIONS = {
    "hotend": ("extruder", "heater"),
    "nozzle": ("extruder", "heater"),
    "overheat": ("temperature", "heater"),
    "overheating": ("temperature", "heater"),
    "disconnect": ("comms", "link"),
    "disconnected": ("comms", "link"),
    "wire": ("cable", "link"),
    "wiring": ("cable", "link"),
}


def _tokens(text):
    """Tokenize with a small audited printer-domain synonym expansion."""
    base = _RE_TOK.findall(text.lower())
    expanded = list(base)
    for term in base:
        expanded.extend(_TERM_EXPANSIONS.get(term, ()))
    return expanded
