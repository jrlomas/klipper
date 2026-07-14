# FD-0001: Time Model

Status: Implemented off-silicon in HELIX 0.9. The host relay uses the public
MCU ClockSync accessor; host preflight and firmware both gate every trajectory
anchor/segment on secondary convergence and freewheel freshness. Hardware
timing validation remains.

This document defines **machine time** — the timeline every intention
is scheduled against — and moves its authority from the host's
statistical estimate to the primary MCU's physical counter.

## Authority: machine time is the primary MCU's clock

Today, each MCU free-runs its own counter (e.g. `DWT->CYCCNT` on ARM,
see [src/generic/armcm_timer.c](../../../src/generic/armcm_timer.c)),
and the *host* is the timekeeper: it regresses every MCU's clock
against host time ([klippy/clocksync.py](../../../klippy/clocksync.py))
and converts all event times into each MCU's domain. The host promises
timing it doesn't physically own.

This document inverts the authority. **Machine time is defined as the
primary MCU's free-running counter**, extended to 64 bits with an
epoch established at connect. Nothing changes physically — the same
crystal ticks — but the contract changes:

* The **host** becomes a slave: it estimates *host-time → machine-time*
  in order to plan far enough ahead, using the same proven machinery
  as today (min-RTT-anchored, exponentially-decayed linear regression —
  `clocksync.py`'s math survives unchanged; only its role is renamed).
  The −3σ conservative bias that today protects command schedule
  times now protects segment *arrival deadlines*.
* **Secondary MCUs** discipline their local clocks to machine time via
  sync beacons (below) and convert on ingest.
* The host never converts motion times into secondary-local clocks
  anymore — segments are stamped in machine time everywhere, and the
  board that executes them owns the conversion.

## Wire representation

* Machine-time instants on the wire remain **32-bit truncated** values
  with 64-bit reconstruction from context, exactly as clocks are
  handled today (`clock32_to_clock64()` in clocksync.py) — no message
  grows.
* Segment durations are 32-bit tick counts (bounded to 2²⁶ by
  [02-Intention_Protocol.md](02-Intention_Protocol.md)).
* Each secondary maintains a discipline pair **(offset, rate)** mapping
  machine time → local ticks, with rate as an unsigned fixed-point Q8.24
  ratio. The integer range covers different nominal timer frequencies
  (for example, a 64 MHz secondary against a 12 MHz primary) while the
  fractional range resolves that 5.333× ratio to about 0.01 ppm; crystal
  mismatch is then tracked around the nominal ratio. Conversion is one
  32×32→64 multiply plus shift **per segment at ingest** — never per step,
  and never on the interrupt path.
* At setup, the host seeds this rate from the two advertised `CLOCK_FREQ`
  constants. Beacon priming measures residual oscillator drift across the
  full priming span; it does not try to rediscover a large nominal ratio
  from individual short USB intervals.

Rationale for ingest-time conversion over host-side conversion
(today's model): it makes segments *identical for every board* (one
encoding of the motion, N consumers), moves per-board state to the
board that owns it, and is what allows a future direct MCU-to-MCU sync
extension without touching the motion protocol.

## Sync beacon protocol (host-relayed)

No new hardware or wiring is required; beacons ride the existing
links. The design borrows PTP's structure (timestamped beacon +
per-link delay correction + slave disciplining) without its generality.

```
host → primary:    sync_beacon_read
primary → host:    sync_beacon seq=%c clock=%u          (Class 1)
host → secondary:  sync_beacon_relay seq=%c machine_clock=%u local_est=%u
```

The host stamps `local_est` — its best estimate of the *secondary's*
local clock at the moment the primary's counter read `machine_clock` —
using its per-link min-RTT-filtered offset measurements (the same
technique `clocksync.py` uses today; measurement quality is anchored
by the smallest observed round-trip, which is unaffected by mean
latency).

On receipt, the secondary updates its (offset, rate) pair through a
slew-limited proportional-integral filter: offset errors are corrected
gradually by biasing rate, never by stepping the clock — a stepped
clock would corrupt in-flight segment schedules. Beacon cadence:
~1 Hz, matching today's clock-query cadence (0.9839 s in
clocksync.py). Positive and negative Q8.24 corrections use the same
explicitly signed saturation bound; the fixed-point numerator is formed by
signed multiplication so negative phase error has fully defined C behavior.

**Why keep the host in the relay path?** It requires zero new
transport capability (works over point-to-point USB/serial where MCUs
cannot hear each other), and the host's relay jitter is filtered by
the same min-RTT + regression machinery that makes today's sync work.
A direct MCU-to-MCU beacon (e.g. CAN broadcast, where all boards share
a bus and hardware RX timestamps are available) is specified as an
optional extension — it removes the host from the loop entirely but
is *not* required for correctness.

## Budgets

* **Target inter-MCU sync error: ≤ ±10 µs.** Today's host-mediated
  sync already achieves tens of µs; multi-MCU stepping tolerates the
  25 µs step-compression distortion, and multi-MCU homing runs on a
  25 ms watchdog budget ([src/trsync.c](../../../src/trsync.c),
  `TRSYNC_TIMEOUT` in [klippy/mcu.py](../../../klippy/mcu.py)). ±10 µs
  keeps cross-board step alignment below one microstep at any
  practical speed.
* **Drift between beacons:** crystals differ by 20–100 ppm →
  uncorrected drift of 20–100 µs per second. At 1 Hz beacons with rate
  (not just offset) discipline, residual error is dominated by rate
  estimation noise, comfortably inside the ±10 µs target — the rate
  term is why 1 Hz suffices.
* **Freewheel budget on beacon loss:** with a disciplined rate, drift
  accrues at the *residual* rate error (≪1 ppm), not the raw crystal
  mismatch. Proposed budget: a secondary that has received no beacon
  for **5 s** must assume its clock is stale.

## Failure behavior

* **Beacon loss (≤ budget):** freewheel on the last (offset, rate).
  Normal operation; brief link congestion is invisible.
* **Beacon loss (> budget):** the secondary refuses further Class-0
  ingest (segments would be scheduled against a clock it can no longer
  vouch for) and, if any actuator is mid-motion, executes its underrun
  ramp ([02-Intention_Protocol.md](02-Intention_Protocol.md)) — a
  controlled stop and a resumable event, in place of executing motion
  at the wrong time or shutting down hard.
* **Primary loss:** machine time itself is gone; this is equivalent to
  today's primary-MCU disconnect and follows the same shutdown path.

## Startup

1. Host connects to the primary, reads the data dictionary, samples
   the counter (as `get_uptime`/`get_clock` do today) and declares the
   64-bit machine-time epoch.
2. Host connects each secondary, primes its (offset, rate) with a
   burst of beacons (mirroring today's 8-sample priming), then drops
   to the 1 Hz cadence.
3. Class-0 traffic to a board is enabled only after its discipline
   filter reports convergence (bounded offset variance) — the
   successor of today's "clock synchronization" connect phase.

## Open questions

* Should the machine-time epoch survive a primary MCU reconnect?
  (Proposed: no — a primary reset invalidates all scheduled state
  anyway; recover exactly as today's restart does.)
* Direct CAN-broadcast beacon extension: worth specifying timestamping
  requirements now, or defer until a CAN-heavy reference machine
  exists?
* Whether the host should expose machine time to clients through the
  API server (useful for synchronized cameras/sensors).
