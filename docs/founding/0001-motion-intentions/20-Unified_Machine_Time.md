# FD-0001: Unified Machine-Time Fabric

Status: architecture and implementation plan. The existing primary-MCU
host-relay, USB-SOF discipline, FDCAN two-step timestamp transfer, affine
MCU filter, convergence gate, and four-timestamp host model remain the
working implementation. This document defines their compatible evolution
into configurable authorities, timestamp adapters, and time bridges. No new
authority-selection or automatic-failover claim is qualified until its
phase-specific gates below pass.

This document extends the original [time model](01-Time_Model.md), the
[transport-derived synchronization study](../../Transport_Time_Synchronization.md),
the [CAN-FD timestamp path](15-CANFD_Transport.md), the
[F767 Ethernet/PTP plan](16-STM32F767_Ethernet.md), and the
[unified gateway architecture](19-Unified_CAN_Gateway.md).

The central decision mirrors the gateway decision:

> USB SOF, Ethernet PTP, CAN two-step beacons, WiFi TSF, dedicated capture
> wires, and software request/reply are not separate machine-time systems.
> They are adapters around one canonical machine-time fabric.

The common boundary is a timestamped observation plus explicit authority,
epoch, uncertainty, and quality. It is not a USB frame number, PTP packet,
CAN timestamp field, GPIO edge, host-monotonic timestamp, or MCU timer value.

## Why the current mechanism must evolve

HELIX currently defines machine time as the primary MCU counter. The host
relays observations to every secondary and each secondary maintains an affine
mapping:

```text
local_ticks = offset + rate * machine_ticks
```

That mechanism works and remains the compatibility default. Physical testing
has nevertheless exposed three architectural facts:

1. **Observation quality belongs to the timestamp substrate.** Matching USB
   SOFs can be exceptionally repeatable; a software-timestamped Ethernet
   exchange can be operationally sound but vary beyond the same admission
   window; Ethernet MAC PTP and FDCAN message-RAM timestamps survive delayed
   CPU service.
2. **The best authority can change with topology.** A standalone printer may
   naturally use its primary MCU. A network-native printer or farm may have a
   NIC PHC or PTP grandmaster. An Ethernet-to-CAN gateway may be the boundary
   clock for a downstream bus.
3. **Bridging time is different from forwarding packets.** A gateway must
   translate between clock domains, propagate uncertainty, and identify the
   authority generation. Merely forwarding a beacon across protocols turns
   bridge latency into false clock phase.

The F767 print-start qualification made the policy problem concrete.
Software-timestamped Ethernet inherited the exact USB-SOF profile and briefly
crossed the global gate even though the authenticated link was healthy.
Per-MCU overrides are an appropriate commissioning tool, but they are not the
final architecture: the selected timestamp path should carry its own measured
quality contract.

## Goals

The unified machine-time fabric must:

1. preserve the current primary-MCU authority as a compatible default;
2. allow a primary MCU, NIC PHC, PTP grandmaster, or qualified gateway clock
   to be selected as authority;
3. use one normalized observation and quality contract across USB, Ethernet,
   CAN, WiFi, capture-wire, and software fallback adapters;
4. compose time across protocol bridges without overstating precision;
5. maintain one authority and epoch for every coordination group;
6. make source changes explicit, observable, and motion-safe;
7. keep the existing fixed-point MCU discipline loop independent of the
   transport that produced an observation;
8. admit Class-0 work from measured uncertainty and freshness, not from a
   transport name or nominal link rate;
9. support deterministic holdover, loss, requalification, and planned
   failover;
10. expose the complete authority-to-actuator time path to Klippy, Atlas,
    tests, and operators; and
11. permit new timestamp substrates without modifying trajectory semantics.

## Non-goals

This architecture does not:

* require UTC, wall-clock time, NTP, or internet access;
* assume that IEEE 1588 capability alone proves a physical error bound;
* make a software callback into a hardware timestamp by changing its label;
* hide topology-specific asymmetry behind one global `+/-10 us` constant;
* silently change authority while scheduled work is active;
* average incompatible clock epochs together;
* require every board to implement every synchronization protocol;
* require a Linux kernel driver for each HELIX transport; or
* make an ordinary Ethernet switch a transparent PTP clock.

