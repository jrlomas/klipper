# RFC 0001: Failure Recovery — Pause, Hold, Resume

Status: Draft / Discussion

Klipper's failure philosophy today is binary: any error — a late
timer, a lost message, a homing timeout — ends in `shutdown()`, which
turns off every heater, releases every motor, and abandons the print.
For a firmware bug on an 8-bit board with no spare state, that was the
only safe answer. For the architecture in this RFC set — boards that
own their clocks, their positions, and their trajectory queues — it
throws away exactly the state that would let a print *survive*.

This document replaces abort-everything with **pause-and-hold** as the
default response to recoverable failures, defines what each board
preserves, and specifies the **execution log** — the uplink twin of
the intention queue — that makes resumption reconstructable rather
than guessed.

## The principle: two queues, exchanged

The intention protocol ([02-Intention_Protocol.md](02-Intention_Protocol.md))
sends a queue *down*: what each actuator should do. This document adds
the symmetric queue *up*: **what the machine actually did.** Every
board maintains a ring buffer of execution records:

```
seg_done   oid end_clock end_pos          ; segment completed
trigger    trsync_oid reason clock pos... ; endstop/probe/fault stop
underrun   oid clock pos                  ; queue ran dry, ramp taken
hold       oid clock pos reason           ; entered pause-and-hold
heater     oid clock state target         ; failsafe policy transition
rebase     oid clock pos                  ; host re-anchored
discipline clock offset_adj rate_adj      ; clock sync adjustment
fault      code clock detail              ; anything else
```

Delivery has two modes:

* **Live** (normal operation): streamed on Class 2 — droppable,
  rate-limited, with a drop counter
  ([03-Traffic_Classes.md](03-Traffic_Classes.md)). The host persists
  a rolling window to disk. This is also the *flight recorder* for
  debugging: "what did the MCU execute in the 100 ms before the
  fault" becomes an answerable question.
* **Post-failure dump** (after any pause/fault): the ring buffer is
  retained and drained *reliably* via Class-1 pull commands
  (`log_dump oid=%c seq=%u`). Recovery must never depend on records
  that were droppable while things were going wrong.

Resume then stops being inference: the host diffs *intentions sent*
against *executions logged*, knows exactly where every joint stopped
and what was already printed, and re-plans from ground truth.

## Failure taxonomy and responses

| Failure | Detection | Response | Resumable? |
| --- | --- | --- | --- |
| Queue underrun (host stall, link congestion) | segment queue empty at v≠0 | decel ramp → hold ([02](02-Intention_Protocol.md)) | Yes — rebase and continue |
| Link loss, board still powered (loose cable, AP dropout) | Class-0 silence / beacon loss past budget ([01](01-Time_Model.md)) | **pause-and-hold**: finish or ramp out current motion, hold positions, heaters per failure policy, keep logging | Yes — replug/reassociate, re-handshake, rebase, continue |
| Board reset / power loss | reconnect handshake shows fresh boot (uptime, config CRC) | that board's volatile state is gone; *other* boards pause-and-hold | Partially — see per-joint recovery below |
| Host crash / host power loss | all boards lose beacons + Class-0 traffic | machine-wide pause-and-hold (autonomous — no host needed) | Yes — host restarts, reads positions + execution logs, resumes |
| Trigger abort (unexpected endstop, servo fault) | trsync fires | coordinated stop (as today), then **hold, not shutdown** | Usually — host inspects logs and decides |
| Genuine firmware fault (watchdog, assertion, `Timer too close` on Class 0) | internal | full shutdown, as today — pause-and-hold requires a *trustworthy* MCU | Via log dump after restart, best-effort |

The last row is the honest boundary: pause-and-hold is for failures
*around* a healthy MCU (links, hosts, cables, queues). When the MCU
itself cannot be trusted, the existing shutdown path — including its
heater cutoff — is the right answer, unchanged.

## Pause-and-hold: the machine state

When a board enters hold, autonomously or by host command:

* **Steppers** stop at the ramp endpoint and remain *energized* —
  holding torque keeps position valid. The enable pin is not released
  (releasing it is what makes today's shutdown unresumable on most
  machines).
* **Servo/FOC joints** hold position under closed loop
  ([04-Actuator_Backends.md](04-Actuator_Backends.md)).
* **Heaters** follow their per-heater **failure policy** (below).
* **Position accumulators, execution logs, and the disciplined clock
  keep running.** Hold is a *state*, not a death.
* Class-1/2 traffic continues if the link is up (a held machine is
  still observable); if the link is down, logs buffer.

### The replugged-toolhead scenario (the motivating case)

A toolhead board's cable comes loose mid-print. Today: comms timeout →
machine-wide shutdown → cold bed → detached part.

