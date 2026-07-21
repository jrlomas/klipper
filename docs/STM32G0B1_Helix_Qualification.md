# STM32G0B1 HELIX motion qualification

## Why spending MCU cycles is the point

Klipper V1 and HELIX make opposite resource trades.

Klipper V1 solves motion on the host, compresses the resulting step times, and
sends a deadline-sensitive stream that grows with the number of physical
edges. The MCU is intentionally simple: it expands that stream and toggles
GPIO. This is excellent use of very small MCUs, but the host-to-board link is
part of the real-time step-generation loop.

HELIX sends a much smaller stream of time-bounded polynomial intentions. The
MCU integrates each intention, solves every half-step crossing against its own
clock, toggles GPIO, and records what it executed. The board therefore works
harder, by design. In return, link traffic scales with changes in the motion
curve rather than with microstep count, and the board owns enough state to
hold, stop on a local interrupt, report its position, and reconcile execution
after a link interruption.

```text
Klipper V1
G-code -> host planner -> host crossing solver -> step compression
       -> [one deadline stream proportional to physical edges] -> MCU GPIO

HELIX
G-code -> host planner -> bounded quintic fitter
       -> [short coefficient stream proportional to curve segments]
       -> MCU crossing solver -> MCU GPIO + execution log
```

The right acceptance criterion is consequently not “does HELIX leave the MCU
idle?” It is:

> Can the least powerful intended board compute the same physically required
> edge stream as V1, within a deterministic interrupt budget, while delivering
> the autonomy and observability that motivated onboard computation?

For the current V0 toolhead, that least-powerful target is the EBB36 v1.2's
64 MHz STM32G0B1. This document records the 2026-07-14 qualification of that
target.

## Result in one paragraph

The STM32G0B1 passes the practical EBB36 extrusion requirement. A production
solver regression time-compresses a captured quintic through 1x, 2x, 4x, 8x,
and 16x rates while preserving its geometry. The 16x case is approximately
20,000 physical extruder steps/s and must keep every solve within a 1/8-step
spatial bound and below 75% of the following pulse interval. It passes on the
real board. At the V0's approximately 705.5 steps/mm gearing, that is 28.3 mm/s
of 1.75 mm filament, or a kinematic 68.2 mm3/s. That is a solver capacity
conversion, not a claim that the installed hotend can melt 68.2 mm3/s. A 32x,
approximately 40,000-step/s probe was rejected because its 1,304-tick
(20.4 us) solve exceeded the 18.75 us solve deadline needed to preserve a
6.25 us / 25% reserve. The interval between 20k and 40k remains unqualified.

![STM32G0B1 practical extrusion qualification](img/helix-g0b1-rate-envelope.svg)

This establishes practical parity, not an unlimited raw-rate claim. V1's
precomputed stream can retain a higher synthetic edge ceiling because the MCU
does less work per edge. HELIX deliberately exchanges some of that unused
ceiling for local motion authority. The current evidence says that exchange is
sound for the EBB36's physical role; it does not say every conceivable
microstep rate is already supported.

## What executes on the STM32G0B1

Each active quintic carries fixed-point velocity, acceleration, jerk, snap,
and crackle. The firmware maintains a drift-free Q32.32 position accumulator
and solves the time at which the curve crosses the next half-step boundary.
The deadline path is specialized for a division-poor Cortex-M0+:

- pre-scaled Horner coefficients keep common intermediates in signed 32-bit
  range;
- four multiply-shift stages evaluate the quintic position;
- pure cruise uses a quotient/remainder recurrence rather than Newton;
- recurring curved motion predicts the next crossing from the previous
  interval, performs four cheap reciprocal refinements, and uses a bounded
  exact quotient fallback if the residual is still outside 1/8 step;
- a prior interval at a segment boundary is only a prediction seed: every
  proposed crossing is checked against the new polynomial before it can be
  scheduled;
- the first two edges of a zero-speed start use full convergence, after which
  the recurring interval fast path takes over;
- a cold solve after a direction reversal has no stale interval predictor;
  if Newton does not land within 1/4 step, a bounded monotonic sign bracket
  selects the nearest physical crossing instead of emitting `t_prev + 1`;
- a segment that cannot converge inside the bounded refinements fails closed
  instead of emitting delayed catch-up pulses;
- an explicit 25% timer reserve protects other precision clients rather than
  treating “finished one tick before the pulse” as success.

This is substantial MCU work, but it is bounded work performed exactly where
the timing authority lives. Temperature sampling, communications, software
TMC UART, and other timer clients remain reasons for the reserve; the solver
is not allowed to consume the whole interrupt interval merely because a test
move still appears to work.

## Experiment 1: V1 edge-stream equivalence

`test/trajectory_v1_pulse_compare.py` sends the same trap queue through two
independent paths:

1. Klipper's original `itersolve -> stepcompress` pipeline, expanded back into
   the edge stream an MCU receives; and
