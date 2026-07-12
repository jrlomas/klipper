# Atlas — Development Handoff &amp; Kickoff

**Purpose.** This document exists to transfer everything a fresh instance
of the assistant needs to begin building **Atlas** (FD-0002) from a
standing start, on a workstation with real GPUs. It is written to be
read *first*, before touching code. If you are that instance: read this
top to bottom, then read the two documents it points you at, then
confirm the open questions with the user before you write anything.

> **One-line orientation.** HELIX is a permanent, friendly fork of Klipper
> that moved the host↔MCU contract from step pulses to **motion
> intentions** (that work is *done* — HELIX 0.9, software-complete). **Atlas**
> is the intelligence layer we are now building *on top* of the honest data
> HELIX already produces — a Pi-resident companion that observes,
> understands, and acts. The project as a whole is **Helix Atlas**.

---

## 0. The first hour (do this in order)

1. **Read the canon**, in this order:
   - [FD-0002 README](README.md) — the Atlas design canon. This HANDOFF
     is the *how to start*; FD-0002 is the *what and why*. Do not
     duplicate it in your head — trust it as the source of truth.
   - [FD-0001 00-Vision](../0001-motion-intentions/00-Vision.md) and its
     [README](../0001-motion-intentions/README.md) — the architecture Atlas
     sits on. You must understand machine time, the execution log, traffic
     classes, capability advertisement, and the intentproto library,
     because Atlas *reuses all of them*.
   - [Helix Test &amp; Bring-up Plan](../../Helix_Test_Plan.md) — the model
     for how we verify. Atlas will need its own equivalent (see §8).
2. **Confirm the environment** (§2). You have GPUs now — prove it and pick
   a model runtime.
3. **Resolve the open questions** (§6) with the user. Three were left open
   deliberately; do not guess them.
4. **Start Milestone A** (§5). The trace plane and the decoder are the
   substrate everything else reads — begin there.

---

## 1. Where things are

**Three repositories**, all developed on branch
**`claude/software-redesign-impl-finn0j`**:

| Repo | Role |
| --- | --- |
| `jrlomas/klipper` | The HELIX host + MCU firmware + `lib/intentproto` + **all the docs** (this file lives here). The primary repo. |
| `OpenAMSOrg/klipper_openams` | OAMS host extras (`oams.py`, `oams_manager.py`, `fps.py`, `hdc1080.py`). |
| `OpenAMSOrg/mainboard-firmware` | OAMS mainboard firmware (STM32F072, 16 KB RAM — the constrained target). |

**Live artifacts:** the docs site builds from `.github/workflows/helix-docs.yaml`
to GitHub Pages (`https://jrlomas.github.io/klipper/`); the landing page
is `docs/helix-landing.html`.

**The map you'll reuse most** (HELIX foundations Atlas is built on — read
these before designing anything):

| Foundation | Where | Why Atlas needs it |
| --- | --- | --- |
| Protocol library, annotation/registry, dictionary, extension self-description, Ed25519 signing | `lib/intentproto/` (`include/intentproto/proto.hpp` is the spine) | The **trace plane** registers events the same way commands are registered; the decoder reads the dictionary as its symbol table; signing secures KB pulls and images. |
| Execution log ("flight recorder") | `src/execlog.c` / `.h` | Primary raw input to the **blackbox decoder**. |
| Machine time | `src/timesync.c`, `klippy/extras/timesync.py`, FD-0001 doc 01 | The **merge key** that orders multi-MCU events into one timeline. |
| Capability / version advertisement | `klippy/extras/helix_status.py` (`HELIX_STATUS`), `BOARD_SYSCALL_ABI`, `FRAMING_V2` | Seed of **fleet coherence**: what a board *is* and *speaks*. |
| First-class bootloader + signed images | `src/boot_app/`, `lib/intentproto/boot/`, `scripts/sign_image.py`, `keys/` | The **auto-flash** mechanism for provisioning + lockstep remediation. |
| Host subsystems Atlas observes | `klippy/extras/failure_recovery.py`, `trajectory_queuing.py`, `trajectory_pwm.py` | Sources of state + events for monitoring and diagnosis. |
| Traffic Class 2 (telemetry) | FD-0001 doc 03 | The transport for trace events + live monitoring. |