## Terminology

**Machine-time authority**
: The clock and epoch that define the canonical timeline for one coordination
  group. It may be a physical MCU counter, a NIC PHC, or another explicitly
  selected monotonic clock.

**Clock identity**
: Stable identity of a physical or logical clock, independent of its current
  network address or transport.

**Timestamp adapter**
: A substrate-specific producer of normalized observations. Examples are USB
  SOF, Ethernet MAC/PTP, FDCAN Tx Event/RX element, WiFi TSF, timer capture,
  and software four-timestamp exchange.

**Discipline sink**
: A local clock mapper that consumes observations. The existing MCU
  `(offset, rate)` PI filter is the first sink implementation.

**Time bridge**
: A boundary-clock component with an inbound disciplined clock and an
  outbound timestamp adapter. It translates authority time across clock and
  transport domains while preserving uncertainty and epoch.

**Time path**
: The directed, acyclic sequence from authority through zero or more bridges
  to a discipline sink.

**Quality generation**
: A monotonically increasing identifier for the assumptions and calibration
  behind a time path. Source change, clock reset, topology change, or
  uncertainty-class change creates a new generation.

## Architectural decomposition

```text
                   selected authority
          primary MCU | NIC PHC | PTP grandmaster
                           |
                  canonical machine time
                           |
              +------------+------------+
              |                         |
       timestamp adapter         timestamp adapter
       USB SOF / capture          Ethernet PTP
              |                         |
       discipline sink             time bridge
       local MCU clock       PTP clock -> gateway timer
                                        |
                              CAN two-step adapter
                                        |
                               downstream CAN sinks
```

The fabric has six interfaces:

1. **Authority provider** publishes clock identity, epoch, frequency,
   monotonic reading, and health.
2. **Timestamp adapter** converts one substrate event into a normalized
   observation.
3. **Observation transport** carries authenticated sync/follow-up/delay
   records without defining their clock semantics.
4. **Time bridge** maps an inbound authority to an outbound timestamp domain.
5. **Discipline sink** updates a local affine mapping and holdover state.
6. **Quality policy** decides whether a path may carry a particular
   coordination scope and traffic class.

This separation prevents a lowest-common-denominator `sync_packet()` API.
USB frame matching, PTP event timestamps, CAN Tx Event FIFOs, and TSF snapshots
have different evidence and failure modes. They become equivalent only after
translation into the canonical observation contract.

## Canonical records

### Authority descriptor

An authority descriptor contains:

```text
authority_id       stable clock identity
authority_epoch    changes on reset or authority replacement
clock_frequency    ticks per second, or rational scale
resolution         smallest represented interval
monotonic          whether the exposed timeline can step backward
source_kind        primary_mcu | phc | ptp_gm | bridge | simulated
health             qualified | degraded | unavailable
quality_generation calibration/topology generation
```

`authority_id` is not an MCU name, IP address, interface name, or USB serial.
Those are routes to the authority, not the authority itself.

### Observation

Every adapter produces the logical equivalent of:

```text
authority_id
authority_epoch
sequence
authority_time
local_clock_id
local_time
source_kind
source_resolution
path_delay_min
path_delay_max
correction
uncertainty
captured_at_age
quality_generation
flags
authentication_context
```

Not every wire message carries all fields. A two-step protocol may capture a
local event first and associate authority time in a follow-up. A compact CAN
frame may imply authority and epoch from the active session. The decoded
observation presented to the discipline sink is complete.

`path_delay_min` and `path_delay_max` preserve asymmetry honestly. A midpoint
estimate may be used by the filter, but a hard assurance claim uses the
interval unless calibrated hardware provides a stronger bound.

### Capture ticket

Two-step adapters retain a bounded capture ticket between event and
follow-up:

```text
source_kind, source_instance, epoch, sequence, local_time, expiry
```

Tickets are fixed-capacity and expire. An authenticated follow-up may consume
one matching ticket exactly once. Replayed, late, mismatched, or ambiguous
follow-ups cannot alter the clock map.