2. quintic `segfit` followed by the exact production crossing solver compiled
   from `src/trajq.c` and `src/traj_stepper.c`.

The test compares every edge count and direction, not just the final position.
It includes acceleration from rest, reverse extrusion, a signed phase wrap, a
short move, and an EBB-like 19.2k-step/s profile.

![V1 and HELIX pulse-count parity](img/helix-g0b1-pulse-parity.svg)

| Case | V1 edges | HELIX edges | Mean time difference | Maximum time difference |
|---|---:|---:|---:|---:|
| Quintic homing profile | 4,000 | 4,000 | 25.08 us | 511.50 us |
| Quintic reverse retract | 2,400 | 2,400 | 30.04 us | 511.50 us |
| Quintic phase wrap | 3,200 | 3,200 | 22.52 us | 511.50 us |
| Quintic short move | 240 | 240 | 53.91 us | 511.50 us |
| Quintic EBB-like fast profile | 4,000 | 4,000 | 23.98 us | 266.33 us |

The large maximum time differences occur near zero velocity, where a bounded
spatial fitting error maps to a comparatively large time difference. They are
not missing or extra edges. Across the fidelity corpus, fitted quintics remain
inside the configured half-microstep motion tolerance and exact coefficient
chaining has zero accumulated fixed-point drift.

This corpus found a real defect during qualification: a quintic beginning at
exactly zero velocity used the queue-idle polling horizon as its first
crossing estimate. On a short acceleration segment that deferred early edges
to the segment boundary and created catch-up pulses. The final solver seeds
inside the segment, fully converges the first two crossings, and then switches
to the recurring predictor. The corpus would fail with 4,001 HELIX edges
against 4,000 V1 edges before the fix; all five cases above now match.

### Experiment 1b: real sliced G-code differential

The synthetic corpus is necessary but was not sufficient. The first
supervised benchmark print became jerky and skipped near seams, even though a
post-run endpoint audit reported matching intended pulse totals. That audit
evaluated the polynomial crossings mathematically; it did not retain the
production solver's interval predictor and reciprocal state across every
physical edge.

`scripts/helix_gcode_pulse_compare.py` now runs an ordinary sliced G-code file
through two real Klippy file-output passes without moving a printer:

1. stock `itersolve -> stepcompress -> queue_step`, expanded into individual
   V1 edges; and
2. the production HELIX G-code/planner/fitter path, decoded into intentions
   and replayed through the exact `src/traj_stepper.c` state machine.

On the captured failed-print intentions, the old firmware solver fell behind
by as many as 571 X steps, 206 Y steps, 122 extruder steps, and 121 Z steps.
It subsequently emitted runs of near-one-tick catch-up pulses. Two assumptions
were invalid: four inexpensive interval corrections were treated as success
even with a large spatial residual, and an interval inherited at a segment
boundary was scheduled without validating it against the new polynomial.

The corrected solver validates boundary predictions and invokes an exact
bounded Newton quotient when the cheap recurrence remains outside 1/8 step.
If fixed-point timer quantization makes 1/8 step unrepresentable, it selects
the nearest bracketed tick up to a hard 1/4-step limit; anything beyond that
fails closed. Exact replay of the entire captured session now has zero
endpoint mismatches and no intervals at or below 64 MCU ticks on X, Y, Z, or
E.

The offline two-layer run used the real benchmark G-code through the end of
layer-two solid infill:

| Actuator | V1 edges | HELIX edges | V1 minimum interval | HELIX minimum interval |
|---|---:|---:|---:|---:|
| CoreXY X | 148,192 | 148,298 | 690 ticks | 692 ticks |
| CoreXY Y | 144,955 | 145,040 | 689 ticks | 694 ticks |
| Z | 960 | 960 | 1,323 ticks | 1,346 ticks |
| Extruder | 34,673 | 34,709 | 7,119 ticks | 6,761 ticks |

The small edge-count differences are expected from the bounded quintic fit;
the decisive properties are continuous direction-consistent edge timing,
endpoint fidelity for every intention, and the absence of catch-up bursts.
Nearest same-direction edge-time p95 differences were 299 us (X), 296 us
(Y), 58 us (Z), and 319 us (E). This test is now the print-scale regression
between synthetic unit vectors and a supervised physical print.

A later 100% retry exposed two cases not reached by the 25% replay. First, a
diagonal cube stroke made one CoreXY motor stationary; its -0.08-sub-unit
rounded residual quantized to a valid all-zero polynomial, but the host
direction validator assigned that hold an arbitrary positive direction and
rejected it. Second, after that host fix, exact replay found isolated one-tick
intervals at cold X, Y, and E direction reversals. Clearing the stale interval
was correct, but Newton's near-zero-velocity initial estimate was not; the old
final timer-order clamp converted failure into `t_prev + 1`.

All-zero polynomials now bypass the inapplicable direction invariant. A cold
or spatially invalid higher-order solve now invokes the bounded sign bracket
before the timer-order guard. The exact CoreXY cancellation and captured X,
Y, and E reversal segments are committed regressions. The corrected 100%
two-layer result is:

