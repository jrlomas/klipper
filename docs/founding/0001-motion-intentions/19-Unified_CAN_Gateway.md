# FD-0001: Unified USB/Ethernet-to-CAN Gateway Architecture

Status: Workstation implementation complete for the first gateway release.
The typed gateway codec, bounded runtime, shared CAN queue, authenticated RMII
gateway binding, SocketCAN/serial host proxy, F767 bxCAN port, H723 RMII/FDCAN
port, and USB compatibility adapter are implemented and compile-tested. The
FK723M1-ZGT6 H723 smoke target has also passed live USB-composite, controller
clock, control-console, and self-test qualification. Ethernet silicon, PTP
timestamp discipline, and electrical H723 CAN-FD bus qualification remain
hardware gates. The NUCLEO-F767ZI remains the
native-Ethernet proof target; the NUCLEO-H723ZG with a CAN FD 7 Click remains
the preferred Ethernet-to-CAN-FD qualification target.

The H723 implementation uses an independent 80 MHz PLL2Q FDCAN kernel clock,
not the 130 MHz APB clock. This makes every negotiated Helix rate (1, 2, 5,
and 8 Mbit/s) exactly divisible. Its USB-OTG bridge target also exposes the
same composite `gs_usb` plus independent CDC control interface as the G0B1
bridge, preserving the no-fake-UUID rule across MCU families.

This document defines one HELIX CAN gateway architecture with interchangeable
host links, time sources, and CAN controllers. It extends the CAN FD rules in
[15-CANFD_Transport.md](15-CANFD_Transport.md), the Ethernet target plan in
[16-STM32F767_Ethernet.md](16-STM32F767_Ethernet.md), and the general link
model in [07-Link_Transport.md](07-Link_Transport.md).

The central decision is simple: **USB-to-CAN and Ethernet-to-CAN are not two
bridges. They are two host-link adapters around the same CAN gateway core.**
The common boundary is a canonical CAN frame plus explicit control and
delivery records. It is not a byte stream, USB endpoint packet, `gs_usb`
structure, UDP datagram, or Ethernet descriptor.

## Goals

The design must:

1. preserve the already-qualified mainline-Linux `gs_usb` path;
2. add authenticated native-Ethernet access to the same CAN fabric;
3. support Classical CAN and ISO CAN FD, including BRS profiles;
4. retain stable gateway, bus, and downstream-node identities when the host
   link changes;
5. make every accepted, transmitted, failed, or dropped frame accountable;
6. prevent a fast USB or Ethernet ingress from silently overrunning CAN;
7. preserve HELIX machine-time quality through transport-specific hardware
   timestamp sources;
8. permit future host redundancy without allowing two active motion owners;
9. keep transport-specific mechanics out of the routing and safety core; and
10. expose one coherent diagnostic surface to Klippy, Atlas, tests, and
    operators.

## Non-goals

This architecture does not:

* tunnel `gs_usb` endpoint packets over Ethernet;
* make Ethernet DMA descriptors look like USB packets;
* promise lossless delivery after arbitrary power loss;
* blindly retransmit uncertain raw CAN transmissions;
* make the F767's bxCAN peripheral support CAN FD; or
* require a custom Linux kernel driver for the first implementation.

## Hardware roles

### NUCLEO-F767ZI: Ethernet proof target

The F767 target proves the native RMII MAC, descriptor ownership, MPU/D-cache
rules, interrupt-driven receive/transmit service, DHCP/static provisioning,
authenticated datagrams, and IEEE 1588 timestamp path. Its internal bxCAN
peripherals can support a Classical-CAN smoke test, but cannot carry HELIX's
64-byte CAN-FD frames. `FD_1M_NOBRS` is still CAN FD and is therefore not a
bxCAN-compatible profile merely because its nominal and data rates are both
1 Mbit/s.

The F767 is consequently not the full gateway qualification target and should
not acquire an external CAN controller merely to make the Ethernet proof more
complicated.

