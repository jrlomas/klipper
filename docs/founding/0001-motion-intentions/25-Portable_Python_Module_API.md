# FD-0001: Portable Python Module API

Status: API architecture and proposed source contract, version 1.0 draft. The
common HELIX firmware engines described here exist. An initial `helix.types`,
`helix.module`, and `helix.compiler` checkpoint implements fixed-width host
integers, declaration metadata, AST validation, direct LLVM lowering, ARM
object generation, and constrained `.hmod` packaging for a small
`@on_start` actor subset. The complete type system, semantic resource API,
native import table, resource binder, compatibility executor, async lowering,
and runtime-loaded module adapters do not yet exist. Names and signatures in
this document remain a draft; no application compatibility is claimed until
the relevant gates are implemented and tested.

This document defines the author-facing API promised by
[portable machine programs](23-Portable_Machine_Programs.md) and the
[target-native module architecture](24-Target_Native_Machine_Modules.md). It
builds above the [board syscall substrate](13-Syscall_API.md), the
[trajectory backends](04-Actuator_Backends.md), the
[DMA acquisition system](17-DMA_ADC_Acquisition.md), and
[autonomous heater control](18-Autonomous_Heater_Control.md).

## Thesis

Portable Python must target HELIX's common semantic engines, not the register
layout of an MCU family.

The same module source must run:

* as ordinary Python through a host/simulator implementation of the API;
* as a live Klipper compatibility application;
* as target-native code on STM32, RP2040, ESP32, Linux-process, or another
  qualified HELIX target; and
* on a mainboard coordinating operations that physically execute on other
  MCUs.

The source never asks whether it is running on STM32, RP2040, or ESP32. It
receives typed, capability-scoped handles and invokes one semantic contract.
The kernel decides whether an operation becomes a direct local call, a
scheduled common-firmware action, or a typed printer-fabric request.

```text
portable Python call
        |
        v
typed HELIX operation
        |
        +------ local common firmware engine
        |
        +------ downstream node through printer fabric
        |
        +------ live Klipper adapter
```

## The 90-percent rule

The portable API should expose the common modules that already perform most
of HELIX's useful timing, safety, queueing, and coordination work. It should
not encourage every uploaded module to implement a second scheduler, ADC
loop, trajectory queue, trigger path, or heater controller.

An audit of [src/Makefile](../../../src/Makefile) and the target-neutral
sources identifies these primary engines:

| Common engine | Current common sources | Python abstraction | What remains target-specific |
| --- | --- | --- | --- |
| Kernel scheduling, objects, commands | `sched.c`, `basecmd.c`, `command.c`, `protocol_abi.c` | `Context`, events, operations, deadlines, resource handles | timer IRQ, console link, reset and board initialization |
| Motion intentions and coordination | `trajq.c`, `traj_stepper.c`, `traj_pwm.c`, `timesync.c` | `MotionGroup`, `Actuator`, `TrackRef`, `MachineClock` | edge generation, timer/counter details, PWM compare writes |
| Acquisition and signal processing | `adc_stream.c`, `acq_block.c`, `acq_ring.c`, `adc_filter.c`, `adc_safety.c`, `dma_resource.c`, `sos_filter.c` | `Sensor`, `AnalogStream`, `Capture`, `Observation` | ADC sequencing, DMA requests, cache/reachability details |
| Hardware events and coordinated stop | `trigger_source.c`, `trigger_analog.c`, `trsync.c`, `endstop.c` | `TriggerSource`, `TriggerCondition`, `StopGroup` | EXTI/comparator/watchdog/capture wiring |
| Thermal control and safe hold | `heater_control.c`, `heater_control_math.c`, `heater_hold.c` | `Heater`, `HeaterProfile`, `HeaterStatus` | ADC/PWM binding and hardware cutoff inputs |
| Scheduled outputs and basic inputs | `gpiocmds.c`, `pwmcmds.c`, `pulse_counter.c`, `buttons.c` | `DigitalOutput`, `PwmOutput`, `CounterInput`, `DigitalInput` | physical pin and peripheral binding |
| Device buses and common device drivers | `spicmds.c`, `i2ccmds.c`, `tmcuart.c`, `sensor_bulk.c`, sensor modules | capability-scoped `SpiDevice`, `I2cDevice`, `SerialDevice` | bus controller, pins, DMA and electrical limits |
| Evidence and diagnostics | `execlog.c`, `trace.c`, `self_test.c` | `Journal`, `Telemetry`, structured fault evidence | transport drain and retained-memory details |
| Typed transport and delivery | `gateway_runtime.c`, `gateway_protocol.c`, protocol/session code | `Device`, typed messages and operations | USB, Ethernet, CAN/CAN FD, Wi-Fi, serial |

These engines are the **semantic 90 percent**. The percentage is an
architectural rule rather than a line-coverage claim: an application should
normally compose these services, while only a driver or new control algorithm
supplies the remaining specialized behavior.

For example, a Python heater application does not read an ADC, average it,
calculate every PWM edge, and refresh a watchdog. It asks the existing
`Heater` engine to select a qualified profile and target. A BLDC control
module is different: its algorithm is new behavior, but synchronized
ADC/PWM, break input, timing, and trajectory sampling still come from a
kernel-owned control domain.

## Design principles

1. **Semantic before electrical.** `Heater.set_target()` is preferred to
   `PwmOutput.set()`. `MotionGroup.run()` is preferred to toggling a step pin.
2. **Handles, never addresses.** Portable source cannot construct a pin,
   peripheral number, memory address, IRQ, DMA request, or node address.
3. **Capabilities, never family tests.** Code declares required rates,
   features, and limits. It never branches on `STM32`, `RP2040`, or `ESP32`.
4. **One local/remote operation model.** The same handle may be implemented on
   the current MCU or another node.
5. **Machine time is explicit.** No wall-clock time, implicit tick frequency,
   or floating-point seconds appear in portable scheduling.
