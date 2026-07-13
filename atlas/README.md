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
| `../src/trace.{c,h}` | **A1** structured trace plane (firmware) — `LOG*` macros, IRAM ring, Class-2 stream, dictionary-registered events, F072-fit | ✅ authored¹ |
| `timeline.py` | merged, machine-time-ordered event store (the spine Planes 2–4 read) | ✅ |
| `daemon.py` | always-on log follower + bounded timeline + deterministic diagnosis; atomically publishes the versioned Moonraker/Mainsail status contract | ✅ |
| `../moonraker_components/atlas.py` | schema-validating API bridge with status/incidents/health endpoints, stale detection, and websocket updates | ✅ |
| `observe.py` | rotation-safe JSONL ingestion for trace, execution, link-stat, and timesync events on exact machine time | ✅ |
| `history.py` / `monitor.py` | bounded SQLite incident history plus persistent per-machine drift baselines | ✅ |
| `decode/trace.py` | **A2** host trace collector — decode trace records via the dictionary onto the merged timeline | ✅ |
| `view.py` | **A3** trace viewer — filter by subsystem/severity/board + live tail | ✅ |
| `decode/klippy_log.py` | **A4** blackbox decoder — useful on a *stock* `klippy.log` today | ✅ |
| `diagnosis/` | **A5** failure-pattern schema + matcher + **"no match → case captured"** (catalog ships empty) | ✅ |
| `provision/` | **A6** board catalog (53 boards), USB/CAN detection, planner, and non-shell confirmed job runner with real Ed25519 verification and private audit | ✅ |
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
| `memory/` | the per-machine **memory file** (quirks, baselines, journaled changes) + the **RAG index** over the KB + memory, with a deterministic stub embedder for grounding. |

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

Tests: `test/atlas_{decoder,diagnosis,trace,view,daemon,provision,fleet,kb,apply,model,eval,memory,patterns,llm}_test.py`
— **157 semantic checks across 20 suites**, all green.

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
```

These are part of the deterministic floor and run on any CPU with only
the standard library (PyYAML is needed only to load on-disk YAML
patterns; the schema/matcher core is plain-dict).