---

## 2. The development environment — and the one distinction that matters

**Previous session (where the plan was written):** a CPU-only cloud
sandbox — 4 vCPUs, ~15 GB RAM, **no GPU/NPU**, filtered network,
ephemeral. That is why the plan could be written but the intelligence tier
could not be tested there.

**Your session (the reason for this handoff):** a VS Code workstation with
**NVIDIA and AMD GPUs**. This unblocks the entire model side — you can run
Qwen3, run Whisper, build the RAG index, and evaluate Atlas end to end.

> **The distinction you must hold onto: dev target ≠ deploy target.**
> You will *develop and validate* the model layer on **NVIDIA (CUDA) /
> AMD (ROCm)** GPUs, because they are fast and present. But Atlas
> *deploys* on a Raspberry Pi 5 + **Hailo-10H** NPU (the AI HAT+ 2), which
> is a different runtime with a hard ~8 GB budget and a model-compilation
> step. Consequences for how you build:
> - **Abstract the model runtime behind an interface** (a `ModelBackend`
>   with `cuda` / `rocm` / `hailo` / `cpu` implementations). Never let
>   CUDA-only assumptions leak into the deployable path.
> - **Develop against the Hailo budget, not the GPU's.** A 24 GB card will
>   happily run Qwen3-14B; the deploy target runs **Qwen3-4B Q4_K_M in
>   ~3 GB**. Pin and evaluate the *deploy* model even while a bigger one is
>   available for experimentation.
> - **Treat the Hailo bring-up as its own checklist** (§8), the way
>   FD-0001's hardware bring-up is a checklist. GPU-green is necessary,
>   not sufficient.
> - The **deterministic floor has no accelerator dependency at all** — it
>   is ordinary CPU code and must stay that way.

Concrete first steps in the new environment: confirm `nvidia-smi` and
`rocminfo`/`rocm-smi`; choose a runtime (llama.cpp with CUDA+ROCm builds
is the most portable toward a Hailo/CPU story; Ollama is fine for fast
iteration; vLLM only if you want throughput experiments); pull
**Qwen3-4B** at Q4_K_M as the pinned deploy model and a larger Qwen3 for
headroom experiments.

---

## 3. Current state — the honest truth

- **HELIX 0.9** is **software-complete, hardware-unvalidated.** The
  motion/comms redesign (FD-0001) is fully implemented and passes the
  off-silicon suites; nothing has run on a printer. The path to 1.0 is
  `docs/Helix_Test_Plan.md`.
- **Atlas is at planning.** FD-0002 (its README) is written and complete
  as a design canon. **No Atlas code exists yet.** This is greenfield on a
  well-specified foundation.
- The `RFC → Founding Document` rename is done (FD-0001/FD-0002; the old
  `docs/rfcs/` path is now `docs/founding/`). If you see "RFC 0001"
  anywhere in *our* material, it's a miss — but leave IETF citations
  (RFC 8032/5869/…) and STM32 register names (`RFC_Msk`) alone.

---

## 4. Decisions already made — do not relitigate

These are settled. Build on them.

- **Name.** The companion is **Atlas**; the project is **Helix Atlas**.
- **Determinism vs intelligence.** Deterministic, auditable code produces
  *facts* (timeline, rule matches, ABI versions). The **LLM interprets and
  drafts**; it is never on a safety-critical path and never the authority.
- **Compute tiers.** Base (Pi 5 CPU, deterministic only) · ASR-accel
  (Pi 5 + Hailo-8/8L → Whisper only, **no LLM**) · Intelligence (Pi 5 +
  **Hailo-10H / AI HAT+ 2, 8 GB → the LLM**). Design against the 10H.
