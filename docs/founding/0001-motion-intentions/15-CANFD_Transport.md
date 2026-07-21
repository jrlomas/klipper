# FD-0001: CAN FD Transport, Negotiation, and Time Transfer

Status: Software vertical slice implemented and workstation-tested; physical
CAN qualification pending. The implementation is checkpointed by commits
`8b278385`, `0403c6f7`, and `0e73783c`.
The 1 Mbit/s case is a profile of the same ISO CAN FD implementation, not a
separate prototype protocol.

This document defines HELIX's CAN FD transport: a mainline-Linux-compatible
USB bridge, bus-wide capability negotiation, honest mixed-node behavior,
Klipper host integration, and hardware-timestamped transfer of machine time
from USB Start-of-Frame (SOF) to downstream CAN boards. It extends the general
link architecture in [07-Link_Transport.md](07-Link_Transport.md), the machine
time model in [01-Time_Model.md](01-Time_Model.md), and the mixed-fleet rules in
[14-Heterogeneous_Fleets.md](14-Heterogeneous_Fleets.md). The extraction of
this USB implementation into a transport-neutral gateway core, and its native
Ethernet frontend, are specified in
[19-Unified_CAN_Gateway.md](19-Unified_CAN_Gateway.md).

## Decisions fixed by this document

1. **Linux owns the bridge configuration.** The SocketCAN netdevice is the
   source of truth. Nominal timing, data timing, FD mode, and transmitter delay
   compensation (TDC) are requested through rtnetlink (`ip link`), not compiled
   independently into the USB bridge.
2. **The existing mainline `gs_usb` driver is retained.** HELIX implements the
   standard FD-capable `gs_usb` device protocol; it does not require a custom
   kernel module.
3. **The interface has an HELIX identity.** USB descriptors report manufacturer
   `OpenAMS` and product `Helix CAN-FD Bridge`. A stable systemd link rule names
   the SocketCAN interface `helixcan0`.
4. **The bridge does not pretend to be a CAN node.** Its own MCU command channel
   is a standard USB CDC ACM interface in the same composite USB device. Klippy
   addresses it by its real chip-derived USB serial path, never by an invented
   `canbus_uuid`.
5. **Every bus bootstraps in Classical CAN at 1 Mbit/s.** Discovery,
   negotiation, abort, bootloader entry, and recovery commands always have a
   Classical-CAN representation no larger than eight data bytes.
6. **The nominal arbitration rate remains 1 Mbit/s.** The selected data rate is
   1, 2, 5, or 8 Mbit/s. The 1 Mbit/s profile uses FD frames without bit-rate
   switching; faster profiles use BRS. Changing the nominal rate is outside
   this version because it removes the universal recovery channel.
7. **ISO CAN FD only.** Non-ISO FD mode is not negotiated or emitted.
8. **FD is enabled by unanimous, verified agreement.** A required participant
   that cannot prepare the requested profile prevents activation. Linux must
   never report an active FD profile while the physical bus silently runs a
   different one.
9. **USB SOF is not forwarded as an untimestamped 1 kHz packet stream.** Matched
   SOF observations discipline the bridge to machine time; the bridge transfers
   that time to CAN nodes with two-step, hardware-SOF-timestamped CAN beacons.
10. **The whole vertical slice lands before physical testing.** Firmware,
    bridge, host transport, Klipper configuration, negotiation, recovery,
    timestamp transfer, bootloader fallback, observability, and documentation
    are implemented together. Workstation unit, simulation, build, and source-
    contract tests still run continuously; what is deferred is piecemeal
    hardware qualification of an intentionally incomplete stack.

## Topology and ownership

The reference topology has three distinct identities:

```text
Linux host
  |
  | USB composite device
  +-- interface 0: gs_usb --------------------> helixcan0
  |                                               |
  +-- CDC ACM: bridge MCU control/time ----------+-- EBB36
                                                  +-- FPS
                                                  +-- other CAN nodes
```

The `gs_usb` interface is a network adapter. The CDC ACM interface is the
bridge MCU itself. Conflating them is what causes the current bridge MCU to
appear as a fictitious local CAN peer. Separating them gives Klippy an ordinary
serial MCU connection for firmware commands, restart, identify, USB SOF, and
diagnostics while SocketCAN exclusively owns traffic to physical CAN nodes.

Linux's mainline `gs_usb` driver matches USB interface zero for the retained
VID/PID, so the CDC interfaces can bind independently to `cdc_acm`. The bridge
firmware must budget endpoint numbers and packet memory explicitly; failure to
fit the composite descriptor on a target is a build error, not a reason to
restore the virtual-CAN-node shortcut.

## Klipper configuration and real identities

### Configuration surface

HELIX adds one named bus object and makes each CAN MCU reference it:

```ini
[mcu can_bridge]
serial: /dev/serial/by-id/usb-OpenAMS_Helix_CAN-FD_Bridge-<device-serial>
restart_method: command

[helix_can helixcan0]
interface: helixcan0
bridge_mcu: can_bridge
nominal_bitrate: 1000000
preferred_data_bitrates: 8000000, 5000000, 2000000, 1000000
require_fd: True
classic_node_policy: refuse
time_source: usb_sof_can_timestamp

[mcu ebb36]
canbus: helixcan0
board_id: <value-reported-by-HELIX_CAN_SCAN>

[mcu fps]
canbus: helixcan0
board_id: <value-reported-by-HELIX_CAN_SCAN>
```

Angle-bracket values above are discovery outputs the operator must replace;
they are deliberately not example UUIDs that could be mistaken for real
hardware.

