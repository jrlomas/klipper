# Atlas — the HELIX companion system

Atlas is the intelligence layer of **Helix Atlas** (FD-0002): a
Pi-resident companion that observes a HELIX machine, understands it,
diagnoses itself, provisions and heals its own fleet, and — with the
intelligence-tier accelerator — can be spoken to. HELIX gives the machine
honesty; Atlas gives it a mind.

**Design canon:** [`docs/founding/0002-companion-system/README.md`](../docs/founding/0002-companion-system/README.md)
· **Handoff:** [`HANDOFF.md`](../docs/founding/0002-companion-system/HANDOFF.md)

## The one rule this package lives by

> **Determinism produces facts; the model only interprets.** Everything
> in this package is ordinary CPU code with **no accelerator dependency**
> and must stay that way. The local model (Milestone C) sits *above* this
> floor and never decides a safety outcome — a deterministic classifier
> does. Do not let a CUDA/ROCm/Hailo assumption leak in here.

## Milestone A — the deterministic floor (complete)

| Module | Plane / task | Status |
| --- | --- | --- |
| `../src/trace.{c,h}` | **A1** structured trace plane (firmware) — `LOG*` macros, IRAM ring, stock-protocol stream, dictionary-registered events, F072-fit | ✅ authored¹ |
| `timeline.py` | merged, machine-time-ordered event store (the spine Planes 2–4 read) | ✅ |
| `daemon.py` | always-on log follower + bounded timeline + deterministic diagnosis; atomically publishes the versioned Moonraker/Mainsail status contract and owns the optional local-model runtime | ✅ |
| `assistant.py` / `ipc.py` | serialized, size-bounded assistant service over a mode-private Unix socket; grounded chat, interpretation, and expiring deterministic config previews | ✅ workstation |
| `config_context.py` / `jobs.py` | root-confined include-aware config retrieval plus read-only Moonraker job-history facts; exact last-success questions bypass the model | ✅ workstation |
| `../moonraker_components/atlas.py` | schema-validating API bridge with status/incidents/health and assistant relay endpoints, stale detection, and websocket updates | ✅ |
| `observe.py` | rotation-safe JSONL ingestion for trace, execution, link-stat, and timesync events on exact machine time | ✅ |
| `incidents.py` / `history.py` / `monitor.py` | deterministic one-occurrence-per-failure capture, private bounded evidence archive, aggregate SQLite history, and persistent per-machine drift baselines | ✅ |
| `../klippy/extras/atlas_trace.py` / `decode/trace.py` | **A2** live MCU collector plus offline decoder — dictionary rendering onto the merged timeline | ✅ software; hardware pending¹ |
| `view.py` | **A3** trace viewer — filter by subsystem/severity/board + live tail | ✅ |
| `decode/klippy_log.py` | **A4** blackbox decoder — useful on a *stock* `klippy.log` today | ✅ |
| `diagnosis/` | **A5** failure-pattern schema + matcher + **"unmatched failure → case captured"**; healthy timelines have no active case | ✅ |
| `provision/` | **A6** board catalog (54 boards), confidence-bounded USB/CAN detection (including running Klipper peers), planner, and non-shell confirmed job runner with Ed25519 verification, build/artifact identity, exact-path flashing, and private audit | ✅ detection hardware-exercised; flash pending |
| `fleet/` | **A7** protocol/ABI hash generated into every firmware dictionary + live `HELIX_STATUS` lockstep verdict; remediation reuses the signed provisioning runner | ✅ software; hardware transition pending |
| `kb/` | **A8** blackbox bundle/redaction, consent-bound outbox, structured feedback, GitHub intake, and signed atomic catalog activation/rollback | ✅ |

¹ A1 is software-authored and hardware-unvalidated (mirrors `execlog.c`
primitive-for-primitive), consistent with HELIX 0.9's status. It needs an
on-target bring-up before "it works" is true (see the §8 checklist).

## Milestone C — workstation path validated; deploy hardware pending

The deterministic contracts were built ahead of the weights. The pinned
Qwen3-4B Q4_K_M model now also runs through the llama.cpp CPU transport on
the workstation; Pi 5 + Hailo-10H compilation and validation remain hardware
bring-up work (HANDOFF §5):