- **Model.** The **Qwen3 dense family** (tool-calling + structured output
  are functional requirements, not preferences). Default **Qwen3-4B
  Q4_K_M**; the exact pin is versioned *data* in the repo.
- **Auto-apply is risk-tiered** — "auto-apply when not catastrophic." A
  **deterministic, non-LLM classifier** sets the tier from the diff:
  safety-affecting → always confirm; consequential-reversible → auto-apply
  with undo + audit; cosmetic → auto-apply. Everything journaled and
  reversible.
- **Component boundary.** **Moonraker** owns state that *changes but is
  polled/API'd* (jobs, report submission, KB pulls, feedback). A
  **standalone Atlas daemon** owns the always-on monitor, the model
  runtime, and the merged-timeline store. **Mainsail panels** for the UI.
- **Knowledge base is public and signed.** The KB lifecycle runs on
  **GitHub Issues** with a labelled state machine and a fixed accept/reject
  vocabulary, so every decision to learn (or not) carries a readable
  rationale. The only path to the fleet is a **reviewed, merged,
  Ed25519-signed** catalog change. (Full spec: FD-0002 §6a.)
- **The trace plane is structured, not printf.** Registered events
  (`DECL_TRACE`), machine-time-stamped, rendered to human strings *on the
  host* via the dictionary. This is the OAMS CAN-printf need answered the
  HELIX way.
- **Values.** Local-first, opt-in, redacted-by-default, no phone-home.
  Keep Klipper's copyright/attribution + GPLv3. The docs are an
  experience, not a reference dump — the *why* is the point.

---

## 5. What to build — Milestone A, broken into tasks

Milestones B–D are sketched in FD-0002 §9. Your job is **Milestone A**:
the deterministic substrate + the empty-but-live KB framework. Everything
here is CPU-only and testable on any machine, so it does not wait on
hardware or the model tier.

- **A1 — Trace plane (firmware).** A `DECL_TRACE` / `LOG(event, args…)`
  macro emitting *event id + typed args* over Class 2, registered through
  intentproto, machine-time-stamped, with per-subsystem levels and an
  IRAM-safe ring. Near-zero cost when off; must fit the F072. *Touch
  `src/`, reuse the intentproto registry — do not fork it.*
- **A2 — Trace collector + merged timeline (host).** Ingest trace events,
  render via the dictionary, and store a **machine-time-ordered** stream
  merged across all MCUs. This store is the spine Planes 2–4 read.
- **A3 — Trace viewer.** A Mainsail panel if it reaches; else a small
  standalone view. Live tail + filter by subsystem/severity/board.
- **A4 — Blackbox decoder.** Merge `execlog` + trace + `link_stats` +
  timesync + **legacy `klippy.log`** into one narrative and reconstruct
  machine state at a fault. Must be useful on a *stock Klipper* log on day
  one, before any new board ships.
- **A5 — Diagnosis harness (empty catalog).** Define the failure-pattern
  **YAML schema** (signature → cause → fix + provenance + confidence),
  write the matcher, and make "no pattern matched → **case captured**" a
  first-class, useful output. It runs and reports even with zero patterns.
- **A6 — Provisioning.** The **board catalog** (pick a *board*, not a
  chip; curated default config; a `Custom` escape hatch), board
  auto-detection (USB/CAN/DFU/Katapult), and one-touch build+flash over the
  existing bootloader.
- **A7 — Fleet coherence.** Derive a **protocol/ABI hash** from
  `intentproto`, bake it into every image and the host, check it at
  handshake (extending `HELIX_STATUS`/`BOARD_SYSCALL_ABI`/`FRAMING_V2`),
  and offer/perform the in-band **signed** flash that brings a behind-board
  into lockstep. Auto-flash and protocol-correctness are one mechanism.
- **A8 — KB framework (live, empty).** The repo layout for the catalogs,
  the blackbox **bundle format**, the **redaction** pass, and the GitHub
  **Issue template + labels** for the §6a lifecycle. No intelligence yet —
  just the rails.

