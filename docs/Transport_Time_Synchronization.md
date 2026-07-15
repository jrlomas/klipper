# Transport-Derived Machine-Time Synchronization

**Helix design and silicon capability study — July 2026**

Status: design proposal. USB Start-of-Frame discipline is implemented and
physically characterized. The CAN, Ethernet/PTP, WiFi/TSF, and dedicated-capture
profiles in this document are researched implementation candidates and must not
be reported as qualified until their acceptance tests pass.

## 1. Purpose

Helix defines machine time as the primary MCU's clock. Every secondary maintains
an affine mapping from that clock into its own execution timer:

```text
local_ticks = offset + rate * machine_ticks
```

The existing discipline filter, convergence gate, holdover behavior, and
Class-0 refusal policy do not need to change when the transport changes. What
does change is the quality of the observation pair used to update the mapping.

This document specifies the preferred observation source for:

- CAN and CAN-FD;
- wired Ethernet carrying IP/UDP;
- WiFi carrying IP/UDP; and
- a transport-independent dedicated synchronization signal.

It also records what the MCU families used by this project can actually capture
in hardware. A peripheral feature in a reference manual is not the same as a
feature implemented in Helix, and neither is the same as a physically qualified
timing profile.

## 2. Assurance vocabulary

Helix uses three distinct labels:

| Label | Meaning |
| --- | --- |
| Software-observed | The event time is read in an ISR, task, driver callback, or request/reply exchange. Interrupt and queue latency are part of the observation. |
| Hardware-referenced | A shared physical event is timestamped by a peripheral, PIO state machine, timer capture channel, or MAC close to the wire. Constant pipeline delay may remain and must be calibrated. |
| Hardware-bounded | The complete timestamp path has a documented and measured error bound that meets the application limit under load, restart, temperature, loss, and wraparound testing. |

The first two describe architecture. Only testing can establish the third.
Faster transport alone does not imply better time. A 1 Mbit/s CAN frame seen by
every receiver at one timestamped bus event can provide a stronger phase
observation than a 100 Mbit/s UDP packet whose one-way queue delay is unknown.

## 3. Common protocol requirements

Every transport profile must produce an observation with:

- a primary-machine-time value;
- the secondary-local time of the same physical or logically bounded event;
- an epoch and monotonically advancing sequence number;
- the timestamp source and its resolution;
- a validity/freshness indication; and
- enough diagnostics to retain raw phase residuals, rejected observations,
  holdovers, and consecutive-rejection counts.

The filter must never step an executing clock. It continues to adjust the
machine-to-local affine map, rejects isolated outliers, freewheels on the last
qualified rate for a bounded interval, and refuses new scheduled traffic after
the freshness budget expires.

Authentication is evaluated after the hardware timestamp is captured but before
the observation is admitted to the discipline filter. An authenticated payload
may refer to a previously captured timestamp; an unauthenticated or replayed
payload must not alter the clock map.

## 4. CAN: two-step, direct MCU-to-MCU broadcast

### 4.1 Mechanism

CAN already gives every attached node a common physical event. The host should
not relay that event between independent links.

```text
primary                   shared CAN bus                 secondaries
   |--- SYNC(epoch, seq) ------>|---------------------------->|
   |  capture actual TX event   |       capture same RX event |
   |--- FOLLOW_UP(epoch, seq, primary_tx_clock) ------------->|
```

The first frame is deliberately timestamp-free. Arbitration determines when it
actually reaches the bus, so the primary does not know the authoritative
transmit timestamp when it queues the payload. After successful transmission it
sends a follow-up containing the timestamp captured for that exact `SYNC`
sequence. This is the same reason a two-step PTP clock sends `Sync` followed by
`Follow_Up`.

The secondary joins:

```text
(primary machine clock at SYNC transmit,
 secondary local clock at SYNC receive)
```

and feeds the pair to the existing discipline filter. Cable propagation over a
printer-scale harness is small, common reception removes host scheduling, and
CAN arbitration delay is no longer an offset error because actual transmission
is timestamped. A dedicated high-priority standard identifier should be
reserved for `SYNC`; `FOLLOW_UP` may use a lower priority because its delivery
latency does not change the already captured pair.

