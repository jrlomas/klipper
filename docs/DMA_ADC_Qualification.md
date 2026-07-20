# DMA ADC Acquisition: Architecture and Qualification

## Executive result

HELIX now has one bounded DMA acquisition architecture for ADC, Ethernet, and
future capture peripherals. It replaces one scheduler callback per ADC
conversion attempt with hardware pacing and one firmware publication per
completed block. The change is most valuable when conversion rate rises,
multiple consumers share an ADC, exact sample phase matters, or a local safety
decision must survive host/transport delay.

The measurements do **not** say that legacy polling is unusable for a single
slow thermistor. On the STM32F072, its measured hard-path CPU slice was only
0.0078%. They do show the structural scaling difference:

* the legacy eight-sample/300 ms thermistor schedule invoked the timer callback
  53.33 times/s for 26.67 conversions/s;
* the equivalent distributed DMA schedule published 3.33 blocks/s, a 16x
  reduction in firmware scheduling events, with the same sample count, report
  deadline, and averaging operation; and
* a 1,000-conversion/s DMA stress run still needed only 15.625 block
  publications/s, fewer events than the 26.67-conversion/s legacy case.

All reported live DMA runs had zero drops, DMA errors, ADC errors, and
overruns, with a ready-queue high-water mark of one block. The standalone H723
also completed a 100 kHz, four-axis synthetic trajectory-solver benchmark
while its ADC produced hardware-oversampled blocks without a continuity fault.

![Scheduler and IRQ event rate](img/adc-dma-event-rate.svg)

## What changed architecturally

The old and new paths are different in where periodic work occurs:

```text
legacy
  MCU scheduler timer -> start conversion -> scheduler retry -> read sample
                      -> repeat sample_count times -> task/report

DMA
  hardware timer -> ADC sequencer -> DMA circular/double buffer
                                -> half/block IRQ -> bounded ready queue
                                -> filter/local safety/report task
```

The common implementation has five explicit contracts:

1. `dma_resource` owns a fixed DMA-reachable arena and rejects conflicting
   peripheral, timer, channel/stream, and DMAMUX claims.
2. `acq_block` and `acq_ring` enforce generation-checked ownership and bounded
   no-overwrite queues.
3. The board backend owns only register-level pacing, sequencing, DMA, and
   error collection. It publishes truthful rate, resolution, oversampling,
   watchdog, and timing capabilities.
4. The generic ADC engine owns logical subscriptions, deterministic filtering,
   capture windows, Class-0/1/2 queues, acknowledgements, deadlines, and local
   HOLD/TRIGGER/SHUTDOWN actions.
5. Klippy merges compatible legacy ADC consumers into one physical schedule
   and falls back before MCU configuration when a target or consumer cannot
   preserve its required semantics.

No host command can DMA an arbitrary address. Every endpoint and DMA request
is compiled into its board backend.

## Direct measurements

The exact source data is
[adc_dma_qualification.csv](data/adc_dma_qualification.csv). The figures are
regenerated without third-party Python packages by running:

```shell
scripts/analyze_adc_dma.py
```

| Target and workload | Physical conversions/s | Scheduler or block events/s | Mean hard path | Maximum hard path | Measured hard-path CPU | Measured deferred CPU |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| F072 legacy, 8 samples/300 ms | 26.67 | 53.33 | 1.462 us | 2.313 us | 0.00780% | not instrumented |
| F072 DMA, 1 ksample/s, SW OSR8 | 1,000 | 15.625 | 29.516 us | 30.083 us | 0.04612% | 1.77937% |
| F072 DMA, distributed 8/300 ms | 26.67 | 3.333 | 29.421 us | 29.958 us | 0.00981% | 0.06984% |
| RP2040 DMA, 1 ksample/s | 1,000 | 15.625 | 4.781 us | 5.167 us | 0.00747% | 0.09117% |
| RP2040 migrated thermistor + host restart | 800 | 13.333 | 5.093 us | 5.417 us | 0.00679% | 0.22089% |
| H723 DMA, 1 ksample/s, HW OSR16 | 16,000 | 15.625 | 0.528 us | 0.888 us | 0.00083% | 0.03279% |
| H723 DMA plus solver load | 16,000 | 15.625 | 0.487 us | 0.587 us | 0.00076% | 0.03241% |

“Physical conversions” counts all conversions accumulated by hardware
oversampling. One H723 trigger produced one published result from 16 physical
conversions. “Hard path” is the legacy timer callback or DMA block-publication
IRQ. The DMA deferred measurement includes block filtering and serial-message
enqueue. The legacy instrumentation does not include its report task, so the
total-CPU columns are intentionally not presented as a perfectly symmetric
benchmark.

