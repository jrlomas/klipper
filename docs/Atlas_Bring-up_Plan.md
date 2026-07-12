# Atlas Bring-up Plan (floor → deploy)

This is the acceptance checklist that takes **Atlas** from *the
deterministic floor runs anywhere* to *the intelligence tier works on the
real deploy target* (a Raspberry Pi 5 + Hailo-10H). It is the Atlas
analogue of the [HELIX Test & Bring-up Plan](Helix_Test_Plan.md), and it
answers the design canon's rule that **nothing is "done" without a test**
([FD-0002](founding/0002-companion-system/README.md) §8).

It is **sequential and cumulative**: each phase assumes the ones before
it passed. The through-line is the distinction the whole build turns on —
**dev target ≠ deploy target**: the model layer is *authored* on a
GPU and only becomes true when it is *validated on the Hailo*, within the
~8 GB / Qwen3-4B budget.

## How to use this document

- Work **top to bottom**. A later phase is never a workaround for a red
  box earlier — go back and fix the cause.
- Each item has a **Do**, an **Expect**, and a **Pass** line. Tick the
  box only when *Pass* is literally true. Where a number is asked for,
  write the measured number next to the box.
- Every **model/accelerator** item is labelled **"authored on GPU,
  validated on Hailo"** — the split must never blur. A green box on the
  GPU is necessary, not sufficient; the same box must go green on the
  Hailo before the capability is real.

**Legend** — `[ ]` open · `[x]` pass · `[~]` pass with a noted caveat
(link it) · `[-]` N/A (record why).

### Rigs referenced below

| Rig | What it is | Needed from |
| --- | --- | --- |
| **Bench host** | Any CPU that runs `python3` and the Atlas test suites (no printer, no GPU). | Phase 0 |
| **Dev GPU** | The workstation GPU — AMD Radeon (ROCm, ~16 GB, primary) or the 8 GB NVIDIA (shares the display). | Phase 3 |
| **Board rig** | One MCU on the bench (ideally a constrained F072 CAN toolhead) reachable over USB/CAN. | Phase 4 |
| **Deploy target** | A Raspberry Pi 5 + Hailo-10H (AI HAT+ 2, 8 GB). | Phase 5 |

---

## Phase 0 — Deterministic floor (any CPU)

Everything here runs on the **bench host** with only the standard library
and gates every commit. No hardware, no GPU, no model.

- [ ] **Decoder (A4).** *Do:* `python3 test/atlas_decoder_test.py`.
  *Expect:* a stock `klippy.log` decodes into a machine-time-ordered
  timeline with honest time-basis flags. *Pass:* ALL PASS.
- [ ] **Diagnosis (A5).** *Do:* `python3 test/atlas_diagnosis_test.py`.
  *Expect:* the empty catalog still runs and captures a case. *Pass:*
  ALL PASS.
- [ ] **Trace collector (A2).** *Do:* `python3 test/atlas_trace_test.py`.
  *Expect:* trace records render via the dictionary and merge onto the
  timeline. *Pass:* ALL PASS.
- [ ] **Viewer (A3).** *Do:* `python3 test/atlas_view_test.py`. *Pass:*
  ALL PASS (incl. live-tail across split lines/tracebacks).
- [ ] **Provisioning (A6).** *Do:* `python3 test/atlas_provision_test.py`.
  *Expect:* the board catalog validates, detection flags ambiguity, the
  planner blocks on UNCONFIRMED/ambiguous. *Pass:* ALL PASS.
- [ ] **Fleet coherence (A7).** *Do:* `python3 test/atlas_fleet_test.py`.
  *Expect:* the protocol hash derives from `intentproto` and the lockstep
  matrix is correct. *Pass:* ALL PASS.
- [ ] **KB + redaction (A8).** *Do:* `python3 test/atlas_kb_test.py`.
  *Expect:* the numeric-only redaction pass never leaks a secret, path,
  or serial. *Pass:* ALL PASS.
- [ ] **Redaction adversarial review.** *Do:* hand a bundle containing
  planted secrets/paths/serials through `assemble_bundle`. *Expect:* none
  survive. *Pass:* the rendered issue body contains zero planted values.

## Phase 1 — Contracts (any CPU, stub model)

The model-facing contracts, tested with a **stub** backend so no weights
are required (§8 tier 2).

- [ ] **Risk classifier (safety gate).** *Do:*
  `python3 test/atlas_apply_test.py`. *Expect:* safety edits always
  confirm; consequential auto-apply with undo; cosmetic auto-apply; the
  most conservative tier wins. *Pass:* ALL PASS.
- [ ] **Apply pipeline.** *Expect:* every applied change is journaled and
  undoable; a safety edit does not apply without explicit confirmation.
  *Pass:* covered green above.
- [ ] **Model backend + deploy profile.** *Do:*
  `python3 test/atlas_model_test.py`. *Expect:* the deploy profile refuses
  a 14B model and pins Qwen3-4B/Q4_K_M; backend selection falls back to a
  stub. *Pass:* ALL PASS.
- [ ] **Eval harness (stub).** *Do:* `python3 test/atlas_eval_test.py`.
  *Expect:* safety-tier accuracy is 100% (deterministic); diagnosis and
  config-edit metrics reflect the catalog/model. *Pass:* ALL PASS.
