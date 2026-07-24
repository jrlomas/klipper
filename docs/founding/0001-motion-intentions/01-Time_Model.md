# FD-0001: Time Model

Status: Implemented and physically characterized in HELIX 0.9. The host relay
uses the public MCU ClockSync accessor; host preflight and firmware both gate
every trajectory anchor/segment on secondary convergence and freewheel
freshness. Independent-USB testing proved that those internal gates do not by
themselves establish the target absolute inter-MCU phase; optional hardware-
bounded timing assurance remains open. The primary-MCU authority described
here remains the compatibility default; its planned evolution into
configurable authorities, timestamp adapters, protocol bridges, and explicit
quality propagation is [20-Unified_Machine_Time.md](20-Unified_Machine_Time.md).

This document defines **machine time** — the timeline every intention
is scheduled against — and moves its authority from the host's
statistical estimate to the primary MCU's physical counter.

## Current authority: machine time is the primary MCU's clock

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
* The canonical wire profile stamps segments in machine time and lets the
  executing board own conversion. The implemented higher-order fitter also
  has an explicit `TSEG_LOCAL_TIME` profile: its coefficients are already in
  the executing timer domain. A secondary stream using that profile carries
  both its shared machine-time intent and an immutable local execution clock
  on each rebase boundary; mixing a machine-time rebase conversion with
  already-local queued durations is forbidden.

## Wire representation

* Machine-time instants on the wire remain **32-bit truncated** values
  with 64-bit reconstruction from context, exactly as clocks are
  handled today (`clock32_to_clock64()` in clocksync.py) — no message
  grows.
* Segment durations are 32-bit tick counts (bounded to 2²⁶ by
  [02-Intention_Protocol.md](02-Intention_Protocol.md)).
* `TSEG_LOCAL_TIME` is a complete stream-domain choice, not a per-field
  optimization. Its durations and derivatives are fitted in the executing
  MCU's timer domain, and a secondary uses `trajectory_rebase_local` so the
  absolute boundary is committed to that same domain. `machine_clock` remains
  present for shared intent/telemetry and the Class-0 convergence gate remains
  mandatory.
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

This remains the behavior of existing configurations. The unified
machine-time fabric does not silently redefine their timeline: selecting a NIC
PHC, PTP grandmaster, or gateway clock requires an explicit authority,
authority epoch, time-path configuration, and coordinated requalification.

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
technique `clocksync.py` uses today). The smallest observed round-trip
rejects ordinary queueing delay, but its midpoint is an offset estimate only
under a link-symmetry assumption; it does not reveal directional latency
asymmetry. A short trailing regression over those extended cross-link
samples supplies the relay endpoint, reducing the leverage of any single
USB regression update while retaining the measured oscillator ratio.

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
a bus and hardware RX timestamps are available) removes the host from the
loop entirely. Physical Pico/EBB36 testing shows that such a hardware-timed
path, or an equivalently qualified transport, **is required for a hard
absolute-phase guarantee**. The host-relayed USB path remains an operational,
statistically qualified scheduled-traffic mode; lack of a hard bound does not
invalidate its demonstrated print behavior.

For STM32 USB FS, a SOF observed only after a global interrupt-disabled
critical section is not a valid timestamp. The firmware therefore clears any
pending SOF immediately before restoring `PRIMASK`, while leaving USB
endpoint/reset flags pending for normal service. This deliberately converts
unbounded ISR latency into a missing observation; the estimator freewheels
from prior qualified samples instead of learning a false phase error.

The proposed two-step CAN broadcast, Ethernet/PTP, WiFi/TSF, and dedicated
timer-capture profiles—together with STM32, RP2040, and ESP32 capability
research—are specified in
[Transport-Derived Machine-Time Synchronization](../../Transport_Time_Synchronization.md).

## Budgets

* **Target inter-MCU sync error: ≤ ±10 µs.** Multi-MCU stepping tolerates the
  25 µs step-compression distortion, and multi-MCU homing runs on a
  25 ms watchdog budget ([src/trsync.c](../../../src/trsync.c),
  `TRSYNC_TIMEOUT` in [klippy/mcu.py](../../../klippy/mcu.py)). ±10 µs
  keeps cross-board step alignment below one microstep at any
  practical speed.
  This is a physical acceptance target, not a consequence of an internal PI
  residual. For independent request/response links, a timestamp known only to
  lie between host send and receive has midpoint uncertainty of one half-RTT;
  the symmetry-free relative bound is the sum of both links' half-RTTs. A
  transport may claim hardware-bounded ±10 us assurance only when that bound
  (or a stronger capture bound) is within 10 us and scope qualification
  confirms it. This assurance label is separate from the Class-0 Scheduled
  traffic class defined in [03-Traffic_Classes.md](03-Traffic_Classes.md).
  USB scheduled motion may instead be statistically qualified against an
  application-derived print tolerance using physical mean, deviation, tails,
  and successful print evidence.
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
