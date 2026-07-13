# FD-0002 · 01 — Compute Tiers and the Model Choice

Status: **Adopted — the deploy target is pinned (Pi 5 + Hailo-10H,
Qwen3-4B Q4_K_M); the tier abstraction is realized in
[`atlas/model/`](../../../atlas/model/) — the `ModelBackend` interface and
the deploy-profile budget guard — and validated on a real GGUF via
llama.cpp.** The deterministic floor runs on the base tier today with no
accelerator at all.

Atlas has one hardware fact at its center, and the whole design falls out
of it: **the deterministic work costs almost nothing, and the intelligent
work costs an accelerator most machines will not have.** So Atlas ships
**selectable profiles**, auto-detects the hardware, and enables the
matching tier — never demanding more than the machine has, never becoming
*less safe* for having less. This document explains the tiers, the one
non-obvious hardware constraint that shapes them, and why the model family
is a functional requirement rather than a fashion pick.

This is the hardware half of Principle 3 in
[00-Vision.md](00-Vision.md#1-principles); the software half — how the
model layer stays swappable across runtimes — is in
[07-LLM-Layer.md](07-LLM-Layer.md).

## The one non-obvious constraint

> **Only the Hailo-10H can host an LLM.** The Raspberry Pi **AI HAT+ 2**
> (Hailo-10H, ~40 TOPS, **8 GB** of dedicated on-board LPDDR4X) can run a
> language model *on the accelerator*. The earlier **Hailo-8 / 8L**
> (26 / 13 TOPS) are vision/CNN NPUs — they **cannot** run an LLM, but
> they *can* accelerate **Whisper** (speech-to-text), which is exactly
> what the voice feature needs.

So the accelerator tier is not one capability but two distinct ones:
**LLM (needs the 10H)** and **ASR (any Hailo, or CPU)**. Collapsing them
would be a design error — it would tie voice to a part it does not need
and imply an LLM on a part that cannot run one. Keeping them separate is
why the tier table below has three columns, not two.

## The three tiers

| | **Base** (Pi 5 only) | **ASR-accel** (Pi 5 + Hailo-8/8L) | **Intelligence** (Pi 5 + Hailo-10H / AI HAT+ 2) |
| --- | --- | --- | --- |
| Trace + blackbox decode | ✓ | ✓ | ✓ |
| Diagnosis **rules** engine | ✓ | ✓ | ✓ |
| Live monitor + anomaly | ✓ | ✓ | ✓ |
| Provisioning + fleet coherence | ✓ | ✓ | ✓ |
| **LLM** interpretation / NL config / control | tiny CPU model or off | tiny CPU model or off | ✓ on-accelerator |
| Report synthesis / RAG over KB | templated | templated | ✓ model-written |
| **Voice** (future) | — | ✓ Whisper on Hailo | ✓ Whisper + LLM |

Everything above the LLM line is the **deterministic floor** — it is the
same code on every tier, has no accelerator dependency, and is the part
that is already built ([02](02-Trace-Observability.md)–[06](06-Provisioning-Fleet-Coherence.md)).
Everything on or below the LLM line is *additive*: it turns on when the
hardware is present and is simply absent (or templated) when it is not.

## The model we design against

The intelligence tier is designed against the **Qwen3 dense family** —
0.6B / 1.7B / 4B / 8B / 14B / 32B, all with a 128K context window,
**tool calling**, **structured output**, and dual-mode thinking (a slow
reasoning mode for a hard diagnosis, a fast mode for routine work).

This is not a taste decision. Atlas's entire safety story is a
**draft → validate → apply** discipline (see [07-LLM-Layer.md](07-LLM-Layer.md)):
the model must emit a *structured* proposal that deterministic code can
parse, check, and classify by risk before anything happens. **Tool calling
and structured output are therefore functional requirements**, not
conveniences — a model that could only emit free-form prose could not be
wired safely into the apply pipeline at all. The dual-mode thinking maps
cleanly onto Atlas's own two speeds: deliberate on an unmatched fault,
quick on a cosmetic edit.

**The pins:**

- **Default (Hailo-10H, 8 GB):** **Qwen3-4B** at Q4_K_M (~2.5–3 GB) —
  fits the on-board RAM with headroom for context and the ASR model.
  Fallback **Qwen3-1.7B** on tighter budgets; stretch **Qwen3-8B** where
  it fits.
- **Power host (Strix-Halo-class):** Qwen3-8B/14B comfortably.
- **Base (Pi 5 CPU):** a 0.6B/1.7B model is possible but slow (a few
  tok/s); treat CPU-LLM as "works, not pleasant," and keep everything
  load-bearing in the deterministic floor.
- **Pinning:** the exact model + quantization + revision is **data in the
  repo** (see [05-Knowledge-Base.md](05-Knowledge-Base.md)), versioned
  with the prompts and memory, so "our Atlas" is reproducible and can be
  re-pinned as the Qwen line advances (3.5 / 3.6 …) or as Hailo's model
  zoo compiles newer weights.

## Dev target ≠ deploy target

There is a distinction the implementation is built to respect, and it is
worth stating plainly because getting it wrong would quietly break the
deployable path. The model layer is *developed and validated* on fast,
present GPUs (NVIDIA/CUDA, AMD/ROCm); it *deploys* on a Pi 5 + Hailo-10H
with a hard ~8 GB budget and a model-compilation step. Two mechanisms keep
the two honest:

- **The model runtime is abstracted behind a `ModelBackend` interface**
  ([`atlas/model/backend.py`](../../../atlas/model/backend.py)) with
  stub / CUDA / ROCm / CPU / Hailo implementations, so no CUDA-only
  assumption can leak into the deployable path. The deploy backend is
  **llama.cpp** (CUDA + ROCm + CPU builds, GGUF/Q4_K_M), already wired and
  validated against a real GGUF.
- **A deploy-profile budget guard** ([`atlas/model/profile.py`](../../../atlas/model/profile.py))
  is what actually keeps the budget honest: `--profile deploy` **refuses
  any model past the Qwen3-4B / ~6 GB ceiling**, so a big dev card cannot
  tempt the project into shipping something the Hailo can't run. The guard,
  not the hardware, enforces the limit.

The payoff of the whole split is a **clean upgrade path**: a bare Pi 5
runs the entire deterministic companion today; add the AI HAT+ 2 and the
*same* Atlas becomes conversational, with no change to the safety floor.
That upgrade being additive-only is the point of tiering, and it is why
none of [02](02-Trace-Observability.md)–[06](06-Provisioning-Fleet-Coherence.md)
mention an accelerator at all.