Classical CAN's eight-byte payload is sufficient for a compact type/epoch/seq
`SYNC` and a separate type/seq/32-bit-machine-clock `FOLLOW_UP`. Exact identifier
and payload allocation belongs in `CANBUS_protocol.md` when implementation
starts. The existing `0x3f0` administration identifier should not be silently
repurposed.

### 4.2 STM32 capability

There are two relevant ST CAN controller generations.

**FDCAN — STM32G0B1, STM32H7, and similar parts.** The FDCAN message RAM stores
an RX timestamp in received elements, and its optional Tx Event FIFO stores a
timestamp plus a message marker for successfully transmitted elements. The
timestamp counter is configurable and the event marker can associate the
captured transmit time with a `SYNC` sequence. ST's FDCAN application note
describes both RX timestamps and the Tx Event FIFO. The G0B1 reference manual
exposes a 16-bit timestamp counter, prescaler, wrap interrupt, RX timestamp
field, and Tx Event FIFO.

This is a strong match for the EBB36's STM32G0B1. Work still required in Helix:

- enable and extend the wrapping FDCAN timestamp counter;
- retain the RX timestamp currently discarded by `src/stm32/fdcan.c`;
- enable Tx Event FIFO storage and correlate its message marker;
- map the FDCAN counter to the Klipper execution timer without an ISR-time
  substitution; and
- remove or bound any global interrupt masking that can delay bookkeeping,
  even though the peripheral timestamp itself survives that delay.

**bxCAN — STM32F072, STM32F407/F4, and STM32F7.** bxCAN's time-triggered
communication mode exposes 16-bit message timestamp fields in both receive and
transmit data-time registers. The STM32F072 and STM32F407 reference manuals each
document this mode. The field is compact and wraps quickly, so firmware must
extend it and reject ambiguous sequence gaps. The current `src/stm32/can.c`
driver does not consume these fields.

For the current boards:

- EBB36 / STM32G0B1: preferred CAN timestamp target; native FDCAN hardware.
- OAMS / STM32F072: feasible with bxCAN TTCM, but the 16 KiB RAM and code-size
  budget make a small fixed implementation mandatory.
- STM32F407/F765 network targets: bxCAN timestamping is available alongside
  their Ethernet capability.
- STM32H723: native FDCAN and the best combined CAN/Ethernet candidate, although
  the present FK723 board was qualified only for computation and USB.

### 4.3 RP2040 capability

RP2040 has no fixed-function CAN controller. Helix already implements CAN with
`can2040`, using four PIO state machines and 32 PIO clocks per CAN bit. PIO is a
deterministic peripheral with independent state machines, FIFOs, and DMA
requests, but the current `can2040` callback reports a parsed frame without a
wire timestamp.

A qualified RP2040 CAN profile therefore requires a PIO extension. Two viable
designs should be prototyped:

1. timestamp every CAN start-of-frame in the unused PIO block with a free-running
   PIO counter, then associate it with the subsequently parsed frame; or
2. have a PIO start-of-frame token trigger a DMA snapshot path and characterize
   the fixed plus arbitration latency into the RP2040 hardware timer.

The PIO clock and the Helix timer are derived from board clock domains and can
be mapped at startup. A normal PIO IRQ followed by `timer_read_time()` is only
an ISR-entry observation and cannot receive the hardware-bounded label without
separate proof. In this firmware, the RP2040's 64-bit timer is driven from the
12 MHz crystal-derived watchdog tick and `timer_read_time()` reads its low word
directly. The independently configured processor/peripheral clock runs at
200 MHz and is reported separately as `MCU_CORE_FREQ`; it is not the scheduling
timebase.

### 4.4 ESP32 capability

The classic ESP32 used by the Lolin32 has an on-chip TWAI controller. In the
project's pinned ESP-IDF v5.3.2 API, `twai_message_t` does not carry a
peripheral RX timestamp, so the available observation is an ISR/driver time.
Newer ESP-IDF TWAI APIs can attach a 64-bit receive timestamp, but that API must
not be assumed to be a wire-level hardware capture on the classic part without
silicon-specific documentation and measurement.