| Actuator | V1 edges | HELIX edges | V1 minimum interval | HELIX minimum interval | HELIX intervals <=64 ticks |
|---|---:|---:|---:|---:|---:|
| CoreXY X | 317,247 | 317,607 | 265 ticks | 260 ticks | 0 |
| CoreXY Y | 321,270 | 323,300 | 265 ticks | 256 ticks | 0 |
| Z | 1,280 | 1,280 | 1,334 ticks | 1,353 ticks | 0 |
| Extruder | 63,758 | 63,842 | 4,821 ticks | 4,755 ticks | 0 |

This workstation result and the Pico, EBB36, and Linux target builds pass. It
does not replace the required on-silicon self-test and supervised print after
flashing the corrected firmware.

#### Long-print fractional-Horner regression (2026-07-20)

A later PLA print failed on the EBB36 with `traj solver divergence` at local
clock 1,194,254,476. Transport was healthy: the CAN bus remained active, the
bridge had no dropped frames, and the EBB36 reported no TX or protocol errors.
The execution log and serial queue identify the active extrusion segment
exactly. It began at local clock 1,190,772,069 and carried:

```text
duration=3584000 velocity=40160 accel=-1643
jerk=274 snap=-25 crackle=1
```

The fitted real-valued polynomial has strictly positive velocity over the
whole segment. The compact deadline evaluator, however, previously stored
each nested Horner stage as an integer. Discarding all 16 fractional bits at
each stage let the small `crackle=1` correction change discontinuously at
`t/65536` boundaries; the remaining stages amplified that one-unit rounding
step into a false late reversal. The crossing bracket correctly rejected the
non-monotonic representation, but the representation—not the intended
trajectory—was wrong.

`traj_poly_fast_setup()` now selects the largest safe per-segment fractional
scale for its int32 Horner state. The recurring timer path retains the same
four nested stage multiplies and one final multiply; it only applies the
preselected final shift. The segment-load range proof is computed once and
remains conservative over the complete duration. A multi-step representation
discrepancy still reaches the existing fail-closed divergence guard.

The captured segment is a permanent production-solver regression in
`test/trajectory_v1_pulse_compare.py`. Before the change it shuts down after
28 pulses. With the correction it emits 29 ordered crossings, ends on the
expected physical step, and has no catch-up burst. An independent rational
evaluation of the wire derivative ladder—not either staged MCU Horner
implementation—puts the worst selected crossing only 0.0934 microstep from
its ideal half-step boundary, inside the one-eighth-step solver target.
Multi-edge endpoint discrepancies continue to fail closed.

The exact 29-clock sequence is also part of the built-in `traj_kernel` test,
so the MCU cannot use its own evaluator as the regression oracle. The final
STM32G0B1 image (`8d2a8904-dirty-20260720_222227-linuxathena`, flash SHA
`99EB20ACD702017C0995B96BA828ABB62B707541`) passed that test on the 64 MHz
EBB36 over `helixcan0`; all five live tests passed and link RTT was 0.96 ms.
The full V1 differential, higher-order chaining, segment-library, extruder,
G-code replay, timesync, and STM32G0B1 firmware-build suites also pass. The
shared source additionally builds for RP2040 and STM32H723, and the Linux
firmware executes the complete live self-test protocol successfully. A
supervised print crossing the formerly failing region remains the final
physical confirmation of this specific correction.

#### Endpoint-only chained-rounding regression (2026-07-20)

The next supervised print reached a different EBB36 `traj solver divergence`
at local clock 1,013,865,718. CAN remained healthy: zero RX/TX errors, retries,
FIFO overruns, protocol errors, or bridge drops. Starting from the retained
local rebase at clock 994,401,998 (`pos=2086839360`, `mcu_pos=425059`), a
workstation replay reproduced all 13 completed extrusion segments and every
recorded accumulator endpoint before entering the failed segment with:

```text
start_clock=1012704698 duration=1536000
velocity=-609627 accel=52985 jerk=-365 snap=-542 crackle=50
acc=9021400908531587309 mpos=425266 prior_interval=7206 direction=-1
```

The curve has strictly negative velocity throughout and is therefore valid.
The solver emitted 66 ordered pulses and then diverged while looking for the
last boundary. This was not another false Horner reversal. The exact chained
endpoint convention reaches that final negative half-step by 0.0071 step,
while an independent rational evaluation of the continuous wire polynomial
stops 0.0248 step before it. The two legitimate fixed-point evaluation
conventions therefore straddled one physical boundary: endpoint admission
required the edge, but no interior sign crossing existed for the deadline
bracket to find.

The bracket now reconciles this case only when all of the following are true:

- its bounded search reached the segment endpoint without a sign crossing;
- the authoritative chained endpoint reaches the current target;
- it does not reach the following target;
- the represented endpoint remains inside the existing half-step ambiguity
  limit; and
