# FD-0001: Autonomous Job Execution and Printer Fabric

Status: architecture and implementation plan. HELIX already provides the
necessary deterministic foundations—compiled motion intentions, a primary-MCU
machine-time authority, distributed trajectory queues, autonomous heater
control, execution logs, authenticated Ethernet, CAN FD, and a typed gateway
core. It does not yet store and execute a complete compiled job without
Klippy. Every autonomous-execution claim remains open until the physical gates
in this document pass.

This document extends the [host architecture](05-Host_Architecture.md),
[failure recovery](08-Failure_Recovery.md), [CAN FD transport](15-CANFD_Transport.md),
[autonomous heater control](18-Autonomous_Heater_Control.md),
[unified gateway](19-Unified_CAN_Gateway.md), and
[machine-time fabric](20-Unified_Machine_Time.md). Portable, event-driven
behavior inside a capsule is defined by
[machine programs and dynamic scores](23-Portable_Machine_Programs.md);
their target-native deployment and hard-real-time extension model is defined
by [native machine modules](24-Target_Native_Machine_Modules.md).

## Thesis

The host should authorize, compile, provision, schedule, and observe a print.
The printer should own and execute an accepted print.

Once a job is armed, loss of Klippy, Moonraker, the farm scheduler, the
workstation, the network switch, or every remote file server must not stop an
otherwise healthy print. The network mainboard holds the complete compiled
score on local nonvolatile storage, remains the machine-time authority, and
feeds timestamped tracks to every downstream controller. When the job ends,
the machine enters its declared safe final state and records the result for
the next authenticated host.

The architectural analogy is an orchestra with a fully notated score. The
host composes and commissions the performance. The mainboard holds the score,
publishes tempo, and distributes each part. The actuator MCUs play their
timestamped parts against the shared clock. Once the performance begins, an
external maestro is not required to continuously tell every musician which
note comes next.

This is not merely host failover. It removes the live host from the execution
dependency graph.

## Central decisions

1. **An armed job is fully local.** Every byte required for normal completion
   is verified on mainboard-owned nonvolatile storage before motion begins.
2. **The mainboard remains the execution-time authority.** Other boards keep
   disciplined replicas of machine time; they are not competing masters.
3. **A network mainboard is also the printer fabric controller.** Native
   Ethernet is the management and provisioning link. Native CAN/CAN FD is the
   first downstream machine bus for the toolhead and accessory MCUs.
4. **Ethernet-to-CAN is one instance of a general bridge.** The common core
   routes typed control, time, job-track, delivery, and telemetry records
   between transport adapters. It is not permanently coupled to CAN.
5. **Storage sources may be redundant; execution storage is local.** A host
   may push a job, a printer may fetch it from replicated network stores, or a
   technician may provision it locally. Network availability is never part of
   the armed job's real-time contract.
6. **Publication is content-addressed and atomic.** A shared-drive view is a
   convenience over immutable objects and committed manifests, not permission
   to edit an executing file in place.
7. **External host ownership and local execution authorization are different
   capabilities.** Expiry or loss of a host control lease does not revoke an
   already armed job.
8. **Normal host loss means continue.** A safety fault, essential-node loss,
   storage starvation, time-quality loss, operator stop, or job-policy event
   may still cause a coordinated pause or controlled stop.
9. **Machine behavior is not welded into the firmware.** Portable behavior is
   compiled for the qualified target, stored with content identity, loaded as
   native code, and activated atomically through the stable HELIX kernel ABI.

## Goals

The autonomous execution architecture must:

1. finish a healthy armed print with all external hosts and storage servers
   disconnected;
2. keep the primary mainboard counter as the stable job-time epoch;
3. replicate machine time over Ethernet, CAN/CAN FD, USB, serial, and future
   transports through one adapter contract;
4. execute one immutable, verified job definition across all participating
   boards;
5. make every job chunk, track, dispatch, execution checkpoint, and terminal
   state accountable;
6. support a native Ethernet mainboard with an integrated CAN-FD fabric for
   toolhead and accessory nodes;
7. allow redundant network repositories at different addresses without
   confusing storage availability with motion authority;
8. permit any authenticated host to reconnect as an observer and one host to
   acquire a mutation lease without resetting the machine;
9. retain bounded memory, storage bandwidth, flash wear, and bus utilization;
10. reuse HELIX trajectory, heater, trigger, recovery, gateway, and Atlas
    primitives rather than introduce a second execution engine;
11. remain useful for one desktop printer as well as a large farm;
12. preserve a compatibility mode in which Klippy continues live streaming
    while autonomous execution is commissioned; and
