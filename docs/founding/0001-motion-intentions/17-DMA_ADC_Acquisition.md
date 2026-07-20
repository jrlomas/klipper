# FD-0001: Unified DMA and ADC Acquisition

Status: Core architecture and workstation implementation complete; target
qualification continuing. The generic ownership/resource core, deterministic
filter, bounded Class-0/1/2 delivery, local safety actions, retained fault
capture, automatic `MCU_adc` compatibility adapter, RP2040,
STM32F0/G0/F4/F7/H7, classic ESP32, and shared F767 Ethernet allocation all
compile and pass their host tests. STM32F072, STM32H723, RP2040, and classic
ESP32 have live acquisition evidence. Physical waveform/SNR qualification,
RP2040 physical motion/jitter and heater-fault injection, G0B1/F767 target
runs, and simultaneous live Ethernet/ADC remain hardware gates rather than
software-completion claims.

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

## Implemented checkpoint (2026-07-17)

The landed architecture now includes:

* `generic/acq_block` enforces the generation-checked `FREE -> DMA_OWNED ->
  READY -> CONSUMER_OWNED -> FREE` lifecycle. A fixed, aligned two-block pool
  never overwrites an unconsumed generation.
* `generic/dma_resource` provides a fixed configuration-time DMA arena,
  power-of-two-aligned buffer/descriptor allocations, and exclusive compiled
  claims for peripherals, timers, DMA channels/streams, and DMAMUX requests.
  The ADC engine now allocates its ping-pong storage from this shared arena and
  every implemented backend declares its complete resource set. On M7 targets
  the linker places the arena outside DMA-inaccessible DTCM and one MPU region
  maps it as shareable, non-cacheable memory before D-cache is enabled.
* `adc_stream` accepts one physical stream of one to four ascending channels,
  at most 64 interleaved values per block, a scheduled machine-clock start,
  rational scan period, uncertainty, epoch, sequence, status, and explicit
  fault/drop counters. Class 0 faults shut down; Class 1/2 faults stop the
  stream and remain queryable.
* RP2040 uses ADC FIFO/DREQ and chained DMA channels 10/11. STM32F072 and
  STM32G0B1 use TIM3 TRGO plus circular DMA1 Channel 1 half/full blocks;
  F4/F7 use native DMA double buffering; STM32H723 uses TIM3, ADC1, DMAMUX
  request 9, DMA1 Stream 0, and non-cacheable aligned blocks in AXI SRAM.
  STM32 starts with an immediate TRGO after arming the ADC so
  the first aperture agrees with the recorded machine-clock start instead of
  lagging it by one scan period. These four configurations cross-compile
  cleanly.
* Classic ESP32 uses the IDF 5.3.2 continuous ADC1/I2S0 DMA driver on core 0
  in both component and modem builds. It uses an 8 KiB non-overwriting driver
  pool, software boxcar averaging to bridge IDF's 20 kconversion/s minimum,
  a cross-core mailbox, and a distinct backend-pool-overflow flag. The v0
  stream is exclusive with the legacy ADC1 oneshot path and advertises an
  inferred, conservative start uncertainty.
* `klippy/extras/adc_stream.py` provides `[adc_stream]`, bounded decoded
  buffering, `ADC_STREAM_START/STOP/STATUS`, and `adc_stream/dump_adc` with
  explicit MCU gaps, host drops, and timing uncertainty.
* Up to eight logical subscriptions select a physical channel, input
  decimation, software OSR, deterministic rounded right shift, report
  decimation, and static Prompt or Telemetry summary class. A 64-bit
  accumulator produces count/min/max/sum summaries. Discontinuity resets a
  partial filter instead of joining samples across a gap. Report schedules are
  rejected unless MCU task work is bounded to at most one summary per
  subscription per DMA block.
* A bounded ready ring separates DMA publication from consumer work. Distinct
  Class-0/1/2 report IDs have independent loss policy; Class 0 requires an
  acknowledgement deadline and can invoke local HOLD, analog TRIGGER, or MCU
  SHUTDOWN. Threshold debounce runs locally from the filtered result.
* A seven-block pre/post capture window retains raw evidence around a command
  or terminal fault. Sixteen-byte wire chunks carry blocks up to 64 values
  without exceeding the protocol frame limit.
