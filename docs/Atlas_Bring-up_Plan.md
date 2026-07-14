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

- [x] **Decoder (A4).** *Do:* `python3 test/atlas_decoder_test.py`.
  *Expect:* a stock `klippy.log` decodes into a machine-time-ordered
  timeline with honest time-basis flags. *Pass:* ALL PASS.
- [x] **Diagnosis (A5).** *Do:* `python3 test/atlas_diagnosis_test.py`.
  *Expect:* the empty catalog still runs and captures a case. *Pass:*
  ALL PASS.
- [x] **Trace collector (A2).** *Do:* `python3 test/atlas_trace_test.py`.
  *Expect:* trace records render via the dictionary and merge onto the
  timeline. *Pass:* ALL PASS.
- [x] **Viewer (A3).** *Do:* `python3 test/atlas_view_test.py`. *Pass:*
  ALL PASS (incl. live-tail across split lines/tracebacks).
- [x] **Always-on service.** *Do:* `python3 test/atlas_daemon_test.py`.
  *Expect:* the service waits for the log, follows appends/rotation, bounds its
  timeline, diagnoses deterministically, and atomically publishes the exact
  Mainsail status contract. An idle heartbeat proves liveness. *Pass:* ALL PASS.
- [x] **Moonraker boundary.** *Do:*
  `python3 test/atlas_moonraker_test.py`. *Expect:* schema/size validation,
  last-good retention, stale detection, status/incidents/health endpoints, and
  transition-only websocket updates. *Pass:* ALL PASS.
- [x] **Service packaging.** *Do:* `python3 test/atlas_install_test.py`.
  *Expect:* an idempotent staged install with a private environment, hardened
  systemd unit, Moonraker registration, and no system-service side effects.
  *Pass:* ALL PASS.
- [x] **Structured observability + durability.** *Do:*
  `python3 test/atlas_observe_test.py`. *Expect:* trace/execution/link/timesync
  JSONL merges on exact machine time; partial lines and rotation recover;
  incidents deduplicate and retain across restart; learned baselines persist
  and flag drift. *Pass:* ALL PASS.
- [x] **Provisioning (A6).** *Do:* `python3 test/atlas_provision_test.py`.
  *Expect:* the board catalog validates, detection flags ambiguity, the
  planner blocks on UNCONFIRMED/ambiguous. *Pass:* ALL PASS.
- [x] **Provision/fleet execution.** *Do:*
  `python3 test/atlas_provision_execute_test.py`. *Expect:* explicit argv only;
  confirmation plus real Ed25519 verification before flash; hard catalog
  blockers cannot be overridden; fleet remediation uses the same audited job.
  *Pass:* ALL PASS.
- [x] **Fleet coherence (A7).** *Do:* `python3 test/atlas_fleet_test.py`.
  *Expect:* the protocol hash derives from `intentproto` and the lockstep
  matrix is correct. *Pass:* ALL PASS.
- [x] **KB + redaction (A8).** *Do:* `python3 test/atlas_kb_test.py`.
  *Expect:* the numeric-only redaction pass never leaks a secret, path,
  or serial. *Pass:* ALL PASS.
- [x] **KB consent + signed pulls.** *Do:*
  `python3 test/atlas_kb_store_test.py`. *Expect:* per-incident consent is
  short-lived and single-use; duplicate cases coalesce; feedback is structured;
  signed catalogs activate atomically, reject tamper/traversal, and roll back.
  *Pass:* ALL PASS.
- [x] **Redaction adversarial review.** *Do:* hand a bundle containing
  planted secrets/paths/serials through `assemble_bundle`. *Expect:* none
  survive. *Pass:* the rendered issue body contains zero planted values;
  automated by `test_rendered_issue_drops_planted_free_text_secrets`.
- [x] **Mainsail Atlas/OpenAMS panels.** *Do:* run the Mainsail unit suite,
  lint, and production build. *Expect:* the panels consume the Moonraker
  boundary without recomputing Atlas facts. *Pass:* 50 tests across 8 test
  files, lint, formatting, build, and distribution zip all green on
  2026-07-14 after merging `mainsail-crew/develop` at `e9e33c11`. Atlas now
  shares the center dashboard column with Temperatures, shows the newest ten
  matching events, and uses a bounded, wrapping, full-width table so every
  column remains visible. The deployed nginx files match the production build;
  published to `jrlomas/mainsail` at `28807856`.