13. deploy compiled machine behavior as target-native modules without
    reflashing the stable HELIX kernel.

## Non-goals

The first autonomous release does not:

* promise continuation through mainboard power loss or mainboard destruction;
* make two mainboards concurrently control one actuator fabric;
* execute Class-0 motion directly from an NFS, SMB, WebDAV, or object-store
  connection;
* port the complete G-code parser, macro engine, Jinja environment, or
  geometric planner into constrained MCU firmware;
* support arbitrary shell commands or host-only macros inside an autonomous
  capsule;
* silently recompile a job after configuration, calibration, firmware, or
  node-identity changes;
* guarantee seamless continuation after an essential downstream MCU fails;
* allow a newly connected host to replace an active job or clock epoch merely
  because it authenticated;
* claim power-loss recovery without physical position and thermal
  requalification; or
* require the MCU to implement a general-purpose multi-user filesystem server.

## Reference topology

```text
       workstation / farm scheduler / Atlas / Mainsail
                           |
                  Ethernet management plane
                           |
        +------------------+------------------+
        |       network mainboard             |
        |                                      |
        | authenticated host endpoint          |
        | content-addressed local job store     |
        | capsule verifier + execution journal |
        | native module loader + application ABI|
        | autonomous job coordinator           |
        | machine-time authority                |
        | safety and node-health coordinator    |
        | typed printer-fabric bridge           |
        +---------+--------------+-------------+
                  |              |
             local axes      CAN / CAN FD
                                 |
                   +-------------+-------------+
                   |                           |
             toolhead MCU                 accessory MCU
          extruder/heater/fans       sensors/OAMS/other axes
          local trajectory queue       local execution queue
          disciplined clock replica    disciplined clock replica
```

The Ethernet endpoint is no longer a remote serial cable. It is the
management entrance to a self-contained printer. The downstream CAN fabric is
not a host adapter hanging outside the machine; it is internal printer
infrastructure.

## Network-mainboard hardware profile

The reference production mainboard should provide:

* an MCU with sufficient SRAM, executable-memory placement, DMA, cache/MPU
  support, and independent peripheral clocks—an STM32H7-class device is the
  current reference;
* native 10/100 Ethernet or better with hardware RX/TX timestamps;
* native FDCAN and a transceiver electrically qualified through the intended
  BRS rate;
* SDMMC/SDIO storage, with microSD acceptable for development and industrial
  eMMC preferable for high-duty-cycle farms;
* enough local flash for A/B firmware and a first-class bootloader;
* hardware watchdog, brownout detection, and reset-cause retention;
* local emergency-stop and critical thermal inputs that do not depend on the
  network;
* optional FRAM or another high-endurance metadata journal if measurements
  show SD journal wear or latency is unacceptable; and
* a service/debug path independent of the production Ethernet endpoint.

The exact H723 Ethernet-to-CAN board remains a qualification vehicle, not
automatically the production design. The silicon profile matters more than
the evaluation-board connector layout.

## The autonomous job capsule

The unit of execution is a **job capsule**, not a mutable G-code file.

G-code may remain the user-facing source. Klippy or a dedicated HELIX compiler
resolves it against an exact printer configuration and emits the deterministic
tracks the firmware already understands. The original G-code may be retained
for provenance, preview, and later recompilation, but it is not the
authoritative real-time input once the capsule is armed.

### Capsule manifest

The versioned manifest contains at least:

```text
capsule_format_version
capsule_id
content_root_hash
source_gcode_hash
compiler_identity and version
protocol_abi_hash
trajectory_format and quantization profile
printer_identity
configuration_hash
calibration_generation
kinematics and transform generation
required node identities and roles
required firmware/protocol capabilities
required native target classes and module ABI
module table, content roots, node assignments, and publishers
module state/checkpoint schema hashes and execution budgets
machine-time profile and authority identity
track table and chunk hashes
job preconditions
safety envelope
pause/cancel/end policies
estimated duration, material, and storage bandwidth
creation identity and optional signature
```

`capsule_id` is content-derived or bound to the content root. A filename,
Moonraker database row, IP address, or farm job number may reference a
capsule, but none is its identity.

### Per-node tracks

The capsule contains separate, timestamped tracks for:

* each trajectory-controlled joint;
* extrusion and pressure-advance output;
* heater targets and locally selected control profiles;
* fans, servos, PWM/DAC outputs, LEDs where job-significant, and other
  scheduled peripherals;
