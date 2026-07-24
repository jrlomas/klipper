# FD-0001: Deep Queues and the Coordinated Execution Horizon

Status: architecture and implementation plan. HELIX currently streams one
directly executable trajectory horizon and stops an individual actuator when
that queue underruns. The secure UDP and physical Rodent tests demonstrated
why those two responsibilities must be separated before queues are made much
deeper.

This document extends the [intention protocol](02-Intention_Protocol.md),
[failure-recovery model](08-Failure_Recovery.md), [heterogeneous-fleet
rules](14-Heterogeneous_Fleets.md), and [autonomous job
architecture](21-Autonomous_Job_Execution.md).

## The observed failure

During the 2026-07-23 V0 Rodent print, the ESP32 stayed associated, reported
approximately -50 dBm RSSI, used no WiFi power saving, and reported no local
UDP RX-ring, TX-ring, or send errors. Nevertheless, the host-side reliable
datagram carrier repeatedly expanded its retransmission timeout to 800 ms.
The directly executable trajectory horizon was only approximately 1.0--1.1
seconds. `stepper_z` therefore exhausted its local stream and entered its
controlled underrun stop while Pico and EBB36 still had work.

Increasing the ordinary Klippy move buffer to two seconds is a useful
compatibility correction for the measured 800 ms outage, but it is not the
final architecture:

* one slow participant can still exhaust its horizon independently;
* assigning ten seconds to Rodent while another board holds one second lets
  the joints diverge for seconds after a link failure;
* a global ten-second Klippy buffer delays speed-factor and interactive
  changes and may exceed a smaller MCU's allocator; and
* received data and authorized execution are incorrectly treated as the same
  state.

## Central decision

HELIX separates **planned**, **staged**, and **committed** time:

```text
now              committed horizon                  planned horizon
 |======================|-----------------------------------|
      executable now          present, verified, not executable

                         execution grant/lease
             renewed only while every essential participant is healthy
```

An MCU may receive and validate a deep future suffix without permission to
execute it. Every actuator in a coordination group may execute only through
one shared machine-time **committed horizon**, conveyed by a renewable
**execution grant**. The host or autonomous mainboard extends that horizon
only after every essential participant has acknowledged enough staged work
and remains healthy.

The queue answers “what should I do later?” The grant independently answers
“how far may I proceed now?”

## Terms

* **planned horizon** -- the end of deterministic work known by the planner;
* **staged horizon** -- the end of work received, authenticated, validated,
  and retained by a particular MCU;
* **committed horizon** -- the latest shared machine-time instant the
  coordination group is authorized to cross;
* **execution grant** -- an epoch-bound, monotonically increasing committed
  horizon installed on every essential MCU;
* **essential participant** -- a node whose loss invalidates coordinated job
  execution, normally every motion/extrusion MCU and any safety-critical
  process controller;
* **group epoch** -- a random, non-repeating execution generation. Reconnect,
  reset, recovery, or a new job creates a new epoch; commands from an old
  epoch can never extend the current grant; and
* **stop bound** -- the maximum machine-time separation between the first
  detected failure and the last motion the group was previously authorized
  to execute.

## Capacity negotiation

Deep queuing is capability- and capacity-driven. At connect time every
participating MCU reports:

```text
trajectory ABI and supported polynomial orders
maximum and currently free segment slots
maximum and currently free queue bytes
maximum representable staged duration
execution-grant support and minimum scheduling lead
controlled-stop capability and configured deceleration
retained epoch/grant state after reset (normally none)
```

Runtime status reports the same free capacity plus staged and committed
horizons. A host must stop staging before either the slot or byte bound. It
must not infer capacity from the MCU family: ESP32 PSRAM, internal ESP32 SRAM,
RP2040, STM32G0, and STM32H7 builds have radically different memory budgets.

The useful prefetch depth is the minimum capacity of the tracks that must
advance together, not the maximum capacity of the richest node. A richer node
may retain more uncommitted work for transport smoothing, but that extra data
does not increase the group's committed horizon.

## Grant protocol

Each grant contains:

