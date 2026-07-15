# Interrupt-driven homing versus legacy polling

Status: measured technical note, 2026-07-15.

## Abstract

HELIX replaces Klipper's periodic endstop sampling with a GPIO edge interrupt
where the MCU supports it. On an SKR Pico driving a Voron V0 Z axis, the
interrupt path stopped the active trajectory **23.086 us after the RP2040
IO_BANK0 ISR-entry timestamp** (32 contacts, 23.000--23.167 us). The equivalent
legacy path cannot timestamp the edge; its configured polling cadence implies
an estimated 48.1--110.6 us edge-to-stop interval on the 20 mm/s pass and
48.1--464.8 us on the 3 mm/s pass, before allowing for switch behavior.

A balanced physical series also found lower whole-system trigger-position
variance with interrupts: 17.9 um versus 40.5 um standard deviation on the
fast pass, and 14.4 um versus 28.7 um on the slow pass. This result supports
using event-driven detection by default while retaining polling as the
portability fallback.

## Why the architectures differ

Both paths feed the same `trsync` coordinated-stop fan-out. Only detection
changes:

```text
Legacy polling
edge -> wait 0..poll_period -> four stable samples over 45 us -> trsync -> stop

HELIX interrupt
edge -> GPIO IRQ -> four qualification reads over 20 us -> trsync -> stop
```

The legacy implementation in [`src/endstop.c`](../src/endstop.c) places an
endstop timer on the MCU scheduler, samples every `rest_ticks`, and requires
four successful samples. The physical edge may therefore arrive anywhere in
one polling period. Its timestamp is reconstructed as the first successful
sample, not observed at the edge.

The HELIX implementation in
[`src/trigger_source.c`](../src/trigger_source.c) lets the GPIO peripheral wake
the MCU. It latches a timestamp at hardware input capture where a port provides
one, or at ISR entry on RP2040, then performs a short qualify-after-event burst.
There is no standing timer-list work while the input is idle and no polling
phase uncertainty.

## Test method

The system under test was:

- Voron V0 with a physical Z microswitch on Pico GPIO25;
- SKR Pico RP2040 firmware `915760f5`;
- 12 MHz Pico scheduler timebase;
- trajectory motion on the Z stepper;
- 20 mm/s first home and 3 mm/s second home, 800 microsteps/mm;
- four 15 us samples on the legacy path and four 5 us qualification reads on
  the interrupt path.

The live path was toggled with:

```ini
[mcu]
hardware_endstop_trigger: False
```

Four eight-home blocks were run in poll--interrupt--interrupt--poll order to
reduce time-order bias. Every `G28 Z` contained a fast and slow switch contact,
giving 32 contacts per mode. A temporary test fixture removed the unrelated Z
hop and parked at Z10 between runs. It was then removed, and the production
configuration was verified byte-for-byte against its pre-test copy. No heater
commands were issued and no run faulted.

The flight recorder supplied:

- interrupt-source OID 23 with the ISR-entry clock;
- trajectory-stepper OID 10 with the actual stop clock and accumulator;
- rebase clocks used to compare repeated physical moves.

One polling fast pass began from an unknown pre-test physical position and was
excluded from the move-repeatability statistics. No interrupt contact and no
slow contact was excluded.

## Results

### Detector latency

The interrupt source-to-actuator delta was 276--278 scheduler ticks across all
32 contacts:

| Metric | Interrupt result |
| --- | ---: |
| Mean | 277.031 ticks / 23.086 us |
| Standard deviation | 0.394 ticks / 0.033 us |
| Minimum--maximum | 276--278 ticks / 23.000--23.167 us |

RP2040 does not route this pin to timer input capture in the current port, so
the measurement starts at ISR entry, not at the electrical edge. It includes
the configured 20 us qualification window, `trsync` dispatch, and trajectory
halt. The residual after qualification is about 3.086 us.

The polling path has no electrical-edge record by design, so its result must be
bounded from the actual timer cadence. At 800 microsteps/mm, the poll periods
are 62.5 us at 20 mm/s and 416.667 us at 3 mm/s. Four samples span three 15 us
intervals, or 45 us. Reusing the observed 3.086 us dispatch cost gives:

| Homing pass | Poll period | Estimated poll edge-to-stop | Estimated mean | Interrupt ISR-entry-to-stop |
| --- | ---: | ---: | ---: | ---: |
| 20 mm/s | 62.5 us | 48.1--110.6 us | 79.3 us | 23.1 us |
| 3 mm/s | 416.7 us | 48.1--464.8 us | 256.4 us | 23.1 us |

The polling estimate assumes an arbitrary edge phase uniformly distributed
within one poll interval. GPIO interrupt-entry latency is not included in the
23.1 us measurement, so this table is not a claim of electrical-edge precision
on RP2040. It is a direct measurement of the event path after ISR entry and an
architecture-derived bound for a path that cannot observe the edge.

### Whole-system repeatability

At both contact speeds the axis was in constant-velocity travel, so the
standard deviation of rebase-to-stop time converts directly to an equivalent
trigger-position deviation:

| Pass | Mode | Contacts | Mean move time | Time sigma | Equivalent position sigma | Position range |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 20 mm/s | polling | 15 | 692.007 ms | 2.025 ms | 40.5 um | 196.3 um |
| 20 mm/s | interrupt | 16 | 691.501 ms | 0.897 ms | 17.9 um | 73.5 um |
| 3 mm/s | polling | 16 | 1003.629 ms | 9.555 ms | 28.7 um | 143.8 um |
| 3 mm/s | interrupt | 16 | 1005.834 ms | 4.807 ms | 14.4 um | 57.1 um |

These move-level numbers include the switch, mechanics, driver, trajectory,
and detector. They are not pure firmware latency measurements. In particular,
the slow-pass mean did not order by detector latency; mechanical hysteresis and
block-to-block drift were larger than the expected sub-millisecond difference.
The useful result is the variance: the interrupt path was no worse and reduced
the observed position sigma by 2.3x on the fast pass and 2.0x on the slow pass.

## Why the change is worthwhile

The measured benefit is not merely a smaller average number:

1. **Bounded response.** Polling adds a speed-dependent uncertainty interval;
   an interrupt begins qualification as soon as the peripheral reports an
   edge.
2. **A meaningful timestamp.** A capture or ISR-entry tick can be reconciled
   with the executed trajectory. A poll only proves the input was active when
   sampled.
3. **No standing scheduler load.** Idle endstops consume no periodic timer
   callbacks, leaving the hard real-time queue to motion and genuinely timed
   work.
4. **A scalable sensing model.** The same event interface admits timer capture,
   comparators, and ADC watchdogs without turning high-rate sensing into more
   software polling.
5. **No compatibility cliff.** Boards without the peripheral or firmware
   support silently retain the legacy path, and the operator can force it with
   `hardware_endstop_trigger: False`.

## Limits and conclusion

This is one printer, one RP2040 port, one switch, and 32 contacts per mode. It
does not qualify STM32 timer capture, analog triggers, probes, or multi-MCU
stop propagation. A logic-analyzer measurement from GPIO edge to step-pin halt
would further isolate electrical-edge latency. Those limits do not weaken the
central finding: the current HELIX path executes a qualified local stop in a
tightly repeatable 23.1 us after ISR entry, while legacy polling necessarily
adds an unobservable polling interval and showed higher physical stop variance
in this balanced series.

For motion control, sensing is naturally event-driven. Polling remains a sound
fallback; it should no longer be the design center.