* synchronization barriers and coordinated checkpoints;
* expected execution-log checkpoints;
* local prompt commands and bounded state transitions;
* target-native machine-program modules plus their inspectable statechart
  metadata for tool changes, runout, calibration, and other variable-duration
  distributed workflows; and
* terminal safe-state actions.

Tracks use the same exact quantized coefficients sent over the current HELIX
wire protocol. The offline capsule replay and live Klippy stream must produce
identical MCU commands for the same compiler/configuration generation.

### Chunking and integrity

Capsules are split into independently verifiable chunks:

* fixed maximum size chosen from measured SD, RAM, and transport behavior;
* per-chunk cryptographic hash;
* ordered track and time range;
* compression profile, uncompressed length, and checksum;
* dependency and checkpoint metadata; and
* optional signature/Merkle proof tied to the manifest root.

Partial downloads may resume by missing hash. Duplicate capsule content is
stored once. Corruption is detected before a chunk enters a Class-0 staging
buffer. A hash mismatch never becomes a trajectory underrun disguised as a
storage hiccup.

### What must be resolved before compilation

The compiler freezes:

* kinematics and joint mapping;
* bed mesh and geometric transforms;
* input shaping;
* pressure advance;
* speed, acceleration, volumetric, thermal, and current limits;
* heater-control profiles;
* macro expansion that affects deterministic job behavior;
* slicer-controlled acceleration and velocity changes; and
* node ownership and track routing.

A capsule is invalid after any relevant generation changes. HELIX does not
guess that a new toolhead, mesh, gear ratio, firmware ABI, or heater cartridge
is "close enough."

### Runtime-dependent behavior

Not every operation can be reduced to one unconditional timeline:

* homing runs locally before the job epoch begins;
* temperature waits are local arming preconditions;
* filament runout, door, thermal, and hardware-trigger events enter declared
  local state-machine branches;
* an ordinary pause executes a coordinated braking/hold policy and may wait
  indefinitely for an operator or host;
* cancellation executes the capsule's safe cancellation sequence;
* resuming after a non-checkpoint interruption may require a returning host to
  compile a splice trajectory; and
* arbitrary host shell commands are rejected from autonomous capsules.

The capsule therefore contains timed trajectory **chapters** separated by
event-driven **workflow barriers**. A portable machine program may wait on
typed sensor evidence, dispatch bounded leaf-device operations, run local
trajectory tracks, retry according to an explicit policy, and enter forward
recovery. When its postconditions are committed, the coordinator rebases the
next trajectory chapter to current machine time.

The source language remains ordinary Python through `@machine_program`.
Unmodified Klipper executes the same source through a compatibility adapter;
HELIX validates it through a bounded statechart IR and compiles it into
target-native modules. The H7 does not run Klippy, Jinja, arbitrary Python, or
a universal score bytecode VM. The complete source/operation contract is in
[23-Portable_Machine_Programs.md](23-Portable_Machine_Programs.md); container,
loader, isolation, target, and hard-real-time contracts are in
[24-Target_Native_Machine_Modules.md](24-Target_Native_Machine_Modules.md).

Version 1 may prohibit live speed-factor changes. A later version may provide
bounded local time scaling only after proving that motion, extrusion,
pressure advance, heaters, and every coordinated track retain their intended
relationships.

## Job lifecycle

The mainboard owns a transactional lifecycle:

```text
DISCOVERED
    -> STAGING
    -> VERIFIED
    -> QUALIFYING
    -> ARMED
    -> RUNNING
    -> PAUSED | COMPLETING | FAILED | CANCELLED
    -> COMPLETE
```

### Stage

The capsule arrives by authenticated push, verified pull, service-mode local
copy, or removable-media import. Chunks land under temporary identities.
Incomplete content is never visible as an executable job.

### Verify

The mainboard validates the complete manifest, content root, chunk hashes,
signature policy, size limits, compiler/format version, configuration hash,
ABI, native module target classes/imports/budgets, node identities, safety
envelope, and required capabilities.

### Qualify

The mainboard discovers every required node, verifies firmware and role,
establishes the machine-time paths, checks storage read performance, validates
heater/sensor readiness, loads every required native module into executable
memory, performs its inert initialization gate, and confirms that no
conflicting owner or job exists.

### Arm

Arming is a local persisted authorization transaction. It records:

* capsule and manifest root;
* printer/configuration/calibration generations;
* participating-node session epochs;
* job-time epoch;
* safety envelope and terminal policy;
* exact module roots and activation generations;
* initial checkpoint;
* operator/host authorization provenance; and
* a monotonically increasing execution generation.

An external host lease may expire immediately after this transaction without
invalidating it.

### Run

