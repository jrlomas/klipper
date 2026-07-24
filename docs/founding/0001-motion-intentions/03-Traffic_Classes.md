# FD-0001: Traffic Classes

Status: Core implemented and workstation-tested in HELIX 0.9; hardware
bring-up pending.

Today, one mechanism carries everything between host and MCU, and one
timer list executes everything on the MCU — so a decorative LED update
carries the same "correct or halt" semantics as a step pulse. This
document defines three traffic classes distinguished **by failure
semantics**, not merely by priority, and maps them onto the wire and
onto MCU execution contexts.

## The problem, concretely

* A `SET_PIN` today becomes `queue_digital_out`/`queue_pwm_out`
  scheduled at an absolute clock; if that clock has passed when the
  timer is inserted, the MCU shuts down (`Timer too close`,
  `sched_add_timer()` in [src/sched.c](../../../src/sched.c)).
* Those commands also consume slots in the *same finite move pool* as
  stepper traffic (`request_move_queue_slot()` in
  [klippy/mcu.py](../../../klippy/mcu.py)) — bulk pin activity can
  crowd out motion.
* On the wire, everything shares one priority scheme
  (`req_clock` ordering in
  [klippy/chelper/serialqueue.c](../../../klippy/chelper/serialqueue.c)),
  with one informal exception: the `BACKGROUND_PRIORITY_CLOCK`
  sentinel ("send when idle") used by neopixels and displays — an
  ad-hoc precursor of exactly the classification this document makes
  explicit.

## The three classes

### Class 0 — Scheduled

*Traffic:* trajectory segments and rebase, trsync arm/watchdog,
endstop sampling configuration — anything whose correctness **is** its
timing.

*Semantics:* executed at an absolute machine-time instant. **The only
class permitted to insert timers into the hard timer list.** Late
arrival or an impossible schedule remains a shutdown — unchanged from
today, but now *confined to the traffic that genuinely warrants it*.
Class-0 commands are acked, retransmitted, and ordered exactly as the
current protocol provides.

### Class 1 — Prompt

*Traffic:* pin writes, PWM/fan/LED updates, configuration, queries and
their replies, and MCU→host events (underrun, faults, beacon replies).

*Semantics:* executed **on arrival, from task context** (the
`sched.c` background task loop) — never from the timer ISR list.
Ordered per-oid. An optional `not_before_clock` argument provides
loose scheduling with **late-OK semantics**: if the clock has already
passed, execute immediately; *there is no failure mode that shuts the
machine down.* This is the explicit fix for "an LED can kill a print."

Class-1 output no longer consumes move-pool slots; it has its own
bounded queue (below).

*What Class 1 gives up:* microsecond placement. A fan speed change
lands within a task-loop iteration (tens to hundreds of µs, unbounded
worst case under load) instead of on an exact tick. For pins that
truly need tick-exact edges synchronized to motion, Class 0 scheduled
pin commands remain available — the point is that *default* pin
traffic should not carry shutdown semantics it never needed.

### Class 2 — Telemetry

*Traffic:* ADC/temperature reports, trajectory status (queue
horizons), FOC tracking error, diagnostic dumps, and the live stream
of the **execution log** ([08-Failure_Recovery.md](08-Failure_Recovery.md))
— note the log's *post-failure dump* is deliberately Class 1
(reliable pull), because recovery must never depend on records that
were droppable while things were going wrong.

*Semantics:* best-effort. Rate-limited at the source, droppable under
congestion; every producer maintains a drop counter so loss is
*visible* (a `stats`-style field) but never fatal. Telemetry formalizes
today's `BACKGROUND_PRIORITY_CLOCK` idle-fill hack into a real policy,
and on datagram transports it may be sent unacked
([07-Link_Transport.md](07-Link_Transport.md)).

## Wire mapping

**Class is a static property of the command ID.** A
`DECL_COMMAND_CLASS(func, class, fmt)` variant tags each command, and
the class appears in the data dictionary the host downloads at
`identify` time. Both ends therefore know every message's class with
**zero wire overhead**, and one framed/acked transport (the existing
seq/ack/retransmit machinery of
[src/command.c](../../../src/command.c)) carries all three classes.
A per-frame channel byte was considered and rejected: it spends
payload on information that is already a function of the message ID.

## Host-side scheduling (serialqueue)

Per-class staging in the transmit path:

* **Class 0** keeps today's machinery unchanged: `req_clock` priority,
  `min_clock` release gating, and reliable ordered delivery. On datagram
  links, trajectory payloads carry a host-only buffered-retry annotation and
  their local execution clock. They default to a 100 ms retry floor while
  sufficient staged slack remains, then retry early enough to preserve the
  configured deadline margin. Routine time beacons and execution-grant
  renewals also use the buffered floor because they have seconds of
  holdover/lease runway; each grant carries its local expiry as its retry
  deadline. Rebase, watchdog, homing, recovery, and other safety-control
  messages retain the urgent 25 ms floor.
* **Class 1** is a FIFO dispatched whenever no Class-0 message is due,
  ahead of Class 2.
* **Class 2** fills remaining frame space and idle links; under
  sustained congestion the *host* drops stale telemetry requests
  rather than queueing them unboundedly.

This is an evolution of `serialqueue.c`'s existing two-stage
(upcoming/ready) design, not a rewrite.

The delivery annotation is deliberately narrower than the command's semantic
class. Trajectory data, periodic time/grant maintenance, and immediate
recovery controls are all Class 0, but their available slack differs:
lookahead data has an execution clock, grants have an expiry clock, and
recovery controls have no patient-retry budget. Urgent and buffered records
are not packed under one sequence number. Because acks are cumulative, a
later urgent record is allowed to pull the complete outstanding window
forward; otherwise a watchdog could accidentally inherit the motion-data
RTO.

## MCU-side buffer accounting

Three separately-bounded pools, sizes reported in the config
handshake:

| Pool | Consumer | Exhaustion behavior |
| --- | --- | --- |
| Segment pool (Class 0) | trajectory queues | impossible by credit flow control; hard error if violated |
| Prompt queue (Class 1) | pending pin writes/queries | flow-controlled by ack window; sender blocks, never the machine |
| Telemetry buffer (Class 2) | outbound reports | oldest dropped, drop counter incremented |

The invariant: **bulk traffic can never exhaust motion memory**, in
either direction.

## Placement decisions for existing subsystems

* **Heater/fan PWM → Class 1 + watchdog.** Position taken: the real
  safety mechanism for heaters was never schedule precision — it is
  the MCU-side `max_duration` watchdog (3 s, `MAX_HEAT_TIME` in
  [klippy/extras/heaters.py](../../../klippy/extras/heaters.py)) that shuts the
  heater off if the host stops refreshing it. A PWM update landing a
  few hundred µs "late" is thermally meaningless, while today it can
  kill the print. The watchdog semantics are preserved bit-for-bit by
  default; the opt-in per-heater *failsafe hold* policy
  ([08-Failure_Recovery.md](08-Failure_Recovery.md)) substitutes its
  own bounded envelope where configured.
  *Flagged as an open question for review since it touches heaters.*
* **Endstop sampling, trsync:** Class 0 (they arm hard timers) — and
  see [09-Hardware_Triggers.md](09-Hardware_Triggers.md) for moving
  the detection itself out of the timer list entirely.
* **ADC sampling:** configuration is Class 1. Acquisition reports are selected
  per logical subscription as Class 0, 1, or 2 under
  [17-DMA_ADC_Acquisition.md](17-DMA_ADC_Acquisition.md). Class 0 is reserved
  for a deadline-bearing value required to continue scheduled operation;
  Class 1 carries reliable prompt readings and all watchdog/fault events; Class
  2 remains the default for periodic temperature/status and raw commissioning
  telemetry. The class governs transport and failure semantics, not ADC/DMA
  interrupt priority. Safety action remains local to the MCU and cannot depend
  on a Python callback arriving.
* **Neopixel/display:** Class 1 (they lose their special-case
  sentinel).

## CAN note

On CAN transports the class maps naturally onto CAN ID priority bits
(lower ID wins arbitration), giving Class 0 physical-layer precedence
(cf. [docs/CANBUS_protocol.md](../../CANBUS_protocol.md)). This is an
optimization, not a requirement — class semantics hold on any
transport.

## Open questions

* Heater PWM: Class 1 + watchdog (position above) vs keeping Class 0
  scheduling for heaters only.
* Whether Class 2 should be entirely unacked on datagram transports
  or keep a coarse "any recent ack" liveness signal.
* Whether `not_before_clock` on Class 1 warrants a matching
  `not_after_clock` with drop (not shutdown) semantics for stale
  cosmetic updates.
