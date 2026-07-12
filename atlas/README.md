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
| `decode/trace.py` | **A2** host trace collector — decode trace records via the dictionary onto the merged timeline | ✅ |
| `view.py` | **A3** trace viewer — filter by subsystem/severity/board + live tail | ✅ |
| `decode/klippy_log.py` | **A4** blackbox decoder — useful on a *stock* `klippy.log` today | ✅ |
| `diagnosis/` | **A5** failure-pattern schema + matcher + **"no match → case captured"** (catalog ships empty) | ✅ |
| `provision/` | **A6** board catalog (50+ boards), USB/CAN detection, one-touch build+flash planner | ✅ |
| `fleet/` | **A7** protocol/ABI hash from `intentproto` + lockstep handshake + signed remediation | ✅ |
| `kb/` | **A8** blackbox bundle, numeric-only redaction, GitHub-Issue intake + label vocabulary | ✅ |

¹ A1 is software-authored and hardware-unvalidated (mirrors `execlog.c`
primitive-for-primitive), consistent with HELIX 0.9's status. It needs an
on-target bring-up before "it works" is true (see the §8 checklist).

## Milestone C prep — the deterministic contracts (built early)

Built ahead of the model tier because they're deterministic and
CPU-testable, so dropping Qwen3-4B on later is a plug-in, not an
integration (HANDOFF §5):

| Module | Contract |
| --- | --- |
| `apply/` | the **non-LLM risk classifier** (safety/consequential/cosmetic from a config diff) + draft→validate→classify→apply pipeline with journal + undo. The safety gate never depends on the model. |
| `model/` | the `ModelBackend` abstraction (stub/cuda/rocm/cpu/hailo) + the **deploy-profile budget guard** that refuses anything past Qwen3-4B / ~6 GB even on a big dev card. |
| `eval/` | the eval harness — diagnosis accuracy, config-edit correctness, and the load-bearing **safety-tier refusal** metric — runnable stub-first, always reported against the deploy profile. |

Verification: [`docs/Atlas_Bring-up_Plan.md`](../docs/Atlas_Bring-up_Plan.md).

Tests: `test/atlas_{decoder,diagnosis,trace,view,provision,fleet,kb,apply,model,eval}_test.py`
— 111 checks across 10 suites.

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

## Tests

```console
$ python3 test/atlas_decoder_test.py
$ python3 test/atlas_diagnosis_test.py
```

These are part of the deterministic floor and run on any CPU with only
the standard library (PyYAML is needed only to load on-disk YAML
patterns; the schema/matcher core is plain-dict).