Consequently:

- classic Lolin32 CAN can provide a statistically qualified direct-broadcast
  profile using an early ISR timestamp;
- it cannot inherit the STM32 FDCAN hardware-bound claim;
- an external timestamp-capable CAN controller or dedicated capture signal is
  required for a strong bound; and
- future ESP variants must be evaluated by exact SoC and IDF version, not by
  the generic "ESP32" name.

### 4.5 CAN acceptance test

The CAN profile is not complete until all of the following pass:

1. Compare peripheral/PIO timestamps with simultaneous analyzer captures of
   the actual CAN start-of-frame and two scheduled GPIO outputs.
2. Repeat at idle, 50 percent, and at least 80 percent bus utilization, including
   deliberate higher-priority arbitration ahead of `SYNC`.
3. Exercise timestamp wrap, dropped `FOLLOW_UP`, duplicate/replayed sequences,
   bus-off recovery, MCU restart, and primary epoch change.
4. Run the trajectory solver concurrently on every motion MCU.
5. Repeat cold and at representative chamber/toolhead temperatures.
6. Retain mean, standard deviation, extrema, timestamp resolution, rejection
   count, convergence time, and the conservative physical bound.

## 5. Wired Ethernet and UDP: hardware PTP

### 5.1 Mechanism

For wired Ethernet, Helix should use a small IEEE-1588/PTP-style profile over
UDP multicast or native PTP Ethernet frames:

```text
master -> all:       Sync(sequence)
master -> all:       Follow_Up(sequence, captured master TX time)
secondary -> master: Delay_Req(sequence, captured secondary TX time)
master -> secondary: Delay_Resp(sequence, captured master RX time)
```

Hardware MAC transmit and receive timestamps remove the IP stack, scheduler,
driver queue, and DMA completion latency from the observation. The delay
exchange estimates path delay; switched networks should use a direct link or a
PTP-aware transparent/boundary switch if a hard bound is required.

Helix does not need UTC. The primary maps the MAC's PTP clock to its monotonic
machine clock and places the corresponding machine timestamp in `Follow_Up`.
Secondaries map their hardware RX clock to their execution timer and update the
same affine discipline used by USB and CAN.

The authenticated Helix envelope must bind epoch, sequence, message type, and
timestamp contents. Hardware capture occurs before authentication, but the
captured event is ignored unless the matching message authenticates and passes
replay checks.

### 5.2 STM32 capability

The STM32F407 and STM32F765 Ethernet MACs support IEEE-1588 hardware
timestamping over MII/RMII. STM32H723's newer Ethernet MAC also documents IEEE
1588 timestamp support, PTP packet filtering/offload, target-time events, and
PPS generation.

Repository status is narrower than the silicon:

- `src/stm32/eth_mac.c` currently supports STM32F4/F7 native RMII transport but
  does not configure its PTP registers or retain DMA descriptor timestamps.
- STM32H723 has the required registers in its device headers, but the current
  Helix native Ethernet driver is not enabled for H7.
- STM32G0B1 and STM32F072 have no integrated Ethernet MAC. They require an
  external MAC/controller that exports hardware timestamps; an ordinary
  packet-only SPI Ethernet interface cannot create a MAC-edge timestamp after
  the fact.

This makes STM32F407/F765 the shortest route to a wired prototype and STM32H723
the strongest next-board architecture once its Ethernet driver and board pinout
are implemented.

### 5.3 RP2040 capability

RP2040 has no Ethernet MAC. UDP is possible through an external SPI or PIO
interface, but hardware PTP requires the external controller to expose TX/RX
timestamps and a clock that can be mapped into the RP2040 timer. A packet-only
controller followed by a software callback is the four-timestamp fallback, not
hardware PTP.

The RP2040's deterministic PIO can implement link protocols, but implementing
a complete timestamping Ethernet MAC in PIO is substantially more work and
risk than choosing a timestamp-capable external MAC or an MCU with native PTP.