6. **Evidence accompanies values.** Sensor data carries timestamp, quality,
   sequence, and source generation.
7. **No hidden blocking.** Any call that may wait returns an awaitable
   `Operation`; synchronous calls are bounded local reads or pure computation.
8. **No hidden allocation.** Records, state, arrays, messages, and result
   capacity are known during compilation.
9. **Safety remains kernel-owned.** A capability handle cannot widen a
   physical ceiling or suppress a kernel fault.
10. **The rich Python API lowers to a small native ABI.** Convenience wrappers
    do not require one firmware function-pointer slot per Python method.

## Package structure

Portable modules may import only these packages:

```text
helix.types       fixed-layout types, records, enums, Result and Option
helix.time        MachineTime, Duration, Deadline and time conditions
helix.module      module/workflow declarations, callbacks and Context
helix.machine     semantic resources and distributed operations
helix.signal      observations, acquisition, filtering and triggers
helix.io          bounded capability-scoped device I/O
helix.rt          hard-real-time control-domain declarations and frames
helix.math        deterministic pure numeric intrinsics
```

Host-only tooling lives under `helix.host`, `helix.testing`, and
`helix.compiler`. Importing those packages from portable code is a compile
error.

Target-family packages such as `helix.stm32` are deliberately absent from the
portable API. A privileged target extension may use a separate nonportable
SDK, but it cannot claim cross-family source compatibility.

## Fixed-layout type system

### Scalars

`helix.types` defines:

```python
bool8
u8, u16, u32, u64
i8, i16, i32, i64
f32, f64
q15, q31
```

`f64` is accepted only when the target profile provides it or the compiler
selects and accounts for a qualified software implementation.

The type defines overflow, conversion, comparison, shift, NaN, saturation,
and rounding behavior. Python host implementations reproduce those semantics;
ordinary Python `int` and `float` are not silently substituted in portable
state.

### Units

Physical values are distinct types:

```python
Duration
Frequency
MachineTime
Temperature
DutyCycle
Current
Voltage
Angle
AngularVelocity
Position
Velocity
Acceleration
```

Explicit constructors convert literals:

```python
Duration.ms(250)
Frequency.khz(20)
Temperature.celsius(260)
DutyCycle.ratio(f32(0.50))
Current.ampere(f32(1.5))
```

Incompatible units do not implicitly mix. Unit representation is selected by
the API specification and remains stable across targets.

### Records, configuration, and state

```python
from helix.types import config, record, state, u8

@record
class LoadRequest:
    lane: u8
    speed: Velocity

@config
class ToolChangeConfig:
    motion: MotionGroup
    cutter: Device[CutterProtocol]
    material: Device[MaterialProtocol]
    filament: Sensor[bool8]

@state
class ToolChangeState:
    successful_changes: u32
    last_lane: u8
```

`@record` is immutable. `@config` is immutable after module activation and
contains loader-bound resource handles or fixed values. `@state` is mutable
only by the owning module and has a fixed layout. A field live across an
`await` is placed in the compiler-generated resumable context.

### Bounded containers

```python
Array[T, N]
Vector[T, MaxN]
Bytes[N]
String[MaxBytes]
Option[T]
Result[T, E]
```

`Array` has fixed length. `Vector`, `Bytes`, and `String` carry a current
length bounded by their compile-time capacity. No operation can grow beyond
that capacity. Portable code has no general `list`, `dict`, `set`, or
unbounded string.

## Resource and capability binding

A configuration field declares the kind of resource required; deployment
maps it to a semantic role in the printer capability graph:

```python
@config
class CoolingConfig:
    hotend: Heater
    part_fan: Fan
    chamber: Sensor[Temperature]
```

Deployment configuration binds:

```text
CoolingConfig.hotend   -> heater.hotend
CoolingConfig.part_fan -> fan.part
CoolingConfig.chamber  -> sensor.chamber
```

The source does not contain `PA3`, `TIM1_CH2`, `ADC1_IN5`, CAN UUIDs, or IP
addresses.

A module may refine requirements:

```python
part_fan: Fan = require(
    FanSpec(min_update_rate=Frequency.hz(20), tachometer=True))

current: AnalogStream = require(
    AnalogStreamSpec(
        channels=2,
        sample_rate=Frequency.khz(40),
        synchronized=True,
        maximum_uncertainty=Duration.ns(100)))
```

`require()` is compile/deployment metadata. It does not probe hardware while
the module executes. Missing requirements reject the module before
activation.

Optional resources are explicit:

```python
door: Option[Sensor[bool8]]
```

The compiler may emit target variants for materially different optional
capabilities. Code may not use runtime family-name inspection as a substitute
for a capability contract.

### Opaque handles

Every resource value is an opaque, generation-bound handle. Logically it
contains:

```text
resource type and API version
semantic identity
owner node and session generation
configuration/calibration generation
authority/capability mask
kernel-local routing token
```

Portable code may pass, store, and compare handles for identity. It cannot
extract a pointer, OID, pin, bus, node address, or transport route.

### Source portability versus target admission

The API is family-neutral, but native-module loading is a target capability.
A small legacy MCU may continue running fixed common firmware operations
without having enough executable RAM, storage, relocation support, or
isolation for a runtime-loaded module.

The compiler therefore distinguishes:

```text
API-compatible       source has no family-specific dependency
backend-supported    LLVM can emit the target object and relocation model
loader-capable       firmware can verify, place, and activate the module
resource-qualified   this board satisfies the module capability manifest
physically-qualified measured timing and safety gates pass
```

Only the final state authorizes execution. This is not a reason to introduce
family-specific Python; it is a reason for precise preflight rejection and
for leaving some small nodes as fixed semantic-operation providers.

## Module and actor model

### Persistent machine applications

The primary deployment unit is a stateful actor:

```python
from helix.module import (
    module, on_start, on_message, on_timer, on_shutdown)

@module(name="filament-supervisor", api="1.0")
class FilamentSupervisor:
    config: FilamentConfig
    state: FilamentState

    @on_start
    def start(self, ctx: Context) -> None:
        ctx.telemetry.emit(SupervisorStarted())

    @on_message(SetActiveLane)
    def set_lane(self, ctx: Context, message: SetActiveLane) -> None:
        self.state.active_lane = message.lane

    @on_timer(period=Duration.ms(100))
    def sample_health(self, ctx: Context) -> None:
        observation = self.config.runout.latest()
        if observation.is_some():
            sample = observation.unwrap()
            if (sample.quality == ObservationQuality.VALID
                    and sample.value):
                ctx.faults.raise_recoverable(RunoutDetected(sample))

    @on_shutdown
    def shutdown(self, ctx: Context, reason: ShutdownReason) -> None:
        ctx.journal.emit(SupervisorStopped(reason))
```

The initial callback set is:

```text
@on_start
@on_message(MessageType)
@on_timer(period=..., phase=...)
@on_observation(config_field)
@on_parameters
@on_cancel
@on_shutdown
```

Callbacks are registered from static metadata. Uploaded code does not replace
the hardware vector table or dynamically attach an arbitrary function
pointer.

### Callback serialization

Application callbacks for one module instance are non-reentrant and execute
to completion in event order. They may perform bounded local work and start a
machine program. They may not block or call `await` directly.

Long or variable-duration behavior is an explicit machine program:

```python
from helix.module import machine_program

@machine_program(
    resources=("motion", "cutter", "material", "extruder"),
    timeout=Duration.seconds(45),
)
async def change_material(
    ctx: Context,
    config: ToolChangeConfig,
    requested: MaterialId,
) -> ChangeResult:
    async with ctx.resources.acquire(
        config.motion, config.cutter, config.material,
        deadline=Deadline.after(Duration.seconds(2)),
    ) as acquired:
        if acquired.is_err():
            return ChangeResult.RESOURCES_BUSY
        await config.motion.run(ctx.tracks["park_and_retract"])
        await config.cutter.invoke(
            CutRequest(), deadline=Deadline.after(Duration.seconds(5)))
        await config.material.invoke(
            UnloadRequest(), deadline=Deadline.after(Duration.seconds(15)))
        await config.material.invoke(
            LoadRequest(requested),
            deadline=Deadline.after(Duration.seconds(20)))
        await config.motion.run(ctx.tracks["prime_clean_and_restore"])
    return ChangeResult.COMPLETE
```

The compiler lowers the function to native resumable entry points. `await`
does not block the MCU scheduler.

### Context

`Context` is passed explicitly and exposes:

```python
class Context:
    clock: MachineClock
    resources: ResourceManager
    operations: OperationManager
    events: EventManager
    tracks: TrackCatalog
    checkpoints: CheckpointManager
    journal: Journal
    telemetry: Telemetry
    faults: FaultManager
```

There is no process-global `printer` object. A module cannot discover
undeclared resources by walking a runtime object graph.

## Time API

### Machine time

```python
class MachineTime:
    epoch: u32
    ticks: u64

class MachineClock:
    def now(self) -> MachineTime: ...
    def quality(self) -> TimeQuality: ...
    def after(self, duration: Duration) -> Deadline: ...
    async def sleep_until(self, deadline: Deadline) -> WakeReason: ...
```

`MachineClock.now()` is a bounded local read of the disciplined machine-time
replica. It never performs a transport round trip.

`MachineTime` values from different epochs cannot be ordered. Conversion from
duration to target-local timer ticks remains a kernel responsibility.

### Deadlines

Every wait has one of:

```python
Deadline.at(machine_time)
Deadline.after(duration)
Deadline.indefinite_safe_hold()
```

An indefinite wait is legal only in a declared safe-hold state. No portable
code calls `sleep()` using wall-clock seconds.

## Observations and sensors

Every observed value has evidence:

```python
from typing import Generic, TypeVar

T = TypeVar("T")

@record
class Observation(Generic[T]):
    value: T
    time: MachineTime
    sequence: u32
    source_generation: u32
    quality: ObservationQuality
    uncertainty: Duration
```

Quality distinguishes at least:

```text
VALID
INFERRED_TIME
STALE
DISCONTINUITY
SENSOR_ERROR
SOURCE_RESET
UNAVAILABLE
```

### Sensor API

```python
class Sensor(Generic[T]):
    def latest(self) -> Option[Observation[T]]: ...

    async def sample(
        self,
        maximum_age: Duration,
        deadline: Deadline,
    ) -> Result[Observation[T], SensorError]: ...

    async def wait(
        self,
        condition: Condition[T],
        deadline: Deadline,
    ) -> Result[Observation[T], WaitError]: ...
```

`latest()` reads the kernel's coherent local observation cache and never
waits. `sample()` may request a fresh local or remote observation and is
therefore awaitable.

Predicates are typed data rather than opaque Python closures:

```python
Equal(value)
Changed()
Rising()
Falling()
AtLeast(value)
AtMost(value)
InRange(low, high)
Outside(low, high)
Stable(condition, duration)
```

The compiler can validate and serialize every condition.

## Acquisition API

`AnalogStream` exposes the existing DMA/block/filter/safety engine rather than
one-shot ADC polling:

```python
class AnalogStream:
    def status(self) -> AcquisitionStatus: ...
    def latest(self, channel: ChannelIndex) -> Option[Observation[u32]]: ...

    async def next_summary(
        self,
        subscription: SubscriptionId,
        deadline: Deadline,
    ) -> Result[Observation[AnalogSummary], AcquisitionError]: ...

    async def capture(
        self,
        samples: u16,
        deadline: Deadline,
    ) -> Result[CaptureRef, AcquisitionError]: ...
```

