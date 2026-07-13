# FD-0002 · 08 — Roadmap and Settled Decisions

Status: **Milestones A and B realized; the Milestone C safety contracts
are built and CPU-tested; the intelligence tier is in progress.** The
`atlas/` package plus [`src/trace.c`](../../../src/trace.c) implement the
deterministic floor; the first curated failure patterns are seeded; the
model-layer contracts (risk classifier, `ModelBackend`, eval harness,
memory + RAG) exist and pass their suites. **151 checks across 16 test
suites, all green.** Every open decision this document once flagged is now
settled.

This is the closing document of the FD-0002 series: where the project was
going, where it has reached, and the decisions that were deliberately left
open for review and are now closed. Read it as the map with the "you are
here" pin already placed.

## The milestones

Each milestone states what is deterministic vs LLM, and what runs on the
base vs the intelligence tier.

- **Milestone A — Observe & Provision.** ✅ **Complete.** Plane 1
  structured trace end-to-end; Plane 3 provisioning/flash + the
  fleet-coherence handshake; *and* the KB **framework live but empty** —
  the decoder, the diagnosis harness over `klippy.log` + the blackbox
  aggregator, and the report scaffolding all run and produce "no known
  pattern (case captured)" from day one. Deterministic; base tier.
  *Realized in* the `atlas/` package + `src/trace.c` (A1 trace plane;
  A2 host collector [`atlas/decode/trace.py`](../../../atlas/decode/trace.py);
  A3 viewer [`atlas/view.py`](../../../atlas/view.py); A4 decoder
  [`atlas/decode/klippy_log.py`](../../../atlas/decode/klippy_log.py);
  A5 diagnosis [`atlas/diagnosis/`](../../../atlas/diagnosis/);
  A6 provisioning [`atlas/provision/`](../../../atlas/provision/), 53-board
  catalog; A7 fleet coherence [`atlas/fleet/`](../../../atlas/fleet/);
  A8 KB framework [`atlas/kb/`](../../../atlas/kb/)).
- **Milestone B — Understand.** ✅ **Seeded.** The diagnosis rule engine +
  the **first curated failure patterns** (thermal / comms / motion,
  9 patterns in [`atlas/diagnosis/patterns/`](../../../atlas/diagnosis/patterns/));
  the GitHub intake, feedback, and acceptance→promotion workflow; signed
  KB pulls. Deterministic core; base tier; model-assisted triage where
  available.
- **Milestone C — Intelligence.** ⏳ **Contracts built early and
  CPU-tested; intelligence in progress.** The local open-weight model:
  interpretation of unmatched cases, NL config/control
  (draft → validate → confirm), RAG over KB + per-machine memory.
  Intelligence tier. The deterministic *contracts* it plugs into are done
  ahead of the weights: the risk classifier + apply pipeline
  ([`atlas/apply/`](../../../atlas/apply/)), the `ModelBackend` + deploy-
  profile guard ([`atlas/model/`](../../../atlas/model/)), the eval harness
  ([`atlas/eval/`](../../../atlas/eval/)), and the memory + RAG index
  ([`atlas/memory/`](../../../atlas/memory/)) — with the backend wired to
  llama.cpp and validated on a real GGUF.
- **Milestone D — Companion at scale.** Proactive baselines + anomaly
  detection maturing; the users-as-trainers loop running at fleet scale;
  voice (future). Both tiers; intelligence tier for voice/NL.

The verification ladder for all of this — from "runs anywhere" to "runs on
the Pi 5 + Hailo-10H deploy target" — is the tickable
[Atlas Bring-up Plan](../../Atlas_Bring-up_Plan.md), the Atlas analogue of
FD-0001's [Helix Test & Bring-up Plan](../../Helix_Test_Plan.md).

## The decisions — all settled

FD-0002 was split from its spine only after the open questions were
resolved (with the user, at implementation kickoff on 2026-07-12). They are
recorded here as the settled record; the rationale for the last four is in
[HANDOFF §6](HANDOFF.md#6-open-questions--resolve-with-the-user-before-building-the-affected-part).

**Settled at review:**

- ✅ **Companion boundary** — **Moonraker** owns anything that *changes but
  is polled or API'd* (state, jobs, report submission, KB pulls, feedback
  capture). A **standalone Atlas daemon** owns the always-on monitor, the
  local-model runtime, and the merged-timeline store. (Seams:
  [07-LLM-Layer.md](07-LLM-Layer.md).)