### 5.4 ESP32 capability

The classic ESP32's integrated EMAC does not provide usable hardware timestamp
support. Espressif explicitly clarified that the earlier IEEE-1588 claim for
that part was an error and that the classic MAC lacks hardware timestamping.
Therefore the current Lolin32 cannot qualify wired PTP even if a PHY is added.

ESP32-P4 is materially different. Its current ESP-IDF Ethernet driver exposes
hardware TX/RX timestamps, PTP clock adjustment, target-time callbacks, and a
PTP example. Espressif currently labels the timestamp API experimental, and PPS
output is available only on later silicon revisions. ESP32-P4 is a valid future
wired-network timing candidate, but it is not the classic Lolin32 and it is not
part of the present pinned v5.3.2 modem qualification.

### 5.5 Ethernet acceptance test

Qualification must cover direct cable and the intended switch topology, with
idle and saturated best-effort traffic. It must compare MAC timestamps with a
logic-analyzer or scope reference, measure delay asymmetry, exercise link
renegotiation and switch restart, and repeat across temperature. A switch path
is qualified as part of the machine; changing it invalidates a hard bound unless
the new path is independently characterized.

## 6. WiFi and UDP

WiFi has retransmission, contention, access-point scheduling, driver queues,
and power-management behavior. A UDP packet timestamped in a task or socket
callback cannot by itself prove one-way delay. Helix therefore defines a
baseline operational profile and a stronger ESP32-specific candidate.

### 6.1 Baseline: authenticated four-timestamp exchange

The portable fallback is NTP/PTP-shaped request/reply timing:

```text
t1: primary/host sends request
t2: secondary receives request
t3: secondary sends response
t4: primary/host receives response
```

Repeated samples feed a minimum-delay filter and robust affine regression.
Helix must expose the measured round-trip interval, reject congestion outliers,
use oscillator holdover, and revoke convergence after the freshness budget.
This profile can be statistically qualified for a printer topology, but it is
not hardware-bounded because forward and reverse delay asymmetry is unknown.

The existing authenticated datagram session already supplies identity, replay
protection, and integrity. Synchronization samples should be a distinct
traffic class, should not wait behind FEC recovery, and should carry their own
epoch and sequence.

### 6.2 ESP32 WiFi TSF candidate

ESP-IDF v5.3.2 exposes `esp_wifi_get_tsf_time()` on the classic ESP32. It returns
zero until a station is connected and has received an AP beacon; the official
API also warns that power-save settings can make the value inaccurate. TSF is
therefore a promising common WiFi reference, not an automatic precision claim.

For the Lolin32 modem architecture:

1. WiFi core 0 brackets a TSF read with the closest available local hardware
   timer reads.
2. It publishes `(TSF, local_timer, bracket_width, BSSID, channel, epoch)`
   through the shared-memory ring.
3. The bare Klipper core fits TSF to its execution clock and rejects wide or
   non-monotonic brackets.
4. AP disconnect, BSSID change, TSF discontinuity, or unacceptable power-save
   state invalidates convergence immediately.

If every motion MCU is an ESP32 station on the same AP and the primary also
maps machine time to that TSF domain, TSF can replace UDP arrival time as the
shared observation. On the current mixed Pico-plus-ESP32 topology, TSF alone
does not reveal the Pico's machine time. A gateway still needs a qualified
Pico-to-TSF mapping through CAN, a capture wire, USB/host relay, or another
shared hardware event.

TSF qualification must compare multiple ESP32 boards against a physical sync
reference while varying AP load, packet loss, RSSI, channel contention,
power-save policy, reconnect, roaming/BSSID change, and temperature. Until that
test exists, TSF remains an experimental hardware-assisted profile.

### 6.3 STM32 and RP2040 over WiFi

Neither STM32 nor RP2040 gains a hardware timestamp merely because a WiFi
module carries its packets. A modem can improve synchronization only if it
exports a timestamp reference together with a tightly paired MCU-local time.
Otherwise the board uses the authenticated four-timestamp fallback and receives
the same statistical assurance label as any other software-timestamped WiFi
endpoint.