### Clock mapping

A discipline sink publishes:

```text
authority_id, authority_epoch, local_clock_id
offset, rate
uncertainty, age
state
quality_generation
accepted/rejected/holdover counters
```

The current MCU Q8.24 rate and offset representation remains a valid sink
profile. The fabric does not require floating point on firmware.

## Quality is a contract, not a transport name

The fabric uses an ordered assurance vocabulary:

| State | Meaning | Class-0 use |
| --- | --- | --- |
| `HARDWARE_BOUNDED` | Complete physical path has a measured worst-case bound under its declared topology and load | Allowed when the bound meets the coordination scope |
| `STATISTICALLY_QUALIFIED` | Measured distribution and tails meet an application-derived policy, but directional delay is not physically bounded | Allowed only by explicit scope policy |
| `OPERATIONAL` | Converged affine map with freshness and diagnostics, without a completed qualification campaign | Commissioning only |
| `HOLDOVER` | No fresh observation; last qualified rate remains usable until a bounded expiry | Existing work may drain; new Class-0 follows policy |
| `DEGRADED` | Source remains observable but no longer meets the active coordination policy | Hold/requalify; do not silently widen |
| `INVALID` | Reset, epoch mismatch, stale source, replay, or unbounded discontinuity | No Class-0 admission |

The names do not encode a numeric promise. A hardware-referenced timestamp can
still be `OPERATIONAL` until physical testing establishes its bound.

Each coordination group declares a requirement:

```text
maximum uncertainty
maximum age
minimum assurance state
permitted holdover
scope: independent | heater | extruder | coordinated axes | fleet event
```

A slow independent telemetry axis may accept a weaker profile than
cross-board CoreXY motors. Configuration cannot use a weak profile for a
stronger scope merely because both currently report `converged`.

## Time-path graph and uncertainty composition

The fabric models synchronization as a directed acyclic graph. Every edge is a
clock mapping with uncertainty and age; every bridge joins an inbound and an
outbound mapping.

For a path with independent bounded errors:

```text
path_bound <= sum(edge_bounds) + bridge_conversion_bounds
```

Statistical edges retain their distribution metadata and cannot be promoted
to a hard sum. Correlated observations record their shared source so the host
does not pretend they are independent and average them into false precision.

The graph validator rejects:

* more than one active authority for a coordination group;
* cycles, including a bridge disciplined from its own downstream emission;
* an authority epoch mismatch anywhere on the path;
* a quality generation not acknowledged by every downstream sink;
* a path whose composed uncertainty exceeds the scope requirement; and
* an unqualified source selected for production Class-0 work.

## Authority providers

### Primary MCU

The existing behavior remains the default. The primary MCU counter establishes
the epoch, the host estimates its mapping, and secondary adapters consume
observations expressed in that time. This requires no new hardware and keeps
all existing printer configurations valid.

### Host NIC PHC

A Linux PTP Hardware Clock can be the authority for a network-native machine
or fleet. The provider uses the PHC directly; it does not discipline motion to
UTC. System-to-PHC cross timestamps are diagnostics or compatibility mappings,
not replacements for hardware packet timestamps.

This profile is attractive when many Ethernet boards share one NIC and when
host redundancy is planned. Write ownership and clock authority remain
separate leases: becoming PTP master does not grant motion-command ownership.

### External PTP grandmaster

An external grandmaster may be selected when a facility already has a
qualified PTP topology. HELIX records grandmaster identity, domain, steps
removed, and topology generation. A best-master-clock election cannot switch
the machine silently; any new grandmaster enters the normal authority-change
workflow.

### Gateway or MCU boundary clock

A gateway may expose a disciplined local clock as the authority for its
downstream coordination group. The upstream authority identity remains in the
path record. This is a boundary-clock role, not an independent time origin.

## Timestamp adapters

### Host-relayed request/reply

The current transport-neutral fallback remains supported. Min-delay filtering
and robust regression provide an operational or statistically qualified
observation. Directional asymmetry remains in the uncertainty interval.

### USB SOF