* `klippy/extras/adc_stream_model.py` is the independent executable reference
  for scan phase, boxcar accumulation, rounding, decimation, and summary
  metadata. Five hundred seeded randomized schedules agree with a second
  direct implementation. The C filter has matching fixed vectors.
* `[mcu] adc_stream_mode: auto` collects compatible legacy consumers into one
  uniform engine when `ADC_STREAM_V1` and integer-compatible schedules are
  available. It falls back before configuration if firmware or consumer
  semantics are unsupported, an unmerged consumer exists, or an explicit raw
  stream owns the MCU; `off` and `force` provide diagnostic control. Legacy
  sample counts are distributed across each report interval to avoid a
  continuous high-rate emulation, while `summary_mode=latest` preserves one
  callback batch. Existing heater range debounce becomes a local shutdown
  policy. Firmware-provided `adc_stream_channel` ranks canonicalize physical
  scan order independently of configuration-section construction order while
  the reordered subscription objects preserve sensor identity. Older
  multi-channel firmware without this metadata falls back before claiming the
  ADC. OpenAMS FPS retains its explicit opt-out.

Evidence at this checkpoint:

| Check | Result |
| --- | --- |
| Ownership, filter, adapter, and host decode tests | Fixed C vectors, 500 seeded randomized schedules, legal/stale block transitions, interleaved timestamps, summary scaling/gaps, merged FPS scheduling, physical-rank ordering with preserved callbacks, old-firmware and split-ownership fallback, and bounded host drops pass |
| Native builds | RP2040, STM32F072, STM32G0B1, and STM32H723 pass clean isolated builds |
| DMA resource and M7 placement | Alignment, exhaustion, idempotent ownership, conflict, release, and status tests pass. F072/G0/H723 isolated images link with the shared manager. The H723 map proves the 2 KiB arena at DMA1-reachable AXI SRAM `0x24000000` rather than DTCM `0x20000000`; MPU setup covers exactly that power-of-two region. |
| ESP32 builds | component, component-RMT, and modem images compile and link with IDF 5.3.2 |
| ESP32 live acquisition | Lolin32 component image, GPIO32, 1 kscan/s, 16 values/block, isolated-lab trust-network WiFi/UDP: 47,072 scans in 2,942 consecutive blocks, `dropped=0`, `status=0`, clean stop |
| STM32F072 live acquisition | OAMS1 rev1.4.3, 16 MHz reference, Katapult at 8 KiB: 58,544 one-channel PC5 scans followed by 10,256 correctly interleaved PC5/internal-temperature scan pairs at 1 kscan/s; zero drops/faults and clean stops. The exact build is retained in the Helix CI compile matrix. |
| RP2040 merged-consumer boot | SKR Pico with consumers constructed as GPIO27, internal temperature, GPIO26 now emits physical order GPIO26, GPIO27, internal temperature. Klipper reached Ready with distinct bed, chamber, and MCU readings and no ADC fault; the former `channels must ascend` configuration shutdown is covered by regression. |
| STM32F072 v1 filtered gate | Standalone OAMS1 rev1.4.3 on PC5 with the FPS geometry: 5 ms physical scans, OSR 5, four filtered outputs per 100 ms Prompt report, raw output disabled. The first run exposed 16-scan DMA blocks crossing the 20-scan report boundary and producing an avoidable 80/160 ms host-delivery pattern. The adapter now selects 10-scan blocks. The corrected run delivered 250 consecutive epoch-1 summaries at steady 100 ms intervals from 5,000 physical scans, then stopped and restarted at summary sequence 0/epoch 2. A further 1,540 scans completed before clean stop; both status snapshots reported `dropped=0`, `status=0`. Summary machine-clock deltas were exactly 4,800,000 ticks at 48 MHz, each four-output report spanned 3,600,000 ticks, and the F0 backend truthfully reported its 240-tick inferred-start uncertainty. |
| STM32F072 polling/DMA profile | The archived legacy 8x/300 ms schedule used 53.33 timer callbacks/s for 26.67 conversions/s. Its equivalent distributed DMA schedule used 3.33 block publications/s, delivered 419 consecutive reports, and had zero drops/errors/overruns. A separate 1 ksample/s DMA stress delivered 581 blocks with the same zero-fault result. Exact counters and graphs are in the qualification paper. |
| STM32H723 hardware OSR | The MPU arena maps at DMA1-reachable AXI SRAM `0x24000000`. PA0 at 1 ktrigger/s and hardware OSR16 produced 802 consecutive 64-value blocks (821,248 physical conversions), zero drops/errors/overruns, and queue high-water one. A second 254-block run remained continuous while the 100 kHz/four-axis trajectory benchmark returned status 0. |
| RP2040 live acquisition | Direct-boot SKR Pico, GPIO27 thermistor, 1 ksample/s: 122 consecutive 64-value blocks (7,808 samples), raw range 3,773..3,881, zero drops/errors/overruns, queue high-water one, correct 200 MHz core/12 MHz scheduler reporting, and clean commanded stop. The test also caught and corrected an erroneous 16 KiB qualification-image offset before the live run. |
| RP2040 `MCU_adc` migration/restart | Forced compatibility migration of the connected GPIO27 thermistor selected an exact 800 scan/s hardware cadence, `input_div=30`, OSR8, 300 ms reports, and 60-value DMA blocks instead of overflowing `ADC_DIV`. Two successive Klippy hosts both reported 27.8..27.9 C; the second reinitialized the still-running DMA engine at epoch 2. Final status after 659 blocks: zero drops, DMA/ADC errors, overruns, telemetry drops, or watchdog events; ready high-water one; publication max 65 scheduler ticks and consumer max 2,188 ticks. A third fresh `HostSession` adopted the retained USB sequence and queried the counters without a cable cycle. |
| F767 Ethernet reuse | The combined ADC/RMII image links with one 16 KiB MPU arena at `0x20020000`. Ethernet descriptors and payloads allocate from `dma_resource`, RX publication uses `acq_ring`, compiled claims cover MAC/DMA, and explicit status counters replace the prior parallel static arena. This is map/build evidence; the board/PHY live contention gate remains open. |
| ESP32 placement guard | A fresh IDF 5.3.2 build places the shared arena in internal DRAM at `0x3ffb2800`; `DMA_ATTR` and `esp_ptr_dma_capable()` prevent DROM/PSRAM allocation. The earlier 47,072-scan Wi-Fi soak remains the live acquisition evidence. |