| Module | Contract |
| --- | --- |
| `apply/` | the **non-LLM risk classifier** plus draft→validate→classify→apply. The live path uses compare-and-swap, atomic config writes, durable audit, reload rollback, and restart-safe undo. The safety gate never depends on the model. |
| `model/` | the `ModelBackend` abstraction (stub/cuda/rocm/cpu/hailo) + the **deploy-profile budget guard** (refuses anything past Qwen3-4B / ~6 GB even on a big dev card) + `LlamaCppBackend.generate` wired to llama.cpp (schema→JSON grammar, tools→tool-calling) and the prompt/tool-schema contracts + assistant helpers. |
| `eval/` | corpus-v2 evaluation — deterministic matcher/classifier invariants reported separately from targeted config edits, diagnosis narrative, prompt-injection resistance, and uncertainty behavior; no misleading combined overall score. |
| `memory/` | the daemon-owned, atomic mode-private **machine memory** (quirks, mirrored baselines, diagnoses, journaled changes) + inspectable deterministic **BM25 retrieval** over the KB + memory on every compute tier. |

The workstation path is a usable product seam, not only a model helper:
the daemon hosts inference, Moonraker relays typed requests under Moonraker's
configured authorization policy,
the Mainsail Atlas panel provides bounded conversational chat and config
previews, a one-click clear for browser-held conversation state, and a default
dashboard position immediately above Temperatures. `atlas assistant` provides
the same operations from a terminal.
The assistant refuses to start with an implicit stub or a missing model. Live
config mutation remains disabled at this workstation checkpoint: targeted
section/key proposals are constructed deterministically, classified, hashed,
given a short-lived token, and shown as previews. Applying,
reloading, rollback, and undo on a real printer remain Phase 4 board-rig work.

**Milestone B** seeds the first curated failure patterns —
[`diagnosis/patterns/`](diagnosis/patterns/) (thermal/comms/motion, 9
patterns), verified in `test/atlas_patterns_test.py`.

Verification: [`docs/Atlas_Bring-up_Plan.md`](../docs/Atlas_Bring-up_Plan.md).
The real model backend is validated end-to-end by
[`scripts/atlas_llm_smoke.py`](../scripts/atlas_llm_smoke.py) against a
real GGUF; [`scripts/atlas_llm_eval.py`](../scripts/atlas_llm_eval.py) runs
the labelled suite against the pinned weights. The recorded workstation
result is in [`docs/Atlas_Model_Eval.md`](../docs/Atlas_Model_Eval.md).
The standard suite mocks the model so it runs without weights.
The pinned corpus-v2 run passed every separately reported category on both
CUDA and ROCm on 2026-07-14; this remains "authored on GPU, Hailo validation
pending," not deploy-target sign-off.

Tests: `test/atlas_{decoder,diagnosis,trace,trace_live,view,daemon,assistant,config_context,jobs,moonraker,install,observe,incident_capture,provision,fleet,kb,apply,model,eval,memory,patterns,llm}_test.py`
— the complete deterministic Atlas workstation suite, all green. Exact check
counts are intentionally left to the test runner so this status line cannot
go stale when coverage grows.

## Try it on a real log

```console
$ python3 -m atlas.cli decode   /path/to/klippy.log
$ python3 -m atlas.cli diagnose /path/to/klippy.log
$ python3 -m atlas.cli view     /path/to/klippy.log --min-severity warning
$ python3 -m atlas.cli bundle    /path/to/klippy.log --issue
```

The decoder recovers what a stock log honestly allows: the host monotonic
clock from `Stats` lines, anchored to wall time by the `Start printer at`
banner. Events between stats lines are marked with a `~` (inferred time).
Real machine time arrives when the trace plane (A1/A2) and execution log
feed the same `Timeline`.

## Run the companion service

The deterministic service can run before Klipper starts; it waits for the log,
then follows appends and rotations while keeping a bounded in-memory timeline:

```console
$ python3 -m atlas.cli serve ~/printer_data/logs/klippy.log \
    --state-file ~/.local/state/atlas/status.json
```

The snapshot is written with rename atomicity and is the stable boundary API
plumbing consumes. Its `timeline` and `diagnosis` objects are the exact contract
rendered by the Mainsail Atlas panel. An idle heartbeat lets consumers tell a
quiet service from a stopped one. The component in `moonraker_components/`
validates and exposes this state without recomputing Atlas facts.