Matching USB frame numbers associate a common periodic event. A hardware SOF
capture is stronger than ISR-entry time. Firmware discards observations known
to have been pending across a masked critical section. The adapter reports
capture mechanism and discard attribution.

### Ethernet PTP

The adapter uses MAC RX/TX timestamps and a four-timestamp or two-step PTP
exchange. Linux uses `SO_TIMESTAMPING` and the NIC PHC; MCU firmware uses its
MAC PTP timer and enhanced descriptors. Switch residence time and PHY
asymmetry remain explicit path terms.

### CAN and CAN FD

The adapter uses a high-priority `SYNC` frame, controller TX/RX timestamps,
and an authenticated `FOLLOW_UP`. Arbitration delay is removed by the actual
transmit timestamp. FDCAN Tx Event/RX element timestamps, bxCAN TTCM, and
future PIO capture are capability-specific implementations of the same
adapter contract.

### WiFi TSF and software exchange

TSF may provide a shared radio-domain reference when the exact SoC, driver,
access point, power state, and reconnect behavior are qualified. Otherwise
WiFi uses the authenticated four-timestamp fallback and remains statistical.

### Dedicated capture

A timer/PIO/MCPWM-captured edge supplies transport-independent phase. A
follow-up over any authenticated transport associates authority epoch,
sequence, and edge time. This is the universal high-assurance fallback for
custom hardware.

## Time bridges

A bridge has two independently testable stages:

1. discipline the bridge timer from its inbound authority; and
2. emit a timestamped outbound event tied to that disciplined timer.

For example:

```text
NIC PHC
  -> Ethernet MAC RX/TX timestamps
  -> H723 gateway timer
  -> FDCAN Tx Event timestamp + FOLLOW_UP
  -> EBB36 FDCAN RX timestamp
  -> EBB36 execution timer
```

The bridge emits:

* upstream authority identity and epoch;
* bridge mapping generation;
* outbound event sequence and actual transmit time;
* inbound, conversion, and outbound uncertainty components;
* freshness/holdover state; and
* topology/profile identity.

A bridge never resets uncertainty to zero. If its inbound source enters
holdover, downstream nodes receive the same transition and remaining budget.
If a bridge reboots, changes CAN bitrate, renegotiates Ethernet, changes PTP
grandmaster, or loses timestamp events, it creates a new quality generation.

The gateway's canonical data-record core and the machine-time fabric remain
separate. A frame may be delivered successfully while its timestamp path is
degraded; conversely, a healthy time path does not prove frame delivery.

## Configuration model

The final syntax is introduced only after configuration-parser fixtures pass.
The intended semantics are:

```ini
[machine_time]
authority: phc:enp0s31f6
fallback_authority: mcu
authority_change: hold_requalify

[time_adapter f767_ptp]
kind: ethernet_ptp
transport: f767
authority: phc:enp0s31f6
quality_profile: f767_direct_ptp_v1

[time_bridge toolhead_can]
input: f767_ptp
output: helixcan0
adapter: can_two_step

[time_sink ebb36]
clock: mcu:ebb36
source: toolhead_can

[time_policy motion_xyz]
scope: coordinated_axes
max_uncertainty: 2us
max_age: 2s
minimum_assurance: hardware_bounded
```

Compatibility translation maps the existing `[timesync]` configuration into:

```text
authority = primary MCU
adapter = host relay, with optional USB-SOF refinement
sink = each secondary MCU
policy = current Class-0 convergence/freewheel settings
```

Per-MCU `converge_window_*` and `host_rate_tolerance_ppm_*` remain available
for commissioning and legacy profiles. Production quality profiles should
name the hardware path, topology, evidence artifact, bound, temperature/load
range, and generation rather than silently embedding a larger tolerance.

## Capability discovery and negotiation

Each participant advertises:

* clock identities and timer frequencies;
* supported authority-provider roles;
* timestamp adapter kinds and capture resolution;
* two-step ticket capacity and expiry;
* supported discipline representation;
* maximum sync cadence;
* holdover capability;
* authenticated time-protocol versions; and
* hardware timestamp/error counters.

