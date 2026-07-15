# FD-0001: Hardware Triggers

Status: Framework, STM32 wiring, and RP2040 IO_BANK0 wiring implemented in
HELIX 0.9. The RP2040 GPIO path and interrupt-versus-polling comparison are
live-validated on a V0; remaining ports, input-capture precision, and analog
sources remain.

Klipper made MCUs deliberately dumb, and nowhere is the cost clearer
than in how the firmware *senses*: endstops are polled by software
timers, analog thresholds are polled through scheduled ADC reads, and
every one of those polls competes for the same hard timer list as
step generation. This document turns sensing over to the peripherals
32-bit MCUs actually ship — external interrupts, analog comparators,
timer capture units — so that triggers are *events*, timestamped at capture
or ISR entry instead of samples that got lucky.

This is the second half of "give MCUs power where they shine": the
intention protocol ([02-Intention_Protocol.md](02-Intention_Protocol.md))
gives boards trajectory autonomy; this document gives them sensing
autonomy.

## What polling costs today

* `endstop.c` arms a software timer that samples the pin every
  `rest_ticks`, requiring `sample_count` consecutive hits
  ([src/endstop.c](../../../src/endstop.c)). Detection latency is the
  polling quantum plus debounce window; every poll is an entry in the
  same timer list that steps run from, so sampling pressure and step
  pressure fight each other precisely when both are highest (probing
  while moving).
* Analog sensing (load cells, filament sensors, thermistor limits) is
  scheduled ADC sampling — the same story with slower quanta.
* A trigger's *timestamp* is "some time within the last polling
  interval", which bounds probe repeatability no matter how good the
  probe is.

## Event-driven trigger sources

Three peripheral classes, all standard on the 32-bit floor this document
assumes ([00-Vision.md](00-Vision.md)):

### 1. GPIO edge interrupts (EXTI / pin-change)

A digital endstop or probe output arms an edge interrupt; the ISR latches the
event time and starts qualification before firing trsync. Detection latency
drops from a polling quantum (typically tens of µs to ms) to interrupt
latency, and idle cost drops to zero — no timer-list entries while nothing
happens. A port may deliberately add a short qualification window before the
coordinated stop.

Noise is handled by **qualify-after-event**, inverting today's logic:
instead of continuously sampling *hoping* to catch a level, the edge
IRQ *starts* a short confirmation (a few fast re-reads or a hardware
glitch filter where the family provides one). A false edge costs one
brief confirmation; it never fires trsync unconfirmed. This preserves
today's noise robustness without today's standing cost.

### 2. Analog comparators with DAC thresholds

For analog sources — amplified load cells, inductive probe analog
stages, current sensing — on-chip comparators turn a threshold
crossing into a hardware event with **no ADC polling at all**.

This is not hypothetical in this repository's lineage: the
`rt-comparator` branch carries a working implementation —
`src/stm32/comp.c`, a *window* comparator on STM32G0 using the
COMP1/COMP2/COMP3 peripherals with both thresholds set by the on-chip
DAC and an IRQ callback into klippy (paired with
`klippy/extras/window_comparator.py`). A window (upper *and* lower
bound) is exactly the right shape for load-cell probing: fire when
force enters the contact band, stay quiet through baseline drift
below it and clip above it. This document adopts that design as the
reference analog trigger source, generalized behind the trigger
interface below.

On families without COMP peripherals, the ADC analog watchdog (auto-
compare in hardware while the ADC free-runs) provides the same
event-not-poll behavior with slightly worse latency.

### 3. Timer input-capture for hardware timestamps

Independent of *detecting* the event, capture units answer *when* it
happened: routing the trigger signal to a timer capture channel
latches the exact tick of the edge in hardware, immune to interrupt
latency and ISR jitter. The trsync record then carries a timestamp
good to ~1 clock tick.

Combined with sub-unit position readback
([02-Intention_Protocol.md](02-Intention_Protocol.md)), probe results
become: hardware-exact trigger time × exact trajectory position at
that time — a precision chain with no polling quantum anywhere in it.

## Interface