The mainboard reads ahead from local storage, validates each chunk again at
the trust boundary, fills bounded per-track staging queues, and dispatches
work according to downstream horizons. Filesystem and compression work occur
in task context. No ISR parses a filesystem or waits for storage. Native code
is already resident before activation; SD is not an executable demand-paging
source.

### Complete

Completion requires more than reaching end-of-file. Every required track must
report its terminal checkpoint, every delivery must be accounted, and the
declared safe end state must be applied. The mainboard then commits a durable
result record and waits for a host.

## Storage architecture

### Local execution store

The mainboard store is authoritative during an armed job. It contains:

* immutable capsule objects;
* committed manifests;
* a bounded execution journal;
* terminal result records;
* compact execution/checkpoint summaries; and
* storage health and wear metadata.

The active store uses an atomic metadata strategy suitable for abrupt reset:
two-phase manifest commit, checksummed records, monotonically increasing
generations, and bounded recovery scanning. Exact filesystem selection follows
power-cut and latency testing; it is not chosen merely because an SD card is
usually formatted as FAT.

The full high-rate execution log should not be synchronously journaled to SD
on every segment. RAM rings, chunk summaries, periodic checkpoints, and
terminal flushes bound wear and latency while retaining sufficient evidence.

### Shared-drive presentation

Users and farm software should be able to treat the job repository like a
shared drive, but the MCU storage contract remains immutable publication:

```text
write temporary object -> verify hash -> publish manifest -> arm by capsule id
```

A host-side FUSE mount, Moonraker storage provider, SMB/NFS gateway, WebDAV
adapter, or ordinary directory may present familiar file semantics. These are
adapters around the job-store API. The MCU does not need a complete SMB/NFS
implementation and must not expose its live mounted filesystem for concurrent
random writes.

Physical local access is allowed in an explicit service mode. USB
mass-storage access and firmware mounting the same writable filesystem at the
same time are forbidden.

### Replicated network stores

A farm may publish the same capsule through multiple storage replicas:

```text
capsule root abc...:
    https://store-a.example/jobs/abc...
    https://store-b.example/jobs/abc...
    helix-store://192.168.20.10/abc...
    helix-store://192.168.20.11/abc...
```

The capsule root—not the server address—defines the content. The first
verified source wins; a failed source may be replaced at a chunk boundary;
servers that return different bytes under the same content identity are
rejected and reported as an integrity incident.

This provides useful redundancy while staging and fleet-wide deduplication.
Once `VERIFIED` and `ARMED`, every remote storage address may disappear. A
capsule that is only partially cached cannot be armed merely because two
network replicas currently appear healthy.

### Capacity and bandwidth

The design must measure:

* compiled bytes per source G-code byte;
* bytes per motion second by track and node;
* compression ratio and decompression CPU budget;
* worst SD read latency, not only sequential throughput;
* queue horizon consumed by an SD latency excursion;
* directory/index recovery time with a full farm workload;
* write amplification and card endurance; and
* storage required for active jobs, retained results, and rollback firmware.

The expected average rate is modest relative to SDMMC bandwidth, but average
bandwidth is not the gate. A long card stall must be absorbed by read-ahead or
become a coordinated controlled hold before any actuator queue underruns.

## Distributed execution

The mainboard keeps the complete capsule and downstream boards retain bounded
rolling horizons.

This hybrid is preferable to placing a full copy on every small MCU:

* host loss no longer matters;
* the mainboard can use large, replaceable storage;
* toolhead boards remain small and inexpensive;
* each actuator still has enough local runway to survive bus jitter;
* one coordinator owns pause, cancel, and checkpoint policy; and
* an essential-mainboard failure still produces a bounded controlled stop
  rather than pretending the printer has redundant brains.

For each track, the coordinator maintains:

```text
stored_through
validated_through
staged_through
admitted_through
executed_through
checkpoint
delivery/accounting state
```

Conservation identities must reconcile capsule chunks to admitted intentions
and execution-log checkpoints. A skipped, duplicated, stale-epoch, or
wrong-node segment is a job fault.

Dispatch uses traffic classes:

* trajectory and coordinated actuator tracks are Class 0;
* pause, cancel, checkpoint, and safety controls are Class 1;
* progress, execution summaries, storage health, and diagnostics are Class 2.

Bulk staging may not starve prompt safety control or time synchronization.

## Machine time without the host

An autonomous job uses the mainboard's monotonic counter and a persisted
job-time epoch as its execution authority.

An external PTP grandmaster or NIC PHC may improve the mainboard clock's rate
and cross-printer comparability, but it is an upstream reference, not
permission to replace the active job epoch. Loss or replacement of the
external source enters bounded mainboard holdover. It does not make a healthy
local job follow a new wall clock.