`board_id` is the board-reported canonical hardware identity: the full native
unique-device identifier when silicon supplies one, with a typed prefix that
prevents two MCU families from sharing a namespace. A provisioned public-key
fingerprint may supersede the raw silicon identifier without changing this
configuration surface. HELIX does not call a six-byte hash a UUID.

The upstream-compatible `canbus_uuid` option remains accepted for existing
printer configurations and tools. Internally it is explicitly a legacy 48-bit
discovery handle, not the canonical identity. New HELIX documentation and
pairing output use `board_id`. Discovery returns the full identity in
sequenced, CRC-protected Classical-CAN fragments because it cannot fit in one
eight-byte admin frame. The host rejects a handle collision instead of
guessing which board was addressed.

The bridge MCU uses only `serial:`. It has no `canbus_uuid`, consumes no CAN
node ID, and never appears in `HELIX_CAN_SCAN` as a downstream device.

### Host object ownership

`[helix_can helixcan0]` is the sole owner of:

- interface bring-up and profile state;
- the required-node inventory;
- Classical bootstrap discovery and node-ID assignment;
- capability intersection and profile epochs;
- the `CAN_RAW_FD_FRAMES` socket mode and active CAN payload MTU;
- time-source quality and CAN timestamp health;
- fallback, bus-off recovery, and requalification.

Node IDs are allocated per named bus, keyed by `(bus, board_id)`. The current
global `canbus_ids` dictionary is insufficient for two CAN interfaces and must
be replaced or made interface-scoped. No MCU opens its long-lived serialqueue
until its bus object reports `PROFILE_ACTIVE` or an explicitly accepted
Classical fallback.

### Privilege boundary

Klippy remains unprivileged. A small `helix-can-manager` systemd service owns
only `CAP_NET_ADMIN`, configures the netdevice through rtnetlink, and exposes a
versioned Unix-socket API under `/run/helix/`. The asyncio-side Klippy bus owner
performs capability preflight, asks this service to apply the selected profile,
and receives structured failure details. Shelling out through unrestricted
`sudo` is not part of the architecture.

The bridge firmware independently enforces the same transaction. The manager
provides diagnostics and policy; it is not the safety boundary.

## CAN FD profiles and capability intersection

The controller and physical transceiver have separate limits. Firmware can
discover its MCU controller capabilities, but it cannot electrically identify
which transceiver was fitted. Each board build therefore declares a board
profile containing:

- controller FD and ISO-FD support;
- transceiver maximum data rate;
- supported data-rate mask;
- maximum payload and message-RAM layout;
- BRS and automatic/manual TDC support;
- CAN RX and Tx Event timestamp support;
- CAN controller clock and achievable sample points;
- bootloader transport capability.

At minimum the following network profiles exist:

| Profile | Nominal | Data | BRS | Maximum payload |
| --- | ---: | ---: | --- | ---: |
| `FD_1M_NOBRS` | 1 Mbit/s | 1 Mbit/s | off | 64 bytes |
| `FD_2M_BRS` | 1 Mbit/s | 2 Mbit/s | on | 64 bytes |
| `FD_5M_BRS` | 1 Mbit/s | 5 Mbit/s | on | 64 bytes |
| `FD_8M_BRS` | 1 Mbit/s | 8 Mbit/s | on | 64 bytes |
| `CLASSIC_1M` | 1 Mbit/s | n/a | n/a | 8 bytes |

The manager chooses the first preferred profile supported by the bridge and
every required node. It also validates realizable nominal/data sample points,
SJW, and TDC—not merely the integer bit rate. Each node computes registers for
its own FDCAN clock, returns the achieved timing, and rejects a profile outside
the network tolerance.

At data rates requiring TDC, the profile carries TDC mode and offset bounds.
The hardware's measured delay value and error counters are exposed as
telemetry. An unqualified hard-coded TDC value is not accepted as an 8 Mbit/s
production profile.

## Transactional `ip link`

The mainline driver already sends separate `GS_USB_BREQ_BITTIMING`,
`GS_USB_BREQ_DATA_BITTIMING`, and `GS_USB_BREQ_MODE` requests. HELIX gives
those requests transactional firmware semantics:

1. `BITTIMING` and `DATA_BITTIMING` validate and populate a
   `staged_profile`; they do not change the physical controller.
2. `MODE RESET` stops host data forwarding and causes a clean FD-to-Classical
   quiesce. The bridge may retain its internal Classical admin receiver while
   the Linux netdevice is down.
3. `MODE START` starts a bounded prepare transaction while the bus still emits
   only Classical frames.
4. The bridge broadcasts `PROFILE_PREPARE(profile, epoch)` and requires a
   reply from every registered required participant.
5. Each node verifies that motion is held, validates the profile, programs the
   inactive/staged FDCAN state, reads it back, and returns a timing digest.
6. On any refusal, timeout, identity mismatch, readback mismatch, or unknown
   active participant, the bridge broadcasts `PROFILE_ABORT`, retains the old
   or bootstrap profile, and fails/stalls the USB control request.
7. After unanimous readiness, the bridge issues `PROFILE_COMMIT`. Nodes apply
   the prepared FD state but continue transmitting Classical frames only,
   acknowledge readback, and enter `FD_ARMED`.
8. The bridge applies its own profile last and completes the USB request.
   Only then does Linux mark `helixcan0` up.
9. Once the host CAN FD socket is ready, the manager sends Classical
   `PROFILE_ENABLE`. This is the only transition that permits FDF/BRS frames.

The profile state machine is:

```text
BOOTSTRAP_CLASSIC -> PREPARING -> FD_ARMED -> FD_ACTIVE
         ^               |           |            |
         |               +-> ABORT <-+            |
         +---------- CLASSIC_DEGRADED <------------+
```

