# RFC 0001: Migration, Validation, and Risks

Status: Adopted -- the migration path HELIX 0.9 followed

A redesign of this size earns trust incrementally or not at all. This
document defines how old and new coexist, how equivalence is proven
before any printer runs the new path, the phased rollout, and the
consolidated risk register.

## Fork stance

This project is a **permanent, friendly fork** of Klipper. It does
not target being merged into mainline, and no part of the plan
depends on upstream acceptance — the philosophy here (autonomous
boards, intentions, pause-and-hold) is a deliberate departure from
mainline's design center, and that is respected in both directions.

What "friendly" means concretely:

* **Track upstream.** Regularly rebase/merge mainline master; the
  legacy protocol path is kept fully intact, so upstream fixes to it
  (and to kinematics, extras, and hardware support) flow in cheaply.
* **Stay backwards compatible with the mainline surface.** Config
  files, G-code, macros, and the API server keep working; a printer
  can be moved between this fork and mainline without rewriting its
  configuration. Every board that runs mainline Klipper runs here in
  legacy mode.
* **No merge-back obligation.** Individual pieces (traffic classes,
  BCH framing) may be *offered* upstream if there's interest, but
  nothing in the roadmap waits on it.

The practical consequence for this document: "migration" means
migrating *a machine* from legacy behavior to the new architecture,
one actuator and one link at a time — not migrating patches into
mainline.

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

## Development fleet and strategy

Development is anchored to one real machine — the author's
OpenAMS-equipped printer — so every phase debugs and iterates on
hardware that is actually in service, not a reference board on a
desk. The fleet doubles as a deliberately broad sample of the 32-bit
floor:

| Board | MCU | Role | What it stresses |
| --- | --- | --- | --- |
| Octopus v1.1 | STM32F4 (F407/F446-class) | mainboard: XYZ steppers, bed, PSU | the full segment/step load; large-sector flash layout |
| EBB36 | STM32G0B1 | toolhead: extruder, hotend, probe wiring | toolhead link loss/replug ([08](08-Failure_Recovery.md)); dual-bank bootloader |
| Filament pressure sensor | STM32G0 (COMP-equipped) | analog trigger source | hardware window comparator ([09](09-Hardware_Triggers.md), from the `rt-comparator` branch) |
| OpenAMS boards | STM32F072 | filament switching/feed | **the floor**: 48 MHz M0, 16 KB RAM — sizes the protocol library's embedded profile and the solver's CPU budget |
| ESP32 devkit | ESP32 | proof of concept | UDP transport, HMAC, OTA-mapped bootloader ([07](07-Link_Transport.md), [11](11-Bootloader.md)) |

Two consequences worth stating:

* **The F072 is the design's honesty check.** If the segment executor
  and library fit the OpenAMS board, they fit anything in the fleet;
  every budget in [02-Intention_Protocol.md](02-Intention_Protocol.md)
  and [10-Protocol_Library.md](10-Protocol_Library.md) is validated
  there first.