This checkpoint does **not** claim native-board analog accuracy/SNR/ENOB,
RP2040 physical motion/jitter or heater-fault injection, G0B1/F767 live
completion, ESP32 reconnect/cache-stall stress, external sample-aperture
measurement, or live Ethernet/ADC contention. Those remain gated below and
are not inferred from cross-builds.

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

The implemented base protocol remains versioned independently from legacy
`analog_in`:

```
config_adc_stream oid=%c
adc_stream_add_channel oid=%c pin=%u
adc_stream_start oid=%c clock=%u period_ticks=%u block_values=%c
                 traffic_class=%c
adc_stream_stop oid=%c
adc_stream_get_status oid=%c

adc_stream_data_telemetry oid=%c sequence=%u epoch=%u class=%c
    first_clock=%u period_num=%u period_den=%u uncertainty=%u
    channels=%c status=%u values=%*s
adc_stream_fault oid=%c status=%u dropped=%u sequence=%u
adc_stream_status oid=%c state=%c class=%c channels=%c block_values=%c
    epoch=%u sequence=%u dropped=%u status=%u
```

`ADC_STREAM_V1=1` adds the generic software-filter and capability surface:

```
adc_stream_subscribe oid=%c sub=%c channel=%c input_div=%hu osr=%hu
                     shift=%c report_div=%hu report_class=%c
adc_stream_set_options oid=%c raw_output=%c
adc_stream_get_capabilities oid=%c

adc_stream_prompt oid=%c sub=%c sequence=%u epoch=%u first_clock=%u
    last_clock=%u uncertainty=%u status=%u count=%hu min=%u max=%u
    sum_lo=%u sum_hi=%u shift=%c
adc_stream_telemetry ...same payload...
adc_stream_capabilities oid=%c version=%c max_channels=%c
    max_subscriptions=%c max_osr=%hu caps=%u
```