Atlas automatically groups error and critical events into physical failure
occurrences and closes an occurrence after a short quiet tail. A healthy
current Klipper session publishes `case: null`; restarting after an old fault
therefore clears the panel's active diagnosis without deleting the durable
history. Each occurrence is aggregated by pattern/case identity in
`incidents.sqlite3` and has one mode-`0600` JSON evidence record under the
mode-`0700` `incidents/` directory. Retention is bounded by age and count.
Detection, grouping, capture, redaction, and retention are deterministic and
do not invoke the model. Newly inserted occurrences also update the private
machine-memory observation count, so repeated failures become frequency-aware
RAG grounding without letting log replay inflate the count.

Occurrence evidence contains only structured redacted events, before/after
stats, MCU/software identities, hashes of the active config and G-code, and at
most 64 normalized numeric G/M/T commands around the reported SD byte
position. It never archives the raw log, config contents, filename, comments,
free-form macro commands, or the full G-code. The Moonraker incidents endpoint
returns the bounded aggregate list and recent occurrence metadata alongside
the current diagnosis; the private evidence files are not exposed through
that API.

To enable the local assistant, set `ATLAS_MODEL` and `ATLAS_LLAMA_CLI` in
the mode-private `atlas.env` written by the installer. For a direct run:

```console
$ python3 -m atlas.cli serve ~/printer_data/logs/klippy.log \
    --state-file ~/.local/state/atlas/status.json \
    --model /models/Qwen3-4B-Q4_K_M.gguf \
    --llama-cli /opt/llama.cpp/bin/llama-completion \
    --printer-config ~/printer_data/config/printer.cfg
$ python3 -m atlas.cli assistant ask "Why did the printer stop?"
$ python3 -m atlas.cli assistant interpret
$ python3 -m atlas.cli assistant propose "Rename the START_PRINT macro"
```

Questions are grounded in the current timeline, a bounded read-only config
excerpt, the active pattern catalog, and optional machine memory. Conversation
context is carried by the client, bounded to eight messages / 8 KiB, and is
not persisted by the daemon. Timeline, config, retrieval, memory, and history
content are fenced as untrusted prompt data.
Config questions traverse only the root-confined Klipper include tree, rank
active request-relevant sections, follow LED hardware/effect references, and
label every excerpt with its source file. Commented examples are not treated
as active configuration. Atlas also reads Moonraker's SQLite job history in
read-only mode: narrow questions such as "last successful print" are answered
deterministically from the newest completed job instead of asking the model to
infer from a transient timeline. The global timeline bound reserves space per
source, preventing a high-rate execution stream from erasing host/trace/link
sources in the Mainsail selector.
The daemon creates `memory.json` with mode `0600`, assigns an opaque local
machine token, mirrors learned monitor baselines and deduplicated diagnoses,
and atomically refreshes the RAG corpus when those facts change.

Install the daemon, its hardened systemd service, and the Moonraker component
on a standard Klipper host with:

```console
$ sudo scripts/install-atlas.sh
```

The installer is idempotent, keeps `atlas.env` mode-private, registers Atlas
with Moonraker, and can stage into `DESTDIR` without touching system services.
Use `--no-start` to install without enabling or restarting anything; run
`scripts/install-atlas.sh --help` for non-standard paths and service users.

## Tests

```console
$ python3 test/atlas_decoder_test.py
$ python3 test/atlas_diagnosis_test.py
$ python3 test/atlas_daemon_test.py
$ python3 test/atlas_moonraker_test.py
$ python3 test/atlas_install_test.py
$ python3 test/atlas_observe_test.py
$ python3 test/atlas_incident_capture_test.py
$ python3 test/atlas_config_context_test.py
$ python3 test/atlas_jobs_test.py
$ python3 test/atlas_assistant_test.py
```

Most deterministic-floor tests run with only the standard library. PyYAML
loads on-disk YAML patterns, and PyNaCl provides the fail-closed Ed25519
verification required for signed provisioning and knowledge-base updates;
install both with `python3 -m pip install -r atlas/requirements.txt` in the
interpreter or virtualenv that runs Atlas.
