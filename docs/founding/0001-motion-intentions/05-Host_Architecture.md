# FD-0001: Host Architecture

Status: Core and machine-time host preflight implemented and
workstation-tested in HELIX 0.9; hardware timing validation remains.

This document maps the redesign onto the existing host code
(`klippy/` and `klippy/chelper/`): what survives untouched, what is
repurposed, what is added, and what dies for migrated actuators.

The load-bearing observation: **the host already computes the right
thing** — a smooth per-joint position trajectory — it just destroys it
by flattening to step pulses before transmission. The new host work is
therefore mostly *subtraction*, plus one new module.

## Survives unchanged

* G-code frontend, macros, API server, all `extras/` modules.
* **Lookahead planning**: `Move`, `LookAheadQueue`, junction velocity
  planning ([klippy/toolhead.py](../../../klippy/toolhead.py)).
* **The trapezoid queue**: `struct move` segments over absolute time
  ([klippy/chelper/trapq.c](../../../klippy/chelper/trapq.c)) — still
  the shared source of truth for all motion.
* **All kinematics** (`kin_cartesian.c`, `kin_corexy.c`,
  `kin_delta.c`, …), **input shaping** (`kin_shaper.c`), **pressure
  advance** (`kin_extruder.c`) — every `calc_position_cb` keeps
  working exactly as-is, because the fitter consumes the same
  position-sampling interface that step generation consumes today.
* Heater control, `max_duration` safety, bed meshing, probing logic.

## Repurposed

* **itersolve** ([klippy/chelper/itersolve.c](../../../klippy/chelper/itersolve.c)):
  its kinematic position evaluation becomes the **joint trajectory
  sampler** feeding the segment fitter. Its *step-boundary root
  finding* (the secant search for each step crossing) is simply not
  called for migrated actuators — that job moved to the MCU.
* **clocksync** ([klippy/clocksync.py](../../../klippy/clocksync.py)):
  the primary-MCU regression survives verbatim (the host still needs
  host-time → machine-time); `SecondarySync`'s re-sloping is replaced
  by the beacon relay of [01-Time_Model.md](01-Time_Model.md).
* **Background thread scaffolding**: the per-stepper worker threads
  introduced by `steppersync.c` (`syncemitter`) are kept as the
  parallelism harness — they run the fitter per joint instead of
  itersolve+stepcompress per stepper.
* **Flush cadence** ([klippy/extras/motion_queuing.py](../../../klippy/extras/motion_queuing.py)):
  the flush-timer structure remains, but its targets change from
  open-loop host constants (`BGFLUSH_*`) to **MCU-reported horizons**
  (`traj_status`, see
  [02-Intention_Protocol.md](02-Intention_Protocol.md)).

## New: the segment emitter

One new C helper plus a thin Python owner (working names
`chelper/segfit.c`, `klippy/extras/trajectory_queuing.py`).

For each migrated joint, per flush interval:

1. **Sample** the joint position q(t) over the flush window via the
   existing `calc_position_cb` chain — kinematics, input shaper and
   pressure advance included for free.
2. **Fit greedily**: extend a candidate quadratic segment while the
   maximum deviation between the (coefficient-quantized) segment and
   the sampled trajectory stays below tolerance; emit and restart when
   it would exceed it. Continuity is C0 by chaining (each segment
   starts at the previous quantized endpoint); velocity continuity is
   implicit wherever the underlying trajectory is smooth. When a quantized
   pure-velocity fit also meets the error budget, prefer it over a quadratic
   with a tiny rounding-induced acceleration. Motion fitting may spend the
   configured path-tolerance budget to enter the MCU cruise fast path after an
   acceleration ramp; value/PWM fitting additionally limits chained endpoint
   bias to 32 sub-units so final-value errors cannot accumulate across chunks.
3. **Force breakpoints** at velocity zero-crossings (protocol
   invariant), at trapq move boundaries (natural fit boundaries), and
   at the duration caps of
   [02-Intention_Protocol.md](02-Intention_Protocol.md).

The flush callback fits through Klipper's **step-generation horizon**
(`step_gen_time`), not the earlier queue-commit horizon (`flush_time`).  This
matches the legacy itersolve/stepcompress contract and keeps a freshly emitted
anchor from already being due when it reaches the MCU.  It also seals and
emits the valid partial fit at every callback boundary.  Otherwise a long
constant-velocity span would remain pending until the 4.096-second duration
cap and arrive with a start clock several seconds in the past.

The sampled kinematic coordinate is logical joint space, while homing and
`SET_POSITION` may leave the MCU in a different physical step coordinate.
Each rebase therefore establishes a constant logical-to-physical sub-unit
offset which the fitter applies to every later sample.  The wire rebase carries
both the continuous physical sub-unit anchor and the integer physical step
counter; neither is inferred from the logical trapq coordinate.
Trajectory readback is normalized to signed 32-bit form before it updates that
offset; this matters for the negative member of a CoreXY pair after a homing
trigger. The host range-checks both rebase fields before shifting or encoding
them, so an out-of-range absolute anchor fails with a named trajectory error
instead of overflowing CFFI or silently wrapping on the wire.