![Measured firmware CPU slices](img/adc-dma-measured-cpu.svg)

![Mean hard-path service time](img/adc-dma-hardpath-latency.svg)

The F072 DMA hard path is slower per event because its simple DMA controller is
stopped, accounted, and restarted at a block boundary. It nevertheless occurs
once per block rather than twice per conversion. The H723 native stream and
520 MHz core make that same publication sub-microsecond.

## Cadence compatibility

Legacy `analog_in` normally takes a short sample burst and then rests. A
uniform shared ADC stream cannot reproduce unrelated per-channel burst gaps
without repeatedly stopping and reprogramming the ADC. Continuous emulation at
the shortest legacy `sample_time` was functionally correct but wasteful: an
8x1 ms/300 ms thermistor would force 1,000 raw conversions/s even though only
26.67/s contribute to reports.

The compatibility adapter now distributes each consumer's `sample_count`
evenly across its `report_time`. For the thermistor example it samples every
37.5 ms, accumulates eight readings, and reports every 300 ms. Integer MCU
clock periods are reduced to a common greatest divisor across all channels.
The selected block size divides every report cycle, and `summary_mode: latest`
preserves one legacy callback batch per report instead of aggregating several
batches.

This is a deliberate semantic choice for slowly changing temperature,
pressure, status, and button-divider inputs. A consumer that requires a burst
aperture, multiple legacy batches, an unrepresentable divisor, or an aperture
longer than its distributed interval remains on the legacy implementation.
Fallback happens before either implementation claims the ADC.

The live F072 distributed schedule produced 419 consecutive 300 ms reports.
Its measured report-clock delta was 14,400,064 ticks instead of 14,400,000 at
48 MHz, a +4.44 ppm integer-timer error. The 1 ksample/s F072 and H723 schedules
were exact at the recorded clock resolution. Actual legacy aperture jitter was
not instrumented in this data set and is not assigned an invented value.

## Overrun and failure behavior

DMA does not make overload impossible; it makes overload bounded and visible.
Each completed block moves through:

```text
FREE -> DMA_OWNED -> READY -> CONSUMER_OWNED -> FREE
```

A generation mismatch, ready-ring exhaustion, backend pool overflow, DMA
transfer error, ADC overrun, or sequence discontinuity increments a distinct
counter and marks the stream discontinuous. Telemetry may be dropped with a
counter. Prompt traffic faults the stream. Critical traffic additionally
invokes its local policy, independent of Python or transport state.

Randomized 100,000-operation ownership/ring tests cover wrap, full, stale
release, and restart behavior. Separate randomized safety tests cover Class-0
acknowledgement wrap, starvation, threshold debounce, and local action. Raw
pre/post blocks are retained in a bounded capture window and transported in
16-byte chunks, so increasing the maximum block to 64 values does not exceed a
wire frame.

## Target results

### STM32F0/G0/F4/F7/H7

F0 and G0 use true circular DMA half/full interrupts. F4 and F7 use native
double-buffer mode. H7 uses DMA1/DMAMUX and a non-cacheable MPU arena in
DMA1-reachable AXI SRAM. F4/F7 software oversampling remains the reference
path. G0/H7 expose hardware oversampling only when the exact target implements
it, and analog-watchdog thresholds coexist with the regular DMA sequence.

The H723 linker map places the 2 KiB arena at `0x24000000`, outside DTCM. The
live hardware-OSR16 run produced 802 blocks and 51,328 published samples from
821,248 physical conversions, with zero faults. The capability reports the
native 12-bit converter resolution; retained oversampling accumulator bits do
not falsely change that native-resolution field.

The first live G0 OSR16 run also showed why digital continuity alone is not an
analog qualification. The EBB36 PA3 thermistor remained near ambient, but the
EBB36 and bridge internal-temperature channels both moved from the 30 C range
to about 50 C. Hardware oversampling changes one slow conversion into a burst
of back-to-back conversions; the G0 internal temperature source cannot settle
through the 39.5-cycle aperture used for external pins. The stream setup also
must not erase the ADC `CKMODE` selected before calibration. The corrected G0
path preserves `CKMODE`, keeps external channels at 39.5 cycles, and selects a
160.5-cycle aperture for internal channel 12. With OSR16 and the existing
eight-value software average still enabled, a clean simultaneous restart read
34.6 C on the EBB36, 31.0 C on the bridge, and 25.2 C on PA3, with Klipper
Ready and no stream fault or fallback. These values restore agreement with the
pre-DMA temperature range; they are a sanity check, not a calibrated accuracy
claim.