* **Backwards compatibility is the migration lever, not just a
  courtesy.** Closed third-party devices (a Beacon-class probe, for
  example) keep working unmodified through the legacy klipper path
  while the fleet above migrates piece by piece — the machine never
  stops being a working printer during development.

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
| P2 | Protocol library ([10-Protocol_Library.md](10-Protocol_Library.md), seeded from the author's OpenAMS legacy-protocol library) + linux-MCU segment executor + host segment emitter behind a config flag; the differ, using the library as its codec | Differ passes acceptance corpus; measured segment bandwidth published; library builds standalone in embedded (F072-budget) and host profiles |
| P3 | Traffic classes (Class 1 prompt execution, per-class pools) — **separable and independently valuable**; can proceed in parallel with P2 | An LED/fan flood cannot shut down a linux-MCU print; heater watchdog behavior verified |
| P4 | Stepper backend on real silicon (STM32F1/F4, RP2040); benchmarks | Meets estimated step-rate ceilings ±25%; prints on a test machine match legacy prints |
| P5 | Time model: machine-time authority + beacon sync; multi-MCU trajectory machines | ≤ ±10 µs measured inter-MCU sync; multi-board test machine prints |
| P6 | FOC reference backend (one open servo platform) + BLDC extruder demo | Servo joint tracks fitted trajectory within its own loop spec; fault→trsync stop demonstrated |
| P7 | Framing v2 (BCH) + UDP transport over WiFi/Ethernet with mandatory HMAC, ESP32 target — separable; can run in parallel from P3 | WiFi toolboard survives scripted 200 ms link stalls with zero shutdowns; underrun/resume demonstrated; unauthenticated datagrams rejected |
| P8 | Failure recovery ([08-Failure_Recovery.md](08-Failure_Recovery.md)): pause-and-hold states, execution log + reliable dump, heater failsafe hold, reconnect-resume | Scripted cable-pull on a toolhead board mid-print: replug → resume completes the print with the bed never leaving temperature |
| P9 | Hardware triggers ([09-Hardware_Triggers.md](09-Hardware_Triggers.md)): EXTI trigger sources, comparator integration (building on the `rt-comparator` branch: `src/stm32/comp.c` window comparator), capture timestamps — separable; can run in parallel from P4 | Probe repeatability measurably better than polled endstop path on the same hardware; trigger latency ≤ 10 µs local |
| P10 | First-class bootloader ([11-Bootloader.md](11-Bootloader.md)) across the fleet targets; in-band update as the normal workflow — separable; valuable from the first flashed board onward | Every fleet board updates in-band over its normal link; interrupted update provably recovers; ESP32 maps onto IDF OTA |

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
| **Network link security** | Addressed, not just flagged: HMAC authentication is mandatory in v1 of the UDP transport ([07-Link_Transport.md](07-Link_Transport.md)); only PSK provisioning remains open. |
| **Heater failsafe hold is a fire-safety trade** | Opt-in per heater, hard ceiling + hard duration bound + on-MCU deviation/runaway checks ([08-Failure_Recovery.md](08-Failure_Recovery.md)); hotends default off. The trade (an unattended warm bed vs a lost print) is made explicitly by the user in config, never by default. |
| **Hardware-trigger false positives** (noisy endstop lines firing edge IRQs) | Qualify-after-event confirmation and hardware glitch filters ([09-Hardware_Triggers.md](09-Hardware_Triggers.md)); a false edge costs a µs-scale confirmation, never an unconfirmed trsync. |
| **Shaper ringing near zero velocity clusters segment breaks** (the v-sign-change invariant forces a break at every crossing; slow moves under input shaping can crowd them) | Fitter-side hysteresis: collapse sub-tolerance oscillations into holds; pre-registered as an expected P2 differ finding rather than a surprise. |
| **Extruder under pressure advance is the fitter's worst case** (E-joint curvature at shaper frequency may demand high segment rates) | Looser default E tolerance (extrusion is mechanically low-pass), measured in P2; flags bits 6–7 reserve a cubic-segment escape hatch ([02-Intention_Protocol.md](02-Intention_Protocol.md)). |

## What success looks like

A corexy printer with an STM32 mainboard, an ESP32 toolboard (WiFi or
a single Ethernet cable) running a BLDC extruder, and a Raspberry Pi
host: the host plans everything it plans today, ships ~10 KB/s of
HMAC-authenticated intentions, and a 200 ms link stall during a speed
benchmark produces — nothing. The queues absorb it. Pull the toolhead
cable mid-print and the machine pauses and holds: motors energized,
positions kept, the bed staying at temperature so the part never
lets go of the plate. Replug the cable; the board reports it never
rebooted, the host drains the execution log, rebases, and the print
finishes. Probing runs off a hardware window comparator with
capture-timestamped triggers, so the probe is as repeatable as its
mechanics, not its polling loop. An LED animation runs the whole time
and could not have hurt anything even if it had stalled the link,
because it was never allowed near the hard timer list.