Klipper transmits scheduled commands before their execution clocks.  A rebase
whose clock is at or beyond the previous emitted horizon is therefore queued
as an ordered executor barrier; the continuous anchor and physical integer
step phase both change only when that barrier is reached.  An overlapping
rebase remains a firmware shutdown.  A confirmed homing trigger, queried stop,
or underrun flushes queued barriers together with queued motion.  This keeps a
later logical move from rebasing an earlier physical trajectory merely because
its packet arrived early.

At the end of every planned path the host emits a one-millisecond `traj_hold`
for every participating actuator.  This is a zero-displacement terminal state,
not a dwell visible to kinematics.  It prevents a tiny fixed-point residual in
one fitted CoreXY stream from looking like live velocity when its queue drains;
a genuinely moving empty queue still takes the configured underrun ramp.

Before any rebase or segment mutates the fitter's chained anchor, the owner
checks the target MCU through `timesync.is_mcu_synced()`. That state is
refreshed at the steady beacon cadence and includes the same freewheel-age
bound as firmware. A stale secondary therefore fails on the host before the
persisted intention twin advances, while firmware independently rejects the
same unsynchronized Class-0 anchor/segment if a caller bypasses the host.

The host also persists each exact wire rebase and segment (clock, duration,
flags, coefficients, and chained start/end position) into the Atlas JSONL
boundary.  Combined with the MCU execution-log records on the same mapped time
axis, this is sufficient to replay the deterministic step solver and audit a
homing run without relying on console timing or motor sound.

**The honesty point this document must state plainly:** with input shaping
and pressure advance active, the joint trajectory is *not* piecewise
constant-acceleration — shaped motion is a sum of time-shifted
trapezoids and PA adds a smoothed derivative term. Per-joint quadratic
segments are therefore a **fitted approximation with an explicit error
budget**, not an exact re-encoding. This is the same trade the
current architecture already makes one level down, where
`stepcompress.c` deliberately distorts step times by up to 25 µs
(`MAX_STEPCOMPRESS_ERROR`) to fit `{interval, count, add}` ramps. The
proposal moves the approximation from the *time* domain (µs of step
timing) to the *position* domain (fractions of a microstep), where its
effect on print quality is directly statable and configurable.

### Segment-rate / bandwidth estimate

Outside shaped regions, segments are as long as the trapezoid phases —
a handful per move. Inside shaped regions, the fitter must track
curvature at the shaper timescale: a 40–60 Hz shaper bends the
trajectory on a 3–5 ms scale, so expect worst-case **~200–500
segments/s per shaped joint** during continuous cornering, ~12 bytes
each → **2.5–6 KB/s per joint**, ~10–25 KB/s for a shaped XY pair plus
extruder. Comfortable on USB (≥ several-hundred KB/s effective) and
CAN at 1 Mbit (~60 KB/s effective); *tight on 250 kbaud UART*
(~20 KB/s effective) — such links need a relaxed fitting tolerance
(fewer, coarser segments) or legacy mode. These are analytic
estimates; measuring them against real `queue_step` byte rates is a
phase-2 deliverable ([06-Migration.md](06-Migration.md)).

For comparison, the same shaped cornering today generates *more*
`queue_step` traffic (compression runs shorten as acceleration
changes), so the expected direction is a large reduction in steady
printing and rough parity in the worst shaped corners.

## Dies (for migrated actuators)

* **stepcompress** step-queue generation and flushing
  ([klippy/chelper/stepcompress.c](../../../klippy/chelper/stepcompress.c)) —
  including its 25 µs timing distortion and the step+dir+step filter;
  its `history` role for homing readback is replaced by
  `traj_get_position` sub-unit readback.
* **The steppersync slot heap**
  ([klippy/chelper/steppersync.c](../../../klippy/chelper/steppersync.c)) —
  the min-heap that models MCU move-queue slot recycling exists only
  because thousands of tiny commands share 1024 slots. Segment credits
  plus time-horizon watermarks replace it.
* Per-stepper `set_next_step_dir`/`reset_step_clock` bookkeeping in
  [klippy/stepper.py](../../../klippy/stepper.py) (subsumed by
  `trajectory_rebase`).

None of it is deleted while any configured actuator still uses the
legacy path ([06-Migration.md](06-Migration.md)).

## Host modernization: boundaries and the reactor question

