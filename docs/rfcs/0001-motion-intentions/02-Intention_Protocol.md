# RFC 0001: The Intention Protocol

Status: Draft / Discussion

This is the core document of the RFC set. It defines what a trajectory
*intention* (segment) is, its wire encoding, how a stepper realizes it
without floating point, and the queue/refill/underrun protocol.

Vocabulary is defined in the [README glossary](README.md). The
actuator-backend interface that consumes segments is in
[04-Actuator_Backends.md](04-Actuator_Backends.md); machine time is
defined in [01-Time_Model.md](01-Time_Model.md).

## Segment semantics

A **segment** is a statement about *where a joint should be as a
function of machine time*:

> q(t) = q₀ + v·t + ½·a·t² for t ∈ [0, T), anchored at machine clock C

with q in the actuator's position units, v and a constant over the
segment. It says nothing about how the actuator achieves that position
— that is the backend's job.

Segments for one actuator form an ordered stream with these rules:

* **Chained encoding.** q₀ and the anchor clock C are *implicit*: each
  segment begins exactly where and when its predecessor ended. The MCU
  carries the endpoint (including sub-unit fractional position) in a
  64-bit accumulator, so chaining introduces zero drift — the
  accumulator integrates exactly the quantized polynomial that was
  transmitted. Only `trajectory_rebase` sets q and C explicitly:
  at motion start, after homing, and after an underrun.
* **No velocity sign change inside a segment.** The host splits
  segments at velocity zero-crossings. This is a protocol invariant,
  not an optimization hint: it means direction is constant per segment,
  the stepper backend never solves for a reversal mid-segment, and
  q(t) is monotonic within every segment. It is the single biggest
  simplification of the MCU execution path.
* **Bounded duration.** A segment lasts at most 2²⁶ ticks (≈0.4–1.4 s
  across 48–180 MHz targets). The host splits longer cruises. This
  bounds fixed-point error accumulation (analysis below) and bounds
  exposure to 32-bit clock wrap-around exactly as today's protocol
  does.

### Position units

Positions are expressed in **sub-units**: 1 native unit = 2¹⁶
sub-units. The *native unit* is defined by the backend:

* stepper backend: one **microstep** (keeping parity with today's
  resolution; whether full-step with sub-unit microstepping would be
  cleaner is flagged as an open question),
* FOC backend: one encoder count or a configured angle quantum,
* PWM/DAC backend: one output quantum.

The host's segment fitter works in sub-units; the sub-unit resolution
(1/65536 of a microstep) is far below any mechanical significance, so
quantization of *position* is never the limiting error term.

## Wire format

New commands (encodings follow the existing VLQ argument scheme of
[docs/Protocol.md](../../Protocol.md)):

```
config_trajectory oid=%c backend=%c underrun_decel=%u
trajectory_rebase oid=%c clock=%u pos=%i
queue_traj_segment oid=%c flags=%c duration=%u velocity=%i accel=%i
traj_hold oid=%c duration=%u
traj_get_position oid=%c
```

MCU→host messages:

```
traj_position oid=%c clock=%u pos=%i          (reply, Class 1)
traj_underrun oid=%c clock=%u pos=%i          (event, Class 1)
traj_status oid=%c horizon_clock=%u free_slots=%hu   (telemetry, Class 2)
```

Field definitions:

* `duration` — segment length in ticks of the *executing MCU's* clock
  after machine-time conversion (see 01-Time_Model.md), ≤ 2²⁶.
* `velocity` — signed 32-bit, in **sub-units per 2¹⁶ ticks**
  (Q16.16 sub-units/tick). Range: ±2¹⁵ = ±32768 sub-units/tick — far
  above any physical rate (a 1 MHz step rate on a 100 MHz clock is
  only 655 sub-units/tick). Resolution: 2⁻¹⁶ sub-unit/tick.
* `accel` — signed 32-bit, in **velocity wire-units per 2¹⁶ ticks**
  (i.e. Q sub-units/tick² with 32 fractional bits) — the same
  "delta-of-the-delta" pattern as today's `queue_step add` argument,
  one level up. Resolution: 2⁻³² sub-unit/tick².