## 7. Dedicated capture signal

A single extra signal is the transport-independent reference profile. The
primary emits a periodic edge at a scheduled machine time; every secondary
captures that edge in hardware and uses transport messages only to associate
epoch and sequence.

```text
primary timer output  --------------------> secondary capture inputs
CAN/UDP follow-up      ---- epoch, seq, primary edge clock ---->
```

This does not require a continuous clock. A low-rate pulse supplies phase and
rate observations while the normal affine map freewheels between edges.

### STM32

General-purpose STM32 timers provide input-capture channels, and all STM32
families in scope have suitable timer instances. The decisive constraint is
board routing: the selected connector pin must map to a timer channel whose
counter can be related to the Helix execution timer. The existing
`src/stm32/timer_capture.c` only implements a subset of F0/G0 TIM2 routes. The
EBB36 PB8 experiment used EXTI ISR-entry timing because that pin was not routed
to the implemented TIM2 capture path.

### RP2040

RP2040's system timer has no GPIO input-capture route. Its IO_BANK0 edge latch
plus ISR is the current implementation and remains load-sensitive. A dedicated
PIO state machine can instead wait on the sync pin and push a deterministic
counter value; the PIO counter is then mapped to the RP2040 timer. This is the
preferred custom-board approach and must be analyzer-qualified.

### ESP32

ESP32's MCPWM capture peripheral provides a capture timer and edge channels,
making it the preferred dedicated-wire timestamp path on the classic Lolin32.
The capture value must be paired with the Helix execution timer and transported
from the IDF core to the bare Klipper core without substituting callback time.

## 8. Target capability matrix

| Target | CAN observation | Wired Ethernet/PTP | WiFi/TSF | Dedicated edge | Recommended profile |
| --- | --- | --- | --- | --- | --- |
| EBB36 STM32G0B1 | Native FDCAN RX + Tx-event timestamp; driver work required | No native MAC | External modem only | Timer capture if a valid channel is routed; PB8 currently EXTI only | Direct FDCAN two-step broadcast |
| OAMS STM32F072 | bxCAN TTCM 16-bit timestamp; driver work required | No native MAC | External modem only | Native timer input capture where routed | Compact bxCAN profile or capture wire |
| STM32F407 | bxCAN TTCM | Native IEEE-1588 MAC; current driver lacks PTP | External modem only | Native timer input capture | Hardware PTP for Ethernet, bxCAN for CAN |
| STM32F765 | bxCAN TTCM | Native IEEE-1588v2 MAC; current driver lacks PTP | External modem only | Native timer input capture | Hardware PTP for Ethernet, bxCAN for CAN |
| STM32H723 | Native FDCAN timestamps | Native IEEE-1588 MAC; H7 driver port required | External modem only | Native timer input capture | Best future combined CAN/Ethernet MCU |
| SKR Pico RP2040 | PIO CAN works; no timestamp exported yet | External timestamp-capable MAC required | External modem only | PIO capture preferred; GPIO ISR is weaker | PIO CAN timestamp prototype or capture wire |
| Lolin32 classic ESP32 | TWAI callback/ISR timestamp in pinned IDF | Classic EMAC has no hardware timestamp | Native TSF API, requires qualification | MCPWM capture | TSF experiment plus four-timestamp fallback |
| Future ESP32-P4 | Evaluate exact TWAI block/driver | Hardware PTP supported by current IDF | Requires external WiFi companion | Hardware capture peripherals | Wired PTP candidate, not a Lolin32 replacement assumption |

## 9. Recommended implementation order

1. **CAN on STM32G0B1.** Add FDCAN RX and Tx Event FIFO timestamps, implement
   compact two-step frames, and compare against the existing scope harness.
2. **RP2040 CAN timestamp.** Extend `can2040` with a PIO-derived start-of-frame
   timestamp so a Pico can participate without weakening the fleet label.
3. **Classic ESP32 TSF experiment.** Export bracketed TSF/local-clock pairs from
   the modem core and compare two stations plus the Pico against a wire capture.
