# Machine-Time Qualification: Timing Error in Print-Domain Units

This note records what cross-MCU clock skew means for a printer. It separates
three different claims that must not be conflated:

1. **Scheduled-traffic correctness:** commands execute against a shared time
   model closely enough for the actuator relationship and print process.
2. **Statistical timing qualification:** measured mean, deviation, tails, and
   physical print results characterize a particular transport and topology.
3. **Hardware-bounded timing assurance:** a capture or hardware timestamp
   provides a defensible worst-case phase bound.

FD-0001 already uses Class 0/1/2 for Scheduled, Prompt, and Telemetry traffic.
Those names are not synchronization-quality grades. A Class-0 scheduled
command can be operationally qualified over USB without claiming that USB
provides a hard 10 us physical bound.

For the complete experimental comparison, explanatory figures, print-domain
analysis, and reproducibility instructions, see
[Shared Machine Time Across Independent Printer MCUs](Machine_Time_White_Paper.md).

## Measured Pico/EBB36 USB results

The rig schedules Pico GPIO24 and EBB36 PB8 from the same requested time and
captures both with a 24 MHz logic analyzer. Firmware independently records
the scheduled and actual GPIO-write clocks, allowing MCU ISR latency to be
removed from the clock-mapping term. Across the campaign, the Pico-minus-
EBB36 ISR differential was approximately -1.77 us with only 0.023 us standard
deviation. The material variation is in the independent USB clock mappings,
not GPIO dispatch.

| Dataset | Edges | Mean (us) | Std. dev. (us) | Physical range (us) |
| --- | ---: | ---: | ---: | ---: |
| Retained robust relay, SCHED_RR, four sessions | 90 | +1.12 | 2.75 | -5.17 to +10.88 |
| Robust steady-state window | 40 | -6.71 | 1.24 | -9.46 to -4.75 |
| Original Klipper per-MCU `print_time` comparator | 30 | +8.36 | 7.60 | +1.50 to +26.67 |
| SCHED_OTHER adverse startup | 12 | -16.17 | 10.55 | -47.50 to -9.54 |
| Rejected phase-continuous acquisition experiment | 20 | +58.11 | 0.61 | +57.08 to +58.79 |

The original-Klipper comparator's exact 30 edge deltas and capture metadata
are retained in
[`klipper_legacy_r1_summary.json`](evidence/machine_time/klipper_legacy_r1_summary.json).

The analyzer campaign was later repeated on Ubuntu
`6.8.1-1015-realtime`. `PREEMPT_RT` changed individual run centers and
spreads but did not remove session-dependent acquisition: the two Helix runs
were +3.19 ±3.74 us and -10.95 ±6.54 us (population standard deviation),
while the original-Klipper comparator was -3.75 ±3.12 us. Realtime host
scheduling can improve consistency, but it cannot recover unknown one-way USB
delay.

## Direct sync-line result

Pico GPIO24 was then connected directly to EBB36 PB8. The Pico was the only
output; PB8 passively latched each rising edge at GPIO-ISR entry. Across two
30-edge runs, an affine fit of secondary ticks against primary ticks produced
only 0.0064 us, 0.0053 us, and 0.0041 us RMS residual. The simultaneous
software-derived USB-map prediction errors had 1.0805 us, 0.3710 us, and
0.6072 us standard deviation.

The fit also measured the stable physical oscillator ratio at -25.19 ppm from
nominal in both runs. Removing that real rate offset leaves nanosecond-scale
edge-pairing residuals. This directly shows that the boards' clocks and ISR
timestamps are highly repeatable and that the remaining microsecond variation
is chiefly USB phase observation and feedback. See the
[raw samples](evidence/machine_time/sync_line_edges.csv) and
[metadata](evidence/machine_time/sync_line_summary.json).

The phase-continuous experiment is not the shipped policy. It is retained in
this table because it demonstrates an important distinction: an offset can be
very repeatable yet have a biased acquisition. Conversely, a conservative
round-trip interval can be much wider than the error that matters to an
ordinary print. Neither observation alone should disqualify a working
transport.

## Spatial interpretation

For a mostly constant inter-board time offset `dt`, the first-order path-phase
shift is:

```
dx = velocity * dt
```

Using the pooled retained SCHED_RR result:

| Toolhead speed | Mean shift (1.12 us) | 1-sigma variation (2.75 us) | Worst observed shift (10.88 us) |
| ---: | ---: | ---: | ---: |
| 100 mm/s | 0.00011 mm | 0.00028 mm | 0.00109 mm |
| 300 mm/s | 0.00034 mm | 0.00083 mm | 0.00326 mm |
| 500 mm/s | 0.00056 mm | 0.00138 mm | 0.00544 mm |