Acquisition rate, channel sequence, hardware/software oversampling, filtering,
windowing, report class, and safety action are activation-time resource
properties. Ordinary applications cannot reconfigure a running ADC shared by
other clients.

`CaptureRef` identifies a bounded immutable block in kernel-owned storage. An
application may process it through bounded iterators or request publication;
it never owns a DMA descriptor.

## Trigger and coordinated-stop API

```python
class TriggerSource:
    def status(self) -> TriggerStatus: ...

    async def arm(
        self,
        condition: TriggerCondition,
        stop_group: Option[StopGroup],
        deadline: Deadline,
    ) -> Result[ArmedTrigger, TriggerError]: ...

class ArmedTrigger:
    async def wait(self) -> Result[TriggerEvidence, TriggerError]: ...
    async def disarm(self) -> Result[None, TriggerError]: ...
```

`TriggerEvidence` contains the hardware-captured or qualified machine time,
source generation, reason, value where applicable, and timing uncertainty.

The target binding chooses EXTI, comparator, ADC watchdog, timer capture, or a
qualified task fallback. The application sees the same contract and can
require a maximum uncertainty or hardware-capture capability.

`StopGroup` maps to the common `trsync` and coordinated execution-horizon
machinery. A module cannot fabricate a stop-group member or remove another
actuator from an armed group.

## Motion API

Portable applications start precompiled trajectory tracks. They do not push
step pulses or construct arbitrary coefficients in ordinary workflow code:

```python
class MotionGroup:
    async def run(
        self,
        track: TrackRef,
        start: TrackStart = TrackStart.after_lead_time(),
        deadline: Deadline = Deadline.from_track(),
    ) -> Result[TrackResult, MotionError]: ...

    async def barrier(
        self,
        checkpoint: TrackCheckpoint,
        deadline: Deadline,
    ) -> Result[ExecutionEvidence, MotionError]: ...

    async def hold(
        self,
        reason: HoldReason,
    ) -> Result[HoldEvidence, MotionError]: ...

    def status(self) -> MotionGroupStatus: ...

class Actuator:
    def status(self) -> ActuatorStatus: ...
    async def position(self, deadline: Deadline) -> PositionEvidence: ...
```

`TrackRef` is a content-addressed capsule object bound to the exact
configuration, calibration, trajectory format, and target capabilities.

Rebase and recovery-rebase are not ordinary methods. They require a
`MotionRecovery` capability issued only after the kernel's reconciliation
gate. This prevents a module from clearing a halt barrier by calling a
convenient low-level function.

The common `trajq`, stepper/PWM/FOC backends, time conversion, group grants,
underrun, hold, and execution log remain below this API.

## Heater and output API

### Heater

```python
class Heater:
    async def set_target(
        self,
        target: Temperature,
        profile: HeaterProfileRef,
        deadline: Deadline,
    ) -> Result[HeaterStatus, HeaterError]: ...

    async def wait(
        self,
        condition: TemperatureCondition,
        deadline: Deadline,
    ) -> Result[HeaterStatus, HeaterError]: ...

    async def enter_hold(
        self,
        policy: HeaterHoldPolicyRef,
    ) -> Result[HeaterStatus, HeaterError]: ...

    async def release_hold(self) -> Result[HeaterStatus, HeaterError]: ...
    def status(self) -> HeaterStatus: ...
```

`HeaterProfileRef` selects an already characterized and safety-qualified
profile. A module cannot install arbitrary gains, disable verification, or
raise the configured temperature/power ceiling while the heater is active.

The kernel owns sampling cadence, PID/predictive loop, watchdog, sensor
limits, output clamping, host-loss behavior, and shutdown.

### Fan and semantic outputs

```python
class Fan:
    async def set(
        self,
        duty: DutyCycle,
        effective: MachineTime,
    ) -> Result[OutputEvidence, OutputError]: ...

    def status(self) -> FanStatus: ...
```

Specialized outputs such as cutters, valves, pumps, lasers, servos, and LEDs
should expose typed device protocols where their semantics are stronger than
raw PWM or GPIO.

## Typed device operations

Distributed and local devices share one protocol declaration:

```python
from helix.machine import operation, protocol

@protocol(name="openams.material", version=1)
class MaterialProtocol:
    @operation(replay=ReplayPolicy.RETURN_RECORDED_RESULT)
    async def load(
        self,
        request: LoadRequest,
        deadline: Deadline,
    ) -> Result[LoadEvidence, MaterialError]: ...

    @operation(replay=ReplayPolicy.RETURN_RECORDED_RESULT)
    async def unload(
        self,
        request: UnloadRequest,
        deadline: Deadline,
    ) -> Result[UnloadEvidence, MaterialError]: ...
```

A configuration field receives `Device[MaterialProtocol]`. Calling
`device.load()` lowers to an operation with:

```text
operation id and generation
protocol and method ABI
target resource handle
fixed request/result schemas
deadline
replay/idempotency policy
configuration and session generations
traffic and failure class
```

If the device is local, the kernel invokes its registered common-module or
native-module handler. If remote, the printer fabric transports the same
typed operation. If running under Klipper, the compatibility adapter invokes
the corresponding Python/MCU driver. Application source is unchanged.

String-named calls such as `machine.call("material.load", **kwargs)` are
permitted only as transitional compatibility wrappers. The compiler-facing
API uses typed protocol declarations.

## Events, messages, and operations

### Messages

Messages are fixed-layout `@record` types:

```python
@message(name="material.runout", version=1)
class RunoutEvent:
    lane: u8
    evidence: Observation[bool8]
```

```python
class EventManager:
    def send(
        self,
        target: ModuleRef,
        message: Message,
        delivery: DeliveryClass = DeliveryClass.PROMPT,
    ) -> Result[DeliveryToken, SendError]: ...

    def start(self, program: MachineProgram[T]) -> ProgramToken[T]: ...
```

The message declaration fixes its wire/storage schema. A module cannot mark
motion or safety authority as droppable telemetry merely by selecting a
different delivery class; each message/operation type has an allowed class
set.

