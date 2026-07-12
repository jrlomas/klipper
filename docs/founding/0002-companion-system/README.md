# Founding Document 0002 — Atlas, the Companion System

*Shorthand: **FD-0002**.* This is a **founding document** of HELIX: the
design canon for **Atlas** — the layer that turns HELIX from
motion-and-comms firmware into **intelligent software**. Atlas is the
companion that watches the machine, understands it, diagnoses itself,
provisions and heals its own fleet, and gets smarter as its users teach
it. HELIX gives the machine honesty; **Atlas** gives it a mind. Together
they are **Helix Atlas** — the name earns itself twice over: an atlas
*shows you where everything is*, and a Titan *carries the weight*.

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
3. **Tiered compute — graceful degradation.** Atlas runs on a bare
   **Raspberry Pi 5** with everything deterministic (decoder, diagnosis
   rules, monitoring, provisioning) as ordinary programs. Add the
   LLM-capable accelerator (the **Hailo-10H** / Raspberry Pi **AI HAT+ 2**,
   or a Strix-Halo–class host) and the **intelligence tier** switches on:
   a local **open-weight** model for interpretation, natural-language
   config and control, and (later) voice. Nothing *requires* the
   accelerator; the LLM tier is purely additive, and Atlas never becomes
   *less* safe for lacking one.
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

## 2. Compute tiers — the hardware reality

The repo ships **selectable profiles**; Atlas auto-detects the hardware
and enables the matching tier. The important, non-obvious fact drives the
whole design:

> **Only the Hailo-10H can host an LLM.** The Raspberry Pi **AI HAT+ 2**
> (Hailo-10H, ~40 TOPS, **8 GB** of dedicated on-board LPDDR4X) can run a
> language model *on the accelerator*. The earlier **Hailo-8 / 8L**
> (26 / 13 TOPS) are vision/CNN NPUs — they **cannot** run an LLM, but
> they *can* accelerate **Whisper** (speech-to-text), which is exactly
> what the voice feature needs. So the accelerator tier is really two
> distinct capabilities: **LLM (needs the 10H)** and **ASR (any Hailo, or
> CPU)**.

| | **Base** (Pi 5 only) | **ASR-accel** (Pi 5 + Hailo-8/8L) | **Intelligence** (Pi 5 + Hailo-10H / AI HAT+ 2) |
| --- | --- | --- | --- |
| Trace + blackbox decode | ✓ | ✓ | ✓ |
| Diagnosis **rules** engine | ✓ | ✓ | ✓ |
| Live monitor + anomaly | ✓ | ✓ | ✓ |
| Provisioning + fleet coherence | ✓ | ✓ | ✓ |
| **LLM** interpretation / NL config / control | tiny CPU model or off | tiny CPU model or off | ✓ on-accelerator |
| Report synthesis / RAG over KB | templated | templated | ✓ model-written |
| **Voice** (future) | — | ✓ Whisper on Hailo | ✓ Whisper + LLM |

**The model we design against.** The **Qwen3 dense family** —
0.6B / 1.7B / 4B / 8B / 14B / 32B, all with a 128K context window, **tool
calling**, **structured output**, and dual-mode thinking (a slow
reasoning mode for a hard diagnosis, a fast mode for routine work). Tool
calling + structured output are precisely what Atlas's
draft → validate → apply discipline requires, so this family is not a
fashion pick — it is a functional requirement.

- **Default (Hailo-10H, 8 GB):** **Qwen3-4B** at Q4_K_M (~2.5–3 GB) — fits
  the on-board RAM with headroom for context and the ASR model. Fallback
  **Qwen3-1.7B** on tighter budgets; stretch **Qwen3-8B** where it fits.
- **Power host (Strix-Halo–class):** Qwen3-8B/14B comfortably.
- **Base (Pi 5 CPU):** a 0.6B/1.7B model is possible but slow (a few
  tok/s); treat CPU-LLM as "works, not pleasant," and keep everything
  load-bearing in the deterministic floor.
- **Pinning:** the exact model + quantization + revision is **data in the
  repo** (§6), versioned with the prompts and memory, so "our Atlas" is
  reproducible and can be re-pinned as the Qwen line advances (3.5/3.6 …)
  or as Hailo's model zoo compiles newer weights.

The payoff of the split: a **clean upgrade path** — a bare Pi 5 runs the
whole deterministic companion today; add the AI HAT+ 2 and the same Atlas
becomes conversational, with no change to the safety floor.

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
auto-influences another machine.** Promotion is a transparent,
public process on GitHub — the rationale for *every* accept and reject is
visible, so the community can see **why** a pattern did or did not become
part of the shared brain. This is specified in full in §6a below.