For comparison, the rejected but stable 58.79 us offset corresponds to
0.00588, 0.01764, and 0.02940 mm at those speeds. These are phase shifts, not
cumulative position errors: all commanded motion and extrusion still execute.

Acceleration adds approximately `0.5 * acceleration * dt^2`. Even at
20,000 mm/s^2 and 58.79 us, that term is only about 0.000035 mm. The velocity
term dominates.

## Extrusion interpretation

When the secondary is an EBB36 controlling only the extruder, a constant skew
shifts flow slightly along the path; it does not change total extrusion. An
upper bound on the volume temporally displaced across an instantaneous flow
transition is:

```
dV = volumetric_flow * dt
```

| Flow | Mean displaced volume (1.12 us) | 1-sigma (2.75 us) | Worst observed (10.88 us) |
| ---: | ---: | ---: | ---: |
| 20 mm^3/s | 0.000022 mm^3 | 0.000055 mm^3 | 0.000218 mm^3 |
| 40 mm^3/s | 0.000045 mm^3 | 0.000110 mm^3 | 0.000435 mm^3 |

At 1.75 mm filament diameter, the 40 mm^3/s worst-observed value is about
0.00018 mm of filament length. Pressure advance, extrusion smoothing, nozzle
pressure, and the non-instantaneous nature of real flow transitions further
shape the visible result. Physical successful prints therefore remain an
essential acceptance signal beside clock measurements.

## Topology matters

The same skew has different consequences depending on which actuators are
distributed:

- **Primary XY plus secondary extruder:** chiefly an extrusion/path phase
  shift; total motion and extrusion remain correct.
- **Coupled kinematic motors split across MCUs:** phase error can become a
  direct vector/geometry error and deserves a tighter application limit.
- **Synchronized sensors, cameras, or metrology triggers:** the application
  may require a hardware-bound timestamp even when printing is unaffected.
- **Fans, displays, and ordinary prompt outputs:** microsecond placement is
  generally irrelevant.

## What the round-trip interval means

For a request/response exchange, an MCU timestamp is bracketed by host send
and receive times. Without assuming equal forward and reverse delay, one
link's midpoint uncertainty is one half-RTT; the conservative relative
two-link half-width is the sum of both half-RTTs. On the measured USB setup
that envelope can be tens of microseconds and reached approximately 88 us in
one run.

That is an honest statement about what software timestamps alone can prove.
It is not a measured prediction that every edge is wrong by 88 us, nor an
automatic reason to reject a printer whose observed distribution and print
results meet its application needs. Helix exposes this interval as
diagnostic evidence; it does not use it as a new USB motion-disqualification
gate.

## Qualification policy

USB machine time is accepted as a **statistically qualified operational
profile** when repeated startup, steady-state, load, loss/recovery, and
temperature tests remain within application-derived print tolerances and
physical prints remain coherent. Reports must retain mean, standard
deviation, extrema, topology, scheduler policy, and sample count.

A shared sync wire into a secondary timer-capture/edge input, CAN receive
hardware timestamps, or another directly qualified timing source may add
**hardware-bounded assurance**. That is an optional stronger claim, not a
prerequisite for the already-demonstrated Pico-XY plus EBB36-extruder use
case.

## USB Start-of-Frame discipline

The follow-up experiment matched the same 11-bit USB frame number across the
RP2040 and STM32 USB FS device controllers, then calibrated those clock pairs
against the direct wire. Fifty matched frames had a stable -0.4622 us phase
with 0.0242 us population standard deviation. When those exact pairs drove
the discipline loop, a steady 50-edge run measured +0.5769 us mapping phase
with only 0.0153 us standard deviation and a +0.5469 to +0.6094 us range.

This is a roughly 24 to 70 fold reduction in variation from the earlier
0.3710 to 1.0805 us software-map runs. The fixed sub-microsecond phase is
measured, not assumed. `[timesync] usb_sof: True` enables this mode in
production: SOF interrupts run for only 10 ms per approximately 1 Hz beacon,
and an unmatched frame falls back to the existing host estimate. The direct
wire is not required after commissioning. A wire-free Klipper service restart
with both commissioning sections removed reacquired the 8/8 exact-pair host
gate and a converged firmware map. See the
[raw SOF samples](evidence/machine_time/usb_sof_edges.csv) and
[summary](evidence/machine_time/usb_sof_summary.json).

### Loaded-print SOF behavior