* `pos` — signed 32-bit in sub-units (±2³¹ sub-units = ±32768 native
  units; at a typical 1280 microsteps/mm that is ±25 m of travel).
* `flags` — reserved (bit 0 proposed: *hold-at-end* hint — prefer
  position hold over underrun ramp if the queue empties after this
  segment; see underrun policy).
* `underrun_decel` — emergency deceleration magnitude in `accel` wire
  units.

`traj_hold` is the optimized dwell (v = 0, a = 0); because a zero
polynomial has no quantization error it may use the full 32-bit
duration range rather than the 2²⁶ cap.

### Message size and bandwidth

Worst-case VLQ sizes: oid(1) + flags(1) + duration(≤4) + velocity(≤5)
+ accel(≤5) ≈ **10–16 bytes per segment**, so 3–5 segments fit in one
64-byte frame (`MESSAGE_MAX` in
[src/command.h](../../../src/command.h)). Compare `queue_step` at ~9
bytes — but a segment typically replaces *tens to hundreds* of
`queue_step` commands plus their `set_next_step_dir` traffic, since one
constant-acceleration span of any length is one segment. Analytic
estimates of segment rates under input shaping are in
[05-Host_Architecture.md](05-Host_Architecture.md); measured
comparisons are a migration-phase deliverable
([06-Migration.md](06-Migration.md)).

### Fixed-point error analysis

Two distinct error sources must be separated:

**1. Cross-segment drift: zero by construction.** The MCU accumulator
integrates the *quantized* coefficients exactly (64-bit adds). The
host-side fitter performs the same integration with the same quantized
coefficients when computing where the next segment must start. Host
and MCU therefore agree on every segment boundary to the sub-unit,
forever, without any correction traffic.

**2. Intra-segment deviation from the ideal trajectory.** Quantizing v
and a bends the executed parabola away from the ideal one by at most

    Δq(T) ≤ εv·T + ½·εa·T²,   εv = 2⁻¹⁷ sub/tick, εa = 2⁻³³ sub/tick²
    (round-to-nearest halves the resolution step)

Worked numbers at the 2²⁶-tick cap: εv·T = 2⁻¹⁷·2²⁶ = 512 sub-units
(1/128 microstep) — negligible. The accel term at 2²⁶ ticks would be
½·2⁻³³·2⁵² = 2¹⁸ sub-units (4 microsteps) — *not* negligible. The rule
that follows: **the fitter treats coefficient quantization as part of
the fit** — it evaluates the quantized polynomial against the sampled
ideal trajectory and splits the segment if the deviation exceeds the
tolerance budget. In practice that caps accelerating segments near
2²³–2²⁴ ticks (≈0.1–0.35 s; ½·2⁻³³·2⁴⁸ = 2¹⁴ sub-units = ¼ microstep),
while cruise (a = 0) segments may run to the full 2²⁶ cap. Since even
0.1 s segments cost only ~150 bytes/s per joint, splitting is cheap.

The default deviation tolerance is proposed as **max(½ microstep,
5 µm equivalent)** per actuator, configurable — chosen to sit at the
same altitude as today's 25 µs step-compression tolerance
(`MAX_STEPCOMPRESS_ERROR` in
[klippy/stepper.py](../../../klippy/stepper.py)). The exact default is
an open question for review.

## Stepper realization without an FPU

This section specifies how a stepper backend turns a segment into step
edges using only integer math. It is the on-MCU equivalent of what
host-side `stepcompress.c` fits *offline* today; the classic
`interval += add` recurrence emerges naturally as its constant-
acceleration limit.

State per actuator: 64-bit position accumulator `q_acc` (sub-units),
current segment coefficients (v, a in extended internal precision),
elapsed segment time `t` (ticks), last step interval.

Each step event must answer: *at what tick does q(t) next cross a
microstep boundary?* — i.e. solve q(t*) = q_target where
q_target = q_last ± 2¹⁶ sub-units.

**Incremental Newton solver.** Because consecutive step intervals vary
slowly (bounded acceleration), the previous interval is an excellent
seed:

```
t_guess   = t_last + interval_last
err       = q(t_guess) - q_target            ; one 32x32→64 mul + adds
v_inst    = v + a·t_guess                    ; one mul
t_next    = t_guess - err · recip(v_inst)    ; one mul (fixed-point)
```

One Newton refinement per step suffices: the seed error is O(a·Δt²),
and one iteration squares the relative error, putting the result well
inside ±1 tick for all physical accelerations. `recip(v_inst)` is a
fixed-point reciprocal maintained *incrementally* by Newton's
reciprocal iteration `r ← r·(2 − v·r)` (two multiplies), so **no
division occurs in the step loop**. Division happens only at segment
load (initial reciprocal) and near v ≈ 0, where the interval is
clamped to a maximum and the code path matches today's slow-move
handling.

The result is quantized to 1 clock tick with a bounded residual — a
*tighter* execution than today's pipeline, which deliberately distorts
step times by up to 25 µs to make them compressible.

**Rejected alternative — fixed-rate DDA.** Sampling q every N µs and
interpolating step edges (forward differencing) needs no solver, but
gives step-time granularity of the sampling period, burns constant CPU
even when idle, and its error grows with step rate exactly where
precision matters most. Newton-per-step costs CPU *proportional to
work done* and keeps single-tick timing. This RFC takes the
Newton-per-step position.

### CPU budget

Baseline from [docs/Benchmarks.md](../../Benchmarks.md): the current
`queue_step` ISR path costs ~83–88 cycles/step on Cortex-M0/M3
(step-rate benchmark: stm32f042 runs 3 steppers at 249 total ticks →
3·48 MHz/249 ≈ 578K steps/s aggregate; stm32f103 at 264 ticks →
≈ 818K steps/s).

The Newton step adds roughly 4–6 long multiplies plus bookkeeping over
the legacy path. On M0 (no single-cycle 32×32→64) a long multiply is
~5–20 cycles; estimate **200–250 cycles/step** total — about 2.5–3×
the legacy per-step cost. Ceilings:

| Target | Clock | est. steps/s aggregate | legacy benchmark |
| --- | --- | --- | --- |
| stm32f042 (M0) | 48 MHz | ~190–240K | ~578K |
| stm32f103 (M3) | 72 MHz | ~290–360K | ~818K |
| rp2040 (M0+, dual core) | 133 MHz | ~530–665K per core | ~2.5M (3 steppers, 14 ticks of its 12 MHz sched timer) |

A typical printer demands 20–80K steps/s aggregate; the budget is
comfortable on mainstream boards and tight only for extreme
microstepping at high speed (e.g. 256 µsteps at 300+ mm/s).
Mitigations, in order: TMC driver interpolation (x16 µsteps on the
wire, x256 in the driver), lower microstepping, or leaving that axis
on the legacy `queue_step` path — which coexists per-actuator
([06-Migration.md](06-Migration.md)). These estimates must be
validated by benchmark in migration phase P4.

### Sampled realization (non-stepper backends)

FOC/PWM backends do not solve for boundary crossings; they evaluate
the polynomial directly at their loop rate: q(Δt) = q₀ + v·Δt + ½a·Δt²
— two multiply-accumulates in 64-bit per sample, trivially FPU-free.
See [04-Actuator_Backends.md](04-Actuator_Backends.md).

## Queue and refill protocol