Suggested order: A1+A4+A5 first (make the machine *talk* and give the talk
a place to be understood), A6+A7 in parallel (the flashing/lockstep pain
is independent), A2/A3/A8 as they're unblocked. Mark nothing "done"
without a test (§8).

**Then, with the model tier (Milestone C) — where your GPUs finally earn
their keep:** the `ModelBackend` abstraction, interpretation of unmatched
cases, NL config/control (draft → deterministic-validate → risk-classify →
apply), RAG over the KB + the per-machine **memory file**, and the
prompt/tool-schema contracts. Build the *contracts and validators* early
(they're deterministic and CPU-testable) so dropping Qwen3-4B onto the
Hailo later is a plug-in, not an integration.

---

## 6. Open questions — resolve with the user before building the affected part

> **Resolved (2026-07-12, with the user, at implementation kickoff).** All
> six are settled; the questions below are kept for the *why*. See
> [FD-0002 §10](README.md#10-open-decisions-for-review-before-we-split--build)
> for the canonical record.
> 1. **Redaction — numeric-only unredacted.** Three tiers: numeric
>    diagnostics ship raw; every string is transformed (paths → basename,
>    free-text dropped, wall-clock → relative machine-time); secrets,
>    network identifiers, and serials/UUIDs are **never** shareable and
>    cannot be allowlisted. Versioned + unit-tested in the floor (A8).
> 2. **KB trust — single project Ed25519 key now**, signature envelope
>    already multi-signer-capable (signer list + threshold) so a
>    web-of-trust is a later policy change, not a format break. Reuses
>    `scripts/sign_image.py` / `keys/`. Reputation weights triage only.
> 3. **ASR — deferred to Milestone D.** Default direction: whisper.cpp +
>    Whisper small/base on CPU; Hailo-8/8L ASR second-class. Blocks nothing
>    before D.
> 4. **Runtime — llama.cpp** (deploy: CUDA+ROCm+CPU, GGUF/Q4_K_M) behind a
>    `ModelBackend` abstraction; **Ollama** for scratch; vLLM only for
>    throughput experiments.
> 5. **Eval — early, stub-model-first.** Metrics: diagnosis accuracy vs a
>    labelled case set, config-edit correctness vs golden diffs, and
>    correct **refusal/confirm on the safety tier**. Cases are versioned
>    data in the repo.
> 6. **Budget — a documented deploy profile** the harness always re-tests
>    against; `--profile deploy` refuses any model past the Qwen3-4B / ~6 GB
>    ceiling. Dev GPUs here: AMD Radeon ~16 GB (ROCm, primary) + an 8 GB
>    NVIDIA that also drives the display (≈5–6 GB usable, fallback) — so the
>    profile guard, not the hardware, keeps the budget honest.

**Left open from the last session:**
1. **Redaction allowlist.** Redact-by-default; a field is shared *only* if
   explicitly allowlisted. Define the list. Is any field *ever* shared
   unredacted? (Blocks A8 and any sharing.)
2. **KB trust model.** Start with a **single project signing key**, or a
   maintainer web-of-trust from day one? Submitter-reputation mechanics.
   (Blocks A8/Milestone B promotion.)
3. **ASR engine.** Which Whisper variant/size, and is the Hailo-8/8L
   ASR-accel tier worth first-class support or just "works on CPU"?
   (Blocks the voice work only — Milestone D.)

**New, raised by the dev environment:**
4. **Model runtime backend.** llama.cpp (most portable toward CPU/Hailo)
   vs Ollama (fastest iteration) vs vLLM (throughput). Recommend
   llama.cpp as the deployable path with Ollama for scratch work — confirm.
5. **Eval harness.** How do we *measure* Atlas quality on the tasks that
   matter (diagnosis accuracy against a labelled case set, config-edit
   correctness, refusal on the safety tier)? Stand this up early so model
   swaps are decisions, not vibes.
6. **Hailo emulation during GPU dev.** How to hold yourself to the ~8 GB /
   Qwen3-4B budget while a bigger card tempts you — a documented "deploy
   profile" you always re-test against.

---

## 7. Conventions &amp; guardrails you must carry

- **Branch.** Work on `claude/software-redesign-impl-finn0j` in each repo.
  If that branch's PR has already been merged, treat new work as fresh:
  restart the branch from the latest default branch (same name) — never
  stack new commits on already-merged history.
- **Commits.** Follow the commit-trailer convention already in the repo
  history (a `Co-Authored-By:` line and a `Claude-Session:` line — use
  *your* session's URL). Commit in logical units with clear messages.
  **Never** create a PR unless the user asks.
- **Never put a raw model identifier** (the `claude-…` ID) in any commit,
  PR, code comment, doc, or other pushed artifact. Chat only.
- **Attribution &amp; license.** Keep Klipper's copyrights and GPLv3
  throughout; the rebrand is identity only. Any throwaway signing key is
  marked **DEV/TEST-only**.
- **The constrained-board rule (F042 policy).** Features that don't fit a
  small MCU simply aren't built there. Atlas's intelligence lives on the
  Pi — the only firmware-side piece is the *cheap, structured* trace plane,
  and it must stay cheap and fit the F072.
- **Structured over free-form; deterministic over clever.** The LLM never
  decides a safety outcome; a deterministic classifier/validator does.
- **Local-first, opt-in, redacted, no phone-home.** This is a promise the
  0.9 docs already made to users — keep it.

---

## 8. How Atlas gets verified (write this checklist early)

Mirror `Helix_Test_Plan.md`. Atlas needs a bring-up checklist with a
clear ladder from "runs anywhere" to "runs on the deploy target":

1. **Deterministic floor (any CPU).** Unit tests for the decoder, the
   diagnosis matcher, the redaction pass, the ABI-hash handshake, the
   provisioning catalog. These gate every commit.
2. **Contracts (any CPU).** The tool schemas, the risk classifier, the
   draft→validate→apply pipeline, the memory-file/RAG-index formats —
   tested with a *stub* model, no weights required.
3. **Model quality (your GPUs).** The eval harness (§6.5) against a
   labelled case set: diagnosis accuracy, config-edit correctness, and —
   critically — **correct refusal/confirm on the safety tier**.
4. **Deploy target (Pi 5 + Hailo-10H).** The same suite on the real NPU,
   within the 8 GB / Qwen3-4B budget, plus latency/tok-s numbers. This is
   the Atlas analogue of FD-0001's hardware bring-up, and it is the only
   place "it works" becomes true for the intelligence tier.

Label every model/accelerator test item **"authored on GPU, validated on
Hailo,"** so the split never blurs.

---

## 9. Documentation still to write (so the next hands after you aren't lost)

- **Split FD-0002** from its single README spine into the numbered series
  (mirroring FD-0001: `00-Vision`, `01-Compute-Tiers`,
  `02-Trace-Observability`, `03-Blackbox-Decoder`, `04-Diagnosis-Engine`,
  `05-Knowledge-Base`, `06-Provisioning-Fleet-Coherence`, `07-LLM-Layer`,
  `08-Roadmap`) once the §6 questions are settled. The README becomes the
  index.
- **An `Atlas_Bring-up_Plan.md`** — the §8 checklist as a real, tickable
  document like the Test Plan.
- **Keep the docs experiential.** As Kevin's Klipper docs and the HELIX
  set do: explain the *why*, the problem, the fix, and what it buys. Atlas
  is also a story about turning a printer into a companion — tell it.

---

*This is the sign-off of the planning session. The plan is canon
(FD-0002); the foundations are real (FD-0001 / HELIX 0.9); the path is
scoped (Milestone A); the environment is ready (your GPUs). Read the
canon, settle the three open questions, make the machine talk, and build
Atlas. — Handoff complete.*