The standard `gs_usb` mode-start control transfer has a one-second host
timeout. The bridge state machine must be asynchronous with respect to MCU
tasks and interrupts but complete within a stricter bounded budget. It must
never busy-wait on endpoint zero. A failure propagates through the ordinary
netdevice open/ioctl path; the manager reports the detailed node and reason
that generic `ip link` cannot encode.

Profile changes are maintenance operations. Klippy must first stop new
Class-0 emission, establish distributed pause-and-hold, drain or invalidate
queued work as appropriate, and verify every affected coordination group is
stationary. A raw profile request during active motion is refused.

## Classical nodes and dynamic entry

An FD-capable controller can send and receive Classical frames. The reverse is
not true: a traditional active CAN controller normally interprets the FDF bit
as a protocol error and emits an error frame. Therefore one electrical segment
cannot carry FD traffic while an active Classical-only controller remains on
it.

At startup:

- `classic_node_policy: refuse` makes the requested FD activation fail and
  names every incompatible node. This is the required policy for a qualified
  HELIX motion bus.
- `classic_node_policy: fallback` allows the higher-level manager to make a
  second, explicit `CLASSIC_1M` request. The failed FD ioctl itself never lies
  by silently falling back.

At runtime, a new HELIX node boots listen-only, receives a periodic Classical
profile beacon, configures the advertised active data timing, and identifies
itself before transmitting. A Classical-only HELIX node announces its
limitation and triggers the configured hold/refuse/fallback policy.

An arbitrary non-HELIX controller cannot be guaranteed to announce itself.
The first evidence may be an error frame against an FD transmission. That
evidence is not by itself sufficient to force a global shutdown: FDCAN already
retransmits and moves through error-warning, error-passive, and bus-off using
the protocol's standardized confinement counters. HELIX reports the physical
errors and state transitions to the host; hardware bus-off is the local
fail-closed boundary. A malformed logical HELIX carrier remains independently
fatal because it indicates incompatible software or a violated negotiated
profile, not ordinary line noise. Recovery sends high-priority Classical
`PROFILE_ABORT`, re-discovers the bus, and latches `CLASSIC_DEGRADED`; FD is
never re-enabled automatically. Machines that require simultaneous legacy and
FD equipment must use separate electrical segments or a gateway.

## CAN frame use and traffic classes

The eight-byte admin protocol always remains Classical. FD payloads are used
selectively after `FD_ACTIVE`:

- urgent stop, profile, discovery, and time-sync frames remain short and
  Classical so recovery never depends on the FD data phase;
- trajectory and reliable command byte streams use 16/32/48/64-byte FD
  payloads according to batching and latency, not blindly 64 bytes;
- telemetry may use FD batches but remains droppable according to
  [03-Traffic_Classes.md](03-Traffic_Classes.md);
- remote-request frames are never emitted in FD format;
- the byte stream framing remains authoritative, so a protocol message need
  not align to a CAN frame boundary.

The host pacing model accounts separately for arbitration-rate bits, data-rate
bits, CRC length, stuffing allowance, inter-frame spacing, and actual FD DLC.
Using `CANBUS_FREQUENCY` alone is no longer an adequate release-time model.

## Retransmission and stale-frame cancellation

HELIX retains the CAN controller's automatic retransmission for arbitration
loss and isolated bus errors. Arbitration loss is normal contention, not a
delivery failure, and recreating that recovery in firmware would add latency
and failure modes. Hardware retransmission is nevertheless only a link-layer
optimization: a CAN acknowledgement proves that at least one active controller
accepted the physical frame, not that the addressed HELIX node consumed it.
The existing framed-protocol acknowledgement and sequence machinery therefore
remains the end-to-end authority.

Automatic retransmission is bounded by application time. Every pending
transmission records its traffic class, profile/time epoch, enqueue time, and
latest useful arrival time. Firmware monitors pending Tx buffers, Tx Event
FIFO results, protocol-error state, and error counters. Once a frame cannot
arrive while it is still useful, firmware requests Tx cancellation and reports
the outcome. An expired Class-0 frame is never delivered late or silently
requeued into a new epoch.

The retry policy is traffic-class specific:

| Traffic | Hardware retry policy | Deadline outcome |
| --- | --- | --- |
| Scheduled motion/extrusion | Retry while the execution-horizon margin remains | Cancel, enter transport hold, and require rebase |
| Reliable control/configuration | Retry within a larger bounded window; protocol ARQ remains authoritative | Fail the transaction or connection explicitly |
| Time/profile/abort control | Highest CAN priority, short bounded retry window | Invoke the corresponding local hold/fallback rule |
| Telemetry | One attempt or a small bounded budget | Drop and count; never block control traffic |

The FDCAN transmit path uses priority queue semantics or dedicated critical
buffers, so low CAN identifiers for abort, profile, and time traffic can pass
bulk FD traffic. A FIFO that permits a retrying telemetry or trajectory frame
to head-of-line-block an abort is not conforming.

Controller-wide one-shot mode (`CCCR.DAR=1`) is not the default. On M_CAN it
also cancels a frame that merely lost arbitration and is not a convenient
per-frame policy. It may be used only by a separately qualified,
fully-time-triggered profile. The default leaves `DAR=0`, performs fast
hardware retry, and imposes the HELIX deadline with Tx cancellation from task
context. A cancellation race is resolved from the Tx Event/cancellation-finish
result: successfully transmitted is accounted as sent, successfully cancelled
as unsent, and neither state is guessed.