The host builds the time-path graph from configuration and capabilities. It
does not select an adapter solely from an MCU family name. Different board
revisions may route different capture pins or use different PHYs.

Negotiation is transactional:

1. prepare authority, path, profile, and next generation;
2. verify every bridge and sink can stage it;
3. commit at an explicit idle boundary;
4. observe fresh timestamps under the new generation;
5. qualify every sink; and
6. admit Class-0 only after the entire coordination group agrees.

Failure before commit aborts the staged generation. Failure after commit
enters hold/requalification; it never falls back within the old motion epoch.

## Authority change and failover

Authority failover is not ordinary clock-source selection. It changes the
meaning of every scheduled timestamp.

Required sequence:

1. stop ingesting new G-Code for affected coordination groups;
2. allow safe queued work to reach a coordinated hold, or trigger the
   existing underrun/hold mechanism;
3. close the old motion/time epoch;
4. select and authenticate the new authority;
5. establish a new authority epoch and quality generation;
6. rebuild every time path and converge every sink;
7. reconcile held actuator positions and pending intentions; and
8. resume only through the normal recovery workflow.

Automatic failover remains disabled until this complete sequence is physically
qualified. A fallback authority may keep heaters or independent telemetry
alive under their own policies without authorizing coordinated motion.

Split brain is prevented by two independent rules:

* one time authority per coordination group and epoch; and
* one motion-write lease per coordination group.

Time authority does not imply motion ownership, and motion ownership does not
permit redefining time.

## Security

Hardware captures occur before authentication, but observations enter the
discipline filter only after the associated record authenticates and passes
epoch, sequence, source, and replay checks.

An attacker must not be able to:

* nominate a new authority through discovery;
* replay an old follow-up against a new capture ticket;
* substitute a lower-quality adapter without a generation change;
* create a path cycle or second authority;
* force repeated authority changes as a motion denial-of-service; or
* use PTP control-plane participation to acquire the motion-write lease.

Standard PTP may provide packet structure and NIC filtering while HELIX still
binds accepted observations to its authenticated session and configured
authority. Facility PTP security, if present, is additive rather than assumed.

## Failure semantics

| Failure | Fabric response |
| --- | --- |
| Isolated bad observation | Reject sample, retain qualified map, increment reason counter |
| Lost follow-up | Expire ticket; no map update |
| Timestamp FIFO overflow | Mark evidence gap; degrade or invalidate according to profile |
| Source freshness exceeded | Enter holdover, then invalidate at configured expiry |
| Link loss with valid holdover | Stop new Class-0 as policy requires; existing queue may drain safely |
| Bridge reset | New quality generation; downstream path invalid |
| Authority reset/change | New authority epoch; coordinated hold and full requalification |
| Topology change | Invalidate hardware-bound profile until the new path is qualified |
| Quality below scope requirement | Reject before lookahead or pause ingestion; never widen silently |
| Mapping discontinuity during motion | Fail closed into coordinated recovery, not global hard shutdown where a safe hold is available |

The last row requires deliberate integration with
[failure recovery](08-Failure_Recovery.md). A transient loss of admission
between lookahead and background flush must become a coordinated hold, not an
uncaught flush exception. Geometry already committed to lookahead cannot be
silently discarded.

## Observability

`MACHINE_TIME_STATUS` becomes the canonical operator command. It reports:

* selected authority identity, kind, epoch, and health;
* every time path and bridge;
* mapping offset/rate, uncertainty, age, and quality generation;
* active coordination-policy requirements;
* accepted/rejected observations by reason;
* ticket expiry, timestamp FIFO overflow, holdover, and requalification;
* authority-change history; and
* whether Class-0 is admitted for each coordination group.

Transport-specific commands remain useful diagnostics:

* `TIMESYNC_STATUS` for current filter internals;
* USB SOF capture/discard attribution;
* Ethernet MAC/PTP descriptor and PHC counters;
* CAN timestamp/FOLLOW_UP counters; and
* WiFi TSF or capture-wire health.

Atlas records structured events for authority, epoch, path, quality, holdover,
and rejection transitions. Routine qualified observations remain aggregated
telemetry; transitions and violations become incidents.