- the endpoint is strictly later than the preceding pulse.

It then emits the sole edge at `t=duration`. The next solve sees the exhausted
segment and cannot emit a same-clock catch-up edge. A direct negative
regression proves that a two-step endpoint discrepancy still shuts down.

The complete captured state is now permanent in
`test/trajectory_v1_pulse_compare.py`: it emits 67 ordered pulses, the final
one at tick 1,536,000, ends at physical `mpos=425199`, and stays within 0.1119
step of the independent rational polynomial. The STM32G0B1 image
(`f15f06a3-dirty-20260720_225213-linuxathena`, flash SHA
`D5FC73A24FAE31BA74E1A5AB39BC951B773BA85C`) passed all five live tests on the
64 MHz EBB36, including the on-silicon endpoint branch; link RTT was 1.064 ms.
A supervised print remains the final physical confirmation.

### Experiment 1c: disconnected extrusion-island rebase failure

A subsequent supervised cube reached stable full-speed XY execution but then
failed on the EBB36 with `Rebase overlaps active trajectory`. The extrusion
fitter emits `TSEG_LOCAL_TIME`: its quintic durations and coefficients are
already quantized at 64 MHz. The old secondary rebase still carried only a
12 MHz machine-time clock, which firmware converted through the *current*
discipline map. Between two close pressure-advance islands, that map had
changed after the first local-time stream was queued. Re-evaluating the second
absolute boundary placed it approximately 6.39 million EBB ticks (about
99.8 ms) inside the first island's queued local horizon, so the firmware
correctly rejected the overlap.

An attempted workaround retained one relative-E accumulator across all
activity islands. Offline endpoint totals stayed close to V1, and it avoided
the overlap, but the physical printer emitted catastrophically too much
plastic on the same G-code that had previously extruded correctly. That
experiment invalidates print-long E retention: each disconnected pressure-
advance/retraction island must recover its own trapq-derived physical anchor.

The corrected protocol keeps those per-island rebases and adds
`trajectory_rebase_local`. A secondary rebase now carries both the shared
machine clock and the exact local execution clock used by its already-local
segments. Firmware gates the command on Class-0 convergence but schedules the
immutable local barrier, so later mapping corrections cannot move it into
queued work. The host also rejects a boundary before its exact recorded local
horizon before transmission. Unit regressions prove two close E windows
produce two anchors and select the local command. The complete 99-layer cube
replay with regenerated Pico/EBB36 dictionaries produced 422 E rebases,
31,904 quintics, 423 holds, and 1,135,901 HELIX pulses versus 1,134,514 V1
pulses (+0.122%), with a 3,909-tick minimum interval and no interval at or
below 64 ticks. Both target builds pass. Exact `75f03262` images were signed,
archived, and flashed to the Pico and EBB36; both identified ABI
`27141a58f61f9fbc`, all five onboard self-tests passed on each, and the EBB36
machine-time discipline reconverged within 2.5 us. A supervised hot print
then exposed the remaining hold-domain defect described below; `75f03262` is
therefore not a physical extrusion pass.

### Experiment 1d: local segment stream with a machine-time terminal hold

The first supervised print on `75f03262` again stopped with `Rebase overlaps
active trajectory`. The capture made the mismatch deterministic. An E island
started at EBB local clock 790,447,180 and its fitted local-time segments plus
the requested terminal hold totalled 45,471,688 local ticks, giving a host
horizon of 835,918,868. The next rebase was correctly scheduled later, at
836,122,930. However, the 64,000-tick terminal hold used the legacy
`traj_hold` command. That command has machine-time semantics, so firmware
converted 64,000 Pico ticks into approximately 341,000 EBB ticks. The actual
firmware horizon consequently extended beyond the next rebase even though
the host's exact-local horizon check passed.

The protocol now retains `traj_hold` for machine-time streams and adds
`traj_hold_local` for a `TSEG_LOCAL_TIME` stream. Both fitted zero spans and
terminal holds select the local command. Secondary trajectory setup refuses
firmware that lacks either the local rebase or local hold ABI. The unequal
12 MHz/64 MHz regression asserts that a shortened 8,730-tick EBB hold stays
8,730 EBB ticks on the wire instead of being interpreted as primary ticks.
The sliced-G-code replay now models both hold domains and applies the same
wrap-safe rebase/horizon comparison as firmware, so the rejected legacy stream
fails offline while the corrected stream passes. A full 99-layer replay emits
422 E rebases, 31,904 E quintics, and 423 local E holds; it expands 1,135,901
E pulses with a 3,909-tick minimum interval and no interval at or below 64
ticks. Exact Pico and EBB36 target builds pass. The resulting board evidence
is recorded below.