Persistent error growth or warning/passive state is surfaced to host policy;
the CAN controller continues its specified retransmission and error-confinement
behavior. Cancellation failure, malformed Helix FD carriers, or hardware
bus-off force distributed hold and the explicit `CLASSIC_DEGRADED` recovery
path. An arbitrary count of recoverable physical errors is not itself a global
firmware-shutdown threshold.
Safety never depends on an emergency frame eventually winning an unhealthy
bus: each node's local watchdog and epoch rules remain authoritative.

Composite-bridge reset creates a short enumeration race: CDC ACM can reconnect
before `gs_usb` has recreated the named SocketCAN interface. The profile
manager therefore retries the complete down/configure/up transaction for only
the transient missing-device, broken-pipe, network-down, and early-link-up
cases, bounded to three seconds. All other configuration errors fail
immediately, and exhaustion remains a Klippy configuration error.

## USB SOF to hardware-timestamped CAN machine time

### Why raw SOF forwarding is rejected

Forwarding every USB SOF as an ordinary CAN packet would consume bus
arbitration 1,000 times per second and would timestamp software scheduling and
CAN arbitration delay rather than the USB edge. Forwarding only occasional
SOFs without a CAN hardware timestamp has the same correctness defect at a
lower packet rate. It is not a substitute for timestamping.

### The adopted chain

USB SOF and CAN timestamping are complementary:

```text
primary machine clock
       |
       | matched USB SOF frame numbers
       v
bridge timer disciplined to machine time
       |
       | FDCAN Tx Event SOF timestamp + two-step follow-up
       v
node FDCAN RX SOF timestamp
       |
       v
node machine-time -> local-time discipline
```

1. The primary MCU and composite USB bridge use the existing brief SOF capture
   windows. Matching USB frame numbers give an exact observation pair; delayed
   or `PRIMASK`-pending STM32 observations remain discarded under the existing
   rule.
2. The bridge disciplines its timer to machine time using the current
   offset/rate filter. If the bridge is itself the primary, this mapping is
   identity and USB SOF pairing is unnecessary.
3. At the machine-time beacon cadence, the bridge queues a high-priority
   Classical `CAN_TIME_SYNC(seq, epoch, quality)` frame with Tx Event FIFO
   storage enabled.
4. The M_CAN/FDCAN peripheral captures the bridge timestamp at actual start of
   frame transmission. Arbitration or firmware delay before SOF is therefore
   excluded.
5. Every receiving FDCAN node retains the RX FIFO element's hardware timestamp
   from actual start of frame reception, even if its IRQ is serviced late.
6. When the bridge reads the Tx Event FIFO timestamp, it converts that exact
   bridge tick through the SOF-disciplined map and sends a Classical
   `CAN_TIME_FOLLOW_UP(seq, machine_clock_at_tx)` frame.
7. Each node associates the follow-up with its retained RX timestamp and feeds
   that pair into the existing machine-time discipline. Follow-up delivery
   latency is irrelevant.

The internal M_CAN counter is only 16 bits, so firmware extends it with
wraparound tracking and rejects an ambiguous timestamp older than one wrap.
The configured prescaler must provide enough resolution while keeping the
follow-up comfortably inside that interval. The constant cable/transceiver
propagation term is measured during qualification and included in the error
budget; it is not confused with arbitration latency.

Every time sample carries or inherits:

- profile epoch and time epoch;
- source type (`usb_sof_can_timestamp` or direct primary CAN timestamp);
- last exact SOF source age and quality;
- TX/RX timestamp wrap health;
- sequence continuity and missed-pair counters.

If SOF freshness, bridge discipline, Tx timestamp, or RX timestamp is invalid,
the sample is discarded. Nodes freewheel on the last qualified mapping and
eventually refuse new Class-0 work under the existing five-second freshness
rule. Software ISR-entry time is never substituted silently.

The bridge also advertises standard `GS_CAN_FEATURE_HW_TIMESTAMP` and appends
the extended bridge CAN timestamp in `gs_usb` frames for host diagnostics. The
host-visible timestamp is useful evidence but is not in the node-discipline
critical path.

## Bootloader and recovery invariants

Bootloader entry is always preceded by a maintenance hold and explicit
Classical quiesce: normally `CLASSIC_1M`, or the allowlisted
`CLASSIC_125K`, `CLASSIC_250K`, or `CLASSIC_500K` compatibility profile for a
bootloader known to use that rate.
A Classical-only Katapult/CanBoot image must never be entered while any
participant may continue FD transmission. The HELIX
first-class bootloader may later advertise FD capability, but the universal
Classical recovery floor remains permanent.

Bridge reset, USB disconnect, host crash, bus-off, or negotiation timeout must
leave nodes able to receive Classical abort/profile beacons. A watchdog on the
bridge-manager heartbeat causes every node to stop FD transmission locally;
it does not require `ip link` to be alive. Active motion follows
[08-Failure_Recovery.md](08-Failure_Recovery.md): distributed hold first,
re-discovery and time reconvergence second, rebase/resume only after the
profile and time epochs agree.

## Concrete implementation map

### Generic firmware

- `src/generic/canbus.h`: expand `canbus_msg` to 64 data bytes; add explicit
  FD/BRS/ESI flags, actual-length helpers, and DLC conversion.
- `src/generic/canserial.c`: negotiate an 8- or 64-byte carrier MTU, retain
  Classical admin traffic, and prevent FD emission outside `FD_ACTIVE`.
- `src/generic/canbus.c` and `src/Kconfig`: replace the single compiled bus
  frequency as the runtime truth with bootstrap timing plus controller and
  transceiver capability declarations.
- Add a transport-profile state machine shared by bridge and node builds,
  including prepare/readback/commit/enable/abort, epochs, heartbeat, and local
  error-triggered fallback.

