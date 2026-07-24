# Founding Document 0001 — Motion Intentions

*Shorthand: **FD-0001**.* This is a **founding document** of HELIX, not
a request for comment. It was written to *argue* an architecture; that
architecture now *exists*, implemented in HELIX 0.9. The reasoning-first
form is preserved because the *why* is exactly what a reader of an
evolved codebase most needs — but read it as the design record of what
HELIX built, not a proposal awaiting a verdict.

FD-0001 is the architectural evolution of Klipper (carried out as a
permanent, backwards-compatible fork): per-joint trajectory
**intentions** instead of pre-computed step pulses, the primary MCU as
the machine's **time authority**, explicit **traffic classes** so
non-critical traffic can never halt a print, **pause-and-hold failure
recovery** with an execution log so failures resume instead of aborting,
**hardware-event triggers** in place of polling, and a
backwards-compatible, authenticated **FEC/UDP link layer** for wireless
and Ethernet boards.

## The documents

HELIX 0.9 implements the deterministic workstation architecture. Target
runtime proof and product provisioning remain before a production release.
See [Releases](../../Releases.md), the
[implementation status audit](../../Helix_Implementation_Status.md), and the
[Test &amp; Bring-up Plan](../../Helix_Test_Plan.md). Each document's own
status line records its more precise state.

| Document | Contents | Status |
| --- | --- | --- |
| [00-Vision.md](00-Vision.md) | Problem, premises, non-goals, prior art | Implemented — HELIX 0.9 |
| [01-Time_Model.md](01-Time_Model.md) | Machine time, primary-MCU authority, beacon sync, budgets | Implemented; mixed-frequency Pico/EBB36 USB discipline qualified, scoped action/CAN pending |
| [02-Intention_Protocol.md](02-Intention_Protocol.md) | Segment semantics, wire format, FPU-free execution, queue/refill, underrun | Implemented — HELIX 0.9 |
| [03-Traffic_Classes.md](03-Traffic_Classes.md) | Scheduled / Prompt / Telemetry classes | Implemented — HELIX 0.9 |
| [04-Actuator_Backends.md](04-Actuator_Backends.md) | Segment core vs backends; stepper, FOC/BLDC, PWM/DAC | Implemented — HELIX 0.9 |
| [05-Host_Architecture.md](05-Host_Architecture.md) | Impact on klippy/chelper; the segment fitter | Implemented and workstation-tested |
| [06-Migration.md](06-Migration.md) | Fork stance, coexistence, validation differ, phases, risk register | Adopted — HELIX 0.9 |
| [07-Link_Transport.md](07-Link_Transport.md) | BCH FEC framing v2, UDP over WiFi/Ethernet, mandatory link auth, ESP32 | Core/target builds pass; Lolin32 UDP auth/FEC qualified, other PHY and motion runs pending |
| [08-Failure_Recovery.md](08-Failure_Recovery.md) | Pause-and-hold, execution log, heater failsafe hold, resume | Implemented — HELIX 0.9 |
| [09-Hardware_Triggers.md](09-Hardware_Triggers.md) | Event-driven sensing: EXTI, comparators, capture timestamps | Framework implemented; per-target wiring and hardware proof remain |
| [10-Protocol_Library.md](10-Protocol_Library.md) | One MIT-licensed protocol library for host, firmware, third parties | Implemented — HELIX 0.9 |
| [11-Bootloader.md](11-Bootloader.md) | First-class bootloader: one image, in-band authenticated updates | Implemented — HELIX 0.9 |
| [12-ESP32_Architecture.md](12-ESP32_Architecture.md) | Network-native ESP32: radio quarantined on a separate core | Partial — component and modem consoles run on Lolin32; motion/peripheral proof pending |
| [13-Syscall_API.md](13-Syscall_API.md) | Unified cross-family board syscall ABI | Implemented — board syscall ABI v1.0 |
| [14-Heterogeneous_Fleets.md](14-Heterogeneous_Fleets.md) | Coexisting firehose (v1) and intent (v2) boards; one coordination timeline | Implemented; 12/64 MHz USB time merge qualified, coordinated action/CAN pending |
| [15-CANFD_Transport.md](15-CANFD_Transport.md) | Transactional CAN FD, real bridge/node identities, SocketCAN control, and timestamped USB-SOF time transfer | Vertical slice and physical 1 Mbit carrier/time base qualified; injected recovery, CAN motion/print, and 2/5/8 Mbit BRS pending |
| [16-STM32F767_Ethernet.md](16-STM32F767_Ethernet.md) | NUCLEO-F767ZI native Ethernet, cache-safe DMA, IRQ service, hardware timestamping, DHCP, and ADC streaming | Physical RMII console/DHCP/session qualified; PTP, ADC concurrency, link flap, and motion-load gates pending |
| [17-DMA_ADC_Acquisition.md](17-DMA_ADC_Acquisition.md) | Unified DMA ownership and block streams; STM32/RP2040/ESP32 ADC acquisition, oversampling, watchdogs, and reporting | Workstation implementation complete; F072/H723/RP2040/ESP32 live gates and RP2040 consumer/restart migration pass; remaining target/waveform/contention fixtures are explicit |
| [18-Autonomous_Heater_Control.md](18-Autonomous_Heater_Control.md) | Host-configured MCU PID and predictive thermal control, guarded characterization, local safety, and host-loss continuity | PID physically qualified; predictive controller implemented and workstation-tested, with paired physical gates remaining |
| [19-Unified_CAN_Gateway.md](19-Unified_CAN_Gateway.md) | One typed gateway runtime with USB and Ethernet host links, CAN and serial services, transport-independent time sources, and exact delivery accounting | Workstation core/proxy/RMII implementation compile-tested; Ethernet/PTP/H723 hardware gates pending |
| [20-Unified_Machine_Time.md](20-Unified_Machine_Time.md) | Configurable time authorities, timestamp adapters, protocol bridges, quality propagation, and motion-safe failover | Architecture and phased implementation plan; current primary-MCU/USB/CAN mechanisms remain the compatibility baseline |
| [21-Autonomous_Job_Execution.md](21-Autonomous_Job_Execution.md) | Stored deterministic job capsules, a network-mainboard printer fabric, replicated repositories, and host-independent completion | Architecture and phased implementation plan; physical autonomous execution pending |
| [22-Coordinated_Execution_Horizon.md](22-Coordinated_Execution_Horizon.md) | Deep per-MCU staging, negotiated queue capacity, renewable group execution grants, bounded coordinated stop, and recovery | Phase A/B implementation and three-board homing/coordinated-motion gate pass; physical expiry-bound and recovery qualification in progress |
| [23-Portable_Machine_Programs.md](23-Portable_Machine_Programs.md) | One restricted-Python workflow source for live Klipper and compiled dynamic scores, with typed operations, resource leases, evidence, and forward recovery | Architecture and implementation proposal; OpenAMS extraction complete locally, compiler/VM and physical gates pending |