Under this design: the toolhead board sees Class-0/beacon silence and
holds (extruder stopped, hotend on failure policy, positions retained);
the mainboard sees the toolhead unreachable and holds its axes the
same way. The user reseats the cable. The host re-handshakes: the
board reports *no reboot* (uptime continuous, config CRC unchanged),
so its entire state is still authoritative — the host drains the
execution log, rebases every joint at its held position, re-disciplines
the clock, and resumes. Nothing was lost because nothing was thrown
away.

### Per-joint recovery after a board reset

If the board actually rebooted, its volatile accumulators are gone.
HELIX recovers on a deliberately simple model — **no encoders, no
closed-loop feedback**. A resume assumes the joint is still at the
last coordinates it was commanded to, with the homing reference it
had, and continues. The host still holds every joint's last commanded
position (the intention twin) and its kinematic homing state; a board
reset does not erase the *host's* knowledge, only the board's volatile
segment accumulator. So the resume re-anchors each joint at that last
commanded position on its next motion and carries on.

The only per-joint question is therefore binary — did this joint's
**homing survive the reset**?

* **Homing retained** (the default for every joint): re-anchor at the
  last commanded position and continue. This covers the extruder (E is
  relative — resume equals re-prime and continue) and every absolute
  axis whose homing reference is trustworthy across the reset. A
  toolhead-board reset therefore does not doom a print.
* **Homing lost**: only when a joint is explicitly declared volatile
  (`motion_homing_volatile: True`) because its reference genuinely
  cannot be trusted across a reset. That joint blocks the resume until
  its axis is re-homed; the host presents the last known intention and
  does not fake a position.

This intentionally collapses the earlier three-way classification
(extruder / independent-reference / none) — which leaned on encoder or
re-qualification machinery HELIX does not build — into "retained vs
lost." Recovery from a non-fatal reset is the common, automatic case;
a re-home is required only when homing is truly gone.

## Heater failure policy — keep the bed hot

The single most print-destroying default in the current architecture:
any shutdown turns the bed off, the part cools, adhesion releases, and
even a theoretically resumable print detaches from the plate.

Per-heater configuration:

```
[heater_bed]
failure_policy: hold          # off (default) | hold
hold_max_temp: 110            # hard ceiling while holding
hold_max_duration: 3600       # seconds of autonomous hold, then off
```

`hold` means: on entering pause-and-hold, the heater's board keeps the
heater at its last commanded target (clamped to `hold_max_temp`)
**autonomously** — the host may be gone, so this requires a new,
deliberately minimal on-MCU capability:

* a hysteresis (bang-bang) controller on the heater's existing ADC
  channel — no PID tuning state, a few lines of integer comparison;
* the existing ADC sanity limits (out-of-range sensor → off) remain
  armed;
* a deviation band: temperature outside ±15 °C of the hold target for
  a sustained period → off (on-MCU runaway check — the host's
  `verify_heater` is not available while the host is the thing that
  failed);
* the `hold_max_duration` deadline → off, unconditionally.

This *replaces* the blanket `max_duration` watchdog for held heaters
with a different — still strictly bounded — safety envelope, and that
trade must be stated plainly: an opt-in held bed keeps ~60–110 °C
unattended for up to the configured duration. That is a fire-safety
decision the user makes explicitly, per heater; hotends default to
`off` and should stay there (a held hotend also cooks filament into a
clog). The policy, ceilings, and runaway checks live in the data
dictionary so the host can display exactly what the machine will do
on failure.

## Resume workflow

1. Connectivity restored (or host restarted). Boards report: boot
   state, held positions, log high-water marks.
2. Host drains execution logs (Class-1 reliable pull), reconciles
   against its persisted intention record, and computes the true
   machine state — including partially-executed segments.
3. Per-axis re-qualification as needed (none, for the
   still-powered-board case).
4. `trajectory_rebase` every joint; restore heater ownership (policy
   hold → normal control); resume the print from the reconciled
   position.

**Print-quality honesty** (unchanged from
[02-Intention_Protocol.md](02-Intention_Protocol.md)): v1 resume is
*mechanically* exact but not cosmetically invisible — melt pressure
decayed, the nozzle sat near the part, a blemish is likely. That is
still categorically better than a detached, abandoned print.
Quality-aware resume (park, wipe, re-pressurize, then rejoin) is a
host-side workflow to build on top of this machinery, not firmware.

## Open questions

* Fan behavior in hold: part fan off (proposed — it fights the held
  bed) vs configurable.
* `hold_max_duration` default (proposed 3600 s) and whether hold
  should require the machine to have been printing (vs idle faults).
* How much execution-log RAM to reserve per board (proposed: size to
  ≥30 s of typical record rates, like the host-side history window),
  and whether records should be delta-compressed.
* Whether a held machine should periodically chirp state on Class 2
  over a *down* link's backup path (e.g. a WiFi board falling back to
  its UART bootstrap link), or simply wait.