- [ ] **Memory-file & RAG-index formats.** *Do:* define and round-trip
  the per-machine memory file and the KB RAG index with a stub embedder.
  *Expect:* stable, versioned schemas. *Pass:* round-trip is lossless.
  *(Milestone C — not yet built.)*

## Phase 2 — Trace plane on hardware (A1)

The one firmware-side piece, on a real MCU — ideally the **constrained
F072** it was designed to fit.

- [ ] **Builds for the target.** *Do:* enable `WANT_TRACE`, build for the
  board. *Expect:* it compiles and links; measure the flash/RAM delta.
  *Pass:* fits the F072 with room to spare (record bytes).
- [ ] **Near-zero cost when off.** *Expect:* with all subsystem levels
  off, no measurable timing impact. *Pass:* step timing unchanged vs a
  build with `WANT_TRACE` off.
- [ ] **Events reach the host.** *Do:* raise a subsystem level, provoke a
  `step_underrun`. *Expect:* the host renders "step_underrun
  horizon_us=… queue_depth=…" from the dictionary. *Pass:* the rendered
  string matches the event.
- [ ] **Machine-time merge.** *Do:* trace from two MCUs. *Expect:* events
  merge into one machine-time timeline (A2). *Pass:* ordering is correct
  across boards.
- [ ] **Ring integrity under load.** *Expect:* dropped records are counted
  (`trace_status`), never silently lost. *Pass:* drop count reconciles.

## Phase 3 — Provisioning, fleet coherence & model quality (dev GPU)

- [ ] **Detect a real board.** *Do:* plug a catalog board; run detection.
  *Expect:* a candidate (or an honest ambiguous set) — never a wrong
  auto-guess. *Pass:* the detected board matches the physical one.
- [ ] **One-touch build+flash.** *Do:* run the plan for that board.
  *Expect:* it builds and flashes over the existing bootloader. *Pass:*
  the board boots the new image. **Never flash on UNCONFIRMED.**
- [ ] **ABI-hash handshake.** *Do:* bake the protocol hash into an image;
  connect. *Expect:* a matching board reads *lockstep*; a stale one is
  offered a signed flash. *Pass:* the verdict matches reality.
- [ ] **Model quality — authored on GPU.** *Do:* run the eval harness with
  a real **Qwen3-4B Q4_K_M** on the dev GPU (llama.cpp CUDA/ROCm).
  *Expect:* diagnosis accuracy and config-edit correctness above the
  agreed bar; **safety-tier refusal at 100%**. *Pass:* record the numbers.
  *(authored on GPU, validated on Hailo)*
- [ ] **Budget honesty.** *Do:* run the harness under `--profile deploy`.
  *Expect:* it refuses anything past the Qwen3-4B / ~6 GB ceiling even
  though the card is bigger. *Pass:* an over-budget model is rejected.

## Phase 4 — Diagnosis & apply on a live machine (board rig)

- [ ] **Blackbox on a real fault.** *Do:* provoke a real fault; run
  `atlas diagnose`. *Expect:* a coherent incident narrative + a captured
  case (or a matched pattern). *Pass:* the narrative reflects what
  happened.
- [ ] **NL config edit end-to-end.** *Do:* ask for a cosmetic then a
  safety-affecting change. *Expect:* cosmetic auto-applies; the
  safety-affecting one *requires confirmation*. *Pass:* the gate behaves,
  and undo restores the prior config.
- [ ] **Redaction on a real bundle.** *Do:* assemble a bundle from a real
  machine. *Expect:* no hostname/key/serial/path survives. *Pass:* manual
  review finds nothing that shouldn't be shared.

## Phase 5 — Deploy target (Pi 5 + Hailo-10H)

The only place "it works" becomes true for the intelligence tier.

- [ ] **Model compiles for the Hailo.** *Do:* compile Qwen3-4B for the
  10H. *Expect:* it loads within the 8 GB on-board budget. *Pass:* loads
  with headroom for context + ASR (record MB).
- [ ] **Eval suite on the NPU — validated on Hailo.** *Do:* run the same
  eval harness on the Hailo. *Expect:* metrics within tolerance of the
  GPU run; **safety-tier refusal still 100%**. *Pass:* record the
  numbers; they match the GPU run within the agreed delta.
- [ ] **Latency / throughput.** *Do:* measure tok/s and end-to-end
  diagnosis latency. *Pass:* within the interactive budget (record).
- [ ] **Graceful degradation.** *Do:* remove the accelerator. *Expect:*
  the deterministic floor still runs; the LLM tier switches off, never
  becoming *less* safe. *Pass:* the base tier is fully functional.

## Phase 6 — Sign-off

- [ ] Every phase above is green in a single pass on the deploy target.
- [ ] Every model/accelerator box is labelled "authored on GPU, validated
  on Hailo," with both runs recorded.
- [ ] The redaction pass has been adversarially reviewed on real bundles.
- [ ] The bring-up owner has ticked this document top to bottom.

---

*Mirror of `Helix_Test_Plan.md`. Keep it tickable, keep the GPU↔Hailo
split explicit, and keep the deterministic floor free of any accelerator
dependency — that is what makes a bare Pi 5 a safe companion today.*