### Awaitable operations

Every potentially delayed effect returns:

```python
E = TypeVar("E")

class Operation(Generic[T, E]):
    id: OperationId
    async def wait(self) -> Result[T, E]: ...
    async def cancel(self) -> Result[CancelEvidence, CancelError]: ...
```

Python `await operation` is shorthand for `await operation.wait()`.
Completions are generation-matched. A stale completion cannot resume a newer
program instance.

## Resources and leases

```python
class ResourceManager:
    def acquire(
        self,
        *resources: Resource,
        deadline: Deadline,
    ) -> LeaseRequest: ...

class LeaseRequest:
    async def __aenter__(
        self,
    ) -> Result[ResourceLease, ResourceError]: ...
    async def __aexit__(self, exception_type, exception, traceback) -> None: ...

class ResourceLease:
    generation: u32
    def contains(self, resource: Resource) -> bool8: ...
```

`async with` releases the lease on normal completion, declared fault, or
cancellation:

```python
async with ctx.resources.acquire(
    config.motion,
    config.extruder,
    config.material,
    deadline=Deadline.after(Duration.seconds(2)),
) as acquired:
    if acquired.is_err():
        return Err(WorkflowError.RESOURCES_BUSY)
    lease = acquired.unwrap()
    ...
```

The compiler sorts statically known resources into canonical order and rejects
provable cycles. Dynamic resource sets are bounded and acquired atomically.
The kernel revokes leases during terminal fault handling.

## Checkpoints, journal, telemetry, and faults

### Checkpoints

```python
class CheckpointManager:
    async def commit(
        self,
        value: CheckpointRecord,
        policy: CheckpointPolicy,
    ) -> Result[CheckpointEvidence, CheckpointError]: ...
```

Only `@record` fields declared in the module's state schema are durable.
Committing a checkpoint does not assert that physical state is valid after
reset; recovery still reconciles evidence.

### Journal

```python
class Journal:
    def emit(self, event: JournalEvent) -> Result[JournalSequence, JournalError]: ...
```

Journal events are bounded, reliable semantic transitions and fault evidence.
They map to the execution log and persistent job journal.

### Telemetry

```python
class Telemetry:
    def emit(self, sample: TelemetryRecord) -> TelemetryDisposition: ...
```

Telemetry is explicitly droppable and rate-budgeted. A full telemetry ring
returns `DROPPED`; it never blocks motion or a control callback.

### Faults

```python
class FaultManager:
    def raise_recoverable(self, fault: FaultRecord) -> None: ...
    def enter_safe_hold(self, fault: FaultRecord) -> Never: ...
    def raise_terminal(self, fault: FaultRecord) -> Never: ...
```

The kernel applies the resource-specific hold/shutdown policy. Modules cannot
catch and suppress a terminal kernel safety fault.

## Capability-scoped I/O profile

Most machine applications should not import `helix.io`. It exists for
portable device drivers and simple bounded peripherals not already covered by
a semantic common module.

Handles are configured and safety-limited before activation:

```python
class DigitalInput:
    def read(self) -> Observation[bool8]: ...
    async def wait_edge(
        self,
        edge: Edge,
        deadline: Deadline,
    ) -> Result[EdgeEvidence, InputError]: ...

class DigitalOutput:
    async def set(
        self,
        value: bool8,
        effective: MachineTime,
    ) -> Result[OutputEvidence, OutputError]: ...
    def status(self) -> OutputStatus: ...

class PwmOutput:
    async def set(
        self,
        duty: DutyCycle,
        effective: MachineTime,
    ) -> Result[OutputEvidence, OutputError]: ...
    def status(self) -> OutputStatus: ...

class CounterInput:
    def latest(self) -> CounterObservation: ...

class CaptureInput:
    async def next(
        self,
        deadline: Deadline,
    ) -> Result[CaptureEvidence, CaptureError]: ...
```

The module cannot change an output's physical pin, active polarity, safe
value, maximum duty, cycle rate, or late-command policy unless its deployment
capability explicitly grants a bounded configuration range.

### Bus devices

```python
class SpiDevice:
    async def transfer(
        self,
        tx: Bytes,
        receive: u16,
        deadline: Deadline,
    ) -> Result[Bytes, BusError]: ...

class I2cDevice:
    async def write_read(
        self,
        write: Bytes,
        receive: u16,
        deadline: Deadline,
    ) -> Result[Bytes, BusError]: ...

class SerialDevice:
    async def exchange(
        self,
        write: Bytes,
        receive: u16,
        deadline: Deadline,
    ) -> Result[Bytes, BusError]: ...
```

Bus, chip select/address, mode, maximum rate, transaction size, shutdown
message, DMA policy, and electrical ownership are properties of the handle.
They are not call arguments.

Transactions are unavailable from a hard-real-time control callback. A
driver needing phase-exact bus activity requires a separately defined
kernel-owned control domain.

### What is not exposed

The portable Python API does not expose:

```text
irq_disable / irq_enable
sched_add_timer / raw timer callbacks
alloc_chunk / move_alloc / OID allocation
raw command encoder/decoder
DMA channel allocation or descriptors
peripheral register reads/writes
NVIC, vector table, cache or MPU programming
pin-number constructors
unbounded filesystem or network sockets
bootloader entry or firmware flash
```

Those remain kernel, maintenance, or privileged target-extension functions.
The raw `board_syscalls` table is not a Python API.

## Hard-real-time control API

Hard-real-time code receives frames; it does not issue ordinary API calls in
its cycle:

```python
from helix.rt import control_cycle

@control_cycle(
    domain="bldc.current_loop",
    rate=Frequency.khz(20),
    deadline=Duration.us(20),
)
def foc_cycle(
    sample: MotorSample,
    state: FocState,
    parameters: FocParameters,
) -> MotorCommand:
    alpha, beta = clarke(sample.phase_a, sample.phase_b)
    d_axis, q_axis = park(alpha, beta, sample.electrical_angle)
    v_d = state.pid_d.update(parameters.target_d - d_axis)
    v_q = state.pid_q.update(parameters.target_q - q_axis)
    return svpwm(v_d, v_q, sample.electrical_angle)
```