Downstream boards maintain disciplined replicas:

```text
mainboard machine time
        |
        +-- Ethernet PTP/timestamp adapter
        +-- CAN/FDCAN two-step adapter
        +-- USB SOF/capture adapter
        +-- serial timestamp adapter
        +-- dedicated edge/capture adapter
```

Every adapter produces the canonical observation defined in
[20-Unified_Machine_Time.md](20-Unified_Machine_Time.md). A time bridge
translates authority epoch, timestamp, uncertainty, freshness, and quality
generation; it never merely forwards a packet timestamp and calls the result
machine time.

The important replication rule is:

> One authority, many disciplined replicas, no silent master election during
> an armed job.

If an essential node's mapping enters holdover beyond its declared horizon,
the mainboard coordinates a hold while still locally available. Loss of the
external host alone does not affect the time fabric.

## From CAN gateway to typed printer-fabric bridge

The unified gateway already separates host-link adapters, a typed core,
machine time, and hardware adapters. Autonomous execution generalizes the
roles:

```text
      typed fabric core
             |
   +---------+----------+-----------+------------+
   |                    |           |            |
Ethernet             CAN/FDCAN   UART/RS-485   future bus
management adapter   adapter     adapter        adapter
```

The core does not reduce every transport to a byte stream. Each adapter
retains its native MTU, delivery, ordering, timestamp, discovery, and failure
semantics, then presents canonical records:

* identity and capability;
* ownership and execution generation;
* time observation/follow-up;
* job-track chunk and admission;
* prompt control;
* delivery completion or uncertainty;
* telemetry and incidents; and
* storage publication/fetch where supported.

CAN remains the first machine-bus implementation because toolhead and sensor
boards already use it, it provides bounded arbitration, and FDCAN carries a
complete 64-byte protocol frame. The abstraction also permits
Ethernet-to-serial, Ethernet-to-RS-485, Ethernet-to-another-Ethernet segment,
or a future deterministic fieldbus without cloning job, ownership, or time
policy.

The production network mainboard may internally route both locally attached
actuators and downstream buses. "Bridge" therefore describes a protocol role,
not necessarily a separate physical adapter.

## Host ownership, reconnection, and redundancy

The architecture has three distinct authorities:

1. **Execution authority:** the armed capsule and its persisted execution
   generation.
2. **Machine-time authority:** the mainboard clock and job epoch.
3. **External mutation owner:** at most one authenticated host lease permitted
   to start, pause, cancel, replace, or reconfigure.

An arbitrary number of authenticated observers may read status and telemetry.
Only the mutation owner may change job state, and acquiring that lease does
not change execution or time authority.

On host disconnect:

* the job continues;
* the mainboard records an informational observer-loss event;
* no heater target or motion queue is cleared;
* no new remote command is inferred; and
* terminal results remain local.

On host reconnect:

1. authenticate printer and host identities;
2. read mainboard reset reason, execution generation, capsule root, job state,
   checkpoint, node states, time quality, heaters, and incidents;
3. attach read-only without changing anything;
4. optionally acquire the mutation lease through an explicit transaction; and
5. adopt the running state instead of replaying configuration or restarting
   firmware.

Two farm controllers can therefore be redundant without two live motion
writers. They replicate scheduler state and storage metadata; the printer is
the authority on whether its accepted job is running or complete.

## Failure semantics

| Failure | Autonomous response |
| --- | --- |
| External Klippy/Moonraker crash | Continue job; record observer loss |
| Farm scheduler or database loss | Continue job; commit result locally |
| All remote job stores disappear after arming | Continue from local capsule |
| Network switch/cable loss to host | Continue local/CAN execution |
| New host connects | Read-only attach; no execution change |
| Current mutation lease expires | Continue job; require a new lease for mutations |
| Corrupt or incomplete capsule before arm | Refuse arm |
| Hash failure during staged read | Stop admitting affected track; coordinated hold before horizon expires |
| Recoverable SD latency excursion | Consume read-ahead; record latency |
| Sustained SD starvation | Coordinated hold, preserving checkpoint |
| Essential downstream bus/node loss | Coordinated hold/controlled stop using local horizons |
| Nonessential telemetry node loss | Continue if manifest policy permits |
| External PTP/reference loss | Mainboard holdover; continue while local time quality remains valid |
| Mainboard reset or power loss | No seamless-continuation claim; safe outputs and operator requalification |
| Local thermal/safety violation | MCU-local shutdown or declared coordinated safety action |
| Filament runout | Local pause branch; wait for operator/host |
| Operator emergency stop | Immediate local safety path; capsule authorization revoked |
| Job completes without a host | Apply terminal safe state, journal result, wait |

