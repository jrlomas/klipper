# FD-0002 · 07 — Plane 4: The LLM as Interpreter *and* Actuator

Status: **End-to-end pinned-model workstation assistant realized; deploy
hardware and live-machine apply pending.** The deterministic safety contracts
the model plugs into are **built and CPU-tested**: the non-LLM risk
classifier + draft→validate→classify→apply pipeline in
[`atlas/apply/`](../../../atlas/apply/), the `ModelBackend` abstraction +
deploy-profile budget guard in [`atlas/model/`](../../../atlas/model/), the
eval harness in [`atlas/eval/`](../../../atlas/eval/), and the per-machine
memory file + RAG index in [`atlas/memory/`](../../../atlas/memory/). The
model backend is wired to **llama.cpp** and the official pinned
**Qwen3-4B Q4_K_M** passes the workstation CPU smoke and labelled suite.
The deploy target remains Pi 5 + Hailo-10H; GPU execution/evaluation, target
model compilation, target metrics, and live-machine mutation remain open. The
CUDA (`sm_75`) and ROCm (`gfx1200`) llama.cpp binaries compile and link on the
workstation, but its current execution environment exposes no GPU device nodes,
so no GPU inference result is claimed. On the
workstation, daemon-owned inference, private IPC, authenticated Moonraker
relays, terminal commands, and the Mainsail companion interface are built and
tested together.

This is the plane other printer stacks are not even considering: a machine
you can *ask*, and (carefully) *tell*. But the entire value of the plane
depends on it never being allowed to do harm, and the design achieves that
by a single discipline stated many ways in this series: **the model
drafts; deterministic code classifies and validates; the human is asked
only when the stakes justify it.** Two modes, one guardrail.

Why the contracts were built *before* the model: they are deterministic and
CPU-testable, so moving the same Qwen3-4B contract from workstation llama.cpp
to Hailo is a backend change, not a safety rewrite. The workstation result is
recorded in [Atlas Model Evaluation](../../Atlas_Model_Eval.md).

## Interpret

Explain an error in plain language, summarize an incident, answer "why did
my print fail?" — **grounded by RAG over the KB and the machine's own
memory file** (its history, quirks, learned baselines). This is the safe
mode: the model reads and explains; it changes nothing.

The grounding is what keeps interpretation honest. The model does not
free-associate about your machine; it retrieves from the signed knowledge
base ([05-Knowledge-Base.md](05-Knowledge-Base.md)) and from the
per-machine memory file ([`atlas/memory/machine.py`](../../../atlas/memory/machine.py))
via the RAG index ([`atlas/memory/rag.py`](../../../atlas/memory/rag.py)),
so an explanation is anchored to real, versioned facts about *this*
machine and the community's corroborated knowledge. The same memory file
is where the health monitor's baselines live
([04-Diagnosis-Engine.md](04-Diagnosis-Engine.md)) — one store, read by
both.

That is now the live daemon path, not only a serialization contract. Atlas
creates the file mode-private and atomically, retains one opaque local machine
token, mirrors learned monitor baselines and deduplicated diagnoses, and
rebuilds retrieval when memory changes. Retrieval uses a deterministic
token-hash index so grounding remains available on the base tier without a
second model; the local LLM performs the non-deterministic interpretation of
the retrieved facts.

## Generate & control — behind the risk tier

"Change the display menu so it does X." "Add a macro that…". "Verify my
config for errors." Atlas produces a **concrete diff or action**, which is
then **deterministically validated** before anything happens. What comes
next depends on a **risk tier — decided by code, not by the model:**

| Tier | Examples | Behaviour |
| --- | --- | --- |
| **Catastrophic / safety-affecting** | thermal limits, endstop/probe config, kinematics, driver current, anything that can damage the machine or a person | **Always confirm.** Never auto-applied. |
| **Consequential but reversible** | pin remaps, macro logic, speed/accel defaults | Auto-apply **allowed**, with a one-click undo and an audit entry; a preview is offered. |
| **Cosmetic / non-functional** | display menu layout, UI labels, colours, wording | **Auto-apply.** |