```text
group_id
epoch
grant_sequence
committed_machine_clock
staged-manifest/checkpoint digest
policy flags
authentication inherited from the carrier session
```

The rules are deliberately narrow:

1. A new epoch begins closed: no new trajectory may execute.
2. Every participant stages work and reports its exact staged horizon and
   checkpoint digest.
3. The coordinator chooses a committed machine clock no later than the
   minimum acknowledged staged horizon.
4. It transmits the future-dated grant to every participant.
5. A participant accepts a grant only for its current epoch and only when its
   sequence and horizon advance monotonically.
6. The coordinator counts the horizon as committed only after every essential
   participant acknowledges the same epoch, sequence, horizon, and digest.
7. Until that all-node acknowledgement exists, the preceding committed
   horizon remains authoritative.
8. Grants are renewed early enough that retransmission and clock uncertainty
   cannot consume the minimum scheduling lead.

Acknowledgements are state reports, so duplicate grant and acknowledgement
messages are idempotent. A delayed packet from an earlier sequence cannot
shorten or extend a later grant.

### Why the grant is future-dated

“Send pause when one MCU disappears” is not sufficient: the pause packet can
be delayed by the same link failure that caused the problem. Instead, every
MCU already holds a stop bound in its disciplined local clock. Continued
motion requires a renewal delivered before that bound. Silence therefore
fails toward a bounded hold.

The grant interval is configurable per transport and topology. It need not
equal the staged depth. A practical system might stage 10--30 seconds while
renewing a 1--2 second executable horizon several times per second.

## Failure behavior

When an essential participant stops acknowledging, disconnects, resets, loses
time qualification, or reports insufficient staged work:

1. the coordinator immediately stops issuing later grants;
2. if the affected links still work, it may replace the remaining committed
   suffix with a common, future-dated coordinated braking checkpoint;
3. every reachable node follows that checkpoint;
4. a partitioned-but-running node cannot receive the replacement and executes
   no later than its last acknowledged grant;
5. at that bound, each MCU discards the uncommitted suffix, performs its
   bounded local controlled-stop policy, remains energized, records its
   terminal accumulator and execution log, and enters recovery hold; and
6. no node accepts more motion until the coordinator creates a new epoch,
   reconciles all held positions, and issues the existing explicit recovery
   rebase.

An immediate hardware endstop, probe, emergency stop, or thermal fault still
uses the existing trsync/hardware-trigger path. The grant is a liveness and
coordination bound, not a replacement for faster local safety mechanisms.

## The distributed-systems limit

Independent lossy links cannot provide a mathematically atomic “all MCUs
pause at exactly the same instant” decision when one participant becomes
unreachable. The coordinator cannot distinguish a dead node from a partitioned
node that received the last grant, and the other nodes cannot know whether
that node stopped early.

HELIX therefore promises a measured **bound**, not impossible atomicity:

```text
latest motion <= last all-node committed horizon
cross-node stop skew <= clock error + IRQ/scheduler error
                       + the declared controlled-stop policy
```

A common hardware interlock, shared trigger wire, deterministic fieldbus
broadcast, or redundant safety controller can tighten that bound. An MCU that
loses power stops electrically and cannot participate in a geometric braking
trajectory. No packet protocol can change those facts.

## Controlled-stop geometry

Expiring a grant in the middle of arbitrary independent joint polynomials and
letting every joint apply unrelated maximum deceleration does not preserve the
Cartesian path. It may still be safer than continuing one joint alone, but it
must be recorded as a recovery stop, not called a transparent pause.

HELIX uses two levels:

* **preferred stop checkpoint** -- while communication remains possible, the
  coordinator supersedes the uncommitted suffix with a shared-duration,
  kinematically planned braking path. Motion and extrusion remain coherent;
* **lease-expiry stop** -- when replacement traffic cannot arrive, each MCU
  stops from its state at the last committed boundary with a bounded local
  policy. The resulting held joint positions are authoritative and require
  the existing reconciliation/splice workflow before resume.