## Host and firmware boundaries

### Host

Introduce a `MachineTimeFabric` service that owns:

* authority providers;
* the validated time-path graph;
* adapter/bridge capability negotiation;
* quality policies;
* authority-change transactions;
* normalized status; and
* compatibility translation from existing `[timesync]`.

The current `klippy/extras/timesync.py` becomes the first adapter and sink
orchestrator instead of the global policy owner. Its robust regressions,
interval diagnostics, and per-link ClockSync access remain reusable.

### Firmware

Introduce a small transport-neutral provider/sink ABI around the existing
`src/timesync.c`:

```text
time_source_register()
time_capture_publish()
time_follow_up_consume()
time_mapping_status()
time_quality_transition()
```

Adapters own peripheral registers and timestamp FIFOs. The discipline core
owns epoch, sequence admission, affine state, convergence, and holdover. The
trajectory core queries only the resulting mapping and policy generation; it
does not inspect USB, PTP, CAN, or TSF state.

Small MCUs may compile one static authority and one adapter. The abstraction
does not imply heap allocation, dynamic protocol loading, or large strings on
firmware.

## Implementation plan

### Phase 0 — freeze current behavior and evidence

- [ ] Capture golden vectors for current primary-MCU relay, USB-SOF refinement,
  firmware convergence, freewheel, and Class-0 admission.
- [ ] Record current configuration/status compatibility fixtures.
- [ ] Preserve the physical Pico/EBB36 and F767 software-timestamp baselines.
- [ ] Add a regression for convergence loss between move preflight and
  background flush; it must enter a recoverable hold rather than a global
  shutdown.

### Phase 1 — canonical types and quality algebra

- [ ] Define authority descriptor, observation, capture ticket, mapping,
  quality state, and generation in Python and fixed-layout C.
- [ ] Add cross-language golden vectors and malformed-record tests.
- [ ] Implement bounded uncertainty/path composition and correlated-source
  handling.
- [ ] Implement graph validation for cycles, multiple authorities, epoch
  mismatch, and insufficient scope quality.
- [ ] Prove that no quality composition promotes statistical evidence to a
  hardware bound.

### Phase 2 — wrap the current mechanism

- [ ] Register the primary MCU as the compatibility authority provider.
- [ ] Wrap host-relayed observations and USB SOF as adapters without changing
  their wire behavior.
- [ ] Wrap `src/timesync.c` as the first discipline sink.
- [ ] Produce identical mapping/convergence results for recorded traces before
  and after the refactor.
- [ ] Retain existing `[timesync]` configuration and status fields.

### Phase 3 — configuration, negotiation, and status

- [ ] Add `[machine_time]`, adapter, bridge, sink, and policy parsing behind an
  experimental feature gate.
- [ ] Add capability discovery and transactional prepare/commit/abort.
- [ ] Implement `MACHINE_TIME_STATUS` and structured Atlas transitions.
- [ ] Reject missing, cyclic, multi-authority, weak-quality, and unknown-clock
  configurations before motion setup.
- [ ] Add config migrations without rewriting existing user files.

### Phase 4 — F767 Ethernet PTP vertical slice

- [ ] Convert F4/F7 Ethernet rings to enhanced timestamp descriptors in the
  existing non-cacheable DMA arena.
- [ ] Enable the F767 MAC PTP clock, packet filtering, RX timestamps, requested
  TX timestamps, rollover handling, and timestamp error counters.
- [ ] Add Linux `SO_TIMESTAMPING`/PHC support and exact timestamp correlation.
- [ ] Run four-timestamp discipline with this workstation's `PHC0`.
- [ ] Compare software and hardware timestamp distributions over a direct
  cable and the intended switch, idle and under network/motion load.
- [ ] Qualify link renegotiation, timestamp loss, PHC step/slew, board reset,
  temperature, and holdover.
- [ ] Select the numeric F767 PTP quality profile from evidence, not the
  current software fallback limits.

### Phase 5 — CAN time adapter and Ethernet-to-CAN bridge

- [ ] Preserve FDCAN RX-element and Tx Event timestamps as canonical capture
  tickets.