`input_div` is phase-locked to the acquisition epoch. `osr` accepted inputs
are accumulated in 64 bits; a non-zero `shift` uses round-half-up before the
right shift. `report_div` filtered values form one summary. The full 64-bit
summary sum is transported as low/high words. Class 1 and 2 use distinct
static message IDs. Class 0 is rejected by this command because its required
acknowledgement, deadline, and local failure-action contract is not yet
implemented. Raw block output is independently switchable.

The complete subscription/safety interface remains the target surface:

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

The target interface's exact field widths may change during implementation,
but these semantics do not. Its three summary messages share a payload schema
(`count`, `min`, `max`, 64-bit sum, format/shift, and continuity flags) but have
distinct command IDs
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

`ADC_DIV` is a 16.8 fixed-point counter clocked at 48 MHz, so its slowest
conversion is about 1.365 ms. Firmware advertises the equivalent bound as
`ADC_STREAM_MAX_SCAN_TICKS_PER_CHANNEL=16384`. The compatibility adapter does
not ask the backend to clip slower thermistor schedules: it chooses the
largest exact common scan divisor below that bound and increases each
subscription's phase-locked `input_div`. Thus physical DMA acquisition may be
faster than the legacy burst cadence while its accepted sample count, boxcar,
threshold evaluation, and report interval remain exact and deterministic.

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

- [x] Measure legacy timer invocations, conversion retries, hard-path CPU, and
  report rate for a representative thermistor; measure low-rate and high-rate
  DMA block/consumer cost with firmware instrumentation.
- [ ] Correlate ADC sample aperture and motion jitter with external
  instrumentation under simultaneous physical high-rate motion.
- [x] Add a host reference model for scan ordering, boxcar accumulation,
  rounding, decimation, timestamps, epochs, and injected missing samples.
- [x] Define v0 limits, status flags, ownership states, and stop-versus-shutdown
  failure behavior before live target testing.
- [x] Publish the per-target capability, ownership, fallback, timing-quality,
  and subscription policy model.

Gate: the baseline is reproducible and the reference model rejects every
silent gap or channel-phase discontinuity.

### Phase 1 - generic DMA/block and ADC cores

- [x] Implement the fixed-lifetime v0 block pool, generation-checked ownership
  state machine, counters, and no-overwrite failure path.
- [x] Generalize allocation and peripheral/DMA resource claims into the shared
  pool/resource manager required by Ethernet and multiple engines.
- [x] Implement ADC engine subscription merging, uniform scan planning,
  software filtering, and bounded Prompt/Telemetry summaries.
- [x] Add local threshold/deadline consumers and generalized raw
  chunking/fault-window capture.
- [x] Add the v0 protocol dictionary/commands and Klippy `[adc_stream]` raw
  acquisition frontend.
- [x] Add `MCU_adc` v1 capability negotiation, merged opt-in adaptation,
  split-ownership rejection, and automatic legacy fallback.
- [x] Enable capability-gated automatic adaptation for legacy `MCU_adc`
  consumers, including local heater range debounce, while retaining atomic
  fallback. Physical heater-fault injection remains a Phase-2 target gate.
- [x] Add distinct Class-0/1/2 report IDs, their bounded queues, Class-0
  acknowledgement/deadline feedback, and local deadline-failure actions.
- [x] Unit-test legal and stale block releases, interleaved multi-channel
  ordering, rational timestamps, sequence gaps, and bounded host-queue drops.
- [x] Add seeded randomized filter/decimation/reference-model tests.
- [x] Add randomized wrap/ring-full/restart/class-starvation tests for every
  target loss policy.

Gate: randomized simulated streams match the host reference bit-for-bit and no
injected discontinuity is reported as contiguous data.

### Phase 2 - RP2040 proof

- [x] Implement FIFO/DREQ, chained ping-pong DMA, error capture, scan order,
  pacing divider, and inferred-start uncertainty; cross-build the RP2040 image.
- [x] Run the direct-boot Pico raw thermistor continuity gate at 1 ksample/s,
  including exact block count, profile counters, clean stop, and zero faults.
- [x] Migrate V0 thermistor monitoring through the compatibility adapter,
  including exact slow-cadence decimation and live host restart.
- [ ] Test raw and decimated acquisition from DC, PWM+filter, and a known
  waveform while homing and high-rate motion execute.
- [ ] Compare interrupt count, CPU budget, sample timing, and motion jitter to
  Phase 0.

