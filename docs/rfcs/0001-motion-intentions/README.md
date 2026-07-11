# RFC 0001: Motion Intentions

A proposed architectural evolution of Klipper (as a permanent,
backwards-compatible fork): per-joint trajectory **intentions**
instead of pre-computed step pulses, the primary MCU as the machine's
**time authority**, explicit **traffic classes** so non-critical
traffic can never halt a print, **pause-and-hold failure recovery**
with an execution log so failures resume instead of aborting,
**hardware-event triggers** in place of polling, and a
backwards-compatible, authenticated **FEC/UDP link layer** for
wireless and Ethernet boards.

## Status

| Document | Contents | Status |
| --- | --- | --- |
| [00-Vision.md](00-Vision.md) | Problem, proposal, premises, non-goals, prior art | Draft / Discussion |
| [01-Time_Model.md](01-Time_Model.md) | Machine time, primary-MCU authority, beacon sync, budgets | Draft / Discussion |
| [02-Intention_Protocol.md](02-Intention_Protocol.md) | Segment semantics, wire format, FPU-free execution, queue/refill, underrun | Draft / Discussion |
| [03-Traffic_Classes.md](03-Traffic_Classes.md) | Scheduled / Prompt / Telemetry classes | Draft / Discussion |
| [04-Actuator_Backends.md](04-Actuator_Backends.md) | Segment core vs backends; stepper, FOC/BLDC, PWM/DAC | Draft / Discussion |
| [05-Host_Architecture.md](05-Host_Architecture.md) | Impact on klippy/chelper; the segment fitter | Draft / Discussion |
| [06-Migration.md](06-Migration.md) | Fork stance, coexistence, validation differ, phases, risk register | Draft / Discussion |
| [07-Link_Transport.md](07-Link_Transport.md) | BCH FEC framing v2, UDP over WiFi/Ethernet, mandatory link auth, ESP32 | Draft / Discussion |
| [08-Failure_Recovery.md](08-Failure_Recovery.md) | Pause-and-hold, execution log, heater failsafe hold, resume | Draft / Discussion |
| [09-Hardware_Triggers.md](09-Hardware_Triggers.md) | Event-driven sensing: EXTI, comparators, capture timestamps | Draft / Discussion |
| [10-Protocol_Library.md](10-Protocol_Library.md) | One MIT-licensed protocol library for host, firmware, third parties | Draft / Discussion |
| [11-Bootloader.md](11-Bootloader.md) | First-class bootloader: one image, in-band authenticated updates | Draft / Discussion |

## Reading order

Start with [00-Vision.md](00-Vision.md). Then, by interest:

* *Protocol / firmware*: 02 → 10 → 04 → 01 → 03 → 09 → 07 → 11
* *Host / klippy*: 02 → 05 → 10 → 08 → 06
* *"Is this safe and landable?"*: 00 → 06 (risk register, fleet) → 08
  (pause-and-hold, heater policy) → 02 (underrun) → 03
* *Third-party device vendor*: 10 → 02 → 03 → 07 → 11

## Glossary

* **Intention / segment** — a per-joint statement "position is
  q(t) = q₀ + v·t + ½a·t² from machine clock C for duration T". The
  unit of the new motion protocol; says *where*, never *how*.
* **Joint / actuator** — one independently driven degree of freedom in
  the machine's *actuator* space (a stepper, a BLDC servo, a DAC
  channel) — after kinematics, not a cartesian axis.
* **Machine time** — the timeline all intentions are scheduled
  against; defined as the primary MCU's counter
  ([01-Time_Model.md](01-Time_Model.md)).
* **Actuator backend** — the MCU-side executor that realizes segments
  on specific hardware (step/dir pulses, FOC setpoints, PWM duty).
* **Segment core** — the actuator-independent MCU module owning
  queues, chaining, time conversion, underrun, and trsync aborts.
* **Chained encoding** — segments carry only (duration, v, a); start
  position and time are implied by the previous segment's exact
  quantized endpoint, so no drift can accumulate.
* **Rebase** — the explicit re-anchoring command
  (`trajectory_rebase`) used at motion start, after homing, and after
  an underrun.
* **Horizon** — how far into the future an actuator's queued segments
  extend, in machine time; reported by the MCU and used for refill
  flow control.
* **Underrun ramp** — the deceleration-to-zero an actuator executes
  autonomously if its queue runs dry mid-motion; a resumable event,
  not a shutdown.
* **Pause-and-hold** — the default response to recoverable failures:
  motors stay energized at their held positions, heaters follow their
  failure policy, state and logs are retained
  ([08-Failure_Recovery.md](08-Failure_Recovery.md)).
* **Execution log** — the uplink twin of the intention queue: a
  per-board ring buffer of what was *actually* executed (segments,
  triggers, holds), streamed live on Class 2 and reliably dumped
  after failures; both flight recorder and resume ground-truth.
* **Failsafe hold (heaters)** — opt-in per-heater policy keeping a
  heater (typically the bed) at its last target autonomously during
  pause-and-hold, under a hard temperature ceiling, duration bound,
  and on-MCU runaway checks.
* **Trigger source** — a hardware event producer (GPIO edge IRQ,
  analog comparator, ADC watchdog) that fires trsync directly, with
  optional timer-capture timestamps
  ([09-Hardware_Triggers.md](09-Hardware_Triggers.md)).
* **Protocol library** — the single MIT-licensed implementation of
  the wire protocol (both framings, codecs, state machines; a
  freestanding C++ core with a C-linkage API), consumed by host,
  firmware, bootloader, and third parties; embedded profile is
  heap-free and sized for the STM32F072
  ([10-Protocol_Library.md](10-Protocol_Library.md)).
* **First-class bootloader** — bootloader shipped inside every
  firmware image, speaking the same protocol for in-band,
  authenticated, unbrickable updates
  ([11-Bootloader.md](11-Bootloader.md)).
* **Traffic class** — one of Scheduled (Class 0), Prompt (Class 1),
  Telemetry (Class 2), distinguished by failure semantics
  ([03-Traffic_Classes.md](03-Traffic_Classes.md)).
* **Framing v2** — the negotiated frame format replacing CRC16 with a
  BCH error-correcting trailer
  ([07-Link_Transport.md](07-Link_Transport.md)).

## Relationship to existing docs

These RFCs describe a *proposal*; the authoritative descriptions of
current behavior remain [Code_Overview.md](../../Code_Overview.md),
[Protocol.md](../../Protocol.md),
[MCU_Commands.md](../../MCU_Commands.md), and
[Benchmarks.md](../../Benchmarks.md), which are cited throughout.