### STM32 FDCAN

- `src/stm32/fdcan.c`: implement ISO FD TX/RX, all DLC mappings through 64
  bytes, FDF/BRS/ESI, `DBTP`, `CCCR.FDOE`, `CCCR.BRSE`, `TDCR`, protocol/error
  status, bus-off recovery, and exact message-RAM sizing.
- Add staged timing calculation and readback for 1/2/5/8 Mbit/s profiles;
  reject non-integral or out-of-tolerance timing rather than rounding without
  disclosure.
- Enable RX-element SOF timestamps, Tx Event FIFO timestamps, timestamp
  wraparound extension, marker/sequence association, and counters for lost or
  ambiguous events.
- Keep the FDCAN ISR bounded: copy metadata/data, acknowledge hardware, and
  defer negotiation and discipline math to task context.

### USB bridge

- `src/generic/usb_canbus.c`: implement standard FD `gs_usb` feature bits,
  extended timing constants, data-bit-timing requests, mode flags, variable
  Classical/FD host-frame sizes, BRS/ESI, hardware timestamps, and real error
  reporting.
- Replace ignored timing/mode writes with staged transactional behavior.
- Build composite descriptors with `gs_usb` on interface zero and CDC ACM on
  separate interfaces/endpoints; route local MCU protocol exclusively over
  CDC.
- Set USB strings to `OpenAMS` / `Helix CAN-FD Bridge` and retain the
  chip-derived USB serial.
- Add endpoint/PMA compile-time budget checks and reset/re-enumeration tests.

### Host and Klipper

- `klippy/chelper/serialqueue.c`: use `struct canfd_frame`, accept both
  `CAN_MTU` and `CANFD_MTU`, set `CAN_RAW_FD_FRAMES`, fragment at negotiated
  payload size, and implement FD-aware wire-time accounting.
- `klippy/serialhdl.py`: open FD-capable SocketCAN sockets, retain Classical
  assignment/bootstrap, carry negotiated profile metadata, and reconnect
  without assuming eight-byte frames.
- `klippy/mcu.py`: add `canbus:` plus canonical `board_id:` selection, connect
  the bridge MCU through ordinary `serial:`, and gate MCU attach on its named
  bus profile.
- `klippy/extras/canbus_ids.py`: key assignments by bus and canonical identity,
  support fragmented identity discovery, and retain a clearly marked legacy
  `canbus_uuid` adapter.
- Add `klippy/extras/helix_can.py`: asyncio-side bus owner, node inventory,
  capability intersection, profile lifecycle, Klippy hold integration, status,
  and Atlas incident emission.
- Add the privileged `helix-can-manager` service and versioned Unix API; install
  a systemd `.link` rule that matches the bridge USB serial/path and names the
  interface `helixcan0`.
- Add `scripts/helix_can_scan.py` to print real board IDs, legacy handles,
  capabilities, selected profile, time-source quality, and incompatibilities.

### Protocol, status, and documentation

- Allocate and document Classical admin/profile/time message IDs without
  colliding with Klipper assignment, emergency, or bootloader traffic.
- Extend `CANBUS_protocol.md`, `CANBUS.md`, `Config_Reference.md`,
  `MCU_Commands.md`, `Status_Reference.md`, and the test plan only when the
  corresponding behavior exists; until then their current classic-only text
  remains the operational truth.
- Atlas records negotiation failures, profile changes, Classical-node entry,
  FD error fallback, timestamp-source loss, bus-off, and recovery as structured
  incidents rather than console-only strings.

## Implementation order and single hardware gate

Implementation proceeds in dependency order, but does not create separate
physical-test products:

1. [x] Freeze wire/state-machine definitions and identity/configuration schema.
2. [x] Implement the generic 64-byte carrier and complete STM32 FDCAN driver.
3. [x] Implement composite USB, full FD `gs_usb`, transactional profile control,
   product strings, and stable interface naming.
4. [x] Implement CAN hardware timestamps and the SOF-derived two-step time path.
5. [x] Implement host CAN FD transport, named bus object, canonical identities,
   manager privilege boundary, and Klippy lifecycle gates.
6. [x] Implement mixed-node containment, bus-off/USB-reset recovery, bootloader
   Classical quiesce, failure-recovery integration, status, Atlas incidents,
   and operational documentation.
7. [x] Complete the CAN-FD workstation regression and adversarial source review
   of the vertical slice. The full Helix host set, intentproto library/C ABI,
   Klippy import, and 1/8 Mbit node plus composite-bridge builds pass on
   2026-07-16.
8. [ ] Complete physical qualification. The conservative `FD_1M_NOBRS`
   electrical, carrier, session-restart, and machine-time base passed on
   2026-07-16; injected recovery, CAN motion/printing, and the same state
   machine at 2/5/8 Mbit/s remain open.

The 1 Mbit/s run is the first electrical qualification point because existing
transceivers can exercise it; it is not permission to omit BRS, TDC,
negotiation, mixed-node handling, timestamping, or recovery from the software
implementation that reaches the bench.

The focused Python/C contract tests, host helper build, and both STM32G0B1
node and composite-bridge builds pass. Step 8 remains unchecked so the verified
1 Mbit/s subset cannot be mistaken for recovery, motion, or faster-transceiver
qualification.

## 2026-07-16 physical qualification checkpoint