## Reading order

Start with [00-Vision.md](00-Vision.md). Then, by interest:

* *Protocol / firmware*: 02 → 22 → 10 → 04 → 01 → 20 → 21 → 23 → 03 → 09 → 17 → 18 → 07 → 15 → 16 → 19 → 11 → 13 → 12
* *Host / klippy*: 02 → 05 → 22 → 21 → 23 → 10 → 20 → 17 → 18 → 15 → 16 → 19 → 08 → 06
* *"Is this safe and landable?"*: 00 → 06 (risk register, fleet) → 08
  (pause-and-hold, heater policy) → 22 (bounded group execution) → 21
  (host-independent execution) → 23 (bounded dynamic workflows) → 02
  (underrun) → 03
* *Third-party device vendor*: 10 → 02 → 03 → 20 → 21 → 23 → 17 → 07 → 15 → 16 → 19 → 11

## Glossary

* **Intention / segment** — a per-joint statement "position is
  q(t) = q₀ + v·t + ½a·t² from machine clock C for duration T". The
  unit of the new motion protocol; says *where*, never *how*.
* **Joint / actuator** — one independently driven degree of freedom in
  the machine's *actuator* space (a stepper, a BLDC servo, a DAC
  channel) — after kinematics, not a cartesian axis.
* **Machine time** — the canonical timeline all intentions are scheduled
  against. It is currently defined as the primary MCU's counter
  ([01-Time_Model.md](01-Time_Model.md)); the compatible evolution to
  configurable authorities and transport-neutral timestamp adapters is
  [20-Unified_Machine_Time.md](20-Unified_Machine_Time.md).
