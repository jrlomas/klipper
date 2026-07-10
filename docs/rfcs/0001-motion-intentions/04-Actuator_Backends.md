# RFC 0001: Actuator Backends

Status: Draft / Discussion

The intention protocol ([02-Intention_Protocol.md](02-Intention_Protocol.md))
deliberately says nothing about motors. This document defines the
MCU-side split between the shared **segment core** and per-actuator
**backends**, and specifies three backends: stepper, FOC/BLDC, and
PWM/DAC.

## The split: segment core vs backend

One new firmware module (working name `src/trajectory.c`) owns
everything actuator-independent:

* per-actuator segment queue (nodes from the shared pool),
* chained position/time bookkeeping (the 64-bit sub-unit accumulator),
* `trajectory_rebase` anchoring and underrun-latch state,
* machine-time → local-clock conversion on segment ingest
  ([01-Time_Model.md](01-Time_Model.md)),
* underrun ramp synthesis ([02-Intention_Protocol.md](02-Intention_Protocol.md)),
* trsync stop-signal registration (IRQ-context abort) — with trigger
  *detection* increasingly delegated to hardware event sources
  ([09-Hardware_Triggers.md](09-Hardware_Triggers.md)),
* the **execution log**: appending segment-complete, trigger, hold and
  rebase records to the board's ring buffer — the flight recorder and
  resume ground-truth of
  [08-Failure_Recovery.md](08-Failure_Recovery.md),
* pause-and-hold state management on link loss or host command,
* the host-visible commands (`queue_traj_segment`, `traj_hold`,
  `traj_get_position`, `log_dump`, …) and status/underrun messages.

A backend implements a small ops interface:

```c
struct traj_backend_ops {
    // Begin executing one constant-acceleration span. Coefficients
    // arrive pre-converted to local ticks and internal fixed point.
    void (*begin_segment)(struct traj_actuator *a
                          , int32_t v_fixed, int32_t a_fixed
                          , uint32_t duration_ticks, uint8_t flags);
    // Cease motion. reason: trsync trigger, underrun terminal,
    // host abort, or machine shutdown.
    void (*stop)(struct traj_actuator *a, uint8_t reason);
    // Current realized position in sub-units (for readback/probing).
    int64_t (*read_position)(struct traj_actuator *a);
};
```

The core guarantees to every backend: segments arrive gap-free in time
(the core inserts holds for gaps, as `trapq_add_move()` inserts null
moves today), velocity never changes sign within a segment, duration
and coefficient ranges are within protocol bounds, and `stop()` may be
called from interrupt context and must be safe there.

## Stepper backend

The reference backend; replaces the `queue_step` execution path for
opted-in steppers.

* **Step generation:** the incremental Newton step-time solver
  specified in [02-Intention_Protocol.md](02-Intention_Protocol.md),
  emitting edges via the existing fast GPIO layer with the existing
  `step_pulse_ticks` / inverted-pin handling.
* **Direction:** derived from the segment's velocity sign; the dir pin
  is set at segment load with the same step↔dir separation guarantees
  the current code enforces (`stepper_load_next()` in
  [src/stepper.c](../../../src/stepper.c)).
* **Event structure:** keeps the proven multi-variant optimization
  pattern of today's `stepper_event_full/edge/avr`
  ([src/stepper.c](../../../src/stepper.c)) — the solver produces the
  next wake time; the pulse-shaping variants are unchanged in spirit.
* **Replaces:** `queue_step`, `set_next_step_dir`, `reset_step_clock`
  (subsumed by `trajectory_rebase`).
* **Preserves semantics of:** `stepper_stop_on_trigger` (via the core's
  trsync hook) and `stepper_get_position` (via `read_position`, now
  with sub-microstep resolution).

## FOC / BLDC backend

The motivating case: a brushless extruder (or any servo joint) driven
by an on-board or on-toolhead FOC loop.

**Contract, not controller.** This RFC specifies only how the drive
consumes segments; the current/velocity/position loop internals are
the drive's business (compare ODrive/SimpleFOC input interfaces, or a
CiA 402 drive in interpolated position mode).

* **Control loop timing:** the FOC loop runs at 10–20 kHz on its *own
  hardware timer/PWM peripheral* — it must never enter the shared
  `sched.c` timer list. Only segment-boundary bookkeeping (load next
  segment, ~every 100 ms) touches the scheduler. This isolates the
  hard timer list from high-rate control work by construction.
* **Setpoint sampling:** each control period at segment-relative time
  Δt, the backend computes
  position setpoint q(Δt) = q₀ + v·Δt + ½a·Δt² and velocity
  feed-forward v(Δt) = v + a·Δt — two 64-bit MACs, no FPU needed —
  and feeds both into the loop. Acceleration feed-forward (torque
  hint) is available for free as `a`.
* **Telemetry (Class 2):** tracking error, measured velocity, bus
  current at a configured decimated rate. Droppable by design.
* **Faults (Class 1):** overcurrent, encoder loss, tracking-error
  blowout → prompt event to the host; optionally armed to fire a
  trsync so a servo fault performs a *machine-wide coordinated stop*
  exactly like an endstop trigger — something the step/dir emulation
  workaround can never do.
* **Underrun behavior:** an FOC drive can hold position at zero
  velocity indefinitely; `hold-at-end` is the natural terminal state
  rather than a decel ramp (which it already implies).

## PWM / DAC backend

One paragraph, included as proof of generality: sample q(t) at a fixed
rate and write it as a duty cycle or DAC level. This gives hobby
servos a native trajectory interface, and lets laser power track
*commanded position-derived velocity* precisely (power ∝ v is the
standard raster requirement) without a special-purpose module.

## Stop semantics

| Stop reason | Stepper backend | FOC backend | PWM/DAC backend |
| --- | --- | --- | --- |
| trsync trigger (endstop/probe/fault) | cease edges immediately (IRQ context), queue flushed, enable unchanged | ramp/servo to hold at current position; queue flushed | freeze output; queue flushed |
| Underrun terminal | at v=0 after synthesized ramp; hold enable | hold position | freeze output |
| Pause-and-hold (link loss, host command — [08](08-Failure_Recovery.md)) | at v=0 after ramp; **stay energized**, position retained | closed-loop hold | freeze output |
| Host abort (rebase/flush) | cease edges | hold position | freeze output |
| Machine shutdown | edges cease; enable pin released by existing shutdown handlers | disable drive (configurable hold-vs-free) | output to configured shutdown value |

All reasons preserve `read_position` so the host can recover exact
state — this is what makes underruns resumable and probing accurate.

## Open questions

* Whether backend configuration rides `config_trajectory` parameters
  or per-backend config commands (proposed: per-backend, mirroring
  today's `config_stepper` pattern).
* Whether FOC fault→trsync arming is default-on for motion-critical
  joints.
* Encoder-equipped stepper (closed-loop stepper) placement: stepper
  backend with telemetry, or FOC backend with a step/dir output stage?
  Deliberately deferred.