The idle result did not carry unchanged into the timer-saturated print path.
On 2026-07-15 the 549.28-second
`xyz-10mm-calibration-cube_0.4n_0.2mm_PLA_V0_120_8m.gcode` run completed with
both host and firmware gates continuously converged, but 24 individual SOF
observations exceeded the configured +/-10 us phase budget and used
one-beacon holdover. The largest raw residual seen by the 2.25-second status
monitor was +171.86 us, and the largest observed run was two rejected samples;
a bounded observation returned before the three-sample sustained-divergence
gate. The monitor did not sample every 1 Hz observation, so this is an
observed maximum rather than a complete extrema claim.

Source inspection explains the difference from the idle nanosecond result.
On STM32G0, `TIMx_IRQHandler()` globally masks interrupts around
`timer_dispatch_many()`. The dispatcher runs due timer callbacks under that
mask and only re-enables interrupts while waiting for a future deadline.
`traj_stepper_event()` calls the quintic crossing solver from that timer path.
Consequently USB's numerically higher NVIC priority cannot preempt a running
motion callback: PRIMASK has disabled every interrupt. A batch of due timer
events may occupy the dispatcher's approximately 100 us repeat window plus
the final callback, which is consistent with the loaded-print observations.

#### Guard-attribution result

A 2026-07-16 loaded print used temporary firmware instrumentation to test that
explanation directly. The instrumentation sampled the STM32 USB `ISTR.SOF`
flag immediately before setting `PRIMASK`, sampled it again after masking, and
recorded any pending flag immediately before interrupts were restored. It also
recorded the guarded section's entry site and elapsed MCU ticks.

The print produced 156 requested-frame misses that all matched an exact
guarded discard with `PRIMASK=1`. Of those:

| Entry classification | Count | Interpretation |
| --- | ---: | --- |
| SOF clear before masking, pending before restore | 144 | Frame arrived while interrupts were masked |
| SOF changed across the entry samples | 4 | Frame arrived in the narrow mask-entry race |
| Idle-transition entry state unavailable | 8 | Pending at restore, but entry could not be classified |
| Already pending before the guarded section | 0 | No evidence that stale pre-entry SOFs caused the outliers |

The first two rows directly prove that 148 of 156 exact misses, or 94.87%,
became pending while the MCU was globally interrupt-masked. Source attribution
placed 132 events in the timer dispatcher, 15 in higher-order trajectory
segment ingestion, one in the timer maintenance task, and eight at the idle
transition.

| Guarded path | Events | Mean elapsed time | Maximum elapsed time |
| --- | ---: | ---: | ---: |
| Initial timer-dispatch entry | 34 | 99.32 us | 355.98 us |
| Re-entered timer dispatch | 98 | 24.51 us | 27.08 us |
| Higher-order trajectory ingestion | 15 | 96.10 us | 109.31 us |
| All exact requested-frame misses | 156 | 46.39 us | 355.98 us |

In the complete set, 113 guarded intervals were at least 10 us, 26 were at
least 50 us, 11 were at least 150 us, and nine were at least 250 us. The
previous monitor-visible +171.86 us print outlier is therefore inside the
directly observed masked-interval distribution.

This establishes the causal chain. USB SOF does not create the long interval.
Instead, an SOF edge occasionally occurs while `PRIMASK=1`; the USB interrupt
remains pending; and `USB_IRQHandler()` calls `timer_read_time()` only after
the timer or trajectory critical section restores interrupts. The stored value
is therefore ISR-service time rather than physical frame-edge time. Frames
that do not overlap a masked interval retain the measured approximately
0.0153 us steady-run standard deviation.

The full attribution build was deliberately removed after the experiment. It
expanded every guarded entry and exit and the print subsequently completed its
object before an end-of-print `Rescheduled timer in the past` shutdown. That
terminal failure is treated as an observer effect, not evidence that clearing
a pending frame is expensive. The elapsed duration was sampled before the
discard-ring write, so bookkeeping can perturb absolute timings but cannot
manufacture the pre-clear/post-pending classification or explain intervals as
large as 100 to 356 us. Production firmware retains only the lightweight exact
frame/`PRIMASK` discard accounting.

The extracted attribution summary is retained in
[usb_sof_irq_attribution_summary.json](evidence/machine_time/usb_sof_irq_attribution_summary.json).

Production therefore treats same-frame SOF values as load-perturbed
ISR-entry observations, not unconditionally exact edge captures. The host
compares their phase residual with the configured `converge_window`, holds
over the already-qualified oscillator map for isolated outliers, and revokes
Class-0 only on three consecutive misses. A one-interval ppm derivative is
diagnostic only: the failed predecessor used a 2 ppm gate, which rejected
three approximately -2.25 us observations even while firmware error remained
7 ticks (0.11 us). The completed print validates the phase-budget policy for
this workload; it does not turn ISR-entry SOF into a hardware timestamp.
