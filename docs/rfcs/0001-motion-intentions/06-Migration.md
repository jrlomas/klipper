# RFC 0001: Migration, Validation, and Risks

Status: Draft / Discussion

A redesign of this size earns trust incrementally or not at all. This
document defines how old and new coexist, how equivalence is proven
before any printer runs the new path, the phased rollout, and the
consolidated risk register.

## Coexistence

* **Per-actuator opt-in.** A config knob selects the protocol per
  actuator, e.g.:

  ```
  [stepper_x]
  motion_protocol: trajectory   # default: legacy
  ```

  One MCU binary serves both: legacy `queue_step` steppers and
  trajectory actuators coexist in the same firmware, with the shared
  pool partitioned at config-finalize time (the existing
  `move_finalize()` mechanism in
  [src/basecmd.c](../../../src/basecmd.c) already sizes nodes to the
  largest registered consumer).
* **The legacy path is permanent**, not deprecated-then-removed: it
  remains the answer for AVR-class boards, for extreme
  microstep-rate corner cases beyond the Newton solver's CPU budget
  ([02-Intention_Protocol.md](02-Intention_Protocol.md)), and for
  minimal-RAM targets.
* **Compatibility guarantees:** config files, G-code, macros, and the
  API server behave identically by default. The data dictionary gets
  a protocol version bump; anything user-visible follows the
  [Config_Changes.md](../../Config_Changes.md) process.
* The link-layer work ([07-Link_Transport.md](07-Link_Transport.md))
  is independently negotiated per link and independently landable.

## Validation harness (before any real motor moves)

The credibility core of this plan is that Klipper already has the two
tools needed to prove equivalence *offline*:

1. **Batch mode** ([docs/Debugging.md](../../Debugging.md)): the host
   can translate a G-code file to MCU commands deterministically,
   with no printer attached.
2. **The Linux MCU target** (`src/linux/`): the full firmware runs as
   a host process.

**The differ:** run identical G-code through both pipelines —

```
old:  G-code → itersolve → stepcompress → queue_step → (simulated
      stepper event) → step times
new:  G-code → segment fitter → queue_traj_segment → (linux-MCU
      segment executor) → step times
```

— and compare per-step. Acceptance criteria:

* per-step time deviation ≤ 25 µs (today's own tolerance,
  `MAX_STEPCOMPRESS_ERROR`), and
* commanded-vs-executed position deviation ≤ the configured fitting
  tolerance at every sample point,
* across a corpus: plain cartesian prints, corexy with input shaping,
  delta, pressure-advance-heavy extrusion, homing/probing sequences.

**Benchmarks:** extend the methodology of
[docs/Benchmarks.md](../../Benchmarks.md) with a `queue_traj_segment`
max-step-rate test to validate the 200–250 cycles/step estimate on
real M0/M3/M0+ silicon, and batch-mode host benchmarks for the fitter.

## Phases

Each phase has an explicit exit criterion; later phases do not start
until it is met.

| Phase | Work | Exit criterion |
| --- | --- | --- |
| P1 | This RFC set; review by maintainers | Consensus on protocol + open questions resolved or explicitly deferred |
| P2 | Linux-MCU segment executor + host segment emitter behind a config flag; the differ | Differ passes acceptance corpus; measured segment bandwidth published |
| P3 | Traffic classes (Class 1 prompt execution, per-class pools) — **separable and independently valuable**; can proceed in parallel with P2 | An LED/fan flood cannot shut down a linux-MCU print; heater watchdog behavior verified |
| P4 | Stepper backend on real silicon (STM32F1/F4, RP2040); benchmarks | Meets estimated step-rate ceilings ±25%; prints on a test machine match legacy prints |
| P5 | Time model: machine-time authority + beacon sync; multi-MCU trajectory machines | ≤ ±10 µs measured inter-MCU sync; multi-board test machine prints |
| P6 | FOC reference backend (one open servo platform) + BLDC extruder demo | Servo joint tracks fitted trajectory within its own loop spec; fault→trsync stop demonstrated |
| P7 | Framing v2 (BCH) + UDP/WiFi transport, ESP32 target — separable; can run in parallel from P3 | WiFi toolboard survives scripted 200 ms link stalls with zero shutdowns; underrun/resume demonstrated |

## Risk register

| Risk | Assessment / mitigation |
| --- | --- |
| **Fixed-point drift over long segments** | Eliminated by construction for cross-segment drift (chained 64-bit accumulator integrates the quantized polynomial exactly); intra-segment deviation bounded by the fitter's quantization-aware check and the 2²⁶-tick cap — worked analysis in [02-Intention_Protocol.md](02-Intention_Protocol.md). |
| **Fitting error visible in print quality** | Tolerance defaults to ≤ ½ microstep — below mechanical noise; the differ quantifies it on real models before any hardware runs; tolerance is per-actuator configurable. |
| **M0-class CPU ceiling** (~190K steps/s estimated vs 578K-class legacy paths) | Legacy path remains per-actuator; TMC interpolation covers high-microstep cases; P4 measures before anything ships. |
| **Underrun ramp breaks kinematic coordination** (corexy path deviation during independent decel) | Accepted and documented — it is an emergency behavior replacing a *shutdown*, not a planned stop. Worst case at 300 mm/s, 5000 mm/s² is a ~9 mm stopping envelope with bounded cross-axis deviation; the alternative today is a dead machine with the nozzle parked in molten plastic. |
| **Deeper queues = more committed motion** (safety concern: 1 s of queued segments vs 0.5 s of steps) | Stop latency is *unchanged*: trsync aborts the segment queue in IRQ context exactly as `stepper_stop()` does today ([src/trsync.c](../../../src/trsync.c)) — queue depth never enters the abort path. Homing keeps a shallow horizon by host policy. |
| **Host CPU regression** (fitter vs itersolve+stepcompress) | Expected neutral-to-favorable ([05-Host_Architecture.md](05-Host_Architecture.md)); measured in P2 batch benchmarks before opt-in is documented. |
| **Low-bandwidth links** (250 kbaud UART vs shaped-motion segment rates) | Detected at connect (link speed known); relaxed tolerance or legacy mode per actuator; numbers in [05-Host_Architecture.md](05-Host_Architecture.md). |
| **Two protocols to maintain** | Real, accepted cost of the permanent legacy path; contained by the backend split (the segment core is one module) and by the differ keeping both paths honest against each other. |
| **WiFi link security** | Flagged, not solved — see [07-Link_Transport.md](07-Link_Transport.md); UDP transport ships behind an explicit "trusted network" statement until an auth layer is specified. |

## What success looks like

A corexy printer with an STM32 mainboard, an ESP32 WiFi toolboard
running a BLDC extruder, and a Raspberry Pi host: the host plans
everything it plans today, ships ~10 KB/s of intentions, and a 200 ms
WiFi stall during a speed benchmark produces — nothing. The queues
absorb it. Pull the antenna off mid-print and the toolhead decelerates
cleanly, reports where it stopped, and resumes when the link returns.
An LED animation runs the whole time and could not have hurt anything
even if it had stalled the link, because it was never allowed near the
hard timer list.