The host must retain enough uncommitted lead between the committed and staged
horizons to compile and deliver the preferred stop. Queue data needs an epoch
and checkpoint identity so the old suffix can be discarded without a stale
rebase resurrecting it.

## Interaction with controls

Deep staging must not make the printer feel ten seconds behind:

* speed factor, feed override, pressure-advance changes, and live tuning apply
  only beyond the committed horizon;
* an interactive change invalidates and recompiles the uncommitted suffix;
* pause requests use the preferred coordinated-stop path when possible;
* emergency stop and hardware triggers remain immediate;
* ordinary prompt and telemetry traffic never consume reserved Class-0 grant
  capacity; and
* autonomous job execution uses the same grant protocol, with the network
  mainboard replacing live Klippy as coordinator.

## Recovery and reset

Volatile MCU reset erases staged work and the installed grant. A restarted MCU
begins closed and advertises a new boot identity. Other participants retain
their old epoch only long enough to stop at its already committed horizon.

Recovery then follows [08-Failure_Recovery.md](08-Failure_Recovery.md):

1. drain execution logs and read held accumulators;
2. invalidate the abandoned staged suffix on every node;
3. reconcile the actual joint-space stop;
4. decide whether homing remains qualified;
5. create a fresh group epoch;
6. issue one common future recovery rebase; and
7. stage and commit a newly compiled suffix.

The secure-session epoch and the execution-group epoch are distinct. A carrier
may reconnect without authorizing motion, and a motion epoch may survive a
brief carrier outage until its bounded grant expires.

## Implementation phases

### Phase A -- observability and negotiated capacity

* extend trajectory status with total/free queue slots and bytes;
* report planned, staged, and committed horizons separately;
* expose per-MCU transport outage, retransmission, and grant-renewal margins;
* add `HELIX_EXECUTION_STATUS` and Atlas events; and
* retain the measured two-second datagram compatibility buffer while this
  work is commissioned.

### Phase B -- closed execution grant

* add epoch/sequence/grant commands and acknowledgements;
* reject trajectory activation without an open current grant;
* schedule lease expiry in each MCU's local disciplined clock;
* discard the uncommitted suffix and enter recovery hold on expiry; and
* regression-test duplicate, delayed, reordered, lost, and old-epoch grants.

### Phase C -- replaceable suffix and coordinated braking

* label staged chunks with epoch and checkpoint;
* split or checkpoint segments at shared machine-time commit boundaries;
* make uncommitted suffix replacement transactional;
* compile one shared-duration braking path across motion and extrusion; and
* prove terminal positions against the host intention twin and MCU execution
  logs.

### Phase D -- deep prefetch

* decouple trajectory staging depth from the ordinary Klippy move buffer;
* stage according to negotiated per-node capacity;
* support a configurable 10-second-or-greater target on capable fleets;
* keep the committed horizon short enough for acceptable control latency; and
* reuse the identical machinery for autonomous job-track dispatch.

## Qualification gates

No “coordinated pause” claim is complete until all of these pass:

- [ ] workstation state-machine tests for every ordering, duplication, wrap,
      reset, and partition case;
- [ ] queue exhaustion cannot allocate past the advertised slot/byte limit;
- [ ] old session and old execution epochs cannot extend a grant;
- [ ] unplug each essential link while all axes and extrusion are moving;
- [ ] measure the stop clock and held position independently on every MCU;
- [ ] demonstrate the declared worst-case stop bound at maximum tested
      transport outage and clock error;
- [ ] verify a reachable slow MCU causes no single-axis underrun;
- [ ] verify a partitioned MCU cannot execute beyond its last acknowledged
      committed horizon;
- [ ] reconcile and resume after both preferred checkpoint braking and
      lease-expiry braking;
- [ ] prove homing/probing hardware triggers retain their lower latency;
- [ ] verify speed-factor and pause latency remain within the configured
      committed horizon; and
- [ ] run full-speed multi-hour prints over USB, CAN FD, Ethernet, and the
      qualified WiFi profile without trajectory underrun or false grant expiry.

Until these gates pass, larger directly executable buffers remain a transport
workaround, not the final coordinated-execution design.