Exact clean-commit `8ca65c37` images were signed, archived, and flashed to the
Pico and EBB36. Klippy identified 204 and 210 commands respectively, both at
version `8ca65c37` and ABI `27141a58f61f9fbc`. All five onboard tests passed
on both MCUs (`crc_wire`, `timer_monotonic`, `timer_rate`, `ram_pattern`, and
`traj_kernel`), the Pico reported its 200 MHz core clock, and EBB36 Class-0
discipline reconverged. The printer returned to `ready`. A new supervised
physical print remains the final acceptance gate; no heater target was issued
as part of this qualification.

### Experiment 1e: late pressure-advance activity boundary

A later supervised run on firmware `5f652c6e` stopped during ordinary
mid-print extrusion with `MCU 'ebb36' shutdown: Timer too close`. This was
not a pause, an underrun, a lost time lock, or solver exhaustion. The EBB36
flight recorder showed a completed local hold at clock 1,432,722,549, then a
new `trajectory_rebase_local` for clock 1,432,803,549. The command was
processed at clock 1,432,823,480: its requested start was already 19,931
EBB36 ticks, or 311.4 us, in the past. Timesync remained converged, both USB
links retained zero invalid bytes, and disabled trace retained zero records.

The host activity scanner expanded an extrusion move by its pressure-advance
pre-active window. When that move became visible after generation had already
advanced into the window, `segfit_check_activity()` returned the historical
unclipped start. Stock `itersolve_generate_steps()` explicitly clips this
lookback to `last_flush_time`; the HELIX fitter now applies the same rule and
returns no activity boundary before its supplied generation cursor. The new
extruder regression starts inside a 40 ms pressure-advance window and proves
that the anchor is exactly the forward cursor, not the stale pre-active
start. The focused host suite and a 100% two-layer replay of the same cube
pass; the replay emits 63,846 E pulses, has a 4,896-tick minimum interval,
and contains no interval at or below 64 ticks. The physical benchmark remains
open until this host correction completes a new supervised print.

### Experiment 1f: rebase transmission release deadline

The next supervised repeat passed the historical-lookback case but stopped at
a later disconnected pressure-advance island. The EBB36 completed its prior
hold at local clock 3,214,788,125. The host emitted a valid later rebase for
3,214,869,210, but firmware processed it at 3,214,903,493: 34,283 ticks, or
535.7 us, too late. The activity boundary was forward-only and timesync was
still valid, so this was not a recurrence of Experiment 1e.

The host had used the prior local execution horizon as the rebase command's
`minclock`. In Klipper serialqueue, `minclock` is a transmission-release gate:
the message stays in the upcoming queue until the MCU acknowledgement clock
passes that value. It is not merely an assertion that two commands execute in
order. With only about 1.3 ms between the terminal hold and the next E island,
that gate consumed the rebase's delivery lead and made `Timer too close`
inevitable under normal USB scheduling jitter.

Every trajectory command for one joint already uses the same serial command
queue, preserving wire order. The host also rejects a rebase clock before its
recorded machine/local horizon, and firmware independently rejects a barrier
before its active queue horizon. The correction therefore keeps all three
ordering/overlap protections but sends rebases with `minclock=0`, allowing
serialqueue to release them at its normal requested-clock lead time. Regression
coverage proves both primary and mixed-clock rebases retain their requested
clock and horizon validation without the late transmission barrier. The next
physical repeat remains the acceptance gate.

### Experiment 1g: late-visible island inside a committed hold

The following physical repeat reached 48.7% of the same cube and commanded
570.5 mm of filament without another MCU timer deadline, validating Experiment
1f. The host then stopped before transmission because a new extruder rebase at
local clock 35,867,360,686 preceded its recorded immutable horizon
35,867,364,943 by 4,257 EBB ticks (66.5 us). Both USB links were clean and the
EBB remained responsive; `Exception in flush_handler` was the host's wrapper
around this explicit overlap rejection.

The preceding flush had already transmitted the normal 1 ms terminal hold
before the later pressure-advance island became visible. That command cannot
be recalled. For an overlap bounded by the terminal-hold duration, the only
coherent timeline is to retain the hold and begin the newly discovered island
at its exact end. The host now converts the committed machine and local
horizons back to print time, advances by any rounding tick still required,
samples the trapq-derived E position at that adjusted instant, and uses the
same instant for the rebase and fitter anchor. An overlap greater than 1 ms is
still rejected as a genuine planner error rather than silently dropping
motion.

The physical 4,257-tick vector is a permanent mixed-clock regression. A
55-layer, 100% sliced-G-code replay through and beyond the failed region produces
194 E rebases, 195 local holds, and 568,122 E pulses. Its minimum E interval is
4,721 ticks and no interval is at or below 64 ticks.

Two following supervised ABS cubes completed at full requested speed. They
commanded 1,293.6 mm and 1,302.9 mm of filament over 778.7 s and 669.0 s of
print time, respectively, with operator-confirmed coherent output. Both MCU
links retained zero invalid bytes and no new retransmits; neither run recorded
a timer fault, rebase rejection, flush-handler exception, toolhead stall, or
MCU shutdown. Each run also exercised this exact class of correction: its
late-visible E island was advanced 30.4 us and 31.0 us, respectively, to the
committed hold horizon before continuing to a clean completion. The repeated
physical result closes the STM32G0B1 sliced-print acceptance gate.