## Phase 1 — Contracts (any CPU, stub model)

The model-facing contracts, tested with a **stub** backend so no weights
are required (§8 tier 2).

- [x] **Risk classifier (safety gate).** *Do:*
  `python3 test/atlas_apply_test.py`. *Expect:* safety edits always
  confirm; consequential auto-apply with undo; cosmetic auto-apply; the
  most conservative tier wins. *Pass:* ALL PASS.
- [x] **Apply pipeline.** *Expect:* every applied change is journaled and
  undoable; a safety edit does not apply without explicit confirmation.
  *Pass:* covered green above.
- [x] **Live apply durability.** *Do:*
  `python3 test/atlas_live_apply_test.py`. *Expect:* compare-and-swap rejects
  stale proposals; real writes are atomic/fsynced; the audit and undo survive
  restart; reload failure restores the original config. *Pass:* ALL PASS.
- [x] **Model backend + deploy profile.** *Do:*
  `python3 test/atlas_model_test.py`. *Expect:* the deploy profile refuses
  a 14B model and pins Qwen3-4B/Q4_K_M; backend selection falls back to a
  stub. *Pass:* ALL PASS.
- [x] **Eval harness (stub).** *Do:* `python3 test/atlas_eval_test.py`.
  *Expect:* corpus v2 reports deterministic matcher/classifier invariants
  separately from config-edit, narrative, injection, and uncertainty model
  metrics; no combined overall score. *Pass:* all 50 contract cases pass.
- [x] **Memory-file & RAG-index formats.** *Do:*
  `python3 test/atlas_memory_test.py`. *Expect:* the per-machine memory
  file is atomically created mode-private, round-trips losslessly, mirrors
  baselines/diagnoses, and journals applied changes; the deterministic
  BM25 index retrieves relevant patterns, quirks, prior incidents, and machine
  baselines and exposes scores/weak retrieval on every compute tier. *Pass:*
  ALL PASS. A staged
  daemon run created `memory.json` at `0600` with one diagnosis and its monitor
  baseline mirrored.
- [x] **Assistant service contract.** *Do:*
  `python3 test/atlas_assistant_test.py` and
  `python3 test/atlas_moonraker_test.py`. *Expect:* an explicit real backend,
  bounded serialized inference with lock-free status, a same-UID mode-private
  Unix socket, Moonraker-policy API relays, bounded conversational/config
  context, and expiring targeted config previews
  whose risk is classified outside the model. *Pass:* ALL PASS. Stub serving,
  oversized prompts/history, unknown operations, and implicit config mutation
  are refused.

## Phase 2 — Trace plane on hardware (A1)

The one firmware-side piece, on a real MCU — ideally the **constrained
F072** it was designed to fit.

- [x] **Host-simulator software preflight.** *Do:* build clean with trace,
  trajectories, and PWM trajectories enabled. *Expect:* the firmware links
  and its generated dictionary decodes registered trace formats. *Pass:* clean
  host-simulator build passed on 2026-07-13. This does not claim F072 fit or
  real timing.

- [x] **Builds for the target.** *Do:* enable `WANT_TRACE`, build for the
  board. *Expect:* it compiles and links; measure the flash/RAM delta.
  *Pass:* on 2026-07-13 the 8 KiB-bootloader F072 build occupied 60,461
  bytes of its 122,880-byte application region (62,419 bytes free). Against
  an otherwise identical trace-disabled build, trace added 1,497 bytes flash
  and 8 bytes static RAM; its configurable ring adds 28 bytes per record at
  runtime. The available EBB36 G0B1 USB and SKR Pico USB builds also linked:
  trace added 1,464/16 and 1,436/16 bytes flash/static-RAM respectively.
  Hardware testing exposed and fixed a missing Kconfig prompt that had made
  the supposed trace-disabled build silently retain `WANT_TRACE`.
- [ ] **Near-zero cost when off.** *Expect:* with all subsystem levels
  off, no measurable timing impact. *Pass:* step timing unchanged vs a
  build with `WANT_TRACE` off.