Trigger sources are producers for the existing trsync fan-out
([src/trsync.c](../../../src/trsync.c)), which is unchanged — this
document only replaces *how triggers are detected*, not what happens
next:

```
config_trigger_source oid=%c trsync_oid=%c kind=%c ...
  kind: gpio_edge  (pin, edge, qualify_ticks, qualify_count)
        comparator (channel/pin, upper_threshold, lower_threshold, window_mode)
        adc_watchdog (channel, high, low)
trigger_source_arm oid=%c reason=%c capture=%c
```

Trigger events append a `trigger` record — with the hardware-captured
timestamp where available — to the execution log
([08-Failure_Recovery.md](08-Failure_Recovery.md)).

The polled `endstop.c` path remains as the portability fallback and
for genuinely slow signals, but it stops being the design center.

## The unlock: what polling made *impossible*

Microsecond latency is the headline, but it undersells the change. The
deeper point is that moving sensing off the timer list and onto the
peripherals — interrupts, comparators, and especially **DMA** —
converts a whole class of things from *impossible in a real-time motion
loop* to *routine*. Polling didn't just do these slowly; it could not do
them at all without starving step generation, because every sample is an
entry in the same hard timer list the steppers run from.

* **Catch an overrun or fault the instant it happens.** An overcurrent
  comparator, a driver `DIAG`/stall flag, a crash/collision input, a
  thermal cutoff — as an interrupt, these fire and stop motion within a
  microsecond of the physical event. Polled, the machine keeps driving
  into the fault until the next sample lands, which in a motion system is
  the difference between a clean abort and a broken part or a ground
  toolhead. Events make the fault path *pre-emptive*, not *periodic*.

* **DMA-driven ADC oversampling.** A DMA controller can stream an ADC at
  tens to hundreds of kHz into a ring buffer with **zero CPU cost per
  sample**, and the CPU processes a block only when DMA raises a
  half/full-transfer interrupt. That buys oversampling-and-decimation for
  extra effective bits, hardware-timed load-cell and pressure capture,
  and analog trigger detection at rates a scheduled `analog_in` poll
  could never reach without consuming the very cycles stepping needs.
  The polled model tops out at a handful of kHz precisely because each
  sample competes with motion; the DMA model does not compete at all.

* **Hardware-exact timestamps and windows.** A timer input-capture unit
  latches the edge tick in silicon, and a comparator + DAC forms an
  analog window that raises an event only on a real threshold crossing —
  detection whose precision is set by the hardware, not by how lucky a
  sample was.

None of these need to be *built* for the architecture to matter: the
value is that the event-and-DMA substrate makes them *reachable*, where
the polled timer list made them structurally out of the question. This
document lays that substrate (`trigger_source`, the comparator and ADC
watchdog backends, input capture); the higher-order uses of it are now
ordinary follow-on work rather than a fight with the scheduler.

## The trigger-locality rule

Hardware triggers make an existing truth sharper, so this document states
it as a rule: **any actuator that must stop on a trigger should share
a board with that trigger's sensor.** Local stop latency is now
microseconds (IRQ → trsync → backend stop, all on-chip); cross-board
stop latency is whatever the link delivers — fine over wired links
within today's 25 ms trsync budget, *not* a precision mechanism over
WiFi ([07-Link_Transport.md](07-Link_Transport.md)). Cross-board
trsync propagation remains what it is today: machine-wide
*coordination* after the precise local stop already happened.

Concretely: a WiFi toolhead board with its own probe and its own Z (or
its own extruder and filament sensor) is a fully precise homing/probing
unit. A WiFi probe stopping motors on a wired mainboard has its
overshoot set by WiFi jitter, and the configuration documentation must
say so.

## Portability

| Capability | Coverage on 32-bit targets |
| --- | --- |
| GPIO edge IRQ | implemented for selected STM32 EXTI families and RP2040 IO_BANK0; live-validated on RP2040; EIC/GPIO-IRQ ports remain |
| Analog comparator | common but not universal (STM32 COMP, RP2350; feature-detected per port) |
| ADC watchdog | most STM32; fallback where COMP absent |
| Timer input capture | universal in some form; capture-to-trsync wiring is per-port work |

