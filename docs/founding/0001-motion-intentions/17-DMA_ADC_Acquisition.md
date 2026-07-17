# FD-0001: Unified DMA and ADC Acquisition

Status: Research complete and architecture adopted. Implementation and
cross-family hardware qualification are pending. This work precedes the
STM32F767 Ethernet implementation.

This document specifies the common HELIX primitives for DMA-backed peripheral
acquisition and applies them first to ADC sampling on STM32, RP2040, and ESP32.
The objective is not merely faster temperature reads. It is to replace a
per-sample polling architecture with hardware-paced acquisition, bounded
block interrupts, explicit ownership, machine-time metadata, and truthful
loss reporting.

The primitive is intentionally split in two:

* a small internal **DMA block-stream substrate** shared by ADC, Ethernet, and
  later capture peripherals; and
* a device-aware **ADC acquisition engine** that owns channel sequencing,
  oversampling, calibration metadata, analog watchdogs, and host reporting.

There will be no command that lets a host DMA arbitrary addresses. Peripheral
endpoints are compiled allowlists selected by board backends. The abstraction
unifies lifecycle and evidence, not raw DMA registers.

## Why the existing ADC loop must change

The legacy path in [src/adccmds.c](../../../src/adccmds.c) gives every logical
ADC input a scheduler timer. Each sample calls `gpio_adc_sample()`; when the
conversion is not ready, the same hard timer is rescheduled and polls again.
`sample_count` therefore multiplies timer-list work. The ISR-side loop adds the
samples into a 16-bit accumulator, wakes a task, and finally reports a small
batch. This has four structural problems:

1. acquisition timing depends on firmware timer service rather than a hardware
   sample clock;
2. every input sample competes with motion for scheduler/interrupt attention;
3. useful oversampling directly increases that competition;
4. there is no retained raw block, DMA error model, or exact evidence of a
   missed sample.

The individual ports expose the same cost in different ways:

* STM32 starts a software conversion and polls EOC after a conservative delay;
* RP2040 starts a one-shot conversion and polls readiness even though the ADC
  already has a pacing timer, FIFO, DREQ, and DMA interface;
* ESP32 cannot call its oneshot driver from the motion timer, so the current
  compatibility backend posts each conversion to a FreeRTOS worker and polls
  a cross-core state machine at roughly millisecond scale.

The current batched `analog_in_state` change reduces wire messages. It does not
remove per-sample scheduler work. DMA does: a timer or ADC clock produces the
samples, DMA fills a block, and firmware runs once per half/full block or error.

## Research basis

The design is based on the following vendor documentation:

* ST's [ADC oversampling application note](https://www.st.com/resource/en/application_note/an5537-how-to-use-adc-oversampling-techniques-to-improve-signaltonoise-ratio-on-stm32-mcus-stmicroelectronics.pdf)
  documents timer-paced ADC-to-RAM DMA, transfer-complete interrupts, software
  decimation, and the hardware oversampling engine present on selected STM32
  ADC generations.
* ST's [STM32G0 ADC presentation](https://www.st.com/resource/en/product_training/STM32G0-Analog-ADC-ADC.pdf)
  documents DMA/interrupt delivery and a hardware oversampler that can produce
  up to 16-bit results.
* ST's [STM32 MPU application note](https://www.st.com/resource/en/application_note/an4838-introduction-to-memory-protection-unit-management-on-stm32-mcus-stmicroelectronics.pdf)
  describes using MPU memory attributes to control cacheability on M7-class
  devices.
