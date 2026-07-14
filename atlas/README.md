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
| `../moonraker_components/atlas.py` | schema-validating API bridge with status/incidents/health and assistant relay endpoints, stale detection, and websocket updates | ✅ |
| `observe.py` | rotation-safe JSONL ingestion for trace, execution, link-stat, and timesync events on exact machine time | ✅ |
| `history.py` / `monitor.py` | bounded SQLite incident history plus persistent per-machine drift baselines | ✅ |
| `../klippy/extras/atlas_trace.py` / `decode/trace.py` | **A2** live MCU collector plus offline decoder — dictionary rendering onto the merged timeline | ✅ software; hardware pending¹ |
| `view.py` | **A3** trace viewer — filter by subsystem/severity/board + live tail | ✅ |
| `decode/klippy_log.py` | **A4** blackbox decoder — useful on a *stock* `klippy.log` today | ✅ |
| `diagnosis/` | **A5** failure-pattern schema + matcher + **"no match → case captured"** (catalog ships empty) | ✅ |
| `provision/` | **A6** board catalog (53 boards), confidence-bounded USB/CAN detection (including running Klipper peers), planner, and non-shell confirmed job runner with real Ed25519 verification and private audit | ✅ hardware exercised |
| `fleet/` | **A7** protocol/ABI hash from `intentproto` + lockstep handshake; remediation reuses the signed provisioning runner | ✅ |
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
| `eval/` | the eval harness — diagnosis accuracy, structured config-edit correctness, and the load-bearing **safety-tier refusal** metric — runnable stub-first or against real GGUF weights, always reported against the deploy profile and actual accelerator provenance. |
| `memory/` | the daemon-owned, atomic mode-private **machine memory** (quirks, mirrored baselines, diagnoses, journaled changes) + the **RAG index** over the KB + memory, with deterministic token-hash retrieval on every compute tier. |

The workstation path is a usable product seam, not only a model helper:
the daemon hosts inference, Moonraker relays authenticated typed requests,
the Mainsail Atlas panel provides bounded conversational chat and config
previews, and `atlas assistant` provides the same operations from a terminal.
The assistant refuses to start with an implicit stub or a missing model. Live
config mutation remains disabled at this workstation checkpoint: proposals are
classified, hashed, given a short-lived token, and shown as previews. Applying,
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

Tests: `test/atlas_{decoder,diagnosis,trace,trace_live,view,daemon,assistant,moonraker,install,observe,provision,fleet,kb,apply,model,eval,memory,patterns,llm}_test.py`
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

Questions are grounded in the current timeline, the active pattern catalog,
and optional machine memory. Conversation context is carried by the client,
bounded to eight messages / 16 KiB, and is not persisted by the daemon.
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
$ python3 test/atlas_assistant_test.py
```

These are part of the deterministic floor and run on any CPU with only
the standard library (PyYAML is needed only to load on-disk YAML
patterns; the schema/matcher core is plain-dict).
