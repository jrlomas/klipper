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
only after every essential participant has acknowledged the grant and remains
healthy.

The queue answers “what should I do later?” The grant independently answers
“how far may I proceed now?” Actual execution ends at the earlier of the
staged-work horizon and the execution-grant horizon. A grant is a ceiling, not
a claim that every queue already contains work through that time. This
distinction is required for endstop-driven homing and probing, whose queues are
intentionally drip-fed in short interruptible windows.

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
policy flags
authentication inherited from the carrier session
```

Phase C adds a staged-manifest/checkpoint digest for transactional suffix
replacement. It is not an acceptance precondition for the Phase B liveness
ceiling.

The rules are deliberately narrow:

1. A new epoch begins closed: no new trajectory may execute.
2. Every participant separately reports health, time qualification, and its
   exact staged horizon.
3. The coordinator chooses a future primary-machine clock that bounds
   execution independently of queue depth.
4. It transmits the future-dated grant to every participant. The primary MCU
   owns this machine clock; every secondary derives its local expiry with its
   onboard disciplined mapping rather than trusting a host-supplied local
   timestamp.
5. A participant accepts a grant only for its current epoch, while Class-0
   time remains qualified, and only when its sequence and machine/local
   horizons advance monotonically.
6. The coordinator reports the horizon as group-confirmed only after every
   essential participant acknowledges the same epoch, sequence, and
   machine-time horizon.
7. Until that all-node acknowledgement exists, the preceding group-confirmed
   horizon remains the host-ingest authority. A subset may already have
   installed the one newer proposed ceiling; therefore a rejected renewal
   during active motion closes ingestion and stops further reproposals.
8. Grants are renewed early enough that retransmission and clock uncertainty
   cannot consume the minimum scheduling lead.

A queue shorter than the grant is valid: it holds, completes, or invokes the
ordinary controlled-underrun path earlier. Requiring it to be staged through
the lease would create a circular dependency and makes safe drip-fed homing
impossible.

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
time qualification, or its separately monitored queue margin becomes unsafe:

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
each node's latest motion <= that node's last accepted grant
partial-delivery divergence <= one renewal increment + clock/IRQ error
                              + the declared controlled-stop policy
```

A coordinator may repropose after a rejection only while the group is idle.
During active motion, it closes ingestion and lets the already-installed
bounded grants stop the group. This prevents a reachable subset from walking
its ceiling forward through repeated proposals while another node rejects
them.

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
5. configure a fresh group epoch and obtain an all-participant grant;
6. issue one common future recovery rebase while normal ingress remains
   closed; and
7. reopen normal ingress, then stage and commit a newly compiled suffix.

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

## Phase A/B commissioning record

The first three-board physical commissioning run used the V0 Pico (RP2040),
Rodent (ESP32/WiFi), and EBB36 (STM32G0B1/CAN) as one execution group. It
deliberately included hardware-triggered homing, whose short drip-fed queues
are the counterexample that established why a grant must be a ceiling rather
than a promise of staged depth.

- [x] RP2040, ESP32, and STM32G0B1 firmware built with the group ABI; fresh
      `e5c121b4` images were flashed to all three motion participants.
- [x] A transiently unqualified WiFi member at startup closed admission
      without latching a false active-motion fault; idle reproposals later
      converged all members on one epoch and sequence.
- [x] A complete physical `G28` retained the hardware-trigger path and ended
      with `xyz` homed at `[110,110,30]`; all three members reported committed
      sequence 378 with no recovery or renewal fault.
- [x] A subsequent coordinated G1 moved Pico X/Y and Rodent Z to
      `[60,60,40]`; all members reported sequence 495, remained clock
      qualified, and Klipper stayed ready.
- [ ] Physically interrupt a member during simultaneous multi-axis and
      extrusion motion, independently measure each held endpoint, and
      qualify the declared divergence and recovery bounds.

Two commissioning defects were corrected before those passes:

1. `toolhead.get_last_move_time()` includes Klippy's future startup scheduling
   lead, so it falsely classified an idle rejection as active motion. The host
   now decides this from each trajectory stepper's primary-machine-domain
   horizon of actually queued nonzero motion; a confirmed trigger or underrun
   clears that horizon.
2. An idle rejection was retried every 100 ms while advancing its proposal by
   the 250 ms renewal interval. That made the expiry race ahead of wall time
   and eventually exceeded the safe half-wrap comparison interval of a
   64 MHz timer. Idle reproposals now use the normal renewal cadence. A
   160-renewal workstation regression proves the proposal remains at the
   configured horizon instead of running away.

The next physical Rodent print exposed a third, different boundary. Klippy
reported approximately 2.1 seconds of planned toolhead work, but
`serialqueue.c` made reqclock-tagged frames eligible for transmission only
100 ms before their deadlines. The first Rodent trajectory underrun followed
a reliable-carrier RTO increase to 200 ms: the next Z segment was present in
the host intention twin but had not been physically sent to the MCU. The
executor began its configured emergency ramp at the segment boundary and
reported the underrun 7,495 Rodent ticks later (374.75 us). This was not an
I2S execution failure: the board reported zero I2S deadline misses, a maximum
refill cost of 16,287 cycles against a 23,040-cycle budget, no WiFi
disconnect or brownout, no modem power saving, and no UDP ring drops.

Datagram transports now set a per-link serialqueue `send_ahead` horizon
(default 1.0 s, configurable to 30 s). This turns planned network lookahead
into physically transmissible work early enough to survive the observed ARQ
backoff. It does not widen the executable safety boundary; the short
all-participant execution grant remains authoritative.

That stop also proved the recovery epoch rule. Firmware correctly refuses to
extend an expired epoch, while the original host grant timer correctly
refused to renew anything during a recovery hold. The two safe local rules
therefore deadlocked `RESUME_MOTION` at the ordinary pre-lookahead check:
`HELIX execution group has no all-MCU grant`. Recovery now configures a fresh
random epoch on every idle member, waits for every configuration
acknowledgement, commits one all-MCU grant, and only then emits coordinated
recovery rebases. Ordinary G-Code ingress remains closed during the entire
transaction, and a timeout leaves the print paused and retryable.

The live host deployment also caught a capacity mistake in observability:
blindly applying the explicitly configured 1,024-record execution-log ring to
newly discovered Rodent exhausted its MCU config allocator. Automatic
trajectory participants now use a separate bounded `execlog_auto_size`
(128 records by default), while explicitly named boards retain
`execlog_size`. After firmware restart Rodent configured 363 move slots, all
three motion participants converged and committed grant sequence 8, and a
reliable `EXECLOG_DUMP` returned named Rodent records as part of 1,098 total
records with no invalid transport bytes.

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