- [ ] **Events reach the host.** *Do:* raise a subsystem level, provoke a
  `step_underrun`. *Expect:* the host renders "step_underrun
  horizon_us=… queue_depth=…" from the dictionary. *Pass:* the rendered
  string matches the event. The same end-to-end path is hardware-proven with
  the bounded `trace_probe` diagnostic; the actual underrun call-site remains
  for the motion phase.
- [x] **Machine-time merge.** *Do:* trace from two MCUs. *Expect:* events
  merge into one machine-time timeline (A2). *Pass:* on 2026-07-13, three
  diagnostic records from each of the 12 MHz Pico and 64 MHz EBB36 rendered
  through their dictionaries on the shared machine-time axis with correct
  cross-board ordering and zero gaps, drops, or write errors.
- [x] **Ring integrity under load.** *Expect:* dropped records are counted
  (`trace_status`), never silently lost. *Pass:* each 64-record hardware ring
  received a 256-record burst after three clean records. Both boards reported
  exactly 192 overwrites, the host observed exactly 192 sequence gaps, and
  `unaccounted_gaps` was zero. Paced four-record batches drained all 64
  survivors (`seq=195..258`) without overflowing the MCU response queue.

## Phase 3 — Provisioning, fleet coherence & model quality (dev GPU)

- [x] **Detect a real board.** *Do:* plug a catalog board; run detection.
  *Expect:* a candidate (or an honest ambiguous set) — never a wrong
  auto-guess. *Pass:* on 2026-07-13 the running SKR Pico and EBB36 were
  detected through their stable USB serial paths. The shared Klipper
  `1d50:614e` identity honestly produced unresolved MCU-family sets (six
  RP2040 candidates including `btt-skr-pico-v1.0`; eleven G0B1 candidates
  including the USB-specific `btt-ebb36-42-v1.2-g0b1-usb`) instead of
  guessing a PCB. Hardware inspection confirms those two catalog entries;
  the exact board remains a required confirmation input to a flash job.
- [x] **One-touch build+flash.** *Do:* run the plan for that board.
  *Expect:* it builds and flashes over the existing bootloader. *Pass:*
  the board boots the new image. **Never flash on UNCONFIRMED.** On
  2026-07-13, explicitly confirmed Pico and EBB36 USB plans rebuilt from the
  archived Kconfig, verified detached Ed25519 signatures, required exact
  byte equality with the signed artifacts, flashed, and appended successful
  jobs to `atlas-provision-audit.json`. The same gate was repeated for the
  long-axis trajectory correction on 2026-07-14; both boards booted
  `fdad253f`, passed all five self-tests, and reported fleet lockstep. It was
  repeated again for the RP2040 interrupt-trigger image: the Pico booted
  `e1ec0b9e`, passed all five self-tests, homed X/Y/Z through distinct hardware
  trigger records, and its signed UF2, signature, and Kconfig were archived
  under `firmware-backups/atlas-e1ec0b9e/pico-trace`.
- [x] **ABI-hash handshake.** *Do:* bake the protocol hash into an image;
  connect. *Expect:* a matching board reads *lockstep*; a stale one is
  offered a signed flash. *Pass:* `HELIX_STATUS` reported both live boards at
  protocol ABI `27141a58f61f9fbc`, fleet lockstep, action `none`. The stale
  verdict/remediation matrix remains covered by the deterministic fleet test.
- [x] **Legacy pinned-model CPU transport preflight.** *Do:* run
  `scripts/atlas_llm_smoke.py` and `scripts/atlas_llm_eval.py` against the
  verified Qwen3-4B Q4_K_M artifact. *Pass:* the legacy v1 9-case transport
  smoke, real interpretation, structured config proposal, and deterministic
  preview path passed. It is not a corpus-v2 model-quality result. See
  [Atlas Model Evaluation](Atlas_Model_Eval.md).
