# Founding Document 0001 — Motion Intentions: Vision

Status: Adopted -- realized in HELIX 0.9

> **These documents are no longer proposals — they are the design
> record of what HELIX built.** The FD-0001 set was written to *argue*
> the architecture; the architecture now *exists*, implemented in the
> HELIX 0.9 codebase (software complete; hardware bring-up pending — see
> [Releases](../../Releases.md)). Each document's status line reflects
> its implementation state. The text is preserved in its original
> reasoning-first form, because the *why* is exactly what a reader of an
> evolved codebase most needs; read "we propose" as "HELIX does, and
> here is the argument for why."

This document introduces the architectural evolution of Klipper that
HELIX carries out: moving the host↔MCU contract from *pre-computed step
pulses* to *per-joint trajectory intentions*, making the primary
micro-controller the machine's time authority, and separating
time-critical motion traffic from commands and telemetry that must never
be able to halt a print.

It is the entry point of this founding document — see [README.md](README.md) for
the reading order and glossary.

## Problem statement

Klipper's current architecture is deliberately asymmetric: the host
does *all* motion work, the MCU does *none* of it beyond toggling pins
at precise clock ticks. That asymmetry produced Klipper's signature
strengths (host-side kinematics, input shaping, pressure advance), but
it also hard-wires three limitations into the lowest layer of the
system.

### 1. The protocol bottoms out at step pulses

The terminal vocabulary of the motion protocol is
`queue_step oid=%c interval=%u count=%hu add=%hi` — a compressed
arithmetic ramp of step pulse times (see the 12-byte
`struct stepper_move` in [src/stepper.c](../../../src/stepper.c)).
Every layer above (trapezoid queue, kinematic inversion, step
compression) exists to reduce smooth motion to these pulses before
transmission.

Consider replacing the extruder's stepper with a brushless (BLDC)
servo motor driven by a field-oriented-control (FOC) loop — as some
commercial printers now do. Klipper has *nothing to send it*. The only
workaround is step/dir emulation in front of the servo drive: the host
discretizes a smooth trajectory into pulses, and the drive immediately
low-pass-filters those pulses back into a trajectory. The information
that actually mattered — position as a function of time — was destroyed
in transit.

The same wall appears for DC servos, voice coils, hobby servos, and
any actuator whose natural input is a setpoint rather than an edge.

### 2. Everything shares one hard-real-time timer, so anything can halt the machine

On the MCU, a single sorted timer list dispatched from one hardware
timer interrupt executes *everything*: step pulses, endstop sampling,
soft PWM edges, scheduled pin writes ([src/sched.c](../../../src/sched.c)).
Every scheduled event carries "correct or halt" semantics: if a newly
added timer's wake time has already passed, the firmware shuts down
with `Timer too close` (`sched_add_timer()` in src/sched.c).

Host-side, non-motion outputs ride the same machinery: a `SET_PIN` or
LED update becomes a `queue_digital_out`/`queue_pwm_out` command
scheduled at an absolute clock — and it even consumes a slot in the
same finite move pool as stepper commands (see
`request_move_queue_slot()` in [klippy/mcu.py](../../../klippy/mcu.py)).
A congested link or a host hiccup can therefore turn a *decorative LED
update* into a full printer shutdown. The timing precision that steps
genuinely need is imposed on traffic that merely wants "soon".

### 3. The host is the single timekeeper and single point of stall

All timing flows through the host's statistical estimate of each MCU's
clock ([klippy/clocksync.py](../../../klippy/clocksync.py)). The MCU
holds only ~0.5–1 s of future work, metered by host-side open-loop
constants. If the host stalls past that horizon, the machine does not
stop gracefully — it dies mid-motion with `Timer too close`, `Move
queue overflow` ([src/basecmd.c](../../../src/basecmd.c)) or
`Rescheduled timer in the past`
([src/generic/timer_irq.c](../../../src/generic/timer_irq.c)),
leaving molten plastic parked on the print.

The host also cannot run over links with real jitter (WiFi, congested
USB hubs) without inflating every safety margin, because the *host*
promises the timing, not the board that owns the clock.

## The proposal in one page

**Intentions, not commands.** The host sends each actuator a queue of
*per-joint trajectory segments*:

> "Actuator N: starting at machine clock C, for duration T, your
> position is q(t) = q₀ + v·t + ½·a·t²."

A segment says *where a joint should be as a function of machine
time* — never how the motor achieves it. The MCU holds a refillable
queue of segments per actuator and realizes them with an
actuator-specific backend:

* a **stepper backend** integrates the polynomial into step edges
  on the MCU (integer-only math — no FPU required);
* a **BLDC/FOC backend** samples the same polynomial at its control
  loop rate as position + velocity feed-forward setpoints;
* a **PWM/DAC backend** samples it for hobby servos, laser power
  tracking, and similar devices.

**The host keeps all the intelligence.** G-code processing, lookahead
planning, kinematics (cartesian/corexy/delta/…), input shaping, and
pressure advance remain 100% host-side. The host samples the joint
trajectory that today drives step generation, and fits it with
piecewise-quadratic segments within an explicit error tolerance —
the exact idea behind today's step compression, lifted one level of
abstraction.