This changes the compatibility behavior in
[08-Failure_Recovery.md](08-Failure_Recovery.md): live-streaming mode still
holds on host loss, while a fully staged and armed autonomous capsule
continues. The mode and authorization must be explicit; an incomplete job can
never inherit autonomous permission.

## Heater and safety ownership

Autonomous heater control is already a local firmware primitive. The job
capsule supplies a stronger supervisory context:

* allowed heater identities and types;
* target sequence and maximum target;
* valid profile generation;
* maximum job and hold durations;
* fan/extrusion feed-forward availability;
* sensor and watchdog requirements;
* terminal and cancellation targets; and
* fault policy.

The external host no longer has to refresh a heater merely to prove it is
alive. The local job coordinator supplies the active authorization generation,
and the heater MCU continues enforcing its own runaway, sensor, maximum-power,
maximum-temperature, and timeout limits.

An autonomous capsule cannot disable physical safety, widen configured
temperature ceilings, or authorize an unknown heater profile. Host
authentication does not override these invariants.

## Host and firmware boundaries

### Host/compiler

The host implementation owns:

* G-code and macro resolution;
* kinematics, lookahead, trajectory fitting, shaping, and extrusion coupling;
* portable-source validation and target-native module compilation;
* capsule construction, chunking, hashing, signing, and size estimation;
* offline semantic and trajectory equivalence checking against live HELIX
  streaming;
* upload/fetch orchestration and replicated-store publication;
* previews and human-readable provenance; and
* fleet scheduling and result collection.

Klippy's current planner becomes the first capsule compiler. The initial
implementation should capture its already quantized outbound HELIX intentions
instead of immediately rewriting planning in a new language.

### Mainboard firmware

The mainboard owns:

* local storage and immutable object index;
* manifest/chunk validation;
* native module verification, loading, isolation, activation, and rollback;
* transactional lifecycle and execution journal;
* node discovery and qualification;
* job epoch and machine-time publication;
* bounded read-ahead and per-track dispatch;
* delivery/checkpoint conservation;
* local pause/cancel/end state machines;
* semantic machine-operation and admitted hard-real-time control-domain APIs;
* host observer/mutation leases; and
* terminal result durability.

### Downstream firmware

Downstream boards retain:

* time discipline;
* bounded trajectory and prompt queues;
* actuator execution;
* local heater and safety control;
* hardware triggers;
* verified native modules only on nodes whose target, memory, isolation, and
  execution budgets explicitly qualify them;
* execution logs and checkpoints; and
* controlled hold behavior.

They do not parse filesystems, G-code, or farm scheduling policy.

## Operator and API surface

The first management surface should include:

```text
HELIX_JOB_LIST
HELIX_JOB_IMPORT SOURCE=<uri-or-upload>
HELIX_JOB_VERIFY CAPSULE=<id>
HELIX_JOB_ARM CAPSULE=<id>
HELIX_JOB_START CAPSULE=<id>
HELIX_JOB_STATUS
HELIX_JOB_PAUSE
HELIX_JOB_CANCEL
HELIX_JOB_RESULT CAPSULE=<id>
HELIX_JOB_DELETE CAPSULE=<id>
HELIX_STORE_STATUS
HELIX_FABRIC_STATUS
MACHINE_TIME_STATUS
```

Names may evolve, but status must report:

* capsule root and lifecycle state;
* execution generation and job epoch;
* configuration/calibration/ABI generations;
* storage source, local completeness, verification, and health;
* bytes/chunks stored, staged, admitted, and executed;
* every required node, role, horizon, checkpoint, and time quality;
* external observers and current mutation lease;
* read-ahead depth and storage-latency extrema;
* delivery and execution conservation residuals;
* pause/failure/terminal reason; and
* whether external host or remote storage loss can currently affect
  completion.

Atlas records lifecycle transitions, integrity failures, storage starvation,
node/time degradation, lease conflicts, and terminal results. Routine chunk
reads and healthy dispatch remain aggregated telemetry.

## Implementation plan

### Phase 0 — freeze current live-stream evidence

- [ ] Capture one or more representative G-code files and the exact
  quantized HELIX command streams produced for every MCU.
- [ ] Record configuration, calibration, ABI, node identity, and machine-time
  generations alongside the streams.
- [ ] Add deterministic replay tests proving a recorded stream reconstructs
  the current motion/extrusion/heater schedule.