- [x] **Workstation assistant end to end.** *Do:* run the pinned model behind
  `atlas serve`, ask through the private socket, and request a safety-affecting
  config edit. *Pass:* the grounded answer returned through the live daemon;
  the exact `extruder.max_temp: 280 → 270` draft was classified `SAFETY`,
  required confirmation, returned `applied: false`, and left the source config
  hash unchanged. The Moonraker-policy endpoints, terminal client, and Mainsail
  companion UI are built and green; deployment authorization remains a
  Moonraker configuration responsibility. See
  [Atlas Model Evaluation](Atlas_Model_Eval.md). This proves workstation
  integration, not live-machine apply.
- [x] **Accelerator runtime builds.** *Do:* compile the pinned llama.cpp
  runtime for both workstation adapters. *Pass:* CUDA 12.0 built/linked
  `llama-completion` for `sm_75` with cuBLAS; ROCm 7.2.4 built/linked it for
  `gfx1200` with HIP/rocBLAS. The pinned Qwen model offloaded all 37 layers
  and generated on both adapters: 104.85 tok/s CUDA, 73.14 tok/s ROCm (short
  smoke, 1,024-token context). The earlier no-device outcome was the
  restricted execution context, not the host. This proves runtime authoring
  and smoke execution only; it does not satisfy model quality below.
- [x] **Corpus-v2 model quality — authored on GPU.** *Do:* run the eval harness with
  a real **Qwen3-4B Q4_K_M** on the dev GPU (llama.cpp CUDA/ROCm).
  *Pass:* on 2026-07-14 CUDA and ROCm independently recorded 4/4 matcher,
  18/18 classifier, 12/12 config edit, 6/6 narrative, 6/6 injection, and 4/4
  uncertainty cases. Every per-kind metric was 100%; no combined overall was
  calculated. The initial failed CUDA pass drove fail-closed contract fixes
  before the successful reruns. See [Atlas Model Evaluation](Atlas_Model_Eval.md).
- [x] **Budget honesty.** *Do:* run the harness under `--profile deploy`.
  *Expect:* it refuses anything past the Qwen3-4B / ~6 GB ceiling even
  though the card is bigger. *Pass:* an over-budget model is rejected; the
  pinned artifact estimated 2,836 MB against the 6,144 MB ceiling.

## Phase 4 — Diagnosis & apply on a live machine (board rig)

- [x] **Workstation host + V0 integration baseline.** *Do:* migrate the
  printer data from its Pi SD card; run this Klipper branch with Moonraker and
  the Atlas-enabled Mainsail fork; connect the SKR Pico and EBB36 over USB.
  *Pass:* on 2026-07-13 all three services were enabled at boot, Klipper
  reached `ready`, both MCUs configured, Mainsail served the live printer, and
  an eight-second link sample advanced both MCUs' synchronized send/receive
  sequences with zero new retransmissions and zero invalid bytes. Temperature
  telemetry from the bed, hotend, chamber, Pico, EBB36, and workstation was
  live. No homing or motion was performed. This test used Klipper's stock
  USB/serial protocol; it did **not** exercise the HELIX datagram carrier,
  `intentproto`, FEC, or the ESP32 modem path. It establishes the known-good
  V0 control baseline, not the later V2.4 CAN or HELIX transport sign-off.
- [x] **Blackbox on a real fault.** *Do:* provoke a real fault; run
  `atlas diagnose`. *Expect:* a coherent incident narrative + a captured
  case (or a matched pattern). *Pass:* the narrative reflects what
  happened. On 2026-07-13 Atlas decoded the live V0 log's earlier
  `sync_beacon` command-format fault, retained the Klipper shutdown and host
  exception chain, and captured unmatched case `cd698ad0fbdeda99` for the
  knowledge base.
- [ ] **NL config edit end-to-end.** *Do:* ask for a cosmetic then a
  safety-affecting change. *Expect:* cosmetic auto-applies; the
  safety-affecting one *requires confirmation*. *Pass:* the gate behaves,
  and undo restores the prior config.
- [x] **Redaction on a real bundle.** *Do:* assemble a bundle from a real
  machine. *Expect:* no hostname/key/serial/path survives. *Pass:* manual
  review finds nothing that shouldn't be shared. A GitHub-issue bundle from
  the live V0 `klippy.log` contained the diagnosis and bounded timeline but no
  hostname, key, USB serial, or filesystem path; provenance content hash
  `eec2daf8a044b94d` recorded the reviewed rendering.

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