Gate: no per-sample scheduler timer remains, sample counts and order are exact,
all forced FIFO/DMA overruns are visible, and heater protections remain
equivalent or stronger.

### Phase 3 - STM32 proof

- [x] Implement and cross-build the v0 timer-TRGO/DMA backends for F072, G0B1,
  and H723, including H7 cache maintenance and DMA-reachable aligned storage.
- [x] Run an initial STM32F072 hardware soak over USB: one- and two-channel
  streams, exact channel interleaving, restart epoch, zero drops/faults, and
  clean stop. The measured 48 MHz clock was within about 22 ppm of nominal;
  the exact OAMS1 build configuration is retained in the CI compile matrix.
- [x] Run the v1 software-filter/subscription hardware gate on standalone
  STM32F072 with the FPS schedule; prove periodic Prompt delivery, exact
  summary clocks, restart epoch/sequence, truthful uncertainty, raw-output
  suppression, and zero drops/faults.
- [x] Add circular/native-double-buffer variants, generalized resource maps,
  and analog-watchdog coexistence.
- [x] Cross-build the F4/F7 software-OSR reference and run hardware OSR16 on
  H723 with exact counts and continuity.
- [ ] Compare software and hardware OSR against the same known analog waveform
  and archive SNR/SINAD evidence.
- [x] Prove the M7 MPU arena and H7 DMA-reachable-memory selection with caches
  enabled.
- [ ] Run STM32G0B1 and F767 live tests, including simultaneous motion/ADC and,
  once resumed, Ethernet/ADC DMA contention.

Gate: all compiled targets expose truthful capabilities; G0B1 and F767 show
continuous, accountable acquisition under load; cache-on and diagnostic
cache-off runs preserve ownership, sequence continuity, and agreement with the
same offline processing reference. Raw analog noise is not expected to be
byte-identical between runs.

### Phase 4 - ESP32 proof

- [x] Add IDF continuous mode for component and modem architectures while
  retaining an explicitly exclusive legacy oneshot compatibility path.
- [x] Implement ADC1-only conflict checks, non-overwriting internal DMA pool,
  cross-core block flow, pool-overflow reporting, and inferred timing.
- [x] Add a general I2S0 resource claimant and calibration metadata/voltage
  conversion.
- [ ] Measure APB-lock/sample timing under Wi-Fi and cache-disabled load.
- [ ] Test Wi-Fi reconnect, flash/cache-disabled intervals, pool exhaustion,
  raw telemetry throttling, and cross-core consumer delay.
- [x] Run a component-mode WiFi/UDP acquisition soak: 47,072 GPIO32 scans at
  1 kscan/s in 2,942 consecutive blocks, with zero MCU drops/status faults and
  a clean commanded stop.
- [x] Advertise inferred ESP32 timing and conservative start uncertainty rather
  than claiming STM32 timer-trigger quality.
- [ ] Measure sample aperture/rate error against external instrumentation and
  replace the conservative bound with evidence.

Gate: continuous Wi-Fi traffic and reconnects cause no silent ADC loss, motion
core starvation, or unreported pool overflow; calibrated and raw outputs are
both reproducible.

### Phase 5 - consumer migration and qualification paper

- [x] Migrate OpenAMS FPS as the first non-safety Prompt consumer, with an
  explicit opt-out and automatic legacy fallback.
- [x] Route compatible heaters, MCU temperature, ADC buttons, scaling, and
  other `MCU_adc` consumers through one capability-gated adapter; local analog
  trigger is available as a Class-0 action.
- [x] Preserve legacy fallback for unsupported boards and add a per-MCU
  diagnostic override.
- [x] Publish reproducible graphs comparing legacy polling versus DMA event
  rate, measured CPU slices, hard-path latency, timer error, and overrun
  behavior.
- [ ] Publish externally measured sample aperture/motion jitter and valid
  waveform-derived SNR/SINAD/ENOB; do not infer them from a grounded or floating
  input.
- [x] Archive exact configurations, live counters, CSV source data, and the
  dependency-free analysis script. Release images remain tied to their commits.

Gate: existing safety behavior passes regression tests; the published data
supports any efficiency or precision claim; unsupported MCUs do not regress.

### Phase 6 - unblock Ethernet

- [x] Reuse the proven DMA pool, resource manager, ownership barriers, block
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
