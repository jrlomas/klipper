# FD-0001: Failure Recovery — Pause, Hold, Resume

Status: Core implemented and workstation-tested in HELIX 0.9; live
host-stall/underrun recovery and powered USB link-loss/reconnect validated on
RP2040 + STM32G0B1 hardware, with autonomous RP2040 bed hold, duration cutoff,
and ceiling cutoff physically validated. Active lost-board motion and an
under-print resume witness remain.

Klipper's failure philosophy today is binary: any error — a late
timer, a lost message, a homing timeout — ends in `shutdown()`, which
turns off every heater, releases every motor, and abandons the print.
For a firmware bug on an 8-bit board with no spare state, that was the
only safe answer. For the architecture in this founding document — boards that
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
  fault" becomes an answerable question. The same JSONL boundary records the
  host's exact wire coefficients as `intention` records and the MCU ring as
  `execution` records, both mapped to Klipper print time.
* **Post-failure dump** (after any pause/fault): the ring buffer is
  retained and drained *reliably* via Class-1 pull commands
  (`execlog_dump oid=%c seq=%u count=%c`). The read-only query and dump
  commands remain permitted while the MCU is in shutdown. The host requests
  at most four records and then waits for a query reply on the same command
  queue before requesting the next chunk. That reply is both the response
  barrier proving the chunk arrived and receive-side flow control; queuing
  sixteen-record bursts was physically observed to increase `bytes_invalid`
  on otherwise healthy Pico and EBB36 USB links. Recovery must never depend on
  records that were droppable while things were going wrong.

Klippy invokes shutdown handlers inside a reactor no-pause critical section,
while a reliable query must wait for its response. The shutdown handler
therefore schedules the Class-1 drain as a reactor callback and returns; the
callback runs after the critical section and can use the response barrier
without causing a second host exception. Execution positions are normalized
to signed 32-bit low-word phase before persistence so a negative CoreXY
trigger remains negative in both reconciliation and the Atlas flight record.
Live position queries pair that phase with the physical microstep counter; the
host unwraps the pair before reconciliation, including across long-axis phase
wraps.

The shutdown path was exercised on the cold V0 on 2026-07-15: after a bounded
Z trajectory, deliberate `M112` put both boards in shutdown, and the deferred
callback still pulled and persisted 42 Pico and 22 EBB36 records (including
the completed Z segments) before firmware restart. After adding per-chunk
flow control, two further physical pulls transferred 1,475 and 1,500 retained
records while both USB links held `bytes_invalid=0`; the printer remained
ready and both heater targets remained zero.

The powered-link path was exercised separately on the same V0. During a cold
50-second Z trajectory on the Pico, the EBB36 USB data cable was physically
removed and reinserted while board power remained present. Klipper entered a
macro-free recovery pause and stopped relaying machine-time queries to the
missing board; the Pico completed the buffered Z40-to-Z30 trajectory at the
exact commanded endpoint. `RECONNECT_MCU MCU=ebb36` then proved that the EBB36
had not rebooted (continuous uptime 32,103,375,787 to 34,127,589,558 ticks),
matched the configured CRC, re-anchored its clock regression, and reconverged
to machine time at -2.4 us without a board or host shutdown. Both heater
targets remained zero, so this is not heater-hold qualification.

That test also defines the host transport boundary. EOF stops both the C
serialqueue worker and its Python consumer, so reopening the tty alone is not
a reconnect. The host retains only ARQ frames already placed on the wire,
restarts both workers without replacing command-queue pointers or protocol
sequence state, and discards never-transmitted ready/upcoming work accumulated
during the outage. Waiting queries receive an explicit local cancellation so
they cannot hang; periodic time-sync traffic is suspended for the paused link,
and a query already in flight at the pause boundary unwinds without killing
the reactor. USB CDC also clears partial receive/transmit staging when the
endpoint is configured again. This prevents stale timers, LED updates, or
motion/meta commands from arriving as a reconnect burst.

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

The RP2040 implementation was physically qualified on the V0 on 2026-07-15
with a 50 C bed target, 65 C ceiling, 2.5-second liveness timeout, and
20-second duration. Host silence engaged the holder without shutting down the
printer; the bed was 50.48 C after 66 controller samples and the holder turned
off at exactly 80 × 250 ms samples.

The first ceiling run revealed why controller state is not sufficient safety
evidence. The holder expired at sample zero and reported output off, but the
ordinary software-PWM timer still owned the shared GPIO and subsequently
reasserted it; the bed rose from 67.97 C to approximately 88 C before `M112`.
The fixed handoff cancels that timer and all queued PWM, rejects updates that
were already in host transport, and retains exclusive ownership through
expiry. In the corrected physical regression, a temporary 55 C ceiling was
crossed at 55.05 C while host PWM requested 13.2%; the holder expired at sample
zero (ADC 3490), host target/power remained zero, Klipper remained ready, and
the bed cooled from 51.86 C to 50.27 C over the recorded 60-second window.
Historical ADC/PID updates accumulated during a host stall are also discarded
instead of being sent with past MCU clocks. Explicit release returns ownership
to the host and re-arms the liveness policy at the current target. The live
hand-back check accepted 91.7% host PWM immediately after release and raised
the bed from 47.84 C to 49.07 C in ten seconds before target/power returned to
zero.

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

An underrun response latches a coordination-group recovery hold before any
later motion flush can emit historical work. Every trajectory backend remains
silent while the host drains logs and reads held accumulators. Those readback
clocks describe a stop that has already happened; they must never be reused as
new command deadlines. The host instead selects one common future Klipper
print time, rebases every live board there, converts the held joint positions
through the machine kinematics, and replaces the already-planned Cartesian
endpoint with the actual controlled-stop coordinate. Virtual-SD ingestion is
paused without invoking user park macros, because a park move before this
reconciliation would itself use a stale coordinate frame.

That shared rebase is a coordinated recovery snapshot, not an indefinitely
live motion anchor. It deliberately has no segment attached: after an
arbitrary operator inspection delay its clock is historical. Each stopped
executor therefore retains the reconciled accumulator but returns to
`need_rebase`; the first later motion receives a new future anchor instead of
appending a hold or segment to the recovery clock.

The cold V0 hardware test on 2026-07-15 stopped Klippy for 1.5 seconds during
a Z trajectory. Pico ramped and held without shutdown; Pico and EBB36 then
accepted a shared future recovery boundary. The host restored X/Y exactly and
changed the stale planned Z endpoint to the MCU-derived ramp endpoint
(87.789057 mm in the first complete reconciliation). A follow-up run exposed
and fixed the idle-snapshot rule above; after a deliberate delay, a cold Z
witness moved exactly from 32.210946 to 37.210946 mm while both boards remained
ready. Powered secondary-USB loss, in-place reconnect, and autonomous bed hold
are now independently qualified as described above. The remaining
qualification is an independent physical position/pulse measurement, active
motion on the disconnected board, and an under-print witness feature. The
current virtual-SD implementation resumes at the next G-Code command; it does
not yet reconstruct the unexecuted suffix of the command that was already
consumed by lookahead when the queue starved. Until that host replanning step exists, the
mechanism is safe and position-coherent but not print-transparent.

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