4. **STM32F4/F7 PTP prototype.** Preserve DMA descriptor timestamps in the
   native RMII driver and implement the four PTP event messages.
5. **Dedicated capture profile.** Specify the connector/pin requirement for new
   boards as the universal high-assurance fallback.
6. **H723 or ESP32-P4 next-board work.** Select by product need: H723 for one MCU
   combining trajectory compute, FDCAN, and Ethernet PTP; ESP32-P4 when a
   network-centric application justifies its different architecture.

## 10. Relationship to current USB qualification

USB SOF, CAN start-of-frame, Ethernet MAC timestamps, WiFi TSF, and a dedicated
capture pulse are all observation sources for the same machine-time model. They
do not create separate motion protocols.

The current USB result remains valid for the Pico-XY plus EBB36-extruder V0:
matched idle SOFs were exceptionally repeatable, while loaded STM32 timer
dispatch delayed isolated ISR-entry observations. The production filter rejects
those samples and holds over the qualified oscillator map. CAN FDCAN timestamps
or Ethernet MAC timestamps would survive delayed CPU service because the event
time is stored by the peripheral. That is the architectural improvement these
profiles seek to verify.

## 11. Primary sources and repository evidence

- STMicroelectronics, [AN5348: Introduction to FDCAN peripherals for STM32 MCUs](https://www.st.com/resource/en/application_note/dm00625700-fdcan-peripheral-on-stm32-devices-stmicroelectronics.pdf).
- STMicroelectronics, [RM0444: STM32G0x1 reference manual](https://www.st.com/resource/en/reference_manual/rm0444-stm32g0x1-advanced-armbased-32bit-mcus-stmicroelectronics.pdf).
- STMicroelectronics, [RM0091: STM32F0x1/F0x2/F0x8 reference manual](https://www.st.com/resource/en/reference_manual/dm00031936.pdf).
- STMicroelectronics, [RM0090: STM32F405/407 reference manual](https://www.st.com/resource/en/reference_manual/dm00031020.pdf).
- STMicroelectronics, [RM0468: STM32H723/733/725/735 reference manual](https://www.st.com/resource/en/reference_manual/dm00603761.pdf).
- STMicroelectronics, [STM32 Ethernet and IEEE-1588 overview](https://www.st.com/en/applications/connectivity/ethernet.html).
- STMicroelectronics, [STM32F765 product capabilities](https://www.st.com/en/microcontrollers-microprocessors/stm32f765ng.html).
- Raspberry Pi, [RP2040 specifications and datasheet](https://www.raspberrypi.com/products/rp2040/specifications/).
- Raspberry Pi, [Pico SDK hardware timer, PIO, DMA, and IRQ APIs](https://www.raspberrypi.com/documentation/pico-sdk/hardware.html).
- Espressif, [ESP-IDF v5.3.2 WiFi TSF API](https://docs.espressif.com/projects/esp-idf/en/v5.3.2/esp32/api-reference/network/esp_wifi.html).
- Espressif, [classic ESP32 hardware-PTP clarification](https://github.com/espressif/esp-idf/issues/13423).
- Espressif, [ESP32-P4 EMAC hardware timestamping and PTP](https://docs.espressif.com/projects/esp-idf/en/stable/esp32p4/api-reference/network/esp_eth.html).
- Espressif, [ESP32 MCPWM capture API](https://docs.espressif.com/projects/esp-idf/en/v5.3.2/esp32/api-reference/peripherals/mcpwm.html).
- Repository evidence: `src/stm32/fdcan.c`, `src/stm32/can.c`,
  `src/stm32/eth_mac.c`, `src/stm32/timer_capture.c`, `src/rp2040/can.c`,
  `lib/can2040/can2040.c`, `src/rp2040/timer.c`, and
  `src/rp2040/gpio_irq.c`.

See also [the machine-time qualification record](Machine_Time_Qualification.md),
[the measured white paper](Machine_Time_White_Paper.md), and
[FD-0001's time model](founding/0001-motion-intentions/01-Time_Model.md).