The cycle callback may use:

* fixed-layout inputs, state, and double-buffered parameters;
* bounded local arithmetic;
* `helix.math` deterministic intrinsics; and
* compiler-proven bounded helper functions.

It may not call `Context`, await, allocate, send a message, access a bus,
acquire a resource, emit ordinary telemetry, or touch an I/O handle.

The kernel captures the input frame, invokes the native callback, validates
the output frame, commits it to the target binding, and records deadline/fault
evidence. Decimated telemetry is written to a preallocated control-domain
ring through fields declared in the returned frame.

## Deterministic math

`helix.math` defines target-independent semantics for:

```text
abs, min, max, clamp
saturating add/subtract/multiply
sqrt, reciprocal_sqrt
sin, cos, sin_cos, atan2
linear interpolation
fixed-point conversion and scaling
```

Each function has a declared numeric profile:

```text
exact integer/fixed-point
correctly rounded
maximum absolute/relative error
target intrinsic with conformance bound
```

The capability manifest records the selected implementation. A target may use
an FPU, fixed-point lowering, a table, or a qualified software helper, but it
must meet the same declared result contract.

Domain libraries such as Clarke/Park transforms, PID, filters, and SVPWM are
ordinary portable modules built from these primitives, not privileged API
calls.

## The small native import ABI

The Python surface above lowers primarily onto a compact import table:

```c
struct helix_module_api_v1 {
    struct helix_api_header header;

    helix_result (*clock_now)(helix_machine_time *out);
    helix_result (*observation_read)(
        helix_handle, helix_schema, void *out);
    helix_result (*operation_start)(
        const helix_operation_request *, helix_operation_id *out);
    helix_result (*operation_cancel)(helix_operation_id);
    helix_result (*resource_acquire)(
        const helix_handle *, uint16_t, helix_lease_id *out);
    helix_result (*resource_release)(helix_lease_id);
    helix_result (*checkpoint_commit)(
        helix_schema, const void *, uint16_t);
    helix_result (*journal_emit)(
        helix_schema, const void *, uint16_t);
    helix_result (*telemetry_emit)(
        helix_schema, const void *, uint16_t);
    void (*fault_raise)(
        helix_schema, const void *, uint16_t, uint8_t severity);
};
```

The exact C layout is frozen only with implementation, but the principle is
normative:

* rich Python methods are compile-time wrappers;
* schemas and operation codes carry domain detail;
* only used imports appear in a module's manifest;
* external references resolve through numeric versioned slots;
* local and remote operations use the same request shape; and
* hard-real-time cycle callbacks do not traverse this table.

### Source-to-firmware mapping

| Python call | Native primitive | Existing common owner |
| --- | --- | --- |
| `ctx.clock.now()` | `clock_now` | `timesync.c`, board timer conversion |
| `sensor.latest()` | `observation_read` | `adc_stream.c`, sensor/trigger cache |
| `sensor.wait(...)` | `operation_start(SENSOR_WAIT)` | acquisition/trigger engine |
| `ctx.resources.acquire(...)` | `resource_acquire` | new module resource manager |
| `device.method(...)` | `operation_start(PROTOCOL_METHOD)` | local handler or gateway/fabric |
| `motion.run(track)` | `operation_start(TRACK_RUN)` | `trajq.c` and actuator backend |
| `motion.hold(...)` | `operation_start(MOTION_HOLD)` | trajectory group/trsync/recovery |
| `heater.set_target(...)` | `operation_start(HEATER_TARGET)` | `heater_control.c` |
| `heater.enter_hold(...)` | `operation_start(HEATER_HOLD)` | `heater_hold.c` |
| `trigger.arm(...)` | `operation_start(TRIGGER_ARM)` | `trigger_source.c`, `trigger_analog.c`, `trsync.c` |
| `ctx.checkpoints.commit(...)` | `checkpoint_commit` | job journal/checkpoint layer |
| `ctx.journal.emit(...)` | `journal_emit` | `execlog.c`, persistent job journal |
| `ctx.telemetry.emit(...)` | `telemetry_emit` | Class-2 ring/transport |
| `ctx.faults.*(...)` | `fault_raise` | kernel safety/recovery coordinator |
| bound `helix.io` transaction | `operation_start(IO_*)` | GPIO/PWM/SPI/I²C/common drivers |

Several entries require new stable wrapper functions around current
command-oriented C modules. Native code must call the semantic engine
directly; it must not encode a protocol command and send it back into its own
console parser.

## Operation registry

Operation schemas are versioned independently of the module import-table ABI.
Initial families are:

```text
CORE        clock, deadline, lifecycle, cancellation
RESOURCE    lease acquire/release and ownership evidence
DEVICE      typed protocol method invocation
MOTION      track, barrier, hold, position, recovery
SENSOR      read, wait and subscription
ACQUISITION summary, capture and status
TRIGGER     arm, disarm, wait and evidence
HEATER      target, wait, profile, hold and status
OUTPUT      semantic fan and bounded GPIO/PWM
IO          scoped SPI/I2C/serial/counter/capture
STATE       checkpoint and reconciliation
EVIDENCE    journal, telemetry, status and fault
```

An operation registry assigns stable numeric ids and request/result schema
hashes. Adding an operation is additive. Changing its meaning requires a new
operation version, not silent reinterpretation.

## Execution profiles

