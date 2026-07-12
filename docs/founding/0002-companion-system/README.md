# Founding Document 0002 — The Companion System

*Shorthand: **FD-0002**.* This is a **founding document** of HELIX: the
design canon for the layer that turns HELIX from motion-and-comms
firmware into **intelligent software** — a companion that watches the
machine, understands it, diagnoses itself, provisions and heals its own
fleet, and gets smarter as its users teach it.

Status: **Planning.** Nothing here is implemented yet. This document is
the spine; on review it splits into the numbered FD-0002 series (mirroring
[FD-0001](../0001-motion-intentions/README.md)) and the first milestones
begin. Read it as the argument for *what we are about to build and why*.

> **The thesis.** [FD-0001](../0001-motion-intentions/00-Vision.md) made the
> machine **honest** — a micro-controller that owns its clock, its
> position, and its queue, and that records what it actually did. That
> was the hard part, and it is done. FD-0002 makes the machine
> **intelligent**: it takes the honest data FD-0001 already produces and
> builds the layer that *reads* it — a companion that runs on the
> Raspberry Pi, is deterministic where it touches data and genuinely
> intelligent where it interprets, and treats this repository not as
> source code but as a **shared brain**: known-failure knowledge, board
> and machine catalogs, and the weights, prompts, and memory that power a
> local assistant, all versioned together. HELIX stops being another
> firmware and becomes a system that helps you run, debug, and maintain a
> machine — and, in time, one you can simply talk to.

---

## 1. Principles

These are the non-negotiables every later section answers to.

1. **Local-first, no phone-home by default.** HELIX 0.9 stripped
   analytics and put "does not phone home" in the docs. The companion
   keeps that promise: everything runs on the Pi, offline-capable. Any
   sharing of a machine's data is **opt-in, per-event, and redacted**.
2. **Determinism for data; intelligence for interpretation.** The facts —
   the merged timeline, the rule matches, the ABI versions — are produced
   by auditable, deterministic code. The **LLM interprets, explains, and
   proposes**; it never silently acts on a safety-critical path. Every
   model-proposed change is a concrete diff that is deterministically
   validated and user-confirmed before it applies.
3. **Tiered compute — graceful degradation.** The companion runs on a
   bare **Raspberry Pi 5** with everything deterministic (decoder,
   diagnosis rules, monitoring, provisioning) as ordinary programs. Add an
   **AI accelerator** (a Hailo-class NPU / the Pi AI HAT, or a
   Strix-Halo–class host) and the **intelligence tier** switches on: local
   **open-weight** models for interpretation, natural-language config and
   control, and (later) voice. Nothing *requires* the accelerator; the
   LLM tier is purely additive.
4. **The repository is a knowledge base, not just code.** Failure
   patterns, board and config catalogs, and the model configuration +
   memory files that make "our" assistant *ours* live and version in this
   repo, alongside the source. Knowledge is a first-class, signed,
   reviewable artifact.
5. **Reuse the ecosystem; build only the missing organs.** Moonraker +
   Mainsail is the stack. We add components and panels where they reach,
   and build new services (trace viewer, provisioning wizard, companion
   daemon) only where they cannot.
6. **The constrained board still comes first.** Everything the firmware
   side adds (the trace plane above all) must be near-zero-cost when off
   and cheap when on, and must fit the 16 KB F072. Features that don't
   fit aren't built there — the intelligence lives on the Pi, not the MCU.

---

## 2. Compute tiers — two Pi 5 profiles

The repo ships **two selectable profiles**; the companion auto-detects
which hardware it is on and enables the matching tier.

| | **Base tier** (Pi 5, no accelerator) | **Intelligence tier** (Pi 5 + accelerator / "Halo") |
| --- | --- | --- |
| Trace + blackbox decode | ✓ deterministic | ✓ |
| Diagnosis **rules** engine | ✓ deterministic | ✓ |
| Live health monitor + anomaly | ✓ deterministic | ✓ |
| Provisioning + fleet coherence | ✓ | ✓ |
| **LLM interpretation** of unmatched cases | small CPU model or off | ✓ local open-weight model |
| **NL config / control** ("change the display to…") | — | ✓ |
| Report synthesis / RAG over the KB | templated | ✓ model-written |
| Voice (future) | — | ✓ local ASR → intent pipeline |

The point of the split is that a **$0 upgrade path** exists: buy the
accelerator, the same companion becomes conversational. The deterministic
floor never depends on it, so a machine is never *less* safe for lacking
one.

---

## 3. Plane 1 — Observe: the structured trace plane  *(Milestone A — START)*