## Experiment 2: on-silicon deadline scaling

`traj_stepper_test_quintic_deadline()` runs inside `HELIX_SELF_TEST`, so it
measures the production firmware on the target rather than a workstation
simulation. It contains three gates:

- a zero-velocity acceleration vector verifies the two cold crossings before
  a recurring interval exists; and
- a captured EBB36 hot-extrusion quintic is time-compressed while its nth
  derivative is multiplied by the nth power of the rate scale. Geometry and
  pulse count remain the same while the available time between edges shrinks;
  and
- the sharp real pressure-advance retract that exposed the print defect must
  execute exactly 133 crossings, remain inside 1/8 step, and keep each exact
  fallback below 75% of its physical interval.

| Geometric scale | Approximate edge rate | Spatial gate | Deadline gate | Result |
|---:|---:|---|---|---|
| 1x | 1.25k steps/s | <= 1/8 step | solve < 75% of interval | Pass |
| 2x | 2.5k steps/s | <= 1/8 step | solve < 75% of interval | Pass |
| 4x | 5k steps/s | <= 1/8 step | solve < 75% of interval | Pass |
| 8x | 10k steps/s | <= 1/8 step | solve < 75% of interval | Pass |
| 16x | about 20k steps/s | <= 1/8 step | solve < 75% of interval | **Pass** |
| 32x probe | about 40k steps/s | crossing remained bounded | 20.4 us > 18.75 us solve deadline | **Rejected** |

The committed automatic pass gate remains 16x. The computation-only
`run_captured_quintic_probe` diagnostic makes both 16x and 32x directly
reproducible on the target; 32x is deliberately reported as `DEADLINE`, not
folded into the automatic self-test failure. Negative evidence defines the
safe engineering boundary and must not be silently converted into a pass by
weakening the reserve.

After the final firmware was flashed, both connected boards passed all five
live self-tests:

| Board | Motion clock | Firmware build | Link RTT | `traj_kernel` |
|---|---:|---|---:|---|
| EBB36 v1.2 / STM32G0B1 | 64 MHz | `a71fad74-dirty-20260714_145655-linuxathena` | 0.26 ms | Pass |
| BTT SKR Pico / RP2040 | 12 MHz scheduler, 200 MHz core | `a71fad74-dirty-20260714_145907-linuxathena` | 0.18 ms | Pass |

The `-dirty` suffix records that this qualification preceded the checkpoint
commit. The source content is the content committed with this document.
Those results qualify the first two gates above. The sharp-retract gate was
added after the failed benchmark print and passes the Linuxprocess live
self-test; a newly flashed STM32G0B1 run remains required before it is marked
on-silicon pass.

## Experiment 3: hot ABS extrusion through the full path

The physical test used the V0 at X=60, Y=60, Z=100, ABS at 260 C, and the bed
off. The extruder was never retracted more than 2 mm.

| Commanded operation | Result |
|---|---|
| +10 mm at 2 mm/s | Completed; reported E=10; printer remained ready |
| +5 mm at 10 mm/s | Completed; reported E=15; printer remained ready |
| -2 mm / +2 mm at 5 mm/s | Completed; returned to E=15; no heatbreak-risking retract |

The 10 mm/s run is about 7.1k physical steps/s with the active BMG gearing. It
proves the real EBB36 step/dir output, TMC driver, hotend, filament load, host
planner, quintic fitter, mixed-clock transport, and onboard solver together.
It is intentionally below the synthetic 20k gate because melt capacity, not
the crossing solver, should limit a hot extrusion test.

## Experiment 4: intention-to-execution reconciliation

The +5 mm / 10 mm/s hot path was isolated to telemetry lines 77268 through
77297 and
audited in the EBB36's local 64 MHz execution-clock domain:

```text
extruder oid=5 segments=8 holds=1 pulses=3529 min_interval_ticks=8762
extruder oid=5 executed_pulses=3529 min_executed_interval_ticks=8762
execution_records=9 matched_boundaries=8 triggers=0 errors=0
```

The intended and executed replay therefore agree on all 3,529 physical edges
and on the 8,762-tick minimum interval (136.9 us, about 7.3k steps/s). No
underrun, endpoint mismatch, clock discontinuity, accumulator discontinuity,
trigger mismatch, or execution fault was present.

This experiment also found an observability bug: persisted intention duration
and clocks were in the Pico's 12 MHz machine-time domain, while the quintic
coefficients and MCU execution log were in the EBB36's 64 MHz local domain.
The motion itself was correct, but the first audit replay used the wrong
duration and could not match execution clocks. Telemetry now records both
machine and execution clock fields, and the auditor can infer the local clock
anchor from a rebase for older captures. A bounded `--before-line` option
prevents a complete path from being confused with older records that have
rolled out of the MCU's finite flight-recorder ring.