| Profile | May await | Semantic API | Scoped I/O | Control frame | Raw board API |
| --- | --- | --- | --- | --- | --- |
| Pure library | No | No | No | No | No |
| Application actor callback | No | Start bounded operations | If declared | No | No |
| Machine program | Yes | Yes | If declared and not time-critical | No | No |
| Hard-real-time control cycle | No | No | No | Yes | No |
| Portable device driver | Yes outside callbacks | Limited | Yes | Optional dedicated domain | No |
| Privileged target extension | Target-specific contract | Target-specific | Target-specific | Possible | Separate nonportable SDK |

The compiler rejects a call unavailable in the current execution profile even
if another profile exposes a similarly named service.

## Host implementation

The Python package supplies real host implementations of the same interfaces:

* fixed numeric types emulate target overflow and rounding;
* records use the exact serialized layout;
* handles route to simulator, Klippy adapter, or remote printer fabric;
* operations are reactor/async adapter awaitables;
* machine time comes from the selected host simulation or MCU discipline
  model;
* journal and telemetry records use the same schemas; and
* control-domain functions can run against recorded sample frames.

Portable source never contains:

```python
if running_on_mcu:
    ...
else:
    ...
```

Environment-specific implementation lives behind the API. Tests may inject
fake handles and deterministic events through `helix.testing`, but production
portable code cannot import that package.

## LLVM lowering

The production compiler path is:

```text
portable Python AST
        |
        v
resolved typed/effect/state model
        |
        v
LLVM IR
        |
        v
target relocatable object
        |
        v
host-side HELIX module linker
        |
        v
.hmod
```

The typed compiler model is an in-memory frontend representation, not an
executed or transmitted language. LLVM IR is a temporary workstation build
artifact. Neither appears in the MCU runtime.

Method wrappers lower to fixed records and numeric import slots. Async
functions lower to explicit state/context and native resume entry points.
Pure helpers may inline. The compiler emits source maps connecting Python
locations, semantic state transitions, LLVM functions, and final instruction
ranges.

Generated C is not part of the production pipeline. A diagnostic C emitter
may exist later, but it cannot define language or API semantics.

## Versioning

Four versions remain separate:

1. **Python source API version** — packages, names, signatures, type behavior.
2. **Native module import ABI** — loader-visible function table and calling
   convention.
3. **Operation/schema versions** — semantic request, result, evidence, and
   replay behavior.
4. **Target/control-domain versions** — native ISA and hardware frame
   contracts.

Every `.hmod` declares:

```text
source API range
native import ABI range
used import slots
used operation/schema versions
required resource capabilities
target class
state/checkpoint schema
compiler and source hashes
```

An additive source helper may require no native ABI change. A new operation
usually adds a schema/operation version while continuing to use
`operation_start`. This prevents the import table from growing with every
device feature.

## Example: portable heater supervisor

```python
@config
class ChamberConfig:
    heater: Heater
    temperature: Sensor[Temperature]
    door: Sensor[bool8]
    profile: HeaterProfileRef

@module(name="chamber-supervisor", api="1.0")
class ChamberSupervisor:
    config: ChamberConfig
    state: ChamberState

    @on_message(SetChamberTarget)
    def set_target(self, ctx: Context, request: SetChamberTarget) -> None:
        ctx.events.start(self.apply_target(ctx, request.target))

    @machine_program(timeout=Duration.minutes(20))
    async def apply_target(
        self,
        ctx: Context,
        target: Temperature,
    ) -> Result[HeaterStatus, ChamberError]:
        door = await self.config.door.sample(
            maximum_age=Duration.ms(100),
            deadline=Deadline.after(Duration.seconds(1)))
        if door.is_err():
            return Err(ChamberError.DOOR_SENSOR)
        if door.unwrap().value:
            return Err(ChamberError.DOOR_OPEN)

        status = await self.config.heater.set_target(
            target,
            self.config.profile,
            deadline=Deadline.after(Duration.seconds(2)))
        if status.is_err():
            return Err(ChamberError.HEATER_REJECTED)

        return await self.config.heater.wait(
            TemperatureCondition.stable(
                target=target,
                band=Temperature.delta_celsius(1),
                duration=Duration.seconds(10)),
            deadline=Deadline.after(Duration.minutes(15)))
```

This source contains no ADC, PWM, polling loop, host heartbeat, target family,
or transport. The common heater, acquisition, observation, deadline, and
operation engines perform those responsibilities.

## Example: portable SPI sensor driver

```python
@config
class PressureDriverConfig:
    device: SpiDevice
    data_ready: TriggerSource

@module(name="pressure-sensor", api="1.0", profile="driver")
class PressureDriver:
    config: PressureDriverConfig
    state: PressureDriverState

    @machine_program(timeout=Duration.ms(20))
    async def read_sample(
        self,
        ctx: Context,
    ) -> Result[PressureSample, PressureError]:
        edge = await self.config.data_ready.arm(
            condition=TriggerCondition.rising(),
            stop_group=Option.none(),
            deadline=Deadline.after(Duration.ms(10)))
        if edge.is_err():
            return Err(PressureError.NO_DATA)

        raw = await self.config.device.transfer(
            tx=Bytes[2](0x12, 0x00),
            receive=u16(4),
            deadline=Deadline.after(Duration.ms(2)))
        if raw.is_err():
            return Err(PressureError.BUS)
        return Ok(decode_pressure(raw.unwrap()))
```

The same source can use an STM32, RP2040, ESP32, or Linux SPI backend whose
bound handle meets the declared transaction and trigger requirements.

## Implementation plan

### Phase 0 — freeze source semantics

- [ ] Implement `helix.types`, exact numeric behavior, units, fixed records,
  bounded containers, `Option`, and `Result` on the host.
- [ ] Freeze naming and signatures in this API as source API 1.0.
- [ ] Define annotation metadata without requiring a custom Python parser.
- [ ] Define callback serialization, cancellation, state ownership, and
  exception/fault rules.

### Phase 1 — operation and capability schemas

- [ ] Define stable schemas for handles, machine time, observations,
  operations, deadlines, leases, evidence, faults, and module lifecycle.