The rule is **"auto-apply when not catastrophic,"** and the load-bearing
detail is that a **deterministic classifier** — not the model — decides the
tier from the diff. This is realized in
[`atlas/apply/classify.py`](../../../atlas/apply/classify.py), driving the
draft→validate→classify→apply pipeline in
[`atlas/apply/pipeline.py`](../../../atlas/apply/pipeline.py). **The safety
gate never depends on the LLM's judgement.** The model could hallucinate a
change to a thermal limit; the classifier would still route it to
"always confirm," because it reads the *diff*, not the model's intent.

The real-file path is
[`atlas/apply/live.py`](../../../atlas/apply/live.py): it rejects stale drafts
with compare-and-swap, preserves file modes, fsyncs an atomic replacement,
persists the complete audit in a mode-private SQLite journal, rolls back when
the injected Klippy reload fails, and can undo after a process restart.

Every applied change — at any tier — is **journaled to the machine's memory
with its diff**, so *undo* and *"what did Atlas change?"* are always
answerable. Atlas drafts; the deterministic layer classifies and
validates; the human is asked only when the stakes justify it.

## How we know it's safe: the eval harness

A safety story you cannot measure is a hope, not a guarantee. The eval
harness ([`atlas/eval/harness.py`](../../../atlas/eval/harness.py)) was
stood up **early and stub-model-first**, and its metrics are exactly the
ones that matter:

- **diagnosis accuracy** against a labelled case set;
- **config-edit correctness** against golden diffs;
- and — the load-bearing metric — **correct refusal / confirm on the
  safety tier.**

That last one is the whole ballgame: if a model swap silently started
auto-applying a kinematics change, the harness catches it. And because the
harness always re-tests against the **deploy profile**
([01-Compute-Tiers.md](01-Compute-Tiers.md)), model choices are decisions
measured against the *shippable* budget, not vibes on a big dev card.

## Voice (future, opt-in)

A microphone → local ASR → the **same** intent pipeline. This introduces
**no new trust surface**: a spoken request is just another way to produce
a proposed, validated, confirmed action. It flows through the identical
classify-and-confirm gate as a typed request, so "talk to your printer"
inherits every guarantee above rather than needing new ones. ASR is
deferred to a later milestone ([08-Roadmap.md](08-Roadmap.md)); the
default direction is whisper.cpp + Whisper small/base on CPU, with
Hailo-8/8L ASR as a second-class accelerator.

## Reusing the ecosystem — the seams

Atlas builds only the missing organs (Principle 5,
[00-Vision.md](00-Vision.md#1-principles)). The seams are named and
minimal:

- **Moonraker components** for anything that is API/state plumbing —
  report submission, provisioning jobs, KB pulls, feedback capture.
- **Mainsail panels** for the trace viewer, the diagnosis/incident view,
  the provisioning wizard, and the companion chat. The trace, diagnosis, and
  companion surfaces are present in the current fork; provisioning remains
  part of its own machine-side bring-up.
- **A standalone Atlas daemon** only for what the stack can't host: the
  always-on monitor, the local-model runtime, and the merged-timeline
  store.

Named seams, minimal new surface. Moonraker owns state that *changes but
is polled or API'd*; the Atlas daemon owns the three things Moonraker
cannot host; Mainsail is the face. That boundary is a settled decision —
see [08-Roadmap.md](08-Roadmap.md).

The daemon and API boundary are now realized in
[`atlas/daemon.py`](../../../atlas/daemon.py): live merged-timeline ownership,
deterministic diagnosis, the serialized local-model runtime, mode-private IPC,
and an atomic versioned state snapshot with an idle heartbeat. The deliberately
boring Moonraker component in
[`moonraker_components/atlas.py`](../../../moonraker_components/atlas.py)
validates and exposes that contract, reports staleness, relays typed assistant
requests, and does not become a second diagnosis implementation. The Mainsail
Atlas panel supplies bounded conversational context and renders classified
config previews; no model prompt or safety decision is implemented in the UI.

The workstation service intentionally stops at preview for config changes.
It returns an expiring token plus exact deterministic diff/risk metadata and
never silently serves the stub backend. Real file mutation, Klippy reload,
rollback, and restart-safe undo are still accepted on the Phase 4 board rig,
where a failed reload can be observed rather than simulated.

Deployment is equally explicit:
[`scripts/install-atlas.sh`](../../../scripts/install-atlas.sh) installs a
mode-private environment, a restartable/hardened systemd unit, the Moonraker
component and configuration, and the service allowlist entry. Its `DESTDIR`
path is acceptance-tested without requiring root or changing a live
workstation.