## Experiment 5: STM32H723 compute-headroom comparison

An FK723M1-ZGT6 development board (STM32H723ZGT6) was flashed directly through
the STM32 ROM DFU interface with the computation-only configuration in
`test/helix-configs/stm32h723-fk723m1.config`. The board's 15 MHz HSE is not a
selectable Klipper H7 reference, so this qualification deliberately uses the
supported internal HSI path and Klipper's conservative 520 MHz H723 clock. It
does not overclock the part and does not depend on an ST-Link.

The board served its dictionary over USB and passed all five built-in tests:
CRC wire vector, monotonic timer, timer-rate fingerprint, RAM pattern, and the
trajectory kernel. The `traj_kernel` result remained the expected value 4.

For capacity measurement, `run_traj_benchmark` creates one to eight independent
solver states without allocating an oid or configuring GPIO. Each state runs
an accelerating segment with a roughly 48-edge duration through the production
quintic execution path.
At the practical H7 rates used below velocity, acceleration, jerk, snap, and
crackle are all non-zero. Two cold edges are warmed exactly as the live backend
does; 32 recurring crossings are then timed. A result passes only when:

- every reconstructed crossing stays within 1/8 physical step;
- the combined solve time for all virtual axes remains below 75% of the
  shortest following pulse interval; and
- all states reach the expected next boundary monotonically.

![H723 aggregate quintic solver capacity](img/helix-h723-capacity.svg)

| Virtual axes | Per-axis rate | Aggregate crossings | Worst solve | Shortest interval | Reserve | Result |
|---:|---:|---:|---:|---:|---:|---|
| 1 | 640k/s | 640k/s | 550 ticks / 1.06 us | 767 ticks / 1.48 us | 28.3% | **Pass** |
| 2 | 320k/s | 640k/s | 993 ticks / 1.91 us | 1,533 ticks / 2.95 us | 35.2% | **Pass** |
| 4 | 160k/s | 640k/s | 1,881 ticks / 3.62 us | 3,067 ticks / 5.90 us | 38.7% | **Pass** |
| 8 | 80k/s | 640k/s | 3,657 ticks / 7.03 us | 6,135 ticks / 11.80 us | 40.4% | **Pass** |
| 1 | 1M/s | 1M/s | 544 ticks / 1.05 us | 519 ticks / 1.00 us | negative | Rejected |
| 2 | 640k/s | 1.28M/s | 988 ticks / 1.90 us | 811 ticks / 1.56 us | negative | Rejected |
| 4 | 320k/s | 1.28M/s | 1,876 ticks / 3.61 us | 1,621 ticks / 3.12 us | negative | Rejected |
| 8 | 160k/s | 1.28M/s | 3,652 ticks / 7.02 us | 3,241 ticks / 6.23 us | negative | Rejected |

The qualified statement is therefore **at least 640,000 aggregate recurring
curved crossings/s with the 25% reserve intact**, not an interpolated maximum.
The worst passing spatial error was only 0.0113 of the allowed 1/8-step error.
The near-constant aggregate pass point is strong evidence that crossing solve
cost, rather than an axis-specific queue artifact, dominates this synthetic
load.

This result supports an H7-first board design. It supplies roughly an order of
magnitude more qualified aggregate curve-synthesis capacity than the present
G0B1 toolhead requirement while retaining the simple MCU toolchain, interrupt
model, and firmware architecture already in use. An FPGA can still be valuable
for exceptionally high channel counts, deterministic waveform fabrics, or
specialized encoders, but the evidence does not justify making one mandatory
for the next HELIX controller.

### Admission-control implication

Firmware should cap configured trajectory objects for memory safety, but a
fixed “maximum axes” number is not the right compute-safety rule. Solver demand
scales primarily with the aggregate physical crossing rate and polynomial
path: eight 80k-step/s axes and one 640k-step/s axis consumed approximately the
same H723 budget in this experiment.

A production fleet profile should therefore advertise a qualified aggregate
curved-crossing budget. At configuration time the host can conservatively sum
each actuator's worst-case `max_velocity / step_distance`, apply a cost factor
for its enabled polynomial backend, and reject a group whose simultaneous
demand exceeds the board's budget after reserve. A separate hard oid count can
remain as a RAM/queue bound. Runtime missed-deadline and queue telemetry remain
the final fail-safe; static admission is intended to prevent reaching them,
not replace them.

## Why this is preferable to the firehose for this board

The V1 firehose is cheaper per edge on the MCU. HELIX is better only if the
additional work buys capabilities and remains inside a proven budget. On the
EBB36 it now does:

| Property | V1 firehose | HELIX intention execution |
|---|---|---|
| Link traffic scales with | compressed physical edges | polynomial segments |
| Nonlinear crossing solve lives on | host | MCU |
| Board knows polynomial position | no | yes, Q32.32 accumulator |
| Stop source can act locally | only against queued pulse playback | yes, against the active intention |
| Link-loss response | exhaust queue, then shutdown | bounded hold/recovery policy |
| Execution evidence | host knows what it sent | MCU flight log records what ran |
| Non-stepper backend | separate host semantics | same intention queue can drive PWM/DAC/FOC |
| Tested EBB practical envelope | established Klipper behavior | about 20k steps/s with reserve |
| Raw synthetic ceiling | expected higher | 40k reserve gate not passed |

The architectural advantage is therefore not “more steps because the MCU does
math.” It is “enough steps for the physical actuator, with local authority and
less real-time dependence on the link.” If a future EBB configuration truly
requires more than the qualified range, the correct response is to optimize
and re-qualify the solver, reduce unnecessary microstepping, or select a more
capable toolhead MCU—not to hide the failed reserve test.

## Reproduction

Host fidelity and pulse comparison:

```shell
~/klippy-env/bin/python test/trajectory_v1_pulse_compare.py
~/klippy-env/bin/python test/segfit_fidelity_test.py
~/klippy-env/bin/python test/extruder_trajectory_test.py
```

Real sliced-G-code file-output comparison (no printer is opened or moved):

```shell
~/klippy-env/bin/python scripts/helix_gcode_pulse_compare.py model.gcode \
  --config ~/printer_data/config/printer.cfg \
  --main-dict path/to/main/klipper.dict \
  --mcu-dict ebb36=path/to/ebb36/klipper.dict \
  --layers 2 --speed-percent 25
```

Focused audit of the captured hot path:

```shell
~/klippy-env/bin/python scripts/helix_motion_audit.py \
  ~/printer_data/logs/atlas-telemetry.jsonl \
  --session dff352c4ec78476ab3edb7a577ff1fa8 \
  --actuator extruder --after-line 77267 --before-line 77298
```

Live on-board gate after flashing a `WANT_SELF_TEST` build:

```text
HELIX_SELF_TEST MCU=ebb36
```

Exercise the partitioned FDCAN receive path without moving or heating:

```text
HELIX_CAN_RX_STRESS MCU=ebb36 ITERATIONS=500 HOLD_US=2000
HELIX_CAN_STATUS BUS=helixcan0
```

The final 2026-07-21 EBB36 image
`5383b0a9-dirty-20260721_010324-linuxathena` (flash verification SHA
`14281554836FC343FE956CA45E3D9D00BB0CD6F7`) completed 500 full-credit bursts
in 2.027 seconds. Both FIFO high-water marks reached two while per-FIFO
overrun, protocol-error, retransmit, invalid-byte, and SocketCAN drop counters
remained zero. The maximum start-of-frame-to-service interval was 152,153
ticks (2.377 ms including frame wire time). The firmware caps `HOLD_US` at
2000; a characterization run at 5 ms retained receive isolation but crossed
Klipper's late-timer safety boundary and is not an accepted field-test
setting.

Computation-only rate sweep on a self-test firmware image:

```shell
~/klippy-env/bin/python scripts/helix_traj_benchmark.py \
  --device /dev/serial/by-id/usb-Klipper_stm32h723xx_...-if00 \
  --rates 20000,40000,80000,160000,320000,640000,1000000 \
  --axes 1,2,4,8
```

`max_error_eighths` is expressed as a fraction of the allowed 1/8-step error;
values below 1 satisfy the spatial gate. A non-zero script exit is expected
when a sweep intentionally includes rejected capacity probes.

Reproduce the captured EBB curve's committed 16x pass and 32x rejection:

```shell
~/klippy-env/bin/python scripts/helix_traj_benchmark.py \
  --device /dev/serial/by-id/usb-Klipper_stm32g0b1xx_...-if00 \
  --captured-scales 16,32
```

The diagnostic reports the measured maximum solve ticks, shortest crossing
interval, spatial error, and reserve for each scale. Its exit status is
non-zero when the requested set intentionally includes the rejected 32x case.

## Remaining qualification

- Extend the two successful sliced-print runs into a purpose-built high-load
  print and a long soak. The completed cubes qualify repeatable coordinated
  XY, Z, and extrusion at the active V0 limits; they do not yet establish the
  high-speed/deep-queue or 24-hour gates.
- Repeat the V0's successful CAN toolhead qualification on the V2.4 wiring and
  workload. V0 CAN success proves the protocol and present physical link, not
  the V2.4 installation.
- Measure GPIO edges with a logic analyzer if an external timing reference is
  required; the present proof uses V1 comparison, fixed-point replay, and the
  MCU's own timer/flight log.
- Do not claim 40k steps/s until the 25% reserve gate passes on silicon.

These limitations are compatible with the conclusion: the STM32G0B1 is not
too small for HELIX's EBB36 role. It is being used much more thoroughly than a
V1 playback MCU, and the qualification demonstrates that the additional work
fits the practical extrusion envelope with deterministic margin.
