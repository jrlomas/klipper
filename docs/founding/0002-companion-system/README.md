# Founding Document 0002 — Atlas, the Companion System

*Shorthand: **FD-0002**.* This is a **founding document** of HELIX: the
design canon for **Atlas** — the layer that turns HELIX from
motion-and-comms firmware into **intelligent software**. Atlas is the
companion that watches the machine, understands it, diagnoses itself,
provisions and heals its own fleet, and gets smarter as its users teach
it. HELIX gives the machine honesty; **Atlas** gives it a mind. Together
they are **Helix Atlas** — the name earns itself twice over: an atlas
*shows you where everything is*, and a Titan *carries the weight*.

Where [FD-0001](../0001-motion-intentions/README.md) made the machine
**honest**, FD-0002 makes it **intelligent**: it takes the honest data
FD-0001 already produces and builds the layer that *reads* it — a
companion that runs on the Raspberry Pi, is **deterministic where it
touches data and genuinely intelligent where it interprets**, and treats
this repository as a **shared brain**. This README was once the single
spine that argued that case; it has now split into the numbered series
below, and the deterministic substrate exists in code.

Status: **Split into the numbered series; the deterministic floor
(Milestone A) and the first patterns (Milestone B) are realized in the
[`atlas/`](../../../atlas/) package + [`src/trace.c`](../../../src/trace.c);
the Milestone C contracts and pinned Qwen3-4B workstation path are
CPU-validated; Pi 5 + Hailo-10H deployment remains hardware bring-up.
157 semantic checks across 20 test suites, all green.** Each document's own
status line records its precise state.

## The documents

Read in order, or jump by interest. Each is self-contained.

| Document | Contents | Status |
| --- | --- | --- |
| [00-Vision.md](00-Vision.md) | The thesis, the six principles, what Atlas is in one breath | Adopted — floor realized, model workstation-validated |
| [01-Compute-Tiers.md](01-Compute-Tiers.md) | Base / ASR-accel / Intelligence tiers; the Hailo constraint; the Qwen3 model choice; dev-target ≠ deploy-target | Adopted — deploy target pinned; `atlas/model/` realized |
| [02-Trace-Observability.md](02-Trace-Observability.md) | Plane 1: the structured, registered trace plane; the merged-timeline substrate | Realized in the Atlas floor (Milestone A) |
| [03-Blackbox-Decoder.md](03-Blackbox-Decoder.md) | Merging execlog + trace + link_stats + timesync + stock `klippy.log` into one narrative | Realized in the Atlas floor (Milestone A) |
| [04-Diagnosis-Engine.md](04-Diagnosis-Engine.md) | The deterministic pattern catalog, the empty-catalog principle, and the proactive health monitor | Realized (Milestone A); patterns seeded (Milestone B) |
| [05-Knowledge-Base.md](05-Knowledge-Base.md) | The shared brain: KB contents, report pipeline, redaction, the §6a lifecycle state machine + label vocabulary, trust & anti-poisoning | Framework realized in the Atlas floor (Milestone A) |
| [06-Provisioning-Fleet-Coherence.md](06-Provisioning-Fleet-Coherence.md) | Plane 3: the board catalog, one-touch provisioning, and fleet coherence as one signed mechanism | Realized in the Atlas floor (Milestone A) |
| [07-LLM-Layer.md](07-LLM-Layer.md) | Plane 4: interpret + generate/control, the risk tiers, the eval harness, voice, and the ecosystem seams | Contracts + pinned-model workstation path realized; target pending |
| [08-Roadmap.md](08-Roadmap.md) | The A–D milestones with their current state, and every (now settled) open decision | Milestones A/B realized; C workstation path green |

## Reading order

Start with [00-Vision.md](00-Vision.md). Then, by interest:

* *The whole story, front to back*: 00 → 01 → 02 → 03 → 04 → 05 → 06 → 07 → 08
* *"What is actually built?"*: 02 → 03 → 04 → 06 → 05 (the deterministic
  floor) → 07 (the contracts that are done) → 08 (the milestone map)
* *Firmware / constrained board*: 02 (the trace plane) → 01 (why the MCU
  never hosts the intelligence)
* *Safety & trust*: 07 (risk tiers, the non-LLM gate) → 05 (redaction, the
  signed KB lifecycle) → 01 (the deploy-budget guard)
* *Hardware & deployment*: 01 → 06 → 08 → the
  [Atlas Bring-up Plan](../../Atlas_Bring-up_Plan.md)

## Glossary

* **Atlas** — the intelligence layer of Helix Atlas: a Pi-resident
  companion that observes, understands, and acts on a HELIX machine. HELIX
  gives the machine honesty; Atlas gives it a mind.
  ([00-Vision.md](00-Vision.md))
* **HELIX** — the permanent, backwards-compatible fork of Klipper that
  moved the host↔MCU contract from step pulses to motion intentions
  (FD-0001). The honest foundation Atlas is built on.
