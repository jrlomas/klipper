# Founding Document 0002 — Atlas: Vision

Status: **Adopted — the deterministic floor (Milestone A) is realized in
the `atlas/` package + `src/trace.c`; the Milestone C contracts and pinned
Qwen3-4B workstation path are CPU-validated; deploy-target intelligence
remains hardware bring-up.**

> **These documents are no longer a single proposal — they are the design
> record of what Atlas is becoming.** FD-0002 was written as one spine to
> *argue* the companion layer; that argument is now settled, the spine has
> split into this numbered series, and the deterministic substrate exists
> in code. The text keeps its reasoning-first form because the *why* is
> exactly what a reader of an evolving codebase most needs. Read "we will
> build" as "Atlas does, for the floor — and here is the argument for why."

This document is the entry point of **FD-0002**, the design canon for
**Atlas** — the layer that turns HELIX from motion-and-comms firmware into
**intelligent software**. Atlas is the companion that watches the machine,
understands it, diagnoses itself, provisions and heals its own fleet, and
gets smarter as its users teach it. HELIX gives the machine honesty;
**Atlas** gives it a mind. Together they are **Helix Atlas** — the name
earns itself twice over: an atlas *shows you where everything is*, and a
Titan *carries the weight*.

See [README.md](README.md) for the full reading order, the document map,
and the glossary.

## The thesis

[FD-0001](../0001-motion-intentions/00-Vision.md) made the machine
**honest** — a micro-controller that owns its clock, its position, and its
queue, and that records what it actually did in an execution log. That core
exists and is workstation-validated in HELIX 0.9; target integration and
hardware qualification remain before 1.0. FD-0002
makes the machine **intelligent**: it takes the honest data FD-0001
already produces and builds the layer that *reads* it.

That layer is a companion that runs on the Raspberry Pi, is
**deterministic where it touches data and genuinely intelligent where it
interprets**, and treats this repository not as source code but as a
**shared brain** — known-failure knowledge, board and machine catalogs,
and the weights, prompts, and memory that power a local assistant, all
versioned together. With Atlas, HELIX stops being another firmware and
becomes a system that helps you run, debug, and maintain a machine — and,
in time, one you can simply talk to.

The honest data already exists; Atlas is what finally *listens* to it.

## 1. Principles

These are the non-negotiables every later document answers to. They are
not aspirations — they are constraints the implementation has been held
to, and they are what makes the deterministic floor safe to ship before a
single model weight is loaded.

1. **Local-first, no phone-home by default.** HELIX 0.9 stripped
   analytics and put "does not phone home" in the docs. The companion
   keeps that promise: everything runs on the Pi, offline-capable. Any
   sharing of a machine's data is **opt-in, per-event, and redacted** —
   realized in the numeric-only redaction pass
   ([`atlas/kb/redact.py`](../../../atlas/kb/redact.py)) described in
   [05-Knowledge-Base.md](05-Knowledge-Base.md).
2. **Determinism for data; intelligence for interpretation.** The facts —
   the merged timeline, the rule matches, the ABI versions — are produced
   by auditable, deterministic code. The **LLM interprets, explains, and
   proposes**; it never silently acts on a safety-critical path. Every
   model-proposed change is a concrete diff that is deterministically
   validated and user-confirmed before it applies. The safety gate is a
   non-LLM classifier ([`atlas/apply/classify.py`](../../../atlas/apply/classify.py)),
   built and tested *before* any model exists — see
   [07-LLM-Layer.md](07-LLM-Layer.md).
3. **Tiered compute — graceful degradation.** Atlas runs on a bare
   **Raspberry Pi 5** with everything deterministic (decoder, diagnosis
   rules, monitoring, provisioning) as ordinary CPU programs. Add the
   LLM-capable accelerator (the **Hailo-10H** / Raspberry Pi **AI HAT+ 2**,
   or a Strix-Halo-class host) and the **intelligence tier** switches on.
   Nothing *requires* the accelerator; the LLM tier is purely additive,
   and Atlas never becomes *less* safe for lacking one. The full argument
   is in [01-Compute-Tiers.md](01-Compute-Tiers.md).
4. **The repository is a knowledge base, not just code.** Failure
   patterns, board and config catalogs, and the model configuration +
   memory files that make "our" assistant *ours* live and version in this
   repo, alongside the source. Knowledge is a first-class, signed,
   reviewable artifact ([05-Knowledge-Base.md](05-Knowledge-Base.md)).
5. **Reuse the ecosystem; build only the missing organs.** Moonraker +
   Mainsail is the stack. We add components and panels where they reach,
   and build new services (trace viewer, provisioning wizard, companion
   daemon) only where they cannot — the seams are named in
   [07-LLM-Layer.md](07-LLM-Layer.md).
6. **The constrained board still comes first.** Everything the firmware
   side adds (the trace plane above all) must be near-zero-cost when off
   and cheap when on, and must fit the 16 KB F072. Features that don't fit
   aren't built there — the intelligence lives on the Pi, not the MCU.
   This is why the only firmware-side organ is the *cheap, structured*
   trace plane of [02-Trace-Observability.md](02-Trace-Observability.md).

## What Atlas is, in one breath

Four planes, one discipline:

- **Observe** — a structured trace plane and a merged-timeline store, so
  the machine can *talk* ([02-Trace-Observability.md](02-Trace-Observability.md)).
- **Understand** — a blackbox decoder and a deterministic diagnosis engine
  that turn a flight recording into an incident report
  ([03-Blackbox-Decoder.md](03-Blackbox-Decoder.md),
  [04-Diagnosis-Engine.md](04-Diagnosis-Engine.md)).
- **Act** — proactive health monitoring, one-touch provisioning, and
  fleet coherence that makes flashing and protocol-correctness the same
  mechanism ([06-Provisioning-Fleet-Coherence.md](06-Provisioning-Fleet-Coherence.md)).
- **Interpret & control** — a local open-weight model that explains,
  drafts config, and (later) listens — always behind a deterministic
  validate-and-confirm gate ([07-LLM-Layer.md](07-LLM-Layer.md)).

Underneath all four is the **shared brain**: a public, signed knowledge
base whose every lesson — and every *refusal* to learn — carries a
readable rationale ([05-Knowledge-Base.md](05-Knowledge-Base.md)).

The first three planes are deterministic, CPU-only, and **already built** —
the Milestone A floor. The fourth sits above them: its safety contracts,
llama.cpp transport, daemon/API/terminal/Mainsail assistant seam, and pinned
Qwen3-4B workstation preflight are realized; GPU authorship, live-machine
apply, and Pi 5 + Hailo-10H validation remain. The roadmap that got
us here and the settled decisions behind it are in
[08-Roadmap.md](08-Roadmap.md).