### NUCLEO-H723ZG plus CAN FD 7 Click: preferred qualification target

The NUCLEO-H723ZG is the preferred first complete Ethernet-to-CAN-FD gateway
when paired with a MikroElektronika CAN FD 7 Click (`MIKROE-5888`):

* native 10/100 Ethernet with IEEE 1588 capability;
* native FDCAN exposed by the Nucleo headers;
* an RJ45 connector and integrated STLINK-V3E;
* no unused display, external memory, audio, or user-interface hardware; and
* sufficient CPU, SRAM, DMA, cache, and MPU resources to stress both sides at
  once without making the evaluation board the bottleneck.

The Click board contains a TI TCAN1462-Q1 transceiver, not a second CAN
controller. The H723's native FDCAN peripheral therefore retains message-RAM,
Tx Event, timestamp, filtering, and error-state ownership. The transceiver is
rated for Classical CAN and CAN FD through 8 Mbit/s and includes signal
improvement capability (SIC), which increases margin against ringing and
unterminated stubs at high data rates. It provides 3.3/5 V logic selection,
standby control, an optional 120-ohm terminator, a DE-9 connector, and an
external CAN header.

The electrical connection is deliberately simple:

| NUCLEO-H723ZG | CAN FD 7 Click | Meaning |
| --- | --- | --- |
| selected `FDCAN_TX` alternate-function pin | `TXD` | controller to transceiver |
| selected `FDCAN_RX` alternate-function pin | `RXD` | transceiver to controller |
| `5V` | `5V` | transceiver supply |
| `3V3` | `3V3` / VIO selected to 3.3 V | logic-level reference |
| `GND` | `GND` | mandatory signal reference |
| GPIO or ground | `STB` | high for standby; low for normal operation |
| n/a | `CANH`, `CANL` | physical bus pair |

The labels `TX` and `RX` on mikroBUS material describe raw transceiver logic
signals; firmware must route an FDCAN peripheral to them, not a UART. The
board preset fixes the exact H723 alternate-function pins only after the
Nucleo schematic/header route is verified on the received board.

This add-on is non-isolated. The qualification harness must share signal
ground, use correct two-end 120-ohm termination, and must not connect two
independently grounded high-energy machines without isolation. Enable the
Click's terminator only when it is one physical end of the bus; the EBB36 or
FPS at the other end supplies the second termination.

Primary hardware references:

* [NUCLEO-H723ZG product page](https://www.st.com/en/evaluation-tools/nucleo-h723zg.html)
* [NUCLEO-H723ZG schematic](https://www.st.com/resource/en/schematic_pack/mb1364-h723zg-e01_schematic.pdf)
* [CAN FD 7 Click product page](https://www.mikroe.com/can-fd-7-click)
* [TCAN1462-Q1 product page](https://www.ti.com/product/TCAN1462-Q1)

### STM32H735G-DK: integrated alternative

The STM32H735G-DK remains a useful integrated alternative. It includes
Ethernet, three FDCAN controllers, and three CAN-FD-compliant transceiver
channels on one board, but it also carries an LCD, external memories, audio,
and other peripherals irrelevant to this gateway. It is preferable only when
minimum wiring matters more than cost and board simplicity.

References:

* [STM32H735G-DK product page](https://www.st.com/en/evaluation-tools/stm32h735g-dk.html)
* [STM32H735G-DK user manual](https://www.st.com/resource/en/user_manual/um2679-discovery-kit-with-stm32h735ig-mcu-stmicroelectronics.pdf)

## Architectural decomposition

```text
 Klippy / Atlas / management
             |
             | stable logical bus: helixcan0
             v
 +---------------------- host endpoint -----------------------+
 | USB backend: AF_CAN/gs_usb | Ethernet backend: HELIX DGF   |
 +-----------------------------+-------------------------------+
                               |
                    canonical gateway records
                               |
 +------------------------ MCU gateway ------------------------+
 | host-link adapter                                            |
 |   USB endpoints + gs_usb   OR   Ethernet MAC + UDP session  |
 |                              |                               |
 |                    CAN gateway core                          |
 | routing | credits | profiles | accounting | ownership       |
 |                              |                               |
 |                    machine-time provider                     |
 | USB SOF discipline         OR   Ethernet hardware/PTP time   |
 |                              |                               |
 |                    CAN hardware adapter                      |
 | STM32 FDCAN | STM32 bxCAN | future external controller      |
 +--------------------------------------------------------------+
                               |
                      physical CAN / CAN FD
                               |
                    EBB36 | FPS | other nodes
```

There are four interfaces, not one generalized stream:

1. **Host-link adapter** moves gateway records between a host and the core.
2. **CAN gateway core** owns routing, queues, policy, and conservation.
3. **CAN hardware adapter** owns controller registers, message RAM, error
   states, and hardware timestamps.
4. **Machine-time provider** disciplines the gateway clock from USB SOF,
   Ethernet PTP timestamps, or an explicitly degraded fallback.

This composition prevents an otherwise attractive mistake: placing USB and
Ethernet behind a lowest-common-denominator byte-stream API. USB control
transfers and Ethernet authenticated datagrams have different failure,
ordering, discovery, and timestamp semantics. They become equivalent only
after translation into canonical gateway records.

## Canonical gateway records

The internal API carries typed records. It must not expose `struct
gs_host_frame`, USB packet boundaries, or Ethernet frame buffers to the core.

### CAN frame record

A frame record contains:

* logical bus/channel;
* 11-bit or 29-bit CAN identifier;
* EFF, RTR, FD, BRS, and ESI flags;
* canonical payload length and up to 64 data bytes;
* a host-assigned delivery cookie;
* session/profile epoch;
* optional controller RX, TX-start, or TX-event timestamp; and
* the timestamp validity and clock-quality generation.

The delivery cookie is not a CAN sequence number. It correlates admission and
completion events without changing the downstream HELIX wire protocol.

### Implemented version 1 binary layout

All multibyte integers are little-endian. Authentication and the outer
datagram sequence are supplied by intentproto; the inner sequence and epoch
prevent a decoded packet from crossing ownership generations or being applied
twice by the service dispatcher.

| Envelope offset | Width | Field |
| ---: | ---: | --- |
| 0 | 2 | magic `HG` (`0x4748` little-endian) |
| 2 | 1 | version (`1`) |
| 3 | 1 | packet flags (`RESET`, `ACK_ONLY`) |
| 4 | 4 | ownership/session epoch |
| 8 | 4 | packet sequence |
| 12 | 2 | record count |
| 14 | 2 | exact record-payload byte count |

Each record begins with a 12-byte header: service, opcode, channel, flags,
data length, and 32-bit delivery/transaction cookie. Record data is bounded to
128 bytes. A packet is rejected before any service callback when a header,
length, service, trailing byte, epoch, sequence, or credit reservation is
invalid. Version 1 service IDs are `CONTROL=0`, `CAN=1`, and `SERIAL=2`.

A CAN frame record contains a 12-byte CAN header followed by 0–64 payload
bytes: 32-bit CAN ID, 32-bit hardware clock, byte length, CAN flags, and a
zero reserved field. The flags preserve FD, BRS, ESI, Tx-event, and timestamp
semantics from `struct canbus_msg`. Serial records remain bytes on a numbered
channel; configuration and break are distinct opcodes, so they cannot be
confused with stream data.

### Control record

Control records cover:

* capability and identity discovery;
* profile prepare, commit, enable, abort, and query;
* interface start, stop, quiesce, and recovery;
* queue-credit advertisement;
* ownership lease acquisition, renewal, and release;
* time-source state and clock-quality changes;
* filter and telemetry policy; and
* versioned status snapshots.

State-changing control records carry a transaction ID and desired generation.
They are idempotent: repeating the same transaction returns the stored result,
while reusing an ID with different contents is rejected.

### Delivery record

The gateway reports distinct milestones:

1. `ADMITTED`: accepted into the bounded CAN egress queue;
2. `SUBMITTED`: handed to the CAN controller;
3. `COMPLETED`: controller reports successful transmission;
4. `FAILED`: controller or bus rejected/aborted it; or
5. `UNKNOWN`: reset or loss destroyed evidence after admission.

`ADMITTED` must never be described as delivery. An `UNKNOWN` result is not
automatically retried at the raw-CAN layer because that could duplicate a
motion or heater command. The sequenced HELIX command layer decides whether a
command is already accepted and whether replay is safe.

## Firmware component boundaries

### `can_gateway_core`

The existing reusable logic in `src/generic/usb_canbus.c` should be extracted
into `src/generic/can_gateway_core.[ch]`. The core owns:

* host-to-CAN and CAN-to-host rings;
* local gateway routing and downstream routing;
* frame admission and credit accounting;
* profile state and maintenance-mode interlocks;
* required-node inventory and capability intersection;
* bus-off/restart state;
* ownership epochs;
* CAN time-beacon scheduling and follow-up association; and
* common statistics.

The first extraction is intentionally smaller than the final target. The
single-producer/single-consumer queue and its accepted/forwarded/drop/highwater
conservation counters now live in `generic/can_gateway.[ch]` and are used by
the qualified USB bridge and the network gateway. Profile and two-step-time
state still live in the USB adapter until the common completion-event model is
implemented; moving them prematurely would change qualified behavior.

The core is task-context code. CAN and link ISRs publish bounded events and
wake tasks; they do not perform authentication, protocol negotiation, or
unbounded routing work.

### USB host-link adapter

`usb_canbus.c` remains responsible for:

* USB descriptors and OpenAMS product identity;
* `gs_usb` endpoint-zero control requests;
* translating `gs_host_frame` to and from canonical CAN records;
* USB packet staging and echo-cookie translation; and
* mainline Linux `gs_usb` compatibility.

The first refactor is accepted only if Linux observes no USB protocol or
behavior change. The current status command remains as a compatibility alias.

### Ethernet host-link adapter

The Ethernet adapter is layered above `eth_mac` and the IP/UDP seam. It owns:

* an authenticated, replay-protected HostSession;
* gateway-protocol datagram encoding and decoding;
* batching several complete records per Ethernet datagram;
* datagram sequence, acknowledgement, loss, duplicate, and reorder tracking;
* session epochs and reconnect handling;
* credit updates and delivery events; and
* link-specific statistics.

It reuses the HELIX datagram session and authentication machinery defined by
[07-Link_Transport.md](07-Link_Transport.md). CAN records use a separately
versioned payload type so a console endpoint cannot be confused with a CAN
gateway endpoint even if both share the same MAC and UDP implementation.

The workstation implementation uses the existing authenticated intentproto
datagram envelope and the versioned gateway payload above. The first host
adapter is `scripts/helix_gateway_proxy.py`: it binds ordinary SocketCAN for
the CAN service and explicitly configured file descriptors for numbered
serial services. Consequently downstream Klipper nodes retain their real CAN
identities; the gateway does not invent a downstream UUID. Static-PSK mode is
implemented, as is the rotating-key HostSession with authenticated gateway
identity and downgrade pinning. Multi-record transmit batching, bounded
credits, transactional CAN profile controls, and cookie-correlated delivery
milestones are implemented. Datagram acknowledgement/retransmit policy and
redundant-host leases remain before the network path is safety-qualified.

### CAN hardware adapter

The existing `canhw_*` boundary remains the correct hardware seam, extended as
needed for explicit completion events and controller capabilities. An adapter
reports:

* supported formats, rates, BRS, TDC, timestamps, and message RAM limits;
* exact accepted nominal/data timing and sample points;
* RX frames and their timestamps;
* TX admission and Tx Event completion;
* controller error state and counters; and
* deterministic stop, reconfigure, and restart results.

The core must not assume every controller has an FDCAN Tx Event FIFO. A bxCAN
adapter can truthfully advertise reduced capabilities and is limited to
Classical profiles.

## Ethernet gateway wire protocol

The gateway protocol is a new HELIX datagram payload, not encapsulated
SocketCAN or `gs_usb`. Its envelope contains:

```text
magic | version | message type | flags
gateway id | session epoch | datagram sequence
ack base | ack bitmap | record count | records...
authentication tag
```

The exact integer widths are fixed during implementation using the following
requirements:

* the complete authenticated datagram fits the configured Ethernet MTU;
* records are independently length-delimited and reject overrun/trailing data;
* unknown optional record types can be skipped, but unknown safety-critical
  control records fail the transaction;
* the authentication tag covers envelope, acknowledgements, and all records;
* sequence comparisons are wrap-safe and scoped to a session epoch; and
* a new session cannot acknowledge or replay records from an old epoch.

Multiple CAN frames should normally share one datagram. Batching amortizes
Ethernet, IP, UDP, authentication, and interrupt overhead and is essential when
CAN is busy. Class-0 traffic has a bounded batching deadline so batching never
adds unbounded scheduling latency.

## Reliability and retransmission

Ethernet CRC protects an individual link frame; UDP still permits whole
datagram loss, duplication, and reordering. HELIX separates these concerns:

* datagram sequences measure transport loss and drive acknowledgements;
* gateway controls and ownership leases are reliably and idempotently
  retransmitted;
* telemetry may be discarded according to its traffic class;
* a CAN frame that has not reached `ADMITTED` can be safely resent; and
* a frame in `ADMITTED`, `SUBMITTED`, or `UNKNOWN` is resolved by the upper
  HELIX command sequence, never blindly repeated by the tunnel.

Packet-level erasure FEC may be negotiated for lossy network links, but should
default off on a qualified wired LAN. FEC does not replace credits and cannot
make an overloaded CAN egress queue safe.

## Backpressure and conservation

A 100 Mbit/s Ethernet ingress can exceed a 1 Mbit/s CAN link by roughly two
orders of magnitude before protocol overhead. Enlarging the queue only delays
failure. The gateway therefore advertises credits derived from free CAN egress
slots and a reserved control margin.

The host may have no more admitted-but-not-completed frames than the advertised
window. Class-0/1 and management traffic reserve separate capacity so Class-2
telemetry or bulk work cannot deadlock recovery.

For every status interval the following identities must hold, modulo explicit
in-flight snapshots:

```text
host_rx = rejected + admitted
admitted = queued + submitted + completed + failed + unknown
can_hw_rx = host_forwarded + locally_consumed + rx_queue_dropped
```

Every term is a monotonic counter. A reset publishes the prior counter epoch
when retained evidence permits; otherwise it increments an explicit
`evidence_lost` generation. No packet disappears into an unnamed difference.

## Machine time across the gateway

Machine-time transfer has two independent stages:

1. discipline the gateway's local clock to host/machine time; and
2. transfer that disciplined time to downstream CAN nodes.

The USB implementation uses matched USB SOF observations for stage one and
hardware FDCAN transmission events for stage two. The Ethernet implementation
uses MAC hardware receive/transmit timestamps, with IEEE 1588/PTP discipline
where available, then the same CAN two-step beacon/follow-up mechanism.

The common core consumes a `machine_time_provider` with:

* source kind and generation;
* offset, rate estimate, uncertainty, and age;
* qualified/degraded/invalid state; and
* conversion between local hardware ticks and machine time.

IRQ-entry timestamps are diagnostics only. They must never substitute for MAC
or CAN peripheral timestamps when those exist. A clock-quality transition is
carried in the profile/session epoch so queued work cannot silently cross from
one time basis to another.

The H735 hardware gate must measure the complete Ethernet-hardware-timestamp
to FDCAN-Tx-event path. It must not assume IEEE 1588 support alone guarantees
the current USB-SOF synchronization result.

## Identity, security, and host ownership

The gateway has a canonical hardware identity independent of its MAC address,
IP address, USB serial, and logical bus name. Downstream boards retain their
canonical `board_id` when reached through either host link.

Network operation requires authentication and replay protection. Discovery
may reveal only the minimum pairing identity before authentication; it cannot
accept motion, heater, profile, or firmware-update commands.

Exactly one authenticated session owns the write lease for a CAN coordination
group. Additional sessions may be read-only observers. A takeover requires a
new monotonically increasing ownership epoch and an explicit safe handoff or
expiry. Merely connecting from a redundant host cannot create a second motion
producer.

This lease is the architectural hook for future host redundancy. It is not a
promise that automatic failover is safe before coordinated pause/resume and
state reconstruction are qualified.

## Linux and Klippy integration

The logical printer bus remains `helixcan0`; changing USB to Ethernet changes
the endpoint backend, not downstream MCU configuration.

The host introduces a `CanFabricEndpoint` boundary:

* the USB backend uses the ordinary AF_CAN socket supplied by `gs_usb`;
* the Ethernet backend uses the authenticated gateway session directly; and
* both present canonical frames, completion events, common status, and profile
  transactions to the bus owner.

An optional `vcan` mirror may expose Ethernet-gateway traffic to `candump` and
other SocketCAN tools. It is not the safety or configuration authority. A
userspace `vcan` relay cannot make normal CAN rtnetlink bitrate requests reach
the remote gateway, nor can it perfectly project remote controller error state
into Linux netdevice counters.

For the first implementation, `helix-can-manager` grows explicit USB and
Ethernet backends and retains its narrow privilege boundary. Exact
`ip link set helixcan0 type can ...` parity for an Ethernet endpoint would
require a small kernel CAN netdevice driver; that is deferred until the direct
backend proves whether the operational value justifies the maintenance cost.

`klippy/extras/helix_can.py` must stop hard-coding USB semantics. In particular:

* `usb_forwarded_frames` becomes `host_forwarded_frames` in status v2;
* `get_usb_canbus_status` remains a compatibility alias;
* `get_can_gateway_status` returns common and nested link-specific status;
* `time_source` reports the actual provider rather than assuming USB SOF; and
* profile, identity, and conservation logic are shared by endpoint backend.

## Common status surface

Every gateway reports at least:

* gateway, session, ownership, profile, and counter epochs;
* host-link kind, state, MTU, RTT, loss, duplicate, reorder, and auth counters;
* host RX, rejected, admitted, and credit-starvation counts;
* CAN TX queued, submitted, completed, failed, and unknown counts;
* CAN hardware RX, host-forwarded, locally-consumed, and dropped counts;
* queue depth, capacity, high-water, and reserved capacity in each direction;
* CAN warning, passive, bus-off, arbitration-loss, and protocol-error state;
* time-source kind, quality, uncertainty, age, rejected observations, and
  generation; and
* the computed conservation residuals.

Atlas should create an incident on a nonzero conservation residual, silent
counter reset, ownership conflict, repeated authentication failure, bus-off,
or time-quality loss during scheduled work. Normal status refreshes are not
diagnoses or incidents.

## Implementation sequence

### Phase 0 — freeze behavior and fixtures

- [x] Capture USB descriptors, control transactions, representative CAN
  traffic, status output, and Linux interface behavior as golden fixtures.
- [x] Add bidirectional conservation tests around the current USB bridge.
- [x] Add deterministic queue-full, bus-off, restart, and Tx-event-loss
  fixtures. These close the workstation contract; physical fault injection
  remains in Phase 6.

### Phase 1 — extract the common core

- [x] Introduce canonical frame, control, and status types.
- [x] Extract the bounded CAN RX queue and conservation counters for reuse by
  USB and Ethernet without changing `gs_usb` descriptors or framing.
- [x] Retain `get_usb_canbus_status` and `usb_forwarded_frames` compatibility
  while adding `get_can_gateway_status` and `host_forwarded_frames`.
- [x] Cross-build the STM32G0B1 USB-to-CAN-FD bridge and run the focused
  USB/CAN transport, manager, and profile suites.
- [ ] Extract profiles, delivery completion, and time-beacon association from
  `usb_canbus.c` after the common completion-event contract is implemented.

### Phase 2 — host endpoint abstraction

- [x] Add a deployable authenticated UDP gateway proxy with typed SocketCAN
  and serial endpoints, preserving real downstream CAN identities.
- [x] Add `CanFabricEndpoint` directly to the Klippy bus owner.
- [x] Move common identity/profile/conservation logic out of USB-specific
  branches in `helix_can.py`.
- [x] Add versioned common status and Atlas ingestion. Routine status remains
  informational; only a gateway incident enters the diagnostic path.
- [x] Keep AF_CAN as the USB backend and existing configuration default.

### Phase 3 — Ethernet gateway protocol

- [x] Specify and cross-test exact version-1 binary envelope, record, CAN, and
  serial layouts.
- [x] Reuse HostSession authentication, replay, board identity, and epoch
  handling, while retaining explicit static-PSK compatibility.
- [x] Implement bounded multi-record batching, credits, transactional CAN
  profiles, and `ADMITTED`/`SUBMITTED`/`COMPLETED`/`FAILED`/`UNKNOWN`
  delivery records.
- [x] Add explicit gateway-datagram acknowledgement/retransmit policy; do not
  conflate it with upper HELIX command ARQ or blindly replay uncertain CAN.
- [x] Implement bounded per-service credits and whole-packet validation before
  dispatch.
- [x] Build cross-language C/Python gateway-core fixtures.
- [x] Test malformed lengths, epochs, replayed packets, service exhaustion,
  and binary golden vectors.
- [x] Exercise deterministic malformed-packet mutations, wraparound, duplicate
  sequences, whole-packet atomic validation, and authentication failures.
- [x] Add a coverage-guided native fuzzer for duplicate transactional controls
  and long reorder/loss traces.

### Phase 4 — F767 Ethernet proof

- [ ] Complete the gates in [16-STM32F767_Ethernet.md](16-STM32F767_Ethernet.md).
- [x] Port the F767 bxCAN compile path and build the complete RMII gateway
  image with authenticated datagrams, DMA MAC, typed services, and classic
  CAN at 1 Mbit/s.
- [x] Run the gateway protocol against host/C fixtures.
- [ ] Qualify hardware MAC timestamps, link loss/recovery, DHCP/static
  configuration, authentication, and sustained bidirectional traffic.
- [ ] Optionally run a Classical-CAN smoke test; do not count it as FD proof.

### Phase 5 — H723 Ethernet-to-CAN-FD bridge

- [x] Add a NUCLEO-H723ZG gateway preset and persistent CI configuration.
- [x] Port and cross-build the H723 Ethernet DMA with native four-word
  descriptors, explicit ring lengths/tail pointers, the shared non-cacheable
  MPU arena, and the existing H723 FDCAN message RAM.
- [ ] Enable and qualify H723 MAC ingress/egress timestamps and PTP discipline
  on physical Ethernet hardware; compile success is not timestamp evidence.
- [ ] Verify H723 pin routing, the CAN FD 7 Click's VIO and standby settings,
  termination, and controller timing at `FD_1M_NOBRS`.
- [x] Build the same authenticated gateway protocol and core used on the
  F767/USB targets into the H723 image.
- [x] Add the dedicated 80 MHz FDCAN clock domain and cross-build the H723
  USB-OTG composite bridge with exact 1/2/5/8 Mbit/s timing support.
- [x] Qualify the FK723M1 H723 controller/USB smoke path: mainline `gs_usb`
  plus CDC-ACM enumerate together, Linux reads back the real 80 MHz FDCAN
  clock, SocketCAN accepts and reads back the exact 1 Mbit/s nominal / 8 Mbit/s
  data profile, the control console reports zero queue/error counters, and all
  five on-board self-tests pass. This is controller and host-interface
  evidence; the board has no external CAN transceiver and no frames were sent.
- [ ] Qualify 2, 5, and 8 Mbit/s BRS profiles against compatible nodes.

### Phase 6 — fault and saturation qualification

The workstation fault model is complete, but checkboxes which explicitly
require a PHY, real CAN error state, motion, or sustained line-rate traffic
remain open until the Ethernet board arrives.

- [ ] Saturate Ethernet-to-CAN and prove credit-bounded memory use.
- [ ] Saturate CAN-to-Ethernet and prove exact conservation.
- [x] Inject UDP loss, duplicate, reorder, and corruption over a real localhost
  UDP socket; authenticated corruption is rejected and no accepted record is
  actuated twice.
- [ ] Disconnect/reconnect Ethernet during idle and scheduled work.
- [x] Reset the gateway model with frames in every delivery state and account
  every nonterminal frame as `UNKNOWN`, never automatic replay.
- [ ] Inject CAN warning, passive, bus-off, and recovery.
- [x] Verify profile rollback and universal Classical recovery at prepare,
  commit, Linux-netdevice, and enable boundaries. A node which fails while
  preparing is included in the abort set because it may already have staged
  state.
- [x] Verify unauthorized and second-writer rejection through authenticated
  peer latching, HostSession identity, replay epochs, and forged-source tests.
- [x] Simulate four-timestamp convergence, asymmetric delay, reorder, outliers,
  epoch reset, holdover expiry, and oscillator drift. The physical PTP-to-CAN
  accuracy gate below remains open.
- [ ] Measure Ethernet/PTP-to-CAN time-transfer accuracy under load.
- [ ] Complete a long physical print over the Ethernet gateway.

### Phase 7 — optional Linux projection and redundancy

- [x] Add an optional `vcan`/real-UDP diagnostic lab; it is a test projection,
  not a second production ownership path.
- [ ] Decide from measured operational needs whether a kernel netdevice driver
  is warranted for exact rtnetlink semantics.
- [ ] Qualify read-only observers, lease expiry, planned takeover, and
  pause/reconstruction before enabling automatic host failover.

## Acceptance gates

The architecture is complete only when:

1. the USB path remains wire-compatible with mainline `gs_usb`;
2. USB and Ethernet adapters pass the same gateway-core conformance suite;
3. every frame is represented by the conservation identities;
4. no queue can grow without a fixed bound or silently discard Class-0/1 work;
5. uncertain delivery cannot cause an automatic raw-CAN duplicate;
6. profile negotiation is unanimous, transactional, and rollback-safe;
7. board and gateway identities survive host-link and IP-address changes;
8. an unauthenticated or second writer cannot control the bus;
9. measured Ethernet-to-CAN time quality satisfies the active motion gate;
10. link loss and gateway restart produce a controlled hold/recovery rather
    than uncontrolled replay; and
11. a sustained physical print succeeds with zero unexplained conservation
    residuals, bus errors, queue drops, or time-quality violations.

## Design consequences

This separation scales beyond the first bridge. A future PCIe, EtherCAT,
Wi-Fi, redundant-Ethernet, or native host-MCU link needs a new host-link
adapter, not another CAN routing implementation. A different CAN controller
needs a hardware adapter, not another network protocol. A better clock source
needs a machine-time provider, not a rewrite of frame forwarding.

Most importantly, the abstraction preserves evidence. HELIX can safely use a
fast and redundant network only if it can say whether a command reached the
gateway, reached the CAN controller, reached the bus, or became unknowable.
That distinction—not Ethernet bandwidth—is what turns an Ethernet-to-CAN
adapter into a trustworthy machine-control gateway.
