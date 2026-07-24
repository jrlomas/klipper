# FD-0001: STM32F767 Ethernet Reference Board Plan

Status: native NUCLEO-F767ZI RMII, HSE-bypass clocking, cache-safe DMA,
authenticated sessions, DHCP, reset/reconnect, and sustained Klipper command
traffic are physically qualified. The ADC concurrency, one-hour motion/load,
link-flap, FEC, and IEEE 1588 hardware-timestamp gates remain open.

The unified DMA ownership, block-stream, ADC acquisition, and oversampling
substrate is specified in
[17-DMA_ADC_Acquisition.md](17-DMA_ADC_Acquisition.md) and must land before
this board's Ethernet driver is converted to IRQ-driven DMA service.

This document turns the ST NUCLEO-F767ZI into the HELIX reference platform
for native 10/100 Ethernet. It is intentionally more than a board enablement
note: the target must establish a reusable STM32 contract for cache-coherent
DMA, interrupt-driven packet service, hardware receive/transmit timestamps,
runtime network provisioning, and timer-triggered ADC collection.

The board is not an FDCAN reference. The STM32F767 provides three bxCAN 2.0B
controllers, not FDCAN. CAN FD/BRS remains qualified on separate STM32G0/H7
targets and joins this board through HELIX's heterogeneous-fleet model
([14-Heterogeneous_Fleets.md](14-Heterogeneous_Fleets.md)). There is no reason
to weaken the Ethernet design to make one evaluation board cover both jobs.
The common USB/Ethernet CAN gateway architecture and the separate integrated
H735 qualification target are defined in
[19-Unified_CAN_Gateway.md](19-Unified_CAN_Gateway.md).

## Why this board

The STM32F767ZI provides a 216 MHz Cortex-M7, 2 MiB flash, 512 KiB SRAM,
dedicated 10/100 Ethernet DMA, RMII, and IEEE 1588v2 hardware timestamping.
HELIX already contains the STM32F7 platform, the F767 CMSIS device header,
the native-RMII MAC/DMA driver, the small ARP/IPv4/UDP layer, and the
authenticated datagram console. The port therefore extends an existing path
instead of introducing a second network stack.

The Nucleo board also makes the clock topology visible. It does not populate
the X3 8 MHz crystal by default. The ST-LINK co-MCU has the board oscillator
and feeds a fixed 8 MHz MCO signal into the F767 HSE input. That saves a
crystal and gives a designer the option of fitting X3 and chosen load
capacitors when oscillator behavior itself is under test. Firmware must treat
the default signal as an external clock and enable HSE bypass; treating it as
a crystal is incorrect.

Primary references:

* [STM32F767ZI datasheet](https://www.st.com/resource/en/datasheet/stm32f767zi.pdf)
* [STM32F76/77 reference manual](https://www.st.com/resource/en/reference_manual/dm00224583-stm32f76xxx-and-stm32f77xxx-advanced-arm-based-32-bit-mcus-stmicroelectronics.pdf)
* [NUCLEO-144 board manual](https://www.st.com/resource/en/user_manual/dm00244518-stm32-nucleo-144-boards-mb1137-stmicroelectronics.pdf)
* [STM32F76/77 errata](https://www.st.com/resource/en/errata_sheet/es0334-stm32f76xxx-and-stm32f77xxx-device-errata-stmicroelectronics.pdf)

## Decisions

The following are requirements, not open alternatives:

1. The F767 is an explicit target, not an F765 alias hidden in a board config.
2. The default Nucleo clock is 8 MHz ST-LINK MCO with `HSEBYP`; a populated X3
   crystal and HSI remain selectable alternatives.
3. Ethernet DMA descriptors and packet buffers live in a dedicated MPU-managed
   DMA arena. They are never maintained by scattered cache-clean calls alone.
4. Ethernet RX/TX completion is interrupt-driven. Protocol decoding,
   authentication, and application dispatch never run in the ISR.
5. IEEE 1588 descriptor timestamps feed HELIX machine-time discipline. IRQ
   entry time is observability data, not the packet timestamp.
6. Initial network parameters may come from `printer.cfg` over a bootstrap
   link. DHCP is then added as a first-class mode; an address is not a board
   identity.
7. The same STM32 DMA substrate supports timer-triggered ADC sampling into a
   circular ping-pong buffer, with block interrupts instead of per-sample
   polling.

## Board definition

Add `MACH_STM32F767` with the `stm32f767xx` device macro, 216 MHz core clock,
2 MiB flash, and 512 KiB SRAM. Add a persistent NUCLEO-F767ZI CI configuration
and a board preset rather than changing the generic RMII defaults.

The Nucleo RMII routing is:

| Signal | Pin | HELIX encoded pin |
| --- | --- | ---: |
| `REF_CLK` | PA1 | 1 |
| `MDIO` | PA2 | 2 |
| `MDC` | PC1 | 33 |
| `CRS_DV` | PA7 | 7 |
| `RXD0` | PC4 | 36 |
| `RXD1` | PC5 | 37 |
| `TX_EN` | PG11 | 107 |
| `TXD0` | PG13 | 109 |
| `TXD1` | PB13 | 29 |

`TX_EN` and `TXD0` differ from the current generic defaults. JP6 and JP7 must
be installed, and the PHY supplies the 50 MHz `RMII_REF_CLK`. The first
hardware pass must read and record the PHY identifier and strap-selected MDIO
address rather than merely assuming address zero.

### Clock startup

Add an F7 HSE source choice with three explicit modes:

* `external-clock`: set `HSEBYP` before `HSEON`; default for the Nucleo;
* `crystal`: use HSE without bypass for a populated X3 and its load network;
* `internal`: use HSI for recovery or operation without the ST-LINK clock.

Every oscillator and PLL wait is bounded. Failure records the failed stage and
either enters a diagnostically useful recovery clock mode or resets; firmware
must not hang forever before the console exists. Initial qualification keeps
the ST-LINK section powered so its MCO remains present.

## One cache-safe STM32 DMA arena

The Cortex-M7 cache and DMA ownership rules are part of the driver ABI. Create
a linker section such as `.dma_nocache` in DMA-reachable SRAM, aligned to a
power-of-two MPU region and sized by linker symbols. Place in it:

* Ethernet normal/enhanced descriptors;
* Ethernet RX and TX frame buffers;
* ADC circular and ping-pong buffers;
* future peripheral-DMA control structures that use the same ownership model.

At boot, one MPU region describes the arena as non-cacheable, shareable,
execute-never Normal memory. The linker asserts that the section fits and is
aligned to the programmed MPU region. The rest of SRAM may then use D-cache
normally. Descriptor and buffer alignment must also meet each peripheral's
requirements.

The MPU removes cache-line ownership ambiguity; it does not remove ordering
requirements. Drivers retain data-memory barriers before transferring a
descriptor to DMA and after observing ownership returned to the CPU. A buffer
cannot be reused until its descriptor has completed, and the allocator must
make double ownership impossible. Debug builds poison released buffers and
assert ring ownership transitions.

This is deliberately preferred over ad-hoc clean/invalidate calls. Those
calls are easy to omit, operate on whole cache lines, and can corrupt adjacent
objects when buffer boundaries are not cache-line isolated. A bounded
non-cacheable arena spends a small amount of peak bandwidth to make the
real-time ownership contract auditable.

## Interrupt-driven Ethernet service

The MAC DMA descriptor ring remains the hardware ingress/egress queue, but
polling is removed from the normal data path.

The Ethernet IRQ top half must:

1. read and acknowledge the DMA/MAC interrupt causes;
2. capture DMA status, ring indices, error bits, and IRQ-entry machine time;
3. publish completed RX descriptor indices to a bounded single-producer ring;
4. reclaim completed TX descriptors and their hardware timestamps;
5. wake the Ethernet task, then return without parsing a packet.

The Ethernet task drains descriptors with a bounded budget, validates frame
length/status, runs ARP/IPv4/UDP and HELIX authentication, and returns RX
descriptors to DMA. If the task budget is exhausted it reschedules itself
without waiting for another edge. RX-buffer-unavailable, overflow, bus error,
CRC, malformed-frame, software-ring-full, and TX-underflow counts are exported
through stats and Atlas. Link transitions are interrupts where the PHY makes
that possible, otherwise a low-rate MDIO health task remains acceptable.

TX submission is non-blocking. Scheduled motion traffic has priority over
telemetry, and a full TX ring applies the traffic-class policy instead of
spinning in either task or ISR context. Ring sizes become configuration values
with conservative Nucleo defaults and are tuned from measured high-water
marks, not intuition.

The MAC descriptor ring is not, by itself, sufficient buffering. After frame
parsing, the native `nano_udp` adapter must also preserve a bounded burst until
the cooperative console task runs. Its receive handoff is a four-datagram
static ring, and each entry owns its payload plus source MAC, IP, and port until
authentication accepts that exact datagram. Queue depth, high-water, and full
drops are exported separately from MAC/DMA errors. This prevents a fast
100-Mbit/s MAC from turning two ordinary back-to-back control requests into an
avoidable v1 ARQ timeout.

The existing transmit and receive Store-and-Forward settings remain mandatory
because they are the documented workarounds for F76/77 Ethernet corruption
errata. The driver does not issue the affected TxFIFO flush sequence.

## Hardware packet timestamps and machine time

Enable the MAC IEEE 1588 timer and enhanced descriptors. Every HELIX time-sync
exchange uses hardware timestamps captured at the MAC boundary:

* `t1`: host transmit timestamp;
* `t2`: F767 RX descriptor timestamp;
* `t3`: F767 TX completion timestamp;
* `t4`: host receive timestamp.

The exchange estimates offset, path delay, drift, and uncertainty without
including F767 IRQ latency. The PTP timer is disciplined toward HELIX machine
time with bounded frequency correction; it must never step backward while
scheduled work exists. Timestamp samples carry sequence and authentication
context so delayed or replayed observations cannot discipline the clock.

There are two host qualification modes:

* a NIC with hardware TX/RX timestamping is the precision reference;
* kernel/software timestamps are a supported fallback but cannot claim parity
  with USB SOF until their measured distribution proves it.

PHY and cable latency, direction asymmetry, switch residence time, timestamp
rollover, link renegotiation, and lost timestamp events are recorded. A direct
host-to-Nucleo cable establishes the floor before a switch is introduced.
The acceptance target is not merely a small mean offset: after convergence,
the distribution, worst accepted observation, holdover drift, and motion-load
behavior must meet or improve upon the qualified USB-SOF discipline. The exact
numeric gate is set from side-by-side data using the same analysis scripts;
until then Ethernet time sync is experimental.

## Network configuration and DHCP

An Ethernet-only console cannot receive the command that tells it its first IP
address. Initial implementation therefore uses a bootstrap control link
(ST-LINK virtual serial, native USB, or an already provisioned address):

1. Klippy reads the board's network section from `printer.cfg`.
2. It identifies the board over the bootstrap link by immutable HELIX board ID.
3. It sends a versioned `config_network` transaction containing mode, address,
   prefix/netmask, gateway, UDP port, and authentication profile.
4. The MCU validates the complete transaction, applies it atomically, and
   optionally stores it with CRC/version in nonvolatile configuration.
5. Klippy establishes the authenticated UDP session and only then releases the
   bootstrap link as the active console.

Static configuration is the first implementation because it gives deterministic
bring-up while still keeping network information in `printer.cfg`, not hidden
in a firmware build. A lost or invalid configuration returns to bootstrap
mode; it does not invent a routable address and accept unauthenticated motion.

DHCP then becomes a mode of the same configuration object. Implement the
bounded client state machine (`INIT`, `SELECTING`, `REQUESTING`, `BOUND`,
`RENEWING`, `REBINDING`) with strict option and length validation. Required
options are address, subnet, router, lease time, server identifier, and renewal
timers. DNS is not required by the MCU console. Persisting a valid lease may
accelerate reboot, but the server remains authoritative.

Fleet identity never depends on the lease. The immutable board ID and session
identity remain the lookup and authentication keys; DHCP reservations,
hostnames derived from board identity, and later mDNS/discovery merely map
that identity to a current address. Address loss or change tears down the old
session and authenticated reply peer, emits a link event, and performs a fresh
handshake. Queued motion follows the normal HELIX horizon/hold policy rather
than executing network-management traffic in a real-time context.

## STM32 timer-triggered ADC DMA primitive

This section records the F767 use case. The authoritative cross-family API,
ownership model, wire surface, and implementation order are in
[17-DMA_ADC_Acquisition.md](17-DMA_ADC_Acquisition.md).

Add a target-neutral `adc_stream` primitive with an STM32F7 backend. This is
collection infrastructure, not a sensor-specific policy.

Configuration describes ADC instance/channel, sample timer, sample rate,
resolution/sample time, block length, circular-buffer length, oversampling or
decimation ratio, and optional analog-watchdog thresholds. A hardware timer
TRGO starts each conversion. DMA writes a circular buffer and interrupts only
at half-transfer, full-transfer, error, or watchdog events; there is no
per-sample firmware timer and no per-sample ISR.

The DMA ISR publishes a small block descriptor containing buffer half, sample
count, sequence, first-sample machine time, period, and status. It never copies
or filters the samples. A bounded task or local consumer processes the stable
half while DMA fills the other half. The first implementation supplies:

* raw block collection for commissioning at a bounded telemetry rate;
* 64-bit sum/min/max and configurable power-of-two decimation;
* block timestamp reconstruction from the hardware trigger period;
* local callbacks for trigger, load/pressure, and control-loop consumers;
* explicit overrun, ADC overrun, DMA error, dropped-telemetry, and watchdog
  counters.

If a consumer has not released one half before DMA returns to it, the primitive
reports an overrun and follows the configured policy: drop telemetry, stop that
stream, or fire a local safety trigger. It never overwrite-and-pretends. ADC
buffers use the common MPU DMA arena. Calibration, conversion into engineering
units, and sensor meaning remain above the primitive.

This is the capability that polling could not provide: tens or hundreds of
thousands of hardware-timed samples per second with CPU work once per block,
while motion remains interrupt-driven and independent.

## Implementation sequence and gates

### Phase 1 - target and board definition

- [x] Add `MACH_STM32F767`, memory/clock constants, device selection, and
  NUCLEO-F767ZI config.
- [x] Add HSE source selection, bypass sequencing, and bounded startup waits.
- [x] Build Linuxprocess, existing F7 targets, F767 serial bootstrap, and F767
  RMII configurations in CI without growing unrelated targets.
- [ ] Record the exact board revision, MCU silicon revision, PHY ID/address,
  jumper state, and clock source.

Gate: serial/bootstrap console runs at the reported 216 MHz core rate and the
clock-failure path is observable rather than a hang.

### Phase 2 - MPU and DMA foundation

- [x] Reuse the unified DMA arena, resource manager, block ownership helpers,
  MPU programming, and alignment assertions from FD-0001 doc 17.
- [x] Move Ethernet rings/buffers into it and enable F7 I/D cache outside the
  arena.
- [x] Add ownership, wrap, exhaustion, and deliberate misalignment tests.

Gate: sustained memory/cache stress produces byte-exact DMA frames with zero
ownership violations, both with caches enabled and in a diagnostic cache-off
build.

### Phase 3 - Ethernet IRQ and network bring-up

- [x] Implement RX, TX-complete, and abnormal-condition IRQ service plus the
  bounded task drain.
- [x] Export ring high-water marks and complete DMA/MAC error counters.
- [x] Add bootstrap `printer.cfg` provisioning and atomic network handover.
  `[helix_network]` uses prepare/commit/abort epochs; firmware defers the
  committed address change until the reply can leave on the old session and
  then clears that authenticated peer.
- [x] Run workstation ARP/checksum, authenticated UDP/session, peer-rejection,
  malformed-frame, bounded-ring, and deterministic loss/duplicate/reorder/
  corruption tests.
- [x] Bring up the physical LAN8742A at 100 Mbit/s full duplex, acquire DHCP,
  complete a no-retry authenticated identify, and run 45,000+ bidirectional
  Klipper datagrams with complete MAC/DMA/session counters.
- [x] Complete a 1,733.5-second physical Ethernet print with Z motion on the
  F767: 2,301,802 G-code bytes, 3,637.98 mm filament, no trajectory
  underrun/deadline/rebase/invalid-segment event, and no communication
  shutdown.
- [x] Correlate the print's unexpectedly high v1 retransmission count to the
  native UDP software handoff, replace its single pending slot with a
  four-entry ring, and repeat the idle test with zero new slot drops and zero
  new retry bytes.
- [x] Reject wrong-PSK, corrupted-tag, and replay traffic on the physical PHY,
  then prove that the next valid command still completes.
- [ ] Repeat FEC-off/FEC-on, reconnect, ring exhaustion, and link flap against
  the physical PHY while solver/execution-log load is active.

Gate: a one-hour bidirectional saturation run concurrent with maximum solver
and execution-log load has no unexplained loss, starvation, memory corruption,
or ISR-budget violation. All induced drops appear in an accountable counter.

### Physical retransmission investigation (2026-07-23)

The first successful Ethernet print was mechanically clean, but the host
reported 56,997 retransmitted bytes for 385,660 bytes written to the F767.
That ratio looked incompatible with a local switched Ethernet path and was
treated as a defect instead of being excused by the successful part.

The layer-by-layer counters localized it:

| Layer | Observation |
| --- | ---: |
| Host NIC | 1 Gbit/s full duplex; zero RX/TX/CRC/frame/missed/FIFO/carrier errors |
| Host UDP socket | zero socket drops; receive queue empty |
| Intentproto session | zero lost/reordered MCU-to-host datagrams; zero auth/replay/epoch failures |
| F767 MAC/DMA | zero RX/TX errors, overruns, DMA errors, TX busy, and underflows |
| UDP console TX | zero response drops, no-peer drops after pairing, or send failures |
| Native UDP handoff | **3,509 `udp_slot_drops`** |

The old handoff accepted one UDP payload and rejected every subsequent payload
until `udp_console_task` drained that slot. ClockSync and HELIX TimeSync both
run at the same 0.9839-second cadence, so the host commonly emitted a
two-datagram burst. The dominant host retry increment was exactly eight bytes,
the retransmitted v1 `timesync_query` wire block.

A controlled idle interval closed the causal loop: 33 additional firmware
slot drops produced 264 additional host retry bytes, exactly `33 × 8`. After
installing the four-entry ring, the same periodic traffic reached a queue
high-water of two, while `udp_slot_drops` remained zero and Klipper's
`bytes_retransmit` remained unchanged over the 55.2-second comparison window.
The fix therefore removes self-inflicted software loss; it does not mask a
weak cable, PHY, switch, kernel, or authenticated datagram layer.

`HELIX_DATAGRAM_STATUS TRANSPORT=f767` now queries these MCU counters on
demand. It deliberately does not poll in the background, because diagnostic
traffic must not become part of the condition being measured.

### Phase 4 - hardware time discipline

- [ ] Enable the PTP timer and enhanced descriptor RX/TX timestamps.
- [x] Add the authenticated, cross-language four-timestamp record and the
  host offset/path-delay/drift discipline with bounded holdover, epoch reset,
  delay filtering, median/MAD outlier rejection, and simulated asymmetry,
  reordering, outlier, and 25 ppm drift coverage.
- [ ] Bind `t1`/`t4` to host NIC timestamps and `t2`/`t3` to enhanced MAC
  descriptors. Simulated timestamp tests are not hardware timestamp evidence.
- [ ] Characterize direct cable, switched link, idle, saturation, link flap,
  oscillator holdover, and simultaneous motion load.
- [ ] Compare the full distribution against USB SOF and publish raw evidence.

Gate: Ethernet meets the machine-time qualification gate under load; no sample
is accepted merely because its IRQ arrived promptly.

### Phase 5 - DHCP and fleet behavior

- [x] Implement DHCP acquisition, renewal, rebinding, expiry, NAK handling,
  strict bounded option parsing, exponential retry, and 30-second static
  fallback/bootstrap behavior.
- [x] Keep the host fabric inventory keyed by canonical board ID; address
  changes clear the old reply peer without changing downstream identity.
- [x] Unit-test offers, ACK, NAK, malformed replies, renewal, rebinding,
  expiry, retry, fallback, and atomic configuration rollback.
- [x] Cold-reset the physical F767 repeatedly, reacquire its DHCP lease, and
  establish a fresh authenticated session without reflashing.
- [ ] Test multiple physical boards, reservation changes, DHCP-server restart,
  duplicate offers, lease expiry during motion, and network partition.

Gate: a fleet can reboot and re-address without manual reflashing, accidental
cross-connection, or unsafe continuation on a stale authenticated peer.

### Phase 6 - ADC DMA primitive

- [x] Reuse the `adc_stream` core and implement/cross-build the STM32F7
  timer/ADC/DMA backend, counters, and telemetry/local-consumer APIs defined in
  FD-0001 doc 17.
- [ ] Qualify that backend on the physical F767 board.
- [ ] Test exact sample rate and timestamp reconstruction with a signal
  generator or looped timer output.
- [ ] Measure ENOB/noise improvement, block-processing cost, overrun behavior,
  and interference while Ethernet and worst-case motion run concurrently.
- [x] Connect the analog watchdog to the hardware-trigger framework without
  routing normal sample collection through trsync.

Gate: continuous acquisition at the claimed rate loses no unreported block,
preserves motion timing, and produces raw and decimated data that agree with
offline reference processing.

## Definition of done

The NUCLEO-F767ZI is qualified only when the exact signed image and configuration
are archived; all phases above have evidence; a cold boot obtains or applies
network configuration; authenticated Ethernet remains stable during real
motion load; hardware timestamps meet the machine-time gate; induced DMA,
link, DHCP, and queue failures are recovered or fail safe; and the ADC stream
runs concurrently without unexplained data loss or motion regression.

Compile success, link LEDs, ping, or a short console exchange are milestones,
not completion.
