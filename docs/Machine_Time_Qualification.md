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

The remaining measurement gap is timing under representative printing and
USB/host load. Idle scope captures characterize the clock mechanism but are
not a substitute for a longer capture during realistic trajectory and
extrusion traffic.