The gap you named: there is no real MCU debug. OAMS's CAN `printf` proved
how badly it's needed and how useful it is to *see what an MCU is doing*.
Free-form `printf` is the wrong answer for HELIX, though — it is expensive
on an F072, it bloats the wire, and it is unparseable, which kills every
downstream ambition in this document.

**The HELIX-native answer: a structured, registered trace channel.**
A `DECL_TRACE` / `LOG(event, args…)` macro emits an *event id + typed
args*, machine-time-stamped, on Class-2 telemetry, and is **rendered to a
human string on the host** via the dictionary — exactly the
annotation/self-description mechanism the command registry already uses.
The firmware author gets `printf` ergonomics
(`LOG(step_underrun, horizon_us, queue_depth)`); the wire gets a few
bytes; the host gets a stream a machine can reason about. Per-subsystem
trace levels, near-zero cost when off.

Because every event carries **machine time**, traces from the mainboard,
a CAN toolhead, and an ESP32 accessory **merge into one timeline** — the
substrate Planes 2–4 all read.

Deliverables: the firmware macro + a small IRAM-safe ring; a host trace
collector; the merged-timeline store; a live viewer (a Mainsail panel if
it reaches, else a standalone). This lands first because everything else
gets easier once the machine can *talk*.

---

## 4. Plane 2 — Understand: the blackbox decoder + diagnosis engine

**Decoder.** Merge every board's execution log + trace events +
`link_stats` + timesync state into one machine-time-ordered narrative and
reconstruct machine state at the moment of a fault. Flight recorder →
incident report. It also ingests the legacy `klippy.log` so it is useful
on day one, on any Klipper machine, before a single new board ships.

**Diagnosis engine.** A deterministic **failure-pattern catalog** (YAML,
in this repo) maps a symptom signature → likely cause → suggested fix,
with provenance and a confidence score. Thermal runaway, comms-timeout →
pause, queue underrun, CRC storms on a flaky wire, endstop bounce,
commanded-vs-executed divergence (lost steps), TMC UART errors. **It runs
even when the catalog is empty** — the framework matches nothing, says so
plainly, and *captures the case* as a candidate. An empty knowledge base
is a starting condition, not a blocker.

**Where the LLM enters (intelligence tier).** Unmatched cases go to the
local model, which interprets the timeline, writes the human explanation,
and **proposes a candidate rule** for human review — never auto-promoted.
The deterministic catalog stays the authority; the model widens its
reach.

---

## 5. Plane 3 — Act: monitoring, provisioning, fleet coherence  *(Milestone A — START)*

**Proactive health monitor.** The diagnosis engine pointed at *live*
telemetry instead of a post-mortem. It learns a **baseline fingerprint**
of a healthy machine and flags drift before failure — rising CRC rate,
widening sync error, creeping thermistor noise, slow position divergence.
This is the difference between a flight recorder and a companion.

**Board catalog + one-touch provisioning.** Pick a **board**, not a chip.
The catalog entry for "BTT Octopus" / "OAMS mainboard" / "ESP32 devkit"
carries its MCU, pin aliases, flash method, the Kconfig fragment, and a
**curated default config** — with **"Custom"** as the full escape hatch.
The companion auto-detects connected boards (USB / CAN / DFU / Katapult),
matches them to the catalog, and builds + flashes the right image in one
action, over the first-class bootloader FD-0001 already ships.

**Fleet coherence — the lockstep answer.** This is the keystone that ties
flashing to correctness. Protocol correctness depends on the host, the
`intentproto` library, and every board's firmware agreeing on the wire
contract. So the **library is the single version authority**: a
protocol/ABI hash derived from `intentproto` is baked into every image
and the host, checked at handshake (building on `HELIX_STATUS` /
`BOARD_SYSCALL_ABI` / `FRAMING_V2`), and when a board is **behind**, the
host offers or performs the in-band **signed** flash that brings it into
lockstep. Auto-flash and protocol-correctness become the *same
mechanism*, not two features — which is exactly why the version-sync
worry and the flashing pain are one problem, solved once.

**One config repository.** Versioned board + machine configs with
defaults, and an update path that can flash the fleet to the matching
release. The catalog is data in this repo, reviewed like code.

---

## 6. The knowledge base &amp; the coordination model

This is the part that makes the repo a **shared brain**. It is also the
part with the sharpest trust and security questions, so it is specified,
not hand-waved.