- [ ] Allocate the initial operation registry and versions.
- [ ] Generate capability requirements and least-authority import manifests
  from source use.
- [ ] Add printer-configuration binding from semantic roles to handles.

### Phase 2 — common-engine adapters

- [ ] Add direct stable wrappers around trajectory, acquisition, trigger,
  heater, output, execution-log, and gateway engines.
- [ ] Do not route local native calls through the console command parser.
- [ ] Implement local/remote operation dispatch with identical result and
  replay semantics.
- [ ] Add the resource manager and coherent observation cache required by the
  source API.

### Phase 3 — host and Klipper executor

- [ ] Implement Python handles and awaitables over the Klippy reactor.
- [ ] Implement deterministic simulation, event injection, and trace capture.
- [ ] Run OpenAMS and heater examples entirely through the semantic API.
- [ ] Establish golden traces before native compilation.

### Phase 4 — LLVM frontend and native ABI

- [ ] Resolve and type-check the portable Python subset.
- [ ] Lower records, callbacks, operations, and async continuations directly
  into LLVM IR.
- [ ] Emit target objects, source maps, import requirements, state schemas,
  and `.hmod` packages.
- [ ] Implement the compact native import table and loader adapters.
- [ ] Prove host and native semantic traces agree.

Implementation checkpoint, 2026-07-24:

The repository now contains the first deliberately narrow vertical compiler
slice. It supports fixed-width integer state and `@on_start` callbacks,
rejects imports and syntax outside its allowlist, emits LLVM IR directly,
builds Thumb objects for the initial STM32G0B1, RP2040, STM32F767, and
STM32H723 target descriptions, and packages self-contained callbacks in a
content-verified `.hmod`. Host arithmetic tests cover checked construction
and explicit wrapping operations. Compiler tests cover rejection behavior,
state layout, cross-target object generation, deterministic packaging, and
container corruption.

This checkpoint does not complete any Phase 4 line as written: the supported
source subset is not yet the proposed portable API, records and operations
are not lowered, async continuations and source maps are absent, and there is
no import table or firmware loader. The partial implementation is recorded
here so later work can distinguish running code from architectural intent
without weakening the acceptance gates.

### Phase 5 — cross-family gates

- [ ] Run one pure module, one event-driven actor, one acquisition consumer,
  one heater workflow, and one scoped bus driver on at least STM32, RP2040,
  ESP32, and the Linux-process target where the capability exists.
- [ ] Demonstrate that unavailable capabilities reject deployment instead of
  compiling family-specific branches.
- [ ] Verify local and remote placement produce equivalent semantic traces.
- [ ] Measure code size, stack, state, event latency, operation overhead, and
  transport-independent behavior.

### Phase 6 — hard-real-time gates

- [ ] Implement control-domain frames and `helix.math` conformance profiles.
- [ ] Compile one control algorithm to at least two materially different MCU
  families without changing its source.
- [ ] Prove deadline, aggregate utilization, invalid-output, stale-sample,
  hardware-break, and module-fault behavior.

## Acceptance gates

The API is qualified only when:

1. portable source imports no target-family, Klippy, OS, or board-register
   package;
2. every external effect is represented by a declared handle, operation,
   observation, control frame, journal event, or fault;
3. the same source executes under the host implementation and as native code;
4. host and native numeric, state-transition, operation, deadline, evidence,
   and terminal traces agree;
5. the same application source runs on at least STM32, RP2040, ESP32, and
   Linux-process targets when their capability graph satisfies it;
6. local and remote resource placement does not change application semantics;
7. no ordinary Python API can disable interrupts, allocate DMA, access a raw
   pin/address, widen safety limits, or clear a protected motion halt;
8. common trajectory, acquisition, trigger, heater, and logging engines remain
   the sole owners of their timing/safety mechanisms;
9. every wait is cancellable and deadline-bounded or explicitly enters a safe
   indefinite hold;
10. stale session/operation/resource generations cannot mutate current state;
11. telemetry backpressure cannot delay scheduled or prompt execution;
12. missing capabilities fail before module activation with an exact
    requirement explanation;
13. an application update preserves, migrates, reconciles, or explicitly
    invalidates its state schema;
14. a hard-real-time callback performs no general API call and remains within
    its admitted aggregate budget; and
15. OpenAMS and a portable BLDC controller validate the workflow and
    hard-real-time extremes of the same API family.

## Rejected alternatives

**Expose `board_syscalls` directly as Python methods**
: It permits application code to control IRQs, schedulers, pins, and
peripherals while bypassing common semantic engines and authority boundaries.

**Create one Python package per MCU family**
: Moves target conditionals into application code and defeats source
portability. Capabilities and target bindings belong below the API.

**Make every firmware command a Python method**
: Preserves the historical wire shape rather than the semantic architecture,
  leaks OIDs and configuration sequencing, and makes local calls traverse a
  protocol parser unnecessarily.

**Let applications poll sensors or busy-wait**
: Recreates the timing and CPU failures the DMA acquisition and hardware
trigger architecture already solved.

**Give every Python method a permanent native import slot**
: Bloats and freezes the kernel ABI. Typed operation schemas allow the rich
source API to evolve above a small import table.

**Use lambdas as serialized sensor predicates**
: General closures hide state and code. A finite set of typed condition
records is inspectable, versionable, and executable on any backend.

**Allow ordinary callbacks to block**
: Turns one module into a scheduler hazard. Variable-duration work belongs in
an explicit resumable machine program.

**Let hard-real-time code call the ordinary API**
: Creates unbounded dispatch, serialization, locking, and transport paths
inside a control deadline. Control cycles exchange fixed frames only.

## Defining principle

The portable Python API is not a prettier spelling for registers or Klipper
commands. It is the language-level view of the stable machine capabilities
HELIX already implements in common firmware.

> Compose common engines. Bind capabilities. Compile natively. Keep target
> details and safety authority below the application.