- [ ] Measure segment counts, compiled bytes, peak bytes/s, and natural
  compression by track.
- [ ] Preserve live Klippy streaming as the compatibility oracle.

### Phase 1 — capsule format and host compiler

- [ ] Define the manifest, track, chunk, checkpoint, and terminal-result
  schemas with explicit versions and bounds.
- [ ] Define the portable machine-program annotation, semantic operation ABI,
  typed/statechart compiler IR, target-native module container, capability
  manifest, and Klipper compatibility executor.
- [ ] Generate capsules by intercepting the current post-lookahead,
  post-quantization HELIX command path.
- [ ] Add content hashes, optional signatures, configuration/calibration
  binding, and capability requirements.
- [ ] Reject host-only macros and unresolved nondeterministic behavior with
  actionable compiler errors.
- [ ] Compile supported sensor branches, bounded retries, resource leases, and
  forward-recovery paths instead of flattening them into one timeline.
- [ ] Build an offline validator and human-readable inspector.
- [ ] Prove live-stream and capsule byte equivalence for the golden corpus.
- [ ] Prove simulator, live Klipper, and target-native workflow semantic
  equivalence for the machine-program corpus.

### Phase 2 — storage substrate

- [ ] Add a target-neutral block-storage and immutable-object API.
- [ ] Implement the first SDMMC/SDIO backend with DMA, bounded completion
  service, timeout, card removal, CRC, and latency accounting.
- [ ] Select and power-cut-test the metadata/filesystem strategy.
- [ ] Implement temporary upload, missing-chunk resume, atomic manifest
  publish, garbage collection, and capacity reservation.
- [ ] Add a service-mode local import path without dual-mount corruption.
- [ ] Benchmark worst-case read latency and derive read-ahead requirements.

### Phase 3 — mainboard job lifecycle

- [ ] Implement stage/verify/qualify/arm/run/pause/cancel/complete state
  machines with persisted generations.
- [ ] Bind arming to printer, configuration, calibration, ABI, node, safety,
  and capsule identities.
- [ ] Add bounded execution journal and reset recovery inspection.
- [ ] Implement storage and node preflight before arming.
- [ ] Add management commands, Moonraker APIs, and Atlas events.
- [ ] Prove an incomplete or corrupt capsule can never become runnable.

### Phase 4 — single-board autonomous execution

- [ ] Execute a stored capsule on local mainboard axes without Klippy
  providing motion after `START`.
- [ ] Load and execute its pinned native workflow modules without a firmware
  rebuild, reset, interpreter, or SD access from the real-time code path.
- [ ] Keep heater, fan, pause, cancel, and terminal policies local.
- [ ] Disconnect the host after arming and complete physical prints.
- [ ] Inject SD stalls, corruption, removal, and capacity exhaustion.
- [ ] Verify read-ahead converts sustained starvation into a controlled hold,
  not a step underrun.
- [ ] Compare steps, execution logs, geometry, extrusion, and heater behavior
  against the live-stream baseline.

### Phase 5 — autonomous printer fabric

- [ ] Generalize the gateway core from CAN service routing to typed printer
  fabric records without weakening native transport semantics.
- [ ] Implement mainboard-to-CAN/FDCAN job-track dispatch and exact delivery
  accounting.
- [ ] Move CAN time publication behind the unified timestamp-adapter ABI.
- [ ] Implement at least one non-CAN adapter or loopback conformance target to
  prove the abstraction is not a renamed CAN API.
- [ ] Qualify local axes plus a CAN toolhead in one autonomous print.
- [ ] Execute one distributed portable workflow spanning mainboard motion and
  at least two downstream semantic device roles.
- [ ] Stage and atomically activate the exact native application generation on
  every participating node that supports runtime modules.
- [ ] Disconnect external Ethernet while the internal fabric continues.

### Phase 6 — host independence and reconnection

- [ ] Split persisted execution authorization from renewable host mutation
  ownership.
- [ ] Allow multiple authenticated read-only observers and exactly one
  mutation lease.
- [ ] Reconnect Klippy/Moonraker to a running job without config replay,
  firmware restart, heater reset, or queue ownership conflict.
- [ ] Test host crash, workstation reboot, switch loss, DHCP lease change,
  and a different host acquiring the mutation lease.
- [ ] Complete jobs with no host present and collect their terminal results
  later.

### Phase 7 — replicated farm storage

- [ ] Implement authenticated push and content-addressed pull sources.
- [ ] Publish one capsule through multiple repository addresses.
- [ ] Resume missing chunks from a different replica and reject content
  disagreement.