The F767 Ethernet build uses the same manager—not a parallel descriptor
allocator—for four RX descriptors/buffers, two TX descriptors/buffers, and ADC
storage. Its 16 KiB non-cacheable arena is at `0x20020000`; compiled claims
cover ETH MAC, ETH DMA, ADC, timer, and stream resources. RX completion is IRQ
published into `acq_ring`, and status exposes frames, queue high-water,
overruns, DMA errors, pool size, and pool use.

### RP2040

The backend uses the ADC FIFO/DREQ and chained DMA channels 10 and 11. It has
no per-sample scheduler callback and reports FIFO errors as discontinuities.
The direct-boot SKR Pico image reported its 200 MHz core and 12 MHz scheduler,
then sampled the connected GPIO27 thermistor at 1 ksample/s. It delivered 122
consecutive 64-value blocks (7,808 samples), with raw codes 3,773 through
3,881, ready high-water one, and zero drops, DMA errors, ADC errors, or
overruns. The profile above uses scheduler-clock ticks; the faster core is not
misrepresented as the profiling clock.

An incorrect qualification configuration initially selected a 16 KiB
Katapult application offset even though the printer's archived images were
direct-boot. The RP2040 ROM remained recoverable through the documented BOOT
jumper plus RESET sequence, and the corrected direct UF2 image was flashed.
The retained test configuration now locks that boot choice. Physical
DC/PWM/waveform accuracy and motion-concurrency fixtures remain separate open
gates; this thermistor run is functional continuity evidence, not SNR data.

The forced `MCU_adc` migration gate then exercised the normal Klippy consumer,
not the raw qualification helper. The first attempt truthfully failed because
the 37.5 ms distributed sample interval exceeded the RP2040 ADC divider. The
firmware now advertises 16,384 scheduler ticks per channel as its maximum scan
period; the host selected the largest exact common divisor, a 15,000-tick
(800 scan/s) cadence, and `input_div=30` retained eight accepted samples and
one report every 300 ms. Two successive Klippy processes reported the GPIO27
thermistor at 27.8..27.9 C. The second process stopped and rearmed the retained
DMA engine at epoch 2 rather than failing on a pending asynchronous abort.

After 659 epoch-2 blocks (39,540 conversions, 49.425 s), status reported zero
drops, DMA/ADC errors, overruns, telemetry drops, or watchdog events and ready
high-water one. Publication cost was 40,276 scheduler ticks total, 65 maximum;
consumer cost was 1,310,157 ticks total, 2,188 maximum. A third independently
created `HostSession` attached to the still-running application, adopted its
retained sequence only after the guarded repeated-NAK bootstrap, and read the
same status without resetting or cycling USB. This validates compatibility
migration and restart lifecycle, but still is not analog-accuracy evidence.

### ESP32

Classic ESP32 uses IDF 5.3.2 continuous ADC1/I2S0 DMA with a fixed
non-overwriting driver pool and cross-core block handoff. ADC2 is rejected
while Wi-Fi is in scope. Raw codes carry calibration metadata; voltage
conversion uses IDF line-fitting when eFuse/default calibration permits it.

A linker-map audit found the generic arena initially orphaned into flash DROM.
It now uses IDF's `DMA_ATTR`, maps at `0x3ffb2800` in internal DRAM in the
qualified component image, and checks `esp_ptr_dma_capable()` at every
allocation boundary. A future linker change therefore fails configuration
closed instead of giving a peripheral a flash or PSRAM pointer.

## Analog accuracy and ENOB boundary

Oversampling reduces uncorrelated noise; it does not correct gain, offset,
reference error, source impedance, settling, or nonlinearity. The current live
records prove scheduling, ownership, filtering, hardware-oversampling
operation, and loss accounting. PC5 on the F072 fixture was at zero and PA0 on
the H723 was floating, so neither is a valid SNR or effective-number-of-bits
fixture.

HELIX therefore makes no new SNR/ENOB claim from these runs. The remaining
accuracy fixture must provide a documented DC source and a low-distortion
waveform, archive raw unfiltered codes, compare software and hardware OSR
against the same reference, and record source/reference uncertainty. ENOB is
then derived from SINAD, not from an attractive but invalid peak-to-peak number.

## Conclusion

The architectural case for DMA is bounded scaling and deterministic ownership,
not a claim that every old thermistor poll consumed excessive CPU. At low rate,
the two implementations are both inexpensive. As acquisition rate rises,
legacy scheduling grows with conversion attempts while DMA scheduling grows
with completed blocks. More importantly, DMA provides a hardware sample clock,
one shared sequencer, explicit continuity, local failure policy, retained fault
evidence, and a resource model that Ethernet can reuse.

Those properties are what make high-rate analog sensing and networked motion
controllers coexist without silently turning every ADC sample into another
motion-scheduler event.