**Queues.** Each actuator has a FIFO of pending segments (intrusive
list, like today's per-oid move queues) drawing from a fixed pool
allocated at config-finalize time — the direct successor of today's
move pool (`move_finalize()` in
[src/basecmd.c](../../../src/basecmd.c)). Target segment node size:
≤24 bytes (vs 12 today). RAM sizing:

| Board | RAM for pool | nodes | horizon at 100 segs/s/joint, 4 joints |
| --- | --- | --- | --- |
| stm32f103 | ~15 KB free | ~640 | ~1.6 s |
| rp2040 | ~200 KB free | 1024 (cap) | many seconds |

Because one segment spans up to hundreds of milliseconds, the *time*
horizon per node is vastly larger than with `queue_step` — small-RAM
boards gain the most.

**Flow control — credits plus measured horizon.** Two mechanisms
compose:

1. *Slot credits* (hard safety): the MCU reports pool size in the
   config handshake (as `move_count` does today) and the host never
   exceeds it. Overflow remains impossible by construction.
2. *Time watermarks* (steady-state policy): the MCU periodically
   reports `traj_status horizon_clock free_slots` on the telemetry
   class. The host refills any actuator whose horizon falls below a
   low watermark (e.g. 0.5 s) up to a high watermark (e.g. 1.0 s).
   This replaces the host-side open-loop `BGFLUSH_*` constants in
   [klippy/extras/motion_queuing.py](../../../klippy/extras/motion_queuing.py)
   with *measured* MCU state. An optional Class-1 low-watermark event
   from the MCU can shave refill latency; the periodic status report
   alone is sufficient for correctness.

**Underrun policy — degrade, don't die.** If an actuator's queue
empties while its velocity is non-zero, the segment core synthesizes a
deceleration segment to v = 0 at the configured `underrun_decel`,
executes it, latches an *underrun* state, and emits
`traj_underrun oid clock pos` (Class 1). While latched, further
`queue_traj_segment` commands are rejected (nak'd as a protocol error
back to the host) until a `trajectory_rebase` re-anchors position and
time — because the synthesized ramp diverged from the host's chained
position by design.

Stated honestly: independent per-joint deceleration breaks kinematic
coordination — on a corexy, the toolhead deviates from the commanded
path during the ramp. This is an *emergency* ramp, not a planned stop;
it is still strictly better than today's behavior, where the same
situation is an instantaneous `Timer too close`/`Move queue overflow`
shutdown mid-extrusion. Backends may prefer a different terminal
behavior (an FOC drive can hold position; the hold-at-end flag lets
the host choose). Heater safety is unaffected — the independent
`max_duration` watchdog semantics are unchanged
([03-Traffic_Classes.md](03-Traffic_Classes.md)).

**Recovery flow:** host notices `traj_underrun` → re-plans from the
reported (clock, pos) → sends `trajectory_rebase` + fresh segments.
For a printer this surfaces as a resumable pause, not a failed print.

## Homing, probing, and trsync

The trsync trigger mechanism ([src/trsync.c](../../../src/trsync.c))
is unchanged. The segment core registers a `trsync_signal` exactly
where `stepper_stop()` registers today
([src/stepper.c](../../../src/stepper.c)): on trigger, *in interrupt
context*, it aborts the actuator's segment queue, invokes the
backend's stop, and preserves the position accumulator.

Consequences:

* **Stop latency is independent of queue depth.** A deeper intention
  queue commits more *planned* motion, but the abort path runs in the
  same IRQ context as today — safety does not degrade
  ([06-Migration.md](06-Migration.md) discusses this risk explicitly).
* **Probe readback improves.** `traj_get_position` returns the
  sub-unit accumulator, giving sub-microstep trigger positions versus
  today's whole-step readback reconstructed from `queue_step` history
  (`stepcompress_find_past_position()` in
  [klippy/chelper/stepcompress.c](../../../klippy/chelper/stepcompress.c)).
* **Drip mode becomes pure host policy.** Today homing feeds 50 ms
  slices (`DRIP_SEGMENT_TIME`,
  [klippy/extras/motion_queuing.py](../../../klippy/extras/motion_queuing.py))
  to keep the MCU queue shallow. Under this protocol the host simply
  keeps the homing actuator's horizon shallow (~50 ms) via the same
  refill machinery — no special protocol mode.

## Open questions

* Native unit ownership: microstep (proposed) vs full-step vs fully
  backend-defined scale reported in the data dictionary.
* Exact default fitting tolerance, and whether it should adapt to
  link bandwidth (see [07-Link_Transport.md](07-Link_Transport.md)).
* Whether `flags.hold-at-end` is per-segment (proposed) or per-actuator
  config.
* Whether underrun segment-rejection should be a nak (proposed) or a
  silent drop with a status flag.