**What the KB contains** (all versioned, reviewable, and **signed** so a
machine can trust an update — reusing FD-0001's Ed25519):
- the **failure-pattern catalog** (signature, cause, fix, provenance,
  confidence);
- the **board** and **config** catalogs;
- the **model configuration + memory files** that define our assistant
  (system prompts, RAG index build, per-family model pins).

**The report pipeline.**
1. On a failure the companion assembles a **blackbox bundle** — the
   merged timeline, the diagnosis (or "no pattern matched"), versions.
2. It is **redacted by default** and **never leaves the Pi without
   explicit, per-event consent**: strip network secrets, keys, PSKs,
   file names/paths, and anything not needed to reproduce the fault.
3. On opt-in submit, the bundle becomes a **GitHub Issue** through a
   structured template + labels — GitHub is the intake and the audit log.

**Users as real-time trainers.** The UI carries lightweight feedback —
"did this diagnosis match?", "did this fix work?" — attached to the case.
Verified outcomes raise a candidate's confidence.

**Acceptance → promotion (governance).** A raw submission **never
auto-influences another machine.** Promotion to the shared catalog is
gated: a maintainer (assisted by the model's triage) reviews the case, a
deterministic check confirms the signature is well-formed and
non-conflicting, the entry is signed, and only then is it published to
the KB where other machines will pull it. Confidence and provenance ride
with every entry.

**Security of aggregation (poisoning, privacy, consent).**
- **Privacy:** local-first; opt-in; redacted; minimized. Nothing about a
  machine is collected that the fault doesn't require.
- **Poisoning defense:** the human + deterministic gate before promotion;
  provenance and submitter reputation; signed entries; the model can
  *propose* but not *promote*.
- **Trust on pull:** machines accept KB updates only if signed by the
  project key — the same discipline as firmware images.

---

## 7. Plane 4 — The LLM as interpreter *and* actuator

The features Klipper isn't even considering. Two modes, one guardrail.

**Interpret.** Explain an error in plain language, summarize an incident,
answer "why did my print fail?" — grounded by RAG over the KB and the
machine's own **memory file** (its history, quirks, learned baselines).

**Generate &amp; control.** "Change the display menu so it does X."
"Add a macro that…". "Verify my config for errors." The model produces a
**concrete diff or action**, which is then **deterministically validated
and shown to the user for confirmation** before anything applies. Nothing
safety-critical is ever auto-applied — the model drafts, the deterministic
layer checks, the human commits. This is the same
NL → structured → validate → confirm discipline throughout.

**Voice (future, opt-in).** A microphone → local ASR → the *same* intent
pipeline. No new trust surface: a spoken request is just another way to
produce a proposed, validated, confirmed action.

---

## 8. Reusing the ecosystem — the seams

- **Moonraker components** for anything that is API/state plumbing
  (report submission, provisioning jobs, KB pulls, feedback capture).
- **Mainsail panels** for the trace viewer, the diagnosis/incident view,
  the provisioning wizard, and the companion chat — if they reach.
- **A standalone companion daemon** only for what the stack can't host:
  the always-on monitor, the local-model runtime, the merged-timeline
  store. Named seams, minimal new surface.

---

## 9. Roadmap

Each milestone states what is deterministic vs LLM, and what runs on the
base vs the intelligence tier.

- **Milestone A — Observe &amp; Provision (START).** Plane 1 structured
  trace end-to-end; Plane 3 provisioning/flash + the fleet-coherence
  handshake; **and the KB *framework* live but empty** — the decoder,
  the diagnosis harness over `klippy.log` + the blackbox aggregator, and
  the report scaffolding all run and produce "no known pattern (case
  captured)" from day one. Deterministic; base tier.
- **Milestone B — Understand.** The diagnosis rule engine + the first
  curated failure patterns; the GitHub intake, feedback, and
  acceptance→promotion workflow; signed KB pulls. Deterministic core;
  base tier; model-assisted triage where available.
- **Milestone C — Intelligence.** The local open-weight model:
  interpretation of unmatched cases, NL config/control (draft → validate →
  confirm), RAG over KB + per-machine memory. Intelligence tier.
- **Milestone D — Companion at scale.** Proactive baselines + anomaly
  detection maturing; the users-as-trainers loop running at fleet scale;
  voice (future). Both tiers; intelligence tier for voice/NL.

---

## 10. Open decisions (for review before we split &amp; build)

1. **The companion-service boundary** — how much lives as Moonraker
   components vs a standalone HELIX daemon. (Affects everything downstream.)
2. **Redaction policy** — the exact default-strip list for shared bundles,
   and whether any field is *ever* sent unredacted.
3. **KB trust model** — single project signing key vs a web of maintainer
   keys; submitter reputation mechanics.
4. **Target model family** — which open-weight model(s) we pin for the
   accelerator tier, and the minimum accelerator we design against.
5. **Auto-apply boundary** — is *anything* ever applied without an
   explicit confirm (e.g. a purely cosmetic display tweak), or is
   confirm-always an inviolable rule?

*When these are settled, FD-0002 splits into the numbered series and
Milestone A begins.*