The FPS STM32G0B1 enumerated as the composite `OpenAMS` / `Helix CAN-FD
Bridge`; mainline `gs_usb` bound interface zero and the checked-in link rule
provided `helixcan0`. The EBB36 was discovered by its full canonical identity,
assigned without a synthetic bridge node, and the complete profile transaction
activated `FD_1M_NOBRS`. Linux read back MTU 72 with 1 Mbit/s nominal and data
timing. At the recorded checkpoint the link was `ERROR-ACTIVE` with zero bus
errors, arbitration loss, warning/passive transitions, bus-offs, receive
drops, or missed frames.

The first sustained MCU run exposed two physical-only defects. STM32 FDCAN
message RAM requires aligned word transfers, and CAN-FD cannot represent
payload lengths 9..11, 13..15, and the other gaps in its DLC table. Passing
such a byte count to hardware silently rounds the DLC. The aligned-access fix
and a `can-utils` sweep then captured all 16 legal physical payload sizes
(0..8, 12, 16, 20, 24, 32, 48, and 64) without controller error growth.

A longer passive capture exposed the more important carrier consequence. The
old byte-stream fragmenter sent a 22-byte MCU-protocol block as legal 20- and
2-byte frames. On multiple runs the two-byte tail was absent from the host
capture even though Linux reported zero dropped/missed packets, the FDCAN lost
FIFO counter remained zero, the bridge's 32-frame forwarding queue had zero
drops and a high-water mark of two, and the EBB36 reported no transmit error or
retry. This reproduces the long-standing operational complaint that SocketCAN
statistics do not prove application delivery; those counters cover different
layers and cannot identify every loss after the adapter accepts a frame.

The carrier therefore no longer fragments a protocol message. Each raw message
is already 5..64 bytes and contains its own logical length, CRC, sync trailer,
and sequence. HELIX restores the multi-message write batching introduced by
upstream commit `c5968a08`: it packs as many complete raw messages as fit in
one CAN-FD frame, never splits a message, selects the smallest physical DLC
that contains the packed prefix, and zero-pads only after the final message.
The receiver walks each in-band message length. An isolated 22-byte message is
one 24-byte physical frame, multiple short messages with different sequences
share a frame, and a 64-byte message remains one frame. Losing a frame can
remove several complete messages, but can never splice a missing tail to the
next frame. The bridge additionally reports
the number of frames accepted from FDCAN and completed into the USB handoff,
alongside FIFO loss, queue drops, high-water, and controller errors. Focused
tests cover every 5..64 byte record and the 22-byte regression; physical
captures now confirm the same boundaries on the FPS/EBB36 link.

The first instrumented restart also demonstrated why those counters cannot be
collapsed into Linux's `dropped=0`: bridge conservation totals were 2,398
FDCAN frames accepted, 1,946 USB handoffs, and 452 explicit staging-queue
drops, while SocketCAN still showed zero drops. The original 32-record queue
was undersized for the MCU configuration-response burst because each 72-byte
`gs_usb` FD record spans two full-speed USB packets. A 128-record repeat
accepted 1,596 frames, forwarded 1,362, and explicitly dropped 234. With the
correct complete-record packer, a 256-record repeat accepted 4,000, forwarded
3,876, and dropped 124 at a high-water mark of 256.

The final 512-record bridge closed that finite burst without hiding it:
repeated cold/session reconnects forwarded all 37,288 accepted frames, returned
to depth zero, reached a bounded high-water mark of 434, and retained zero
queue drops and zero unaccounted handoff. A physical 1,013-frame capture decoded
1,070 complete protocol records, including 56 multi-record frames, with no
invalid carrier. Three following profile transitions retained zero invalid or
retransmitted bytes at the Classical-to-FD parser boundary. The G0B1 bridge
therefore uses the measured 512-record elasticity budget. Queue capacity does
not qualify a future BRS profile; profile admission must still respect
worst-case encoded `gs_usb` throughput on USB Full Speed.

A subsequent trajectory print isolated a different queue boundary on the
downstream EBB36. At print start its CAN receive error and Klippy retransmit
counters were both zero. After roughly 943 seconds of mixed XY and extrusion
traffic, the EBB36 aggregate receive count had reached 11,114 and Klippy had
retransmitted 669,735 bytes. Immediately before the extruder trajectory
underrun, its retransmission timeout jumped to 537 ms with 300 bytes waiting.
At the same boundary the bridge and SocketCAN remained error-active and
lossless: accepted and forwarded frame totals were equal, with zero bridge
queue drops and zero unaccounted handoff. `RESUME_MOTION` reconciled all four
joints and the print continued without a firmware restart.

The STM32 FDCAN IRQ had acknowledged and processed only one RX FIFO element per
RF0N event even though a deferred interrupt can find all three hardware slots
occupied. The remaining elements could be stranded until another arrival and
eventually produce RF0L loss. The handler now clears the event and drains the
bounded hardware FIFO in one service pass. New diagnostics split the legacy
aggregate into `rx_fifo_overruns` and `rx_protocol_errors` and retain the FIFO
high-water mark.

Physical closure passed on 2026-07-17 with firmware `219569e9` on both the FPS
bridge and EBB36. Moonraker job `0000ED` repeated the same
`Voron_Design_Cube_v8_0.4n_0.2mm_PLA_V0_120_26m.gcode` workload and completed
1,733.405 seconds of motion with 3,637.983 mm of filament. The EBB36 received
4,577,247 bytes with zero retransmitted or invalid bytes. Its hardware FIFO
high-water reached two of three entries—direct evidence that multi-entry
service was exercised—while `rx_fifo_overruns`, `rx_protocol_errors`, and the
compatibility `rx_error` aggregate all remained zero. The link retained an
approximately 1 ms RTT and the 25 ms minimum RTO instead of the previous
537 ms escalation.