- ✅ **Model & accelerator** — pin the **Qwen3 dense family**; default
  **Qwen3-4B (Q4_K_M)**; design the intelligence tier against the
  **Hailo-10H / AI HAT+ 2** (8 GB). Base and ASR-accel tiers run the
  deterministic floor without an LLM. (Argument:
  [01-Compute-Tiers.md](01-Compute-Tiers.md).)
- ✅ **Auto-apply** — **"auto-apply when not catastrophic,"** with a
  deterministic (non-LLM) classifier setting the risk tier and every change
  journaled + undoable. (Mechanism: [07-LLM-Layer.md](07-LLM-Layer.md);
  realized in [`atlas/apply/`](../../../atlas/apply/).)
- ✅ **Acceptance is public** — the full KB lifecycle runs on GitHub Issues
  with a written, fixed-vocabulary rationale for every accept and reject.
  (Spec: [05-Knowledge-Base.md](05-Knowledge-Base.md).)

**Settled at implementation kickoff (2026-07-12):**

- ✅ **Redaction policy — numeric-only unredacted.** Three tiers, versioned
  and unit-tested in the floor: always-share (numeric diagnostics),
  transform-then-share (paths → basename, free-text dropped, wall-clock →
  relative machine-time), and never-share with no allowlist override
  (secrets, network identifiers, serials/UUIDs). Some fields ship
  unredacted — **numeric diagnostics only**; every string is redacted, and
  secrets cannot be allowlisted at all. *Realized in*
  [`atlas/kb/redact.py`](../../../atlas/kb/redact.py); detail in
  [05-Knowledge-Base.md](05-Knowledge-Base.md).
- ✅ **KB trust model — single project key now, multi-signer-ready.** One
  project Ed25519 signing key (reusing FD-0001's image-signing:
  `scripts/sign_image.py`, `keys/`), with a signature **envelope** that
  already carries a signer list + threshold, so migrating to a maintainer
  web-of-trust later is a policy change, not a format break. Submitter
  reputation weights **triage priority only**, never the promotion gate.
- ✅ **ASR engine — deferred to Milestone D, default recorded.**
  whisper.cpp + Whisper small/base on CPU is the default direction;
  Hailo-8/8L ASR is second-class ("works on CPU" is the floor). Revisited
  when voice work begins; it blocks nothing earlier.
- ✅ **Model runtime, eval, Hailo-budget.** Deploy backend **llama.cpp**
  (CUDA + ROCm + CPU, GGUF/Q4_K_M) behind a `ModelBackend` abstraction
  ([`atlas/model/backend.py`](../../../atlas/model/backend.py)), **Ollama**
  for scratch iteration, vLLM only for throughput experiments. The **eval
  harness** ([`atlas/eval/`](../../../atlas/eval/)) is stood up early and
  stub-model-first: diagnosis accuracy vs a labelled case set, config-edit
  correctness vs golden diffs, and the load-bearing **refusal/confirm on
  the safety tier**. **Budget discipline** is a documented *deploy profile*
  the harness always re-tests against
  ([`atlas/model/profile.py`](../../../atlas/model/profile.py):
  `--profile deploy` refuses any model past the Qwen3-4B / ~6 GB ceiling) —
  so the profile guard, not the dev hardware, keeps the deploy budget
  honest.

## Where this leaves us

The open items are settled; FD-0002 has split into this numbered series;
Milestones A and B are realized and Milestone C's safety contracts are in
place and green. What remains is the *intelligence itself* — bringing the
pinned model up on the Hailo-10H within budget and letting it interpret,
draft, and (later) listen behind the deterministic gate that already
exists. The floor is honest, the contracts are proven, and the machine can
talk. The rest is teaching it what to say.

For the bring-up ladder and the current status of every task, see the
[Atlas Bring-up Plan](../../Atlas_Bring-up_Plan.md) and the
[development handoff](HANDOFF.md).