The current host carries two irreconcilable programming philosophies:
klippy's bespoke greenlet reactor and hand-rolled event loop, and the
asyncio world everything *around* klippy (Moonraker, front-ends,
tooling) is written in. The boundary between klippy's Python and its
C helpers is the sharpest pain: the wire protocol is implemented a
second time in `chelper` ([klippy/chelper/msgblock.c](../../../klippy/chelper/msgblock.c)
duplicates `src/command.h`'s constants), bound through stringly-typed
FFI with no versioned header, while firmware-side command registration
happens in `DECL_*` linker-section metaprogramming expanded by a
build-time generator. It works; it is also unreadable from outside,
and every one of those idioms concentrates change in whoever already
holds the whole system in their head.

Positions this fork takes:

* **All new host components are asyncio-native**: the segment
  emitter's Python owner, the transport layer for
  [07-Link_Transport.md](07-Link_Transport.md), the failure-recovery
  orchestration of [08-Failure_Recovery.md](08-Failure_Recovery.md),
  and their tests. New code does not extend the greenlet reactor.
* **The protocol library replaces the duplicated wire code**
  ([10-Protocol_Library.md](10-Protocol_Library.md)): host transmit
  machinery becomes a consumer of the same MIT library the firmware
  uses, through a real versioned C API and a cffi binding — not a
  parallel implementation.
* **Honest scope note:** wholesale replacement of klippy's reactor is
  *not* undertaken up front — the legacy motion path keeps running on
  it, bridged to the asyncio side at a single documented seam, and the
  reactor shrinks as subsystems migrate rather than being rewritten in
  place. Rewriting the reactor first is how forks die; strangling it
  is how they ship.

### The seam (implemented)

That single documented seam now exists as
[klippy/extras/asyncio_bridge.py](../../../klippy/extras/asyncio_bridge.py).
It runs **one asyncio event loop in a dedicated daemon thread**
alongside the greenlet reactor and exposes a small, thread-safe,
two-way handoff — nothing in `reactor.py` is modified; the reactor is
used exactly as a well-behaved external caller would:

* **reactor → asyncio**: `bridge.run_coro(coro)` schedules a
  coroutine on the loop with `asyncio.run_coroutine_threadsafe()` and
  returns a `ReactorCompletion`; the coroutine's
  `concurrent.futures.Future` done-callback relays the outcome back
  with `reactor.async_complete()` onto that completion, which the
  reactor greenlet `wait()`s on. `run_coro_wait(coro)` is the
  blocking, raise-on-error convenience form.
* **asyncio → reactor**: `bridge.call_reactor(fn)` runs `fn(eventtime)`
  in reactor context via `reactor.register_async_callback()` (klippy's
  existing pipe + async-queue cross-thread wake) and resolves an
  `asyncio.Future` with the result through `loop.call_soon_threadsafe()`.

Lifecycle is tied to `klippy:connect` / `klippy:disconnect`. The core
`AsyncioBridge` takes only a reactor, so the handoff is unit-tested
without a full printer:
[test/asyncio_bridge_test.py](../../../test/asyncio_bridge_test.py)
drives the real reactor and asserts both directions close (and that
the coroutine and the reactor callback run on different threads).

**Proof-of-consumer.** To show the seam is real without rewriting
anything, the `EXECLOG_DUMP` drain in
[failure_recovery.py](../../../klippy/extras/failure_recovery.py) has
an **opt-in** asyncio path behind `asyncio_drain:` (default off): the
drain orchestration runs as a coroutine on the bridge, and each
board's MCU I/O — which is reactor-only — hops back to the reactor
through `call_reactor`, exercising *both* directions on a live
consumer. Any bridge failure falls back to the direct reactor drain,
so the working extra never regresses. The shutdown flight-recorder
drain and every other reactor call are deliberately left on the
reactor.

**Migration path.** New asyncio-native owners adopt the seam the same
way: `trajectory_queuing`'s flush/segment-emit loop and
`failure_recovery`'s link-loss orchestration become coroutines that
`await bridge.call_reactor(...)` for the reactor-only touches (MCU
command queues, clock reads, `pause_resume`) while their own logic
runs on the loop, and reactor-context entry points reach them with
`run_coro`/`run_coro_wait`. The library transmit machinery of
[10-Protocol_Library.md](10-Protocol_Library.md) and the UDP transport
of [07-Link_Transport.md](07-Link_Transport.md) are asyncio-native on
the loop side of this same seam. No consumer is converted wholesale in
this step; each migrates behind its own flag as it is validated.

## Buffering policy restated

* `BUFFER_TIME_HIGH` (1.0 s pause wall in toolhead.py) survives as the
  host-side planning depth limit.
* `MIN_SCHEDULE_TIME` (0.1 s) survives as the minimum arrival margin,
  now interpreted against machine time.
* The `BGFLUSH_*` step-generation windows are replaced by per-MCU
  measured horizons — the host stops guessing how full the MCU is.

## Host CPU expectation

Sampling plus quadratic fitting is expected to cost the same order as
today's itersolve secant search plus stepcompress bisection (both are
a few arithmetic operations per sample point at comparable sample
densities), while everything downstream (per-step queue management,
slot heap, compression) disappears. Expected net: neutral to
favorable; to be measured in phase 2 via the existing batch-mode
benchmark harness ([docs/Benchmarks.md](../../Benchmarks.md)).

## Open questions

* Fitter sampling density: fixed rate vs adaptive (curvature-driven).
* Whether the fitter should fit jerk-limited cubics later (protocol
  reserves nothing for it today; flags byte could version segment
  order — deliberately out of scope for v1).
* Exact placement of the emitter in the flush pipeline relative to
  extra-axis (non-kinematic) consumers.