Final `HELIX_CAN_STATUS` conservation was exact: 236,528 bridge frames were
accepted and forwarded, queue depth returned to zero, high-water was three,
and drops and unaccounted handoff were zero. SocketCAN reported 294,844 RX and
61,130 TX packets with zero errors, drops, missed frames, warning/passive
transitions, or bus-offs. The print crossed the former roughly 943-second
underrun boundary and completed its end macro without trajectory recovery or
`Timer too close`. This closes the EBB receive-drain physical regression for
the 1 Mbit `FD_1M_NOBRS` profile; injected bus faults and faster BRS profiles
remain separate qualification items.

### Receive-window/FDCAN FIFO partition (2026-07-21)

A later successful long print exposed seven sparse EBB36 FIFO losses despite
the bounded drain fix above. Bridge conservation remained exact: all 653,122
frames accepted by the bridge were forwarded, its queue had zero drops and
zero unaccounted handoff, and SocketCAN reported no loss. The remaining defect
was a capacity mismatch inside the node, not a slow Linux bridge.

The reliable byte stream advertises a 192-byte receive window, which permits
three 64-byte command frames to be outstanding. STM32G0/G4 FDCAN provides
three entries in each receive FIFO. Helix had routed the three-credit command
stream, two-step time sync/follow-up, administration, and node-control frames
through FIFO0. A motion critical section may legally defer the FDCAN IRQ; a
full three-frame data window therefore left no slot for independent control
traffic. Draining pending entries after the IRQ ran could not recover a frame
already overwritten by hardware.

The filter layout now treats the two receive FIFOs as separate traffic planes:

- FIFO0 contains only the assigned node's reliable host-to-MCU command stream;
  its three hardware entries exactly match the protocol's three-frame credit.
- FIFO1 contains administration, time sync, time follow-up, and assigned
  ID+1 conflict/control traffic.
- A single bounded ISR drains and acknowledges both FIFOs. It acknowledges
  each copied element before protocol dispatch so parsing cannot retain scarce
  hardware capacity.
- Diagnostics retain the compatibility aggregate and add per-FIFO overrun and
  high-water counters plus maximum start-of-frame-to-service ticks.

The no-I/O `HELIX_CAN_RX_STRESS` regression queues three near-maximum command
records while the MCU masks interrupts for a firmware-capped 2 ms. Five
hundred iterations on the final 64 MHz EBB36 image
`5383b0a9-dirty-20260721_010324-linuxathena` (flash verification SHA
`14281554836FC343FE956CA45E3D9D00BB0CD6F7`) completed in 2.027 seconds.
Both FIFO high-water marks reached two while FIFO0/FIFO1 overruns, protocol
errors, retransmissions, invalid bytes, and SocketCAN drops remained zero. The
maximum start-of-frame-to-service interval was 152,153 ticks (2.377 ms), which
includes the deliberately injected 2 ms IRQ hold and frame wire time. A
boundary-only 5 ms experiment also retained zero receive loss but correctly
tripped Klipper's unrelated late-timer guard; the shipped diagnostic is
therefore immutably capped at the proven-safe 2 ms. All 200 preceding live
trajectory-kernel suites also passed with zero receive errors.

On 2026-07-20 a later print exposed an incorrect policy boundary in the first
implementation: eight recoverable physical FDCAN errors inside 10 ms caused
`MCU 'canbridge' shutdown: CAN FD protocol error burst`, although the
controller was still capable of retransmission and error confinement. The
physical-error counter is now diagnostic; malformed logical carriers retain
their bounded fatal gate, and the explicitly enabled hardware bus-off IRQ is
the physical fail-closed signal. The same incident exposed a composite-reset
enumeration race in which CDC returned before `gs_usb` recreated
`helixcan0`. A bounded transient-only manager retry was installed, and a live
`FIRMWARE_RESTART` subsequently returned the printer to Ready with
`FD_1M_NOBRS`, zero bridge errors/drops, and exact accepted/forwarded frame
conservation without USB replug.

The resumed run produced the same signature again: the EBB36 aggregate reached
20,534 receive errors and 1,226,991 retransmitted bytes while bridge
conservation remained exact. The extruder underrun correctly entered recovery,
but 1.3 seconds later an already delayed soft-PWM update reached the EBB36 and
the generic scheduler raised `Timer too close`. Helix now marks software-PWM
updates as late-applicable on firmware that advertises the traffic-class
policy: a delayed duty update is applied promptly while the independent
`max_duration` heater watchdog remains armed. This prevents stale prompt state
from converting an established trajectory hold into a global MCU shutdown;
true Class-0 motion timing remains fail-closed.

Bridge maintenance uses an explicit state boundary rather than a queue-size
assumption. `HELIX_CAN_QUIESCE BUS=<name>` waits for queued motion, stops the
time source, sends the FD-abort transaction to every downstream node, and asks
the constrained manager to read back Classical 1 Mbit. The operator then stops
Klipper before entering the bridge bootloader, preventing automatic reconnect
from reactivating FD between quiesce and reset.

For retained Katapult/CanBoot images compiled at legacy Classical rates, the
same command accepts the allowlisted `CLASSIC_125K`, `CLASSIC_250K`, or
`CLASSIC_500K` profile. Those profiles are maintenance-only: they are absent
from capability intersection and application negotiation. The composite
bridge now applies SocketCAN's exact runtime nominal timing to FDCAN instead of
silently requiring its compile-time 1 Mbit value. FPS hardware readback
qualified the 500 kbit Classical transition and the return to 1 Mbit. The
allowlisted manager still owns the sole `CAP_NET_ADMIN`; arbitrary rates and
interfaces remain impossible through its socket.