- [ ] Move the existing CAN two-step path behind the adapter ABI.
- [ ] Compose F767/H723 PTP-to-gateway and gateway-to-CAN mappings.
- [ ] Forward authority epoch, quality generation, uncertainty, and holdover
  in compact authenticated records.
- [ ] Scope the physical PTP-to-FDCAN-to-node path against analyzer captures.
- [ ] Prove delayed ISR service cannot alter peripheral-captured event time.

### Phase 6 — configurable authority and planned failover

- [ ] Support primary-MCU and NIC-PHC authority selection.
- [ ] Implement coordinated authority-change epochs and hold/requalification.
- [ ] Test loss at every prepare, commit, convergence, and resume boundary.
- [ ] Prove a second host or PTP master cannot create a second active
  authority or motion writer.
- [ ] Keep automatic failover disabled until a physical interrupted-print
  campaign passes.

### Phase 7 — remaining adapters

- [ ] Move dedicated timer/PIO/MCPWM capture behind the adapter ABI.
- [ ] Prototype RP2040 PIO CAN/capture timestamps.
- [ ] Prototype ESP32 TSF with explicit AP/BSSID/power-state generations.
- [ ] Retain authenticated software four-timestamp fallback for unsupported
  hardware.
- [ ] Publish a capability/qualification matrix by exact board revision.

### Phase 8 — fleet and redundancy qualification

- [ ] Run multiple printers and coordination groups from one PHC authority.
- [ ] Qualify read-only observers, planned host takeover, and PHC/grandmaster
  replacement.
- [ ] Test switch congestion, restart, topology replacement, and transparent-
  clock operation where used.
- [ ] Prove one printer's weak/degraded path cannot lower another group's
  quality policy.
- [ ] Complete long simultaneous prints with exact time-path and delivery
  accounting.

## Test strategy

Every adapter must pass the same conformance suite:

1. monotonic sequence and epoch handling;
2. exact capture/follow-up association;
3. duplicate, replay, reorder, loss, and expiry;
4. timer wrap and ambiguous-gap rejection;
5. affine convergence and bounded correction;
6. holdover entry, expiry, and reacquisition;
7. source reset and quality-generation change;
8. authentication before discipline;
9. fixed memory and CPU budgets; and
10. status/accounting conservation.

Every physical profile adds:

1. external edge or analyzer comparison;
2. idle and representative worst-case processing load;
3. link/bus saturation and higher-priority contention;
4. cold and operating-temperature runs;
5. reset, disconnect, renegotiation, and topology change;
6. mean, deviation, tails, extrema, and rejected observations;
7. conservative path uncertainty, not only average offset; and
8. coordinated motion or print evidence appropriate to its declared scope.

## Acceptance gates

The architecture is complete only when:

1. existing primary-MCU/USB configurations retain behavior and wire
   compatibility;
2. all adapters feed one canonical discipline and quality interface;
3. a time path has exactly one authority and no cycle;
4. authority and quality changes always create explicit generations;
5. bridges preserve or increase uncertainty, never erase it;
6. Class-0 admission is evaluated against coordination scope;
7. transient time-quality loss produces a controlled hold/recovery path;
8. hardware-bounded labels are backed by retained physical evidence;
9. F767 hardware PTP meets or improves upon its software-timestamp profile;
10. Ethernet-to-CAN time transfer is measured end to end;
11. planned authority failover cannot create split brain or stale scheduled
    work; and
12. Atlas and operator status can explain exactly why every sink is qualified,
    degraded, holding over, or invalid.

## Design consequences

Machine-time authority becomes configurable without making time ambiguous.
Transport-specific hardware remains fully exploited without leaking into the
trajectory protocol. A new PCIe NIC, EtherCAT distributed clock, TSN endpoint,
WiFi timestamp source, or dedicated timing wire needs an adapter and evidence
profile, not another motion-time implementation.

Most importantly, bridges become honest about time. HELIX can move a command
from Ethernet to CAN and separately prove how the destination interpreted its
timestamp. Packet delivery, clock transfer, and motion ownership remain
distinct contracts that meet only at the Class-0 admission boundary.