* **Job capsule** — an immutable, content-addressed compiled print containing
  the exact configuration-bound, timestamped tracks and policies needed for
  autonomous local execution
  ([21-Autonomous_Job_Execution.md](21-Autonomous_Job_Execution.md)).
* **Machine program / dynamic score** — one portable, bounded workflow source
  that live Klipper executes through an adapter and HELIX compiles into an
  event-driven statechart between timed trajectory chapters
  ([23-Portable_Machine_Programs.md](23-Portable_Machine_Programs.md)).
* **Printer fabric** — the typed in-machine control, time, delivery, and
  telemetry plane joining the network mainboard to local actuators and
  downstream CAN/CAN-FD, serial, or future transport adapters. It is not a
  transport-neutral byte stream.
* **Actuator backend** — the MCU-side executor that realizes segments
  on specific hardware (step/dir pulses, FOC setpoints, PWM duty).
* **Segment core** — the actuator-independent MCU module owning
  queues, chaining, time conversion, underrun, and trsync aborts.
* **Chained encoding** — segments carry only (duration, v, a); start
  position and time are implied by the previous segment's exact
  quantized endpoint, so no drift can accumulate.
* **Rebase** — the explicit re-anchoring command
  (`trajectory_rebase`) used at motion start, after homing, and after
  an underrun.
* **Horizon** — how far into the future an actuator's queued segments
  extend, in machine time; reported by the MCU and used for refill
  flow control.
* **Committed horizon / execution grant** — the shared machine-time bound
  through which a coordination group is currently authorized to execute.
  Work may be staged beyond it but remains inert until every essential
  participant acknowledges a later grant
  ([22-Coordinated_Execution_Horizon.md](22-Coordinated_Execution_Horizon.md)).
* **Underrun ramp** — the deceleration-to-zero an actuator executes
  autonomously if its queue runs dry mid-motion; a resumable event,
  not a shutdown.
* **Pause-and-hold** — the default response to recoverable failures:
  motors stay energized at their held positions, heaters follow their
  failure policy, state and logs are retained
  ([08-Failure_Recovery.md](08-Failure_Recovery.md)).
* **Execution log** — the uplink twin of the intention queue: a
  per-board ring buffer of what was *actually* executed (segments,
  triggers, holds), streamed live on Class 2 and reliably dumped
  after failures; both flight recorder and resume ground-truth.
* **Failsafe hold (heaters)** — opt-in per-heater policy keeping a
  heater (typically the bed) at its last target autonomously during
  pause-and-hold, under a hard temperature ceiling, duration bound,
  and on-MCU runaway checks.
* **Trigger source** — a hardware event producer (GPIO edge IRQ,
  analog comparator, ADC watchdog) that fires trsync directly, with
  optional timer-capture timestamps
  ([09-Hardware_Triggers.md](09-Hardware_Triggers.md)).
* **Protocol library** — the single MIT-licensed implementation of
  the wire protocol (both framings, codecs, state machines; a
  freestanding C++ core with a C-linkage API), consumed by host,
  firmware, bootloader, and third parties; embedded profile is
  heap-free and sized for the STM32F072
  ([10-Protocol_Library.md](10-Protocol_Library.md)).
* **First-class bootloader** — bootloader shipped inside every
  firmware image, speaking the same protocol for in-band,
  authenticated, unbrickable updates
  ([11-Bootloader.md](11-Bootloader.md)).
* **Traffic class** — one of Scheduled (Class 0), Prompt (Class 1),
  Telemetry (Class 2), distinguished by failure semantics
  ([03-Traffic_Classes.md](03-Traffic_Classes.md)).
* **Framing v2** — the negotiated frame format replacing CRC16 with a
  BCH error-correcting trailer
  ([07-Link_Transport.md](07-Link_Transport.md)).

## Relationship to existing docs

These founding documents record the *why* and the design of the HELIX
evolution; the authoritative descriptions of current behavior remain
[Code_Overview.md](../../Code_Overview.md),
[Protocol.md](../../Protocol.md),
[MCU_Commands.md](../../MCU_Commands.md), and
[Benchmarks.md](../../Benchmarks.md), which are cited throughout.