The retained EBB36 v1.2 vendor bootloader remains explicitly unqualified. The
application accepted its verified legacy-handle reboot, but the preserved
Katapult image (`v0.0.1-79-g25a23cd`) answered on none of the standard
125/250/500 kbit or 1 Mbit Classical rates and did not enumerate on USB. The
same bridge applied and read back every tested timing. This isolates the defect
to that bootloader's vendor pin/clock/transport configuration. A known PB0/PB1,
8 MHz-reference, 1 Mbit Katapult image must be installed through DFU and then
qualified by a complete CAN application flash.

### Mandatory bridge-rate admission

The bridge architecture is valid only when its effective forwarding service
rate is greater than the encoded CAN offered rate for the selected profile.
This is a system invariant, not an implementation recommendation. Raw link
labels are insufficient: an 8 Mbit CAN data phase is not compared directly to
"12 Mbit" USB Full Speed. Admission includes arbitration at the nominal rate,
actual message-size distribution, Kevin's multi-message serial batching
(`c5968a08`), fixed `gs_usb` host-record expansion, USB packetization, endpoint
service cadence, and host scheduling.

Bridge RAM is only burst elasticity. A larger queue is acceptable when a
finite producer burst reaches a bounded high-water mark, then drains, while
the exact conservation invariant remains true:

```text
FDCAN frames accepted
    = USB records fully handed off + explicit queue drops + current depth
```

If depth or latency grows with test duration, no finite queue makes the
profile safe. HELIX must refuse that profile on that bridge or require a faster
upstream transport. Qualification at 1 Mbit does not imply 2/5/8 Mbit
qualification; every profile gets a sustained worst-case saturation run and
must show zero FIFO/queue loss, zero unaccounted handoff, bounded high-water
with margin, and complete drain after load.

Powered-board session takeover was repeated across three Klipper process
restarts. Each run received the EBB36 session-reset acknowledgement, cleared
the stale framed sequence, renegotiated FD, loaded the full MCU dictionary, and
returned the printer to ready without reflashing or cycling board power.

The bridge and EBB36 also reached firmware `flags=7` convergence. The EBB36
uses the bridge's FDCAN Tx Event and its own RX-element timestamp, so USB and
host scheduling delay are excluded from the downstream sample. On this
workstation the Pico and FPS sit in USB frame-number domains that never produce
an exact same-frame pair. After eight unclassified probes, Helix therefore
disables that optional optimization and continuously refreshes the bridge from
the qualified minimum-RTT host clock regression. A positively identified
IRQ-guard discard still retains bounded holdover; an unclassified miss never
freezes a merely stable or eventually stale mapping.

## Acceptance matrix after implementation

- Classical-only regression with upstream-compatible configurations.
- Full 0..64-byte DLC/length, FDF/BRS/ESI, padding, and malformed-frame tests.
- Arbitration loss and an isolated injected error retransmit successfully;
  expired Class-0 work is cancelled rather than delivered late.
- A retrying bulk frame cannot head-of-line-block abort, profile, or time
  traffic, and telemetry retry exhaustion is counted without taking the bus
  down.
- `ip link` requested timing appears in bridge register readback; unsupported
  timing makes netdevice open fail.
- Capability intersections select 8, 5, 2, or 1 Mbit/s exactly; a Classical
  node causes explicit refusal or a second explicit fallback transaction.
- Failure of any required node during prepare/readback prevents bridge commit.
- An FD error storm suppresses FD locally on every node and leaves Classical
  recovery traffic operational.
- Bridge MCU connects through CDC with no CAN node ID or fake UUID.
- Stable USB strings and `helixcan0` naming survive replug and firmware restart.
- `CAN_RAW_FD_FRAMES` host sockets receive interleaved Classical and FD frames.
- Byte-stream framing remains correct across arbitrary CAN FD chunk boundaries.
- USB SOF -> bridge -> CAN-node timestamp pairs remain valid under delayed ISR
  service because timestamps are captured in FDCAN message RAM/Event FIFO.
- Timestamp wrap, missing follow-up, stale SOF source, bridge reset, and epoch
  mismatch all discard samples and reach bounded holdover/fail-closed behavior.
- Bootloader entry forces Classical quiesce before reset and application return
  renegotiates FD from bootstrap.
- Bus-off, bridge USB removal, host-manager restart, and a joining
  Classical-only device produce bounded hold/recovery behavior.
- Sustained trajectory, extrusion, and FPS telemetry load at each qualified
  profile preserves queue horizon, time convergence, and zero unexplained CAN
  error growth.
- Scope/logic-analyzer evidence records sample point, propagation, TDC,
  synchronization error distribution, and physical margins at 1/2/5/8 Mbit/s.

## Primary references

- Linux kernel, [SocketCAN configuration and CAN FD sockets](https://docs.kernel.org/networking/can.html).
- Linux kernel, [mainline `gs_usb` driver](https://github.com/torvalds/linux/blob/master/drivers/net/can/usb/gs_usb.c).
- Bosch, [M_CAN User's Manual](https://www.bosch-semiconductors.com/media/ip_modules/pdf_2/m_can/mcan_users_manual_v331.pdf).
- STMicroelectronics, [RM0444 STM32G0x1 reference manual](https://www.st.com/resource/en/reference_manual/rm0444-stm32g0x1-advanced-armbased-32bit-mcus-stmicroelectronics.pdf).
- CAN in Automation, [CAN FD fundamentals](https://www.can-cia.org/can-knowledge/can-fd-the-basic-idea).
- CAN in Automation, [CiA 1305 layer-setting services](https://www.can-cia.org/can-knowledge/cia-1305-layer-setting-services-lss-for-canopen-fd).