Each port advertises its trigger capabilities in the data dictionary;
the host chooses hardware sources when present and falls back to
polled sampling otherwise. Nothing in the machine configuration needs
to change between a board with COMP and one without — only the
achieved latency does.

### Host integration (implemented)

`MCU_endstop` (`klippy/mcu.py`) now selects the detection path
automatically. When the firmware's dictionary advertises the
`trigger_source` command set (i.e. `CONFIG_WANT_TRIGGER_SOURCE` was
built for that board), each digital endstop/probe configures **both** a
polled `config_endstop` (kept for `query_endstop` and as the fallback)
and a hardware `config_trigger_gpio` on the same pin. A normal homing
move (`triggered=True`) then arms the edge interrupt with
`trigger_source_arm` — attaching it to the same trsync dispatch the
polled path used, so the coordinated-stop fan-out is unchanged — and
reads the latched edge tick back from `trigger_source_query` (a
hardware input-capture timestamp where the port wired one, else the
ISR-entry read), with no `rest_ticks` back-dating. A move that instead
waits for the pin to *release* (`triggered=False`) stays on the polled
path, whose edge sense is chosen per move; a board without the
`trigger_source` commands (e.g. a code-size-constrained target) also
falls back silently. This covers every consumer of the digital endstop
interface — cartesian/CoreXY homing, trajectory-stepper homing, `probe`,
and `bltouch` — because they all drive `home_start`/`home_wait`
polymorphically. Set
`[mcu] hardware_endstop_trigger: False` to force the legacy polled path
on a given MCU.

### RP2040 live result

The RP2040 port owns and arms IO_BANK0 edge latches per configured GPIO,
clears stale pending state on arm/disarm, and gives the shared bank handler a
higher NVIC priority than the timer IRQ. RP2040 has no routed timer
input-capture implementation here, so it records `timer_read_time()` once at
ISR entry. On 2026-07-14 an SKR Pico running `e1ec0b9e` completed X and Y
sensorless homing and both passes of the physical Z endstop. Distinct
hardware-source flight-recorder records preceded their actuator stop records
by 261–300 ticks at the 12 MHz scheduler timebase (21.8–25.0 us), matching a
20 us qualify-after-event window plus dispatch. This validates interrupt
detection and coordinated stop; it is not a physical-edge latency benchmark
or timer-input-capture claim. On 2026-07-15 the current clean `915760f5`
trigger-enabled image was flashed to the Pico and repeated the result: the
live capability report included hardware trigger sources, all five board
self-tests passed, and cold homing retained OID 19/21/23 hardware-source
records. The first corresponding actuator stops followed by 264, 262, and
277 scheduler ticks, respectively. Thus the current candidate preserves the
interrupt path.

The 2026-07-15 forced-fallback comparison then ran 16 physical Z homes per
mode in a balanced poll--interrupt--interrupt--poll order. Across the 32
interrupt contacts, the ISR-entry record preceded the trajectory stop by
23.086 us on average with a 23.000--23.167 us range. The legacy path cannot
record the edge; its configured cadence bounds the corresponding detector
path at an estimated 48.1--110.6 us for the 20 mm/s pass and 48.1--464.8 us
for the 3 mm/s pass. Whole-system trigger-position variance was no worse and
was lower in both series. See the measured technical note
[Interrupt-driven homing versus legacy polling](../../Interrupt_vs_Polling.md)
for the method, repeatability data, and limitations.

## Open questions

* Whether qualification parameters (`qualify_ticks/count`) should have
  hardware-filter equivalents auto-selected per family, or stay
  explicit.
* Comparator threshold calibration flow (the DAC thresholds are in
  counts; mapping from grams-of-force belongs host-side — where does
  the calibration data live?).
* Whether capture-timestamped triggers should adjust *past* trajectory
  reconstruction (they arrive after the fact by ISR latency; the
  timestamp is exact but the stop began at ISR time — document the
  distinction in probe math).
* Encoder index-pulse capture as a trigger source for closed-loop
  joint re-qualification after resets
  ([08-Failure_Recovery.md](08-Failure_Recovery.md)) — natural
  extension, deliberately not specified in v1.