**Security of aggregation (poisoning, privacy, consent).**
- **Privacy:** local-first; opt-in; redacted; minimized. Nothing about a
  machine is collected that the fault doesn't require.
- **Poisoning defense:** the human + deterministic gate before promotion;
  provenance and submitter reputation; signed entries; the model can
  *propose* but not *promote*.
- **Trust on pull:** machines accept KB updates only if signed by the
  project key — the same discipline as firmware images.

---

## 6a. How reports are aggregated &amp; accepted (the KB lifecycle)

The knowledge base is a public asset, so **how a case becomes knowledge
is itself public.** Nothing is promoted in a back room; every decision —
accept *or* reject — leaves a readable rationale in the open. The
mechanism is ordinary GitHub, used deliberately.

**State machine.** Every submission is a GitHub Issue that moves through
labelled states, and the label *is* the audit trail:

1. `case/new` — an opt-in, redacted blackbox bundle arrives as an Issue
   from a structured template (symptom, merged-timeline excerpt,
   diagnosis or "no match", firmware/host/library versions, Atlas's
   proposed rule if any). A bot validates the template and attaches a
   **content hash** and the submitter's provenance.
2. `case/triage` — deduplicated against existing cases and open patterns
   (Atlas assists by clustering similar bundles). Duplicates are linked to
   the canonical case, raising its **observation count**, not spawning
   noise.
3. `case/analysis` — a candidate **pattern** is drafted: signature →
   cause → fix, with a confidence seed. This is a proposed change to the
   catalog data, opened as a **pull request linked to the Issue**, so the
   exact diff to the shared brain is reviewable.
4. `case/verify` — the fix is corroborated: reproduction, a
   deterministic check that the signature is well-formed and does **not
   conflict** with an existing pattern, and real-world **"did this fix
   work?"** feedback from other machines that hit it. Confidence rises
   with independent confirmations.
5. `accepted` **or** `rejected/*` — a maintainer merges the PR (the
   pattern enters the signed catalog) **or** closes it with a
   **`rejected/<reason>`** label. Either way the closing comment states
   the rationale in plain language.

**Every decision carries its "why."** Acceptance and rejection both
require a written rationale on the Issue, drawn from a fixed, public
vocabulary so reasons are consistent and searchable:

- Accept reasons: `reproduced`, `multi-machine-confirmed`,
  `root-cause-clear`, `fix-verified`.
- Reject reasons: `rejected/not-reproducible`,
  `rejected/machine-specific` (a local quirk, not general knowledge),
  `rejected/duplicate`, `rejected/insufficient-data`,
  `rejected/unsafe-fix`, `rejected/superseded`.

A reader can therefore open the KB's Issue tracker and see not just
*what* Atlas knows, but the *entire argument* for every entry — and for
everything the project decided **not** to learn, which is often the more
instructive record.

**Promotion gate (what actually changes the shared brain).** A pattern
influences other machines **only** when: its PR is merged into the
catalog on the default branch, the catalog is **Ed25519-signed** by the
project key, and machines pull the signed update. So the model can
*propose* a pattern (step 3), the community can *corroborate* it
(step 4), but only a **reviewed, merged, signed** change reaches the
fleet — the same trust discipline HELIX already applies to firmware
images. Raw submissions never touch another machine.

**Confidence &amp; decay.** Each accepted pattern carries a confidence that
rises with independent confirmations and **decays** if later cases
contradict it or a HELIX/Atlas release supersedes the underlying cause —
so the brain forgets stale lessons instead of hoarding them. A
contradicted pattern re-enters the lifecycle at `case/verify` rather than
being silently trusted.

**Anti-poisoning, restated concretely.** Because the only path to the
fleet is a signed merge to the catalog, a bad actor cannot inject
knowledge by spamming submissions: they can open Issues (which are
public and deduplicated), but they cannot merge, sign, or bypass the
`case/verify` corroboration. Submitter provenance and reputation weight
triage priority, never the final gate.

---

## 7. Plane 4 — The LLM as interpreter *and* actuator

The features Klipper isn't even considering. Two modes, one guardrail.

**Interpret.** Explain an error in plain language, summarize an incident,
answer "why did my print fail?" — grounded by RAG over the KB and the
machine's own **memory file** (its history, quirks, learned baselines).

**Generate &amp; control.** "Change the display menu so it does X."
"Add a macro that…". "Verify my config for errors." Atlas produces a
**concrete diff or action**, which is then **deterministically
validated** before anything happens. What comes next depends on a
**risk tier**, not a blanket rule:

| Tier | Examples | Behaviour |
| --- | --- | --- |
| **Catastrophic / safety-affecting** | thermal limits, endstop/probe config, kinematics, driver current, anything that can damage the machine or a person | **Always confirm.** Never auto-applied. |
| **Consequential but reversible** | pin remaps, macro logic, speed/accel defaults | Auto-apply **allowed**, with a one-click undo and an audit entry; a preview is offered. |
| **Cosmetic / non-functional** | display menu layout, UI labels, colours, wording | **Auto-apply.** |

Every applied change — at any tier — is journaled to the machine's
memory with its diff, so *undo* and *"what did Atlas change?"* are always
answerable. The rule is **"auto-apply when not catastrophic,"** with a
deterministic classifier (not the model) deciding the tier from the diff,
so the safety gate never depends on the LLM's judgement. Atlas drafts;
the deterministic layer classifies and validates; the human is asked only
when the stakes justify it.

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

**Settled (this review):**
- ✅ **Companion boundary** — **Moonraker** owns anything that *changes
  but is polled or API'd* (state, jobs, report submission, KB pulls,
  feedback capture). A **standalone Atlas daemon** owns the always-on
  monitor, the local-model runtime, and the merged-timeline store.
- ✅ **Model &amp; accelerator** — pin the **Qwen3 dense family**; default
  **Qwen3-4B (Q4_K_M)**; design the intelligence tier against the
  **Hailo-10H / AI HAT+ 2** (8 GB). Base and ASR-accel tiers run the
  deterministic floor without an LLM.
- ✅ **Auto-apply** — **"auto-apply when not catastrophic,"** with a
  deterministic (non-LLM) classifier setting the risk tier and every
  change journaled + undoable (§7).
- ✅ **Acceptance is public** — the full KB lifecycle runs on GitHub
  Issues with a written, fixed-vocabulary rationale for every accept and
  reject (§6a).

**Settled (implementation kickoff — 2026-07-12):** the four remaining items
are now decided; the rationale for each is in
[HANDOFF §6](HANDOFF.md#6-open-questions--resolve-with-the-user-before-building-the-affected-part).
- ✅ **Redaction policy — numeric-only unredacted.** Three tiers, all
  versioned and unit-tested in the deterministic floor: *(a) always-share*
  — versions + ABI hash, board **model**/MCU family (from the catalog, not
  the physical serial), kinematics type, trace event ids + **numeric**
  args, execlog numeric fields, `link_stats` counters, timesync numeric
  state, diagnosis + confidence; *(b) transform-then-share* — file paths →
  basename or dropped, string/free-text args dropped, wall-clock →
  relative machine-time offsets; *(c) never-share, no allowlist override
  possible* — secrets/keys/PSKs/tokens, hostnames/IPs/MACs,
  serials/UUIDs, account identifiers. So *yes*, some fields ship
  unredacted — **numeric diagnostics only**; every string is redacted and
  secrets cannot be allowlisted at all.
- ✅ **KB trust model — single project key now, multi-signer-ready.** One
  project Ed25519 signing key (reusing FD-0001's image-signing:
  `scripts/sign_image.py`, `keys/`), with a signature **envelope** that
  already carries a signer list + threshold, so migrating to a maintainer
  web-of-trust later is a policy change, not a format break. Submitter
  reputation (derived from public GitHub history) weights **triage
  priority only**, never the promotion gate.
- ✅ **ASR engine — deferred to Milestone D, default recorded.**
  whisper.cpp + Whisper small/base on CPU is the default direction;
  Hailo-8/8L ASR is second-class ("works on CPU" is the floor). Revisited
  when voice work begins; it blocks nothing earlier.
- ✅ **Model runtime, eval, Hailo-budget.** Deploy backend **llama.cpp**
  (CUDA + ROCm + CPU, GGUF/Q4_K_M) behind a `ModelBackend` abstraction,
  **Ollama** for scratch iteration, vLLM only for throughput experiments.
  **Eval harness** stood up early and stub-model-first (§8 tier 2):
  diagnosis accuracy vs a labelled case set, config-edit correctness vs
  golden diffs, and — the load-bearing metric — correct **refusal/confirm
  on the safety tier**. **Budget discipline** is a documented *deploy
  profile* the harness always re-tests against (`--profile deploy` refuses
  any model past the Qwen3-4B / ~6 GB ceiling). The dev workstation's dev
  GPUs are an AMD Radeon (~16 GB, ROCm, primary) with an 8 GB NVIDIA that
  also drives the display (≈5–6 GB usable, fallback) — so the profile
  guard, not the hardware, is what keeps the deploy budget honest.

*The open items are settled; FD-0002 splits into the numbered series
and Milestone A (Observe + Provision) begins. Development starts from the
[Atlas Development Handoff](HANDOFF.md), which carries everything a fresh
instance needs to begin work — including the crucial **dev-target
(NVIDIA/AMD GPU) ≠ deploy-target (Hailo-10H)** distinction.*