* **Trace plane** — the structured, registered debug channel: a
  `DECL_TRACE` / `LOG(event, args…)` macro emitting *event id + typed
  args*, machine-time-stamped on Class-2 telemetry, rendered to a human
  string on the host via the dictionary. The substrate Planes 2–4 read.
  ([02-Trace-Observability.md](02-Trace-Observability.md); realized in
  [`src/trace.c`](../../../src/trace.c),
  [`atlas/decode/trace.py`](../../../atlas/decode/trace.py))
* **Blackbox decoder** — the component that merges every board's execution
  log + trace + `link_stats` + timesync + legacy `klippy.log` into one
  machine-time-ordered narrative and reconstructs machine state at a fault.
  Flight recorder → incident report.
  ([03-Blackbox-Decoder.md](03-Blackbox-Decoder.md); realized in
  [`atlas/decode/klippy_log.py`](../../../atlas/decode/klippy_log.py))
* **Diagnosis engine** — the deterministic failure-pattern catalog
  (signature → cause → fix + provenance + confidence) matched over the
  timeline; runs and reports even with an empty catalog ("no match → case
  captured"). ([04-Diagnosis-Engine.md](04-Diagnosis-Engine.md); realized
  in [`atlas/diagnosis/`](../../../atlas/diagnosis/))
* **KB lifecycle** — the public state machine (`case/new` → `case/triage` →
  `case/analysis` → `case/verify` → `accepted` / `rejected/*`) by which a
  submitted case becomes signed knowledge on GitHub Issues, with a
  fixed-vocabulary written rationale for every decision.
  ([05-Knowledge-Base.md](05-Knowledge-Base.md); vocabulary in
  [`atlas/kb/labels.yaml`](../../../atlas/kb/labels.yaml))
* **Fleet coherence** — making flashing and protocol-correctness the *same*
  mechanism: a protocol/ABI hash derived from `intentproto` is baked into
  every image and the host, checked at handshake, and a behind-board is
  brought into lockstep by an in-band **signed** flash.
  ([06-Provisioning-Fleet-Coherence.md](06-Provisioning-Fleet-Coherence.md);
  realized in [`atlas/fleet/`](../../../atlas/fleet/))
* **Compute tiers** — Base (Pi 5 CPU, deterministic only) · ASR-accel
  (Pi 5 + Hailo-8/8L → Whisper, no LLM) · Intelligence (Pi 5 + Hailo-10H /
  AI HAT+ 2, 8 GB → the LLM). The LLM tier is additive; Atlas is never
  *less* safe for lacking it. ([01-Compute-Tiers.md](01-Compute-Tiers.md))
* **Deploy profile** — the documented budget the eval harness always
  re-tests against; `--profile deploy` refuses any model past the
  Qwen3-4B / ~6 GB ceiling, so a big dev card can't tempt the project past
  what the Hailo-10H can run. The guard, not the hardware, keeps the budget
  honest. ([01-Compute-Tiers.md](01-Compute-Tiers.md); realized in
  [`atlas/model/profile.py`](../../../atlas/model/profile.py))
* **Deterministic floor** — everything below the LLM line: the trace
  collector, decoder, diagnosis rules, monitor, provisioner, fleet
  coherence, and KB framework. Ordinary CPU code with **no accelerator
  dependency**; the part that is already built and gates every commit.
* **Risk tier** — the deterministic (non-LLM) classification of a proposed
  change as catastrophic (always confirm), consequential-reversible
  (auto-apply with undo), or cosmetic (auto-apply). The safety gate never
  depends on the model. ([07-LLM-Layer.md](07-LLM-Layer.md); realized in
  [`atlas/apply/classify.py`](../../../atlas/apply/classify.py))

## Where to start building

* [HANDOFF.md](HANDOFF.md) — the **development handoff & kickoff**:
  everything a fresh instance needs to begin work, including the crucial
  **dev-target (NVIDIA/AMD GPU) ≠ deploy-target (Hailo-10H)** distinction,
  the milestone-A task breakdown, and the settled decisions. Read it after
  the canon.
* [Atlas Bring-up Plan](../../Atlas_Bring-up_Plan.md) — the tickable
  verification ladder from "the deterministic floor runs anywhere" to "the
  intelligence tier works on the Pi 5 + Hailo-10H." The Atlas analogue of
  FD-0001's [Helix Test & Bring-up Plan](../../Helix_Test_Plan.md).
* The implementing package: [`atlas/`](../../../atlas/) (see its
  [README](../../../atlas/README.md)) and the firmware trace plane
  [`src/trace.c`](../../../src/trace.c) / [`src/trace.h`](../../../src/trace.h).

## Relationship to FD-0001 and existing docs

Atlas *reuses* the HELIX foundations rather than reinventing them: machine
time and the execution log ([FD-0001 doc 01](../0001-motion-intentions/01-Time_Model.md),
`src/execlog.c`), the intentproto annotation/registry and dictionary, the
Ed25519 image signing, the traffic classes, and the capability
advertisement — all of [FD-0001](../0001-motion-intentions/README.md) is
the substrate Atlas reads. The authoritative descriptions of current
behavior remain [Code_Overview.md](../../Code_Overview.md),
[Protocol.md](../../Protocol.md), and [MCU_Commands.md](../../MCU_Commands.md);
these founding documents record the *why*.