**The primary MCU is the time authority.** Machine time *is* the
primary MCU's counter. The host plans in machine time and slaves its
own estimate to it; secondary MCUs discipline their local clocks to the
primary via relayed sync beacons. Boards coordinate purely by executing
against pre-agreed machine-time values, exactly as today — but the
authority moves to where the crystal actually is.

**Traffic is classified by failure semantics.** Three classes:
scheduled (motion, triggers — the only class allowed into the hard
timer list; late = shutdown, as today), prompt (pins, fans, LEDs —
executed on arrival, *late is OK, never shutdown*), and telemetry
(best-effort, droppable). An LED can never again halt a print.

**Queues degrade gracefully.** The MCU reports its queued time horizon;
the host refills against measured watermarks. If the queue ever runs
dry mid-motion, the MCU synthesizes a controlled deceleration to zero
velocity and reports an underrun event — a resumable pause instead of
today's instant shutdown.

**Failures pause and hold; they do not abort.** Boards preserve their
positions, keep motors energized, keep the bed at temperature (per an
explicit opt-in policy), and log what they actually executed to an
uplink **execution log** — the symmetric twin of the intention queue.
A loose toolhead cable becomes: replug, re-handshake, rebase, resume —
not a cold bed and a detached print
([08-Failure_Recovery.md](08-Failure_Recovery.md)).

**Sensing becomes event-driven.** Endstops, probes, and analog
thresholds move from timer-polled sampling to the hardware the MCUs
already ship — edge interrupts, analog comparators with DAC
thresholds, timer input-capture timestamps — so triggers are
microsecond events, not samples that got lucky
([09-Hardware_Triggers.md](09-Hardware_Triggers.md)).

**The protocol is one library, and it is permissive.** Today the wire
protocol is implemented twice — once in the firmware, once in the
host's C helper — and third parties get neither. A single,
MIT-licensed, no-heap library (freestanding C++ core, C-linkage API)
implements the entire protocol for the host, our firmware, the
bootloader, and anyone else's device, open or closed
([10-Protocol_Library.md](10-Protocol_Library.md)). The fork itself
remains GPL.

**The bootloader is part of the firmware, not an add-on.** Every
build ships bootloader + application as one image; updates are
in-band protocol commands over whatever link the board already uses,
authenticated on networks, unbrickable by construction
([11-Bootloader.md](11-Bootloader.md)).

**The link layer gains forward error correction and a UDP transport.**
A backwards-compatible framing extension (negotiated through reserved
bits that legacy firmware provably rejects) replaces the 16-bit CRC
with a BCH error-correcting code, and defines a UDP datagram transport
with packet-level erasure coding — making WiFi-attached MCUs (ESP32
class) first-class citizens. Deep intention queues are precisely what
make a jittery wireless link survivable.

### Pipeline overview

```
G-code
  │  (unchanged)
  ▼
Lookahead planner (toolhead.py)          ┐
  ▼                                      │ host — unchanged
Trapezoid queue (trapq.c)                │
  ▼                                      ┘
Kinematics / input shaper / pressure advance (kin_*.c, as samplers)
  ▼
Per-joint segment fitter (NEW)           ── fits q(t)=q₀+v·t+½at² within tolerance
  ▼
Class-0 channel (framed, FEC, acked)     ── serial / USB / CAN / UDP-WiFi
  ▼
Per-actuator segment queues on MCU (NEW) ── 0.5–1 s horizon, MCU-reported
  ▼
Actuator backend (NEW)
  ├─ stepper: integer step-time solver → step/dir pins
  ├─ FOC/BLDC: setpoint + feed-forward sampling at loop rate
  └─ PWM/DAC: fixed-rate sampling
```

## Decisions taken as premises

The following decisions were made before this document was drafted and are
treated as premises; the documents design *within* them rather than
relitigating them:

1. **Intention level: per-joint trajectory segments.** The host keeps
   kinematics, input shaping and pressure advance; the MCU receives
   per-actuator quadratic position segments. (The alternative — sending
   cartesian segments and running kinematics on the MCU — was rejected:
   it requires FPU-class MCUs, forces porting every kinematic model and
   filter to firmware, and sacrifices Klipper's biggest differentiator.)
2. **Time authority: the primary MCU.** No new sync hardware is
   required; sync beacons ride the existing links. Direct MCU-to-MCU
   sync (e.g. CAN broadcast) is an optional extension.
3. **Hardware floor: 32-bit MCUs.** STM32, RP2040, SAMD, ESP32, etc.
   No FPU required — all segment execution is fixed-point/integer.
   AVR (8-bit) boards remain supported only on the unchanged legacy
   `queue_step` path.
4. **Deliverable: this founding document first.** No implementation is proposed
   for merge until the design has been reviewed. A phased prototype
   plan is in [06-Migration.md](06-Migration.md).
5. **Link evolution is backwards compatible.** Legacy CRC16 framing
   remains the default and permanent fallback; the new framing is
   negotiated per link ([07-Link_Transport.md](07-Link_Transport.md)).
6. **This is a permanent, friendly fork.** The work does not target
   merging into mainline Klipper and does not depend on upstream
   acceptance of any part of it. It tracks upstream (regular rebases,
   identical config/G-code surface by default, the legacy protocol
   kept intact so every existing board works), but the design is free
   to follow its own philosophy
   ([06-Migration.md](06-Migration.md)).
7. **Licensing split.** The protocol library is MIT (original,
   clean-room code — enabling closed-source devices and vendors);
   the host and firmware applications remain GPL
   ([10-Protocol_Library.md](10-Protocol_Library.md)).
8. **Readability is a requirement, not a style preference.** One
   implementation per concept, plain data over linker-section
   metaprogramming (self-registering static descriptors — no source
   scraping, no external generators), documented and versioned
   interfaces at every boundary, and new host components on standard
   asyncio rather than a bespoke reactor. A competent developer must be able to join at
   any single boundary without apprenticeship in the whole
   ([05-Host_Architecture.md](05-Host_Architecture.md),
   [10-Protocol_Library.md](10-Protocol_Library.md)).

## Non-goals

* **No G-code, config-file, or macro breakage.** Existing printers
  continue to work unmodified; the new protocol is per-actuator opt-in.
* **No closed-loop control on the host.** Control loops (FOC, servo
  PID) live entirely on the MCU/drive side; the host ships trajectories
  and reads telemetry.
* **No weakening of the default heater safety model.** The
  `max_duration` watchdog semantics are preserved by default; the
  opt-in per-heater *failsafe hold* policy of
  [08-Failure_Recovery.md](08-Failure_Recovery.md) substitutes a
  different, still strictly bounded envelope only where the user
  explicitly configures it (see also
  [03-Traffic_Classes.md](03-Traffic_Classes.md)).
* **No new transport requirement.** Everything works over today's
  serial/USB/CAN links; UDP/WiFi is an addition, not a replacement.
* **No AVR port of the new motion path.** The legacy path is the
  permanent fallback for 8-bit targets.

## Prior art

The intention-queue idea is not novel — it is the standard pattern in
adjacent industries, which is strong evidence for its viability:

* **CiA 402 "interpolated position mode" (CANopen/EtherCAT drives).**
  Industrial servo drives accept buffers of time-stamped position
  setpoints/segments which the drive interpolates and executes against
  a distributed clock, with underrun and rebase semantics. This is
  precisely the per-joint intention queue, field-proven for decades in
  machines far more safety-critical than 3D printers.
* **RepRapFirmware** executes motion from segment descriptions on the
  MCU rather than pre-computed pulses, demonstrating segment execution
  is tractable on hobbyist-class 32-bit controllers.
* **ODrive / SimpleFOC** expose trajectory and position+velocity
  feed-forward input interfaces — the exact contract our FOC backend
  consumes ([04-Actuator_Backends.md](04-Actuator_Backends.md)).
* **IEEE 1588 (PTP)** provides the framing for our sync beacon design:
  timestamped beacons, relay correction, slave clock disciplining
  ([01-Time_Model.md](01-Time_Model.md)).
* **Klipper's own trapezoid queue** (`struct move` in
  [klippy/chelper/trapq.h](../../../klippy/chelper/trapq.h)) is already
  a piecewise-constant-acceleration segment representation over
  absolute time — evidence that the host has had the right intermediate
  representation all along; it just never crossed the wire.

## Document map

| Doc | Contents |
| --- | --- |
| [01-Time_Model.md](01-Time_Model.md) | Machine time, primary-MCU authority, beacon sync, drift budgets |
| [02-Intention_Protocol.md](02-Intention_Protocol.md) | Segment semantics, wire format, FPU-free execution, queue/refill, underrun |
| [03-Traffic_Classes.md](03-Traffic_Classes.md) | Scheduled / Prompt / Telemetry classes and their transport mapping |
| [04-Actuator_Backends.md](04-Actuator_Backends.md) | Segment core vs backend split; stepper, FOC, PWM backends; stop semantics |
| [05-Host_Architecture.md](05-Host_Architecture.md) | What survives, what is repurposed, what dies in klippy/chelper |
| [06-Migration.md](06-Migration.md) | Coexistence, validation harness, phased rollout, risks |
| [07-Link_Transport.md](07-Link_Transport.md) | BCH FEC framing v2, UDP over WiFi/Ethernet, link security, ESP32 |
| [08-Failure_Recovery.md](08-Failure_Recovery.md) | Pause-and-hold, execution log, heater failsafe hold, resume |
| [09-Hardware_Triggers.md](09-Hardware_Triggers.md) | Event-driven sensing: EXTI, comparators, capture timestamps |
| [10-Protocol_Library.md](10-Protocol_Library.md) | One MIT protocol library for host, firmware, and third parties |
| [11-Bootloader.md](11-Bootloader.md) | First-class bootloader: one image, in-band authenticated updates |