- [ ] Add a Moonraker directory provider and optional host-side filesystem
  presentation.
- [ ] Stage one capsule to many printers with content deduplication and
  bounded network load.
- [ ] Remove every repository after arming and prove all printers finish.

### Phase 8 — long-duration and fault qualification

- [ ] Run full-size, multi-hour prints with the host absent.
- [ ] Measure storage latency, queue horizons, bus utilization, CPU, RAM,
  card temperature, wear estimates, and execution conservation.
- [ ] Exercise runout, pause, cancel, node loss, time holdover, bus-off,
  storage starvation, and safe terminal state.
- [ ] Cold- and hot-test the exact network-mainboard/storage revision.
- [ ] Verify Atlas can reconstruct the complete local job without an
  always-connected host log.
- [ ] Publish the capability and remaining-failure matrix honestly.

## Verification matrix

| Scenario | Required outcome |
| --- | --- |
| Host disconnect one second after start | Print continues without motion or heater discontinuity |
| Host remains off through completion | Safe completion and durable result |
| Network store removed after `ARMED` | No execution effect |
| Network store removed during `STAGING` | Stage pauses/resumes or changes replica; capsule cannot arm incomplete |
| Two repositories serve the same root | Either source verifies identically |
| Repository returns wrong bytes | Hash rejection and integrity incident |
| Second host observes | Read-only state matches mainboard |
| Second host requests mutation | Explicit lease transaction; no implicit takeover |
| DHCP/IP changes during job | Local execution unaffected |
| External PTP disappears | Mainboard holdover according to qualified profile |
| CAN toolhead queue runs low | Mainboard prioritizes refill before telemetry |
| CAN toolhead disappears | Coordinated hold/controlled stop |
| Short SD latency spike | Absorbed by read-ahead |
| Sustained SD stall | Coordinated hold before actuator horizon expires |
| Corrupt active chunk | No corrupted command is admitted |
| Mainboard reset | Safe outputs; no unqualified blind continuation |
| Job reaches final byte | Wait for track acknowledgements, apply safe terminal state, then commit complete |

## Acceptance gates

The architecture is complete only when:

1. the capsule and live-stream paths produce byte-equivalent quantized
   intentions for the same input generation;
2. no capsule can arm without complete local verified content;
3. an external host, network, and every remote storage source can disappear
   after arming while a representative physical print completes;
4. the mainboard remains the one job-time authority and every downstream
   board reports an acceptable disciplined replica;
5. local and downstream tracks reconcile stored, admitted, and executed
   checkpoints without unexplained residuals;
6. storage stalls are covered by measured read-ahead or cause a coordinated
   hold before queue exhaustion;
7. heaters and safety remain locally bounded throughout host absence;
8. reconnecting or replacing a host does not reset, replay, or duplicate
   execution;
9. a second writer cannot mutate the job without an explicit ownership
   transaction;
10. corrupt, stale-generation, wrong-node, or wrong-ABI content is rejected
    before actuation;
11. the final safe state and terminal result survive until a host returns;
12. a long autonomous Ethernet-mainboard/CAN-toolhead print passes physical,
    timing, thermal, storage, and fault-injection qualification;
13. every capsule-pinned native module is target-, ABI-, capability-,
    generation-, and publisher-qualified before arming;
14. changing a machine application requires an atomic module deployment, not
    a firmware flash; and
15. no native instruction fetch or workflow continuation depends on SD
    latency after its active module is admitted.

## Product consequence

This architecture still eliminates the per-printer Raspberry Pi, its SD-card
image, Linux updates, services, credentials, and maintenance burden. It does
not replace that burden with one enormous real-time farm host.

A farm controller becomes a replicated scheduler and repository client. It
may assign a capsule to one printer or a group, observe progress, and collect
results. The printers remain autonomous execution appliances. A farm-service
outage delays new assignments and dashboards; it does not destroy work already
accepted by the machines.

For a single user, the same printer can be driven directly from a workstation,
then keep printing when the laptop sleeps or leaves the network. The protocol
and firmware remain GPL infrastructure; fleet orchestration, repository,
dashboard, support, and management can form the product layer without making
safe execution dependent on that product's continuous availability.

Target-native modules add a second product boundary. The stable GPL kernel
supplies safe machine capabilities; printer behavior is compiled, signed,
deployed, inspected, rolled back, and fleet-managed as an application.
Machine applications can evolve at software-development speed while executing
locally at native MCU speed, without turning each behavior change into a
firmware service event.

The defining principle is:

> Provision globally. Execute locally. Observe from anywhere.