* The [RP2040 datasheet](https://datasheets.raspberrypi.com/rp2040/rp2040-datasheet.pdf)
  specifies a 12-bit, 500 ksps ADC with a fractional pacing divider,
  round-robin inputs, result FIFO, per-sample error bit, DMA DREQ, and chainable
  DMA channels.
* The ESP-IDF [continuous ADC driver](https://docs.espressif.com/projects/esp-idf/en/v5.3/esp32/api-reference/peripherals/adc_continuous.html)
  defines DMA conversion frames, ISR callbacks, pool-overflow reporting,
  target-specific channel metadata, APB-frequency locking, and the classic
  ESP32 I2S0/ADC2 limitations.
* ESP-IDF's [ADC calibration driver](https://docs.espressif.com/projects/esp-idf/en/v5.1.2/esp32/api-reference/peripherals/adc_calibration.html)
  is required because classic ESP32 reference voltage varies materially across
  chips; oversampling cannot correct gain, offset, or nonlinearity.
* ESP-IDF's [DMA-capable memory documentation](https://docs.espressif.com/projects/esp-idf/en/v5.3.2/esp32/api-reference/system/mem_alloc.html#dma-capable-memory)
  requires internal `MALLOC_CAP_DMA` memory and excludes external PSRAM.

## Hardware capability matrix

The generic interface must expose these differences instead of reducing every
target to the weakest implementation.

| Property | STM32 classic ADC IP (F0/F1/F2/F4/F7) | STM32 newer ADC IP (selected G0/G4/L4/H7) | RP2040 | classic ESP32 |
| --- | --- | --- | --- | --- |
| Sample pacing | timer TRGO / external trigger | timer TRGO / external trigger | ADC fractional divider | digital ADC controller clock |
| DMA source | ADC data register | ADC data register | ADC FIFO DREQ | I2S0-backed continuous driver |
| Multi-channel identity | fixed scan rank, implicit | fixed scan rank, implicit | fixed round-robin order, implicit | unit/channel tag in result |
| Hardware oversampling | do not assume; DMA + software decimation | capability-selected hardware accumulator/shift | none; DMA + software decimation | none in classic target; software decimation |
| Block interrupt | DMA half/complete/error | DMA half/complete/error | chained DMA completion/error | conversion-frame/pool-overflow callback |
| Sample phase quality | hardware timer exact | hardware timer exact | captured start plus ADC divider | frame/start inference with advertised uncertainty |
| Cache concern | F7 requires MPU/cache policy | H7 requires reachable RAM and MPU/cache policy | no data cache | internal DMA-capable memory; IRAM-safe ISR option |
| Important conflict | ADC/DMA stream ownership | ADC/DMAMUX stream ownership | one ADC engine shared by all inputs | ADC2 conflicts with Wi-Fi; I2S0 is consumed |

This table describes backend classes, not every member of a large product
family. Each compiled target publishes its actual capabilities and limits.
For example, a specific STM32G0 may expose hardware oversampling while an F767
uses the same generic block engine with software decimation.

## Architecture

```
 timer / ADC clock
        |
        v
 ADC sequencer -> peripheral FIFO/register -> DMA -> fixed block set
                                                    |
                                       half/full/error interrupt
                                                    |
                                                    v
                                         acquisition block queue
                                          /         |          \
                                safety monitor  local filter  capture data
                                      |             |             |
                               watchdog/trsync  control input  classed reports
                                                    |
                                             summary/report task
```

The interrupt publishes completed blocks. It does not parse, calibrate, filter,
format protocol messages, or call a host callback. Those operations run in a
bounded task or a registered local consumer.

### Layer 1: DMA memory and resource ownership

Introduce an internal `dma_pool` and `dma_resource` API:

* `dma_pool_alloc(size, alignment, capabilities)` is allowed only during MCU
  configuration and never from an ISR. It returns fixed-lifetime storage.
* `dma_claim(endpoint, request, owner)` claims a DMA channel/stream/request
  tuple and fails configuration on conflict. There is no preemption.
* `dma_release()` is used only while stopped and primarily supports reset or
  reconfiguration; production streams retain their resources.
* descriptors, control blocks, and buffers have separate capability bits so a
  backend may apply stricter alignment or reachability.

Memory policy is backend-specific behind the same contract:

* STM32 M7 descriptors and buffers use an aligned non-cacheable MPU region in
  DMA-reachable SRAM. Non-M7 targets still validate DMA reachability.
* RP2040 uses ordinary SRAM but should place alternating buffers in different
  SRAM banks where practical so DMA can fill one while a core processes the
  other.
* ESP32 uses preallocated internal `MALLOC_CAP_DMA` memory. PSRAM is rejected.

All drivers retain ordering barriers around ownership changes. Non-cacheable
memory solves coherency; it does not make descriptor publication atomic.

### Layer 2: the completed-block primitive

The unit of transfer is a fixed-size block with this logical metadata:

```
struct acq_block {
    void *data;
    uint32_t sequence;
    uint32_t epoch;
    uint32_t item_count;
    uint64_t first_machine_clock;
    uint32_t period_numerator;
    uint32_t period_denominator;
    uint32_t uncertainty_ticks;
    uint32_t status;
};
```

The period is rational so rates that do not divide the MCU clock remain
truthful. `first_machine_clock` describes the first conversion, not IRQ entry.
`uncertainty_ticks` is zero only when the backend can prove a hardware-timed
phase. Status includes peripheral error, DMA error, sample error, watchdog,
overrun, discontinuity, and inferred-timestamp flags.

Each block follows one auditable state machine:

```
FREE -> DMA_OWNED -> READY -> CONSUMER_OWNED -> FREE
```

Only the backend ISR may publish `DMA_OWNED -> READY`; only the consumer task
may release `CONSUMER_OWNED -> FREE`. Sequence and generation values detect a
stale release. Debug builds poison free blocks and assert illegal transitions.

A bounded single-producer/single-consumer index ring carries ready blocks. The
ring never silently overwrites old data. An acquisition-ring exhaustion policy
is one of:

* `DROP_CAPTURE`: allowed only for a non-safety capture stream; release that
  completed block, advance sequence, set a discontinuity, and increment a
  counter;
* `STOP_STREAM`: stop acquisition at the next safe boundary and report a
  prompt fault;
* `SAFETY_TRIGGER`: stop the stream and fire its configured local trigger.

There is deliberately no generic "overwrite oldest" safety mode because it
destroys the evidence needed to detect a discontinuity.

Outbound raw telemetry has a separate `DROP_TELEMETRY` policy after local
consumers and safety processing have finished. Network backpressure may discard
commissioning data; it may not consume the only copy of an unprocessed control
or safety block.

### Layer 3: ADC engine and logical subscriptions

One physical ADC instance has one owner and one acquisition schedule. Logical
consumers subscribe to that engine rather than programming the peripheral
independently. Configuration is collected before the stream starts, then the
backend constructs one channel pattern and base rate.

Every subscription states:

* pin/internal channel and optional ADC-unit preference;
* purpose: `MONITOR`, `CONTROL`, `CAPTURE`, or `TRIGGER`;
* requested per-channel sample rate and maximum acceptable timing error;
* sample time/attenuation/resolution requirements;
* oversampling ratio, shift, and filter mode;
* block size, report rate, and loss policy;
* report traffic class, delivery deadline where applicable, and local failure
  action;
* raw-code safety thresholds and consecutive-failure policy;
* whether raw samples, summaries, or only a local callback are required.

The engine accepts subscriptions only when one hardware schedule satisfies all
of them. The portable v1 schedule uses a fixed, uniform scan pattern and
integer output decimation. Backends may later advertise weighted/repeated scan
ranks, but callers cannot depend on them. If a fast pressure sensor and slow
thermistors cannot coexist without violating bandwidth or safety, the system
must allocate another ADC instance or reject configuration with the conflicting
owners named. It must not silently change a sample rate.

Safety subscriptions are pinned. A commissioning capture cannot reconfigure
or starve heater monitoring after the MCU enters the ready state.

### Layer 4: traffic-class delivery

Traffic class is a property of each subscription's **outbound data product**.
It does not change ADC pacing, DMA priority, block ownership, local filter
execution, or analog-watchdog interrupt priority.

* **Class 0 — Scheduled:** a timestamped, deadline-bearing summary whose timely
  delivery is a prerequisite for continuing scheduled operation. It uses a
  separately bounded critical-report queue and the reliable ordered transport.
  Failure to enqueue or receive the transport acknowledgement before its
  delivery deadline invokes the subscription's declared local action (`HOLD`,
  `TRIGGER`, or `SHUTDOWN`) instead of allowing the machine to continue without
  a required input. The transport must therefore return Class-0 acknowledgement
  state to the producer; a carrier that cannot do so may not advertise this
  report mode. Class 0 does not mean that Python runs in an interrupt and does
  not add a per-sample firmware timer; hardware acquisition time and block
  progression establish the report deadline.
* **Class 1 — Prompt:** a reliable, ordered reading or event that should reach
  the host promptly but is not tied to an absolute execution instant. Analog
  watchdog trips, acquisition faults, continuity breaks, and requested bounded
  post-event data use Class 1. Queue pressure may stall or coalesce an explicitly
  coalescible reading, but it may not silently discard a fault event.
* **Class 2 — Telemetry:** best-effort periodic values and raw commissioning
  captures. These are source-rate-limited and may be dropped under congestion;
  sequence gaps and drop counters make loss visible.

Most thermistor/status reports remain Class 2 because local range checks and
heater watchdogs are the safety mechanism. A load/pressure value used in a
host control decision may be Class 1. Class 0 is intentionally rare: choosing
it requires a deadline and a local failure action, not merely declaring the
sensor "important."

A report class never substitutes for local protection. A sensor may emit
Class-2 telemetry while its comparator/ADC-watchdog path is safety critical,
and a Class-0 report still cannot stop a heater or actuator faster than a local
hardware trigger.

### Layer 5: oversampling and filtering

The API distinguishes four rates and never calls an average "extra bits"
without evidence:

* conversion rate: all ADC conversions per second;
* channel input rate: raw conversions per second for one channel;
* oversampling ratio (`OSR`): inputs accumulated per filtered result;
* output/report rate: filtered results produced/reported per second.

Selected STM32 targets use the hardware accumulator and right shift. Other
targets DMA raw samples and perform the same operation once per completed
block. The reference software path uses a 64-bit accumulator, deterministic
rounding, and a boxcar filter followed by integer decimation. A later CIC/FIR
filter may be added without changing block ownership or wire metadata.

Oversampling reduces in-band uncorrelated noise and trades bandwidth for SNR.
It does not remove offset, gain error, reference error, integral nonlinearity,
or aliasing. The documentation and status output therefore report `OSR` and
filter parameters, not an invented effective resolution. ENOB improvement is
claimed only from measured noise/SINAD data with the sensor front end and
sample rate stated. Analog antialias filtering remains a hardware requirement.

### Layer 6: analog watchdog integration

The existing STM32 watchdog backend temporarily owns an ADC and free-runs one
channel, which prevents normal sampling on that ADC. Refactor watchdogs as ADC
engine subscriptions:

* where hardware permits a watchdog alongside the regular sequence, the same
  conversions feed both DMA and the threshold comparator;
* where hardware watches only one channel or one data stage, the backend
  advertises those limits;
* where a comparator peripheral is available, it remains preferable for the
  fastest independent safety edge;
* where simultaneous operation is impossible, configuration fails explicitly.

Watchdog metadata states whether the threshold observes a raw conversion, a
hardware-oversampled result, or a software-filtered block. Those are different
safety semantics and may not be substituted silently.

## Versioned firmware and wire interfaces

The internal board contract is intentionally narrower than a universal DMA
DSL:

```
adc_stream_configure(config, caps, buffers)
adc_stream_start(start_clock, epoch)
adc_stream_stop(mode)
adc_stream_irq_ack_and_publish()
adc_stream_get_status()
```

The target-independent core owns subscriptions, blocks, sequence continuity,
filters, local consumers, and reporting. The backend owns registers, DMA
requests, start phase, raw result layout, and error decoding.

The proposed protocol surface is versioned independently from legacy
`analog_in`:

```
config_adc_stream oid=%c adc=%c rate=%u block_items=%hu flags=%u channels=%*s
adc_stream_subscribe oid=%c sub=%c channel=%c osr=%hu shift=%c mode=%c
                     report_div=%hu report_class=%c deadline_ticks=%u
                     fail_action=%c low=%u high=%u fault_count=%c
adc_stream_start oid=%c clock=%u epoch=%u
adc_stream_stop oid=%c mode=%c
adc_stream_query oid=%c

adc_stream_status oid=%c epoch=%u next_seq=%u running=%c raw_count=%u
                  ready_highwater=%hu dma_errors=%u adc_errors=%u
                  overruns=%u telemetry_drops=%u watchdog_events=%u
adc_stream_scheduled oid=%c sub=%c seq=%u first_clock=%u deadline=%u ...
adc_stream_prompt oid=%c sub=%c seq=%u first_clock=%u ...
adc_stream_telemetry oid=%c sub=%c seq=%u first_clock=%u ...
adc_stream_data_telemetry oid=%c sub=%c seq=%u offset=%hu flags=%c data=%*s
adc_stream_data_prompt oid=%c sub=%c seq=%u offset=%hu flags=%c data=%*s
```

Exact field widths may change during implementation, but these semantics do
not. The three summary messages share a payload schema (`count`, `min`, `max`,
64-bit sum, format/shift, and continuity flags) but have distinct command IDs
because FD-0001 makes class a static dictionary property, not a mutable frame
field. Raw blocks are chunked across protocol frames and retain block sequence
and offset. Routine capture uses Class 2; a bounded requested fault-window dump
uses Class 1. Bulk raw data is never Class 0. No ISR calls `sendf()`.

The data dictionary publishes `ADC_STREAM_V1` plus target capabilities:

* ADC count, channels, maximum conversion rate, and sequence length;
* supported resolutions/sample times/attenuations;
* hardware oversampling ratios and shifts;
* exact external trigger, inferred timestamp, and per-sample tag support;
* watchdog stage/count and simultaneous watchdog+DMA support;
* maximum block/ring storage and active DMA resource assignments.

The host registers response handlers for the selected class-specific message
ID. DMA completion wakes the MCU task; per-subscription acquisition counters
cross their report boundary; and the MCU pushes the appropriate report. Python
receives it later in Klippy reactor context. There is no host polling loop and
no path from a hardware ISR directly into Python.

## Time model

A completed-block interrupt says "data is available"; it does not say when the
samples occurred. Each backend establishes sample time as follows:

### STM32

A machine-timer compare starts or gates a timer whose TRGO triggers the ADC.
The scheduled compare establishes the first conversion phase. Scan rank and
ADC conversion timing define offsets inside the sequence. DMA half/complete
latency is irrelevant to sample timestamps.

### RP2040

The ADC pacing divider defines sample intervals. Firmware captures the machine
clock immediately around the transition into `START_MANY`, records start
uncertainty, and derives later sample times from the divider. The FIFO has a
per-sample error bit but no channel tag, so the configured round-robin order
and exact sample count are part of continuity. A FIFO overflow invalidates
channel phase until the stream is restarted with a new epoch.

### ESP32

The continuous driver supplies frames at the configured digital-controller
rate. Classic ESP32 does not provide the same timer-TRGO phase contract as
STM32. The backend captures start/frame boundaries, reconstructs sample times
from the configured pattern/rate, and advertises measured uncertainty. The
system must not promote inferred timestamps to hardware-exact timestamps.

All backends increment `epoch` on a discontinuous restart. A consumer may not
interpolate across epochs or missing block sequences.

## Backend plans

### STM32 family

Implement family operations for the ADC register generations already present
in `stm32/adc.c`, `stm32/stm32f0_adc.c`, and `stm32/stm32h7_adc.c`, plus a
compile-time DMA request map.

* Configure a general-purpose timer TRGO, regular sequence, circular DMA, and
  half-transfer/transfer-complete/transfer-error IRQs.
* Use circular half buffers on simple DMA controllers and native double-buffer
  mode where available; both publish identical blocks.
* Use hardware oversampling only when the selected MCU header and capability
  table prove the feature. F4/F7 software decimation remains fully supported.
* Place F7/H7 buffers in the DMA MPU arena. On H7, choose memory reachable by
  the selected DMA domain; never infer reachability from a CPU pointer.
* Resolve DMA/DMAMUX conflicts at configuration and report the competing
  endpoint.
* Integrate analog watchdog IRQs with the active scan rather than overwriting
  the ADC's operating mode.

Initial compiled targets are STM32G0B1, STM32F407, STM32F767, and STM32H723.
The G0B1 and F767 then receive live acquisition tests; H7 proves the second M7
memory/DMA topology before broader family claims.

The current STM32 source families map into the plan as follows:

| HELIX target family | Existing oneshot backend | First stream mode | Oversampling policy |
| --- | --- | --- | --- |
| F0 | `stm32f0_adc.c` | timer trigger + DMA channel | software unless the exact part advertises hardware support |
| F1/F2/F4/F7 | `adc.c` | timer trigger + scan DMA | software; do not manufacture a hardware capability |
| G0 | `stm32f0_adc.c` | timer trigger + DMA/DMAMUX | hardware when the exact ADC exposes it, software reference retained |
| G4/L4/H7 | `stm32h7_adc.c` | timer trigger + scan DMA/DMAMUX | hardware capability plus software reference mode |

The table is a porting map, not proof that every pin, trigger source, and DMA
request is interchangeable. Per-part tables remain authoritative.

### RP2040

Replace one-shot polling with ADC FIFO DREQ and two chained DMA channels. One
channel fills block A while the completed channel is rearmed for block B; the
minimum block duration must leave measured rearm margin. If that margin is not
robust, add a small control-DMA chain instead of depending on ISR timing.

* Enable per-sample error bits and preserve them through filtering/status.
* Treat FIFO overflow/underflow and DMA bus errors as discontinuities.
* Use fixed round-robin channel order and reject schedules it cannot express.
* Keep the 500 ksps hardware ceiling and the ADC's measured 8.7 ENOB distinct
  from any software output width.
* Place alternating blocks in separate SRAM banks when linker layout permits.
* Give DMA completion lower priority than motion timer and hardware endstop
  interrupts; choose block sizes from measured interrupt budget.

The SKR Pico is the first live proof because it is already part of the V0
qualification rig.

### ESP32 family

Use ESP-IDF continuous mode instead of the oneshot worker. On classic ESP32:

* reserve I2S0 for ADC DMA and reject a conflicting feature;
* use ADC1 while Wi-Fi is active; ADC2 is not a supported HELIX Wi-Fi
  acquisition source;
* enable the IRAM-safe continuous ISR option and keep callback code/data in
  internal memory;
* preallocate the driver pool and HELIX block/ring storage in DMA-capable
  internal RAM;
* disable overwrite/flush behavior, consume pool-overflow events, and make
  every loss visible;
* retain the APB-frequency lock for the whole running interval;
* create and publish the IDF calibration scheme/availability, but perform
  raw-to-voltage conversion outside ISR context.

In component mode, an IDF task drains completed frames into the generic block
core. In modem mode, core 0 owns IDF, ADC, and DMA; an internal-RAM shared block
ring publishes descriptors to the bare motion core. Routine filtering and
decimation happen before crossing cores. High-rate raw capture is a bounded
commissioning mode and may not flood the motion-core shared-memory console.

Later ESP32 variants use IDF SoC capability macros for ADC unit, output format,
DMA engine, and known errata. They do not inherit classic ESP32 assumptions by
name alone.

## Host integration and compatibility

`MCU_adc` detects `ADC_STREAM_V1`. Supported MCUs translate existing
`setup_adc_sample()` requests into subscriptions and retain the callback shape
expected by heaters, `temperature_mcu`, ADC buttons, scaling, and query tools.
Unsupported MCUs keep `adccmds.c` unchanged.

Migration is capability-based and reversible per MCU. During qualification a
diagnostic mode can run a DMA stream and a low-rate legacy observer against the
same stable input when hardware sharing permits, but only one path owns heater
safety. No production configuration runs two independent owners of one ADC.

The host exposes:

* effective channel rate, OSR, filter, calibration source, and timing quality;
* block/ring high-water marks and every error/drop counter;
* physical ADC/DMA resources and conflicting owners;
* start epoch and last contiguous sequence;
* raw capture controls with bandwidth and duration limits.

Atlas records acquisition overruns, watchdog events, calibration absence,
DMA errors, and continuity breaks as structured incidents. Normal summaries do
not become incidents.

## Interrupt priorities and budgets

Acquisition DMA interrupts are below motion timer, hardware endstop/trsync,
and critical transport timestamp interrupts. Analog watchdog/comparator safety
IRQs may be higher than block completion because they do constant bounded work
and can stop a local actuator.

Block size is a latency/overhead choice, not a fixed universal constant. Every
configuration computes:

* block duration;
* worst measured ISR publication time;
* consumer processing deadline;
* number of spare blocks;
* time to overrun at the current rate.

Qualification requires margin under simultaneous worst-case motion and
transport load. Instrumentation GPIO toggles are used only in dedicated
measurement images because they perturb the ISR being measured.

## Implementation sequence

### Phase 0 - baseline and reference model

- [ ] Measure legacy timer invocations, conversion retries, CPU time, report
  bandwidth, and motion jitter for representative thermistor and high-rate
  oversampling configurations.
- [ ] Add a host reference model for scan ordering, boxcar accumulation,
  rounding, decimation, timestamps, epochs, and injected missing samples.
- [ ] Define capability constants, status flags, ownership states, and failure
  policies before target code.

Gate: the baseline is reproducible and the reference model rejects every
silent gap or channel-phase discontinuity.

### Phase 1 - generic DMA/block and ADC cores

- [ ] Implement fixed-lifetime DMA pool allocation, resource claims, block
  state machine, SPSC ready ring, counters, and simulated backend.
- [ ] Implement ADC engine subscription merging, uniform scan planning,
  software filtering, local consumers, raw chunking, and summaries.
- [ ] Add protocol dictionary/commands and `MCU_adc` capability negotiation.
- [ ] Add distinct Class-0/1/2 report IDs, their bounded queues, Class-0
  acknowledgement/deadline feedback, and local deadline-failure actions.
- [ ] Unit-test wrap, stale release, ring full, stop/restart epoch, rational
  timestamps, multi-channel ordering, filter overflow, class starvation, and
  all loss policies.

Gate: randomized simulated streams match the host reference bit-for-bit and no
injected discontinuity is reported as contiguous data.

### Phase 2 - RP2040 proof

- [ ] Implement FIFO/DREQ, chained ping-pong DMA, error capture, scan order,
  pacing divider, and inferred-start uncertainty.
- [ ] Migrate V0 thermistor monitoring through the compatibility adapter.
- [ ] Test raw and decimated acquisition from DC, PWM+filter, and a known
  waveform while homing and high-rate motion execute.
- [ ] Compare interrupt count, CPU budget, sample timing, and motion jitter to
  Phase 0.

Gate: no per-sample scheduler timer remains, sample counts and order are exact,
all forced FIFO/DMA overruns are visible, and heater protections remain
equivalent or stronger.

### Phase 3 - STM32 proof

- [ ] Implement classic and newer ADC-IP operations, timer TRGO, DMA mapping,
  circular/double buffers, and watchdog coexistence.
- [ ] Prove software OSR on F4/F7 and hardware OSR on a capable G0/G4/H7
  target against the same offline reference.
- [ ] Prove the M7 MPU arena and H7 DMA-reachable-memory selection with caches
  enabled.
- [ ] Run STM32G0B1 and F767 live tests, including simultaneous motion/ADC and,
  once resumed, Ethernet/ADC DMA contention.

Gate: all compiled targets expose truthful capabilities; G0B1 and F767 show
continuous, accountable acquisition under load; cache-on and diagnostic
cache-off runs preserve ownership, sequence continuity, and agreement with the
same offline processing reference. Raw analog noise is not expected to be
byte-identical between runs.

### Phase 4 - ESP32 proof

- [ ] Replace the oneshot worker with IDF continuous mode in component and
  modem architectures.
- [ ] Implement ADC1/Wi-Fi and I2S0 conflict checks, IRAM-safe callbacks,
  internal DMA memory, APB lock, calibration metadata, and shared-ring flow.
- [ ] Test Wi-Fi reconnect, flash/cache-disabled intervals, pool exhaustion,
  raw telemetry throttling, and cross-core consumer delay.
- [ ] Measure sample-rate error and timestamp uncertainty instead of assuming
  STM32 timer-trigger quality.

Gate: continuous Wi-Fi traffic and reconnects cause no silent ADC loss, motion
core starvation, or unreported pool overflow; calibrated and raw outputs are
both reproducible.

### Phase 5 - consumer migration and qualification paper

- [ ] Migrate heaters, MCU temperature, ADC buttons, scaling, and analog
  trigger consumers target by target.
- [ ] Preserve legacy fallback for unsupported boards and add a per-MCU
  diagnostic override.
- [ ] Publish graphs comparing legacy polling versus DMA: ISR/timer rate, CPU
  time, sample-period error, motion jitter, overrun behavior, SNR, and ENOB.
- [ ] Archive exact images, configurations, raw captures, and analysis scripts.

Gate: existing safety behavior passes regression tests; the published data
supports any efficiency or precision claim; unsupported MCUs do not regress.

### Phase 6 - unblock Ethernet

- [ ] Reuse the proven DMA pool, resource manager, ownership barriers, block
  queues, counters, and IRQ publication rules in the F767 Ethernet driver.
- [ ] Run ADC and Ethernet simultaneously to validate resource mapping,
  memory bandwidth, cache policy, priorities, and bounded task drain.

Gate: the Ethernet implementation introduces no second DMA ownership model.

## Definition of done

The unified acquisition layer is complete when supported targets perform no
per-sample scheduler polling; STM32, RP2040, and ESP32 reference backends pass
their hardware gates; every block has machine-time metadata and explicit
continuity; no buffer has ambiguous CPU/DMA ownership; every overflow and DMA
error is counted and policy-handled; oversampling claims are supported by raw
evidence; heater and trigger safety semantics are preserved; and Ethernet can
consume the same primitives without inventing a parallel DMA framework.
