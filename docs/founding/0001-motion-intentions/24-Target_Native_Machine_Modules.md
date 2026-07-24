# FD-0001: Target-Native Machine Modules

Status: architecture and implementation plan. The board syscall ABI exists,
but the native-module compiler, container, loader, isolation boundary, storage
lifecycle, and hard-real-time control domains described here do not. No
runtime-loaded module or portable BLDC claim is implemented or qualified.

This document extends the
[actuator-backend contract](04-Actuator_Backends.md),
[board syscall ABI](13-Syscall_API.md),
[DMA acquisition architecture](17-DMA_ADC_Acquisition.md),
[autonomous job capsule](21-Autonomous_Job_Execution.md), and
[portable machine-program source model](23-Portable_Machine_Programs.md).

## Thesis

HELIX virtualizes the **machine**, not the processor instruction set.

A machine behavior is authored once in a restricted, typed Python source
language. A workstation compiles it ahead of time for the printer's actual
target class. The resulting payload contains native target instructions, is
streamed to the printer, stored as an immutable object on mainboard-owned
nonvolatile storage, loaded into executable memory, and invoked through a
stable HELIX module API.

Changing a machine application must not require rebuilding or reflashing the
firmware:

```text
restricted Python source
          |
          v
  typed/effect-checked IR
          |
          v
 generated C or LLVM input       host reference implementation
          |                                 |
          v                                 v
 target cross-compiler              simulator / live Klipper
          |
          v
 native target instructions
          |
          v
 signed, content-addressed .hmod
          |
      authenticated transfer
          |
          v
 mainboard local store -> executable SRAM -> native call
```

There is no CPython, MicroPython, Java-style bytecode machine, WebAssembly
runtime, or general-purpose instruction interpreter in the MCU execution
path. The state graph produced during compilation remains valuable for
validation, visualization, checkpoints, and trace comparison; it is not the
instruction set the MCU interprets.

This creates a third model between live-host Klipper and traditional
monolithic firmware:

> Compile machine behavior for its target, deploy it like an application, and
> execute it autonomously at native speed without reflashing the firmware.

## Central decisions

1. **Compilation occurs on the host.** The MCU never carries a compiler or
   performs a JIT compilation.
2. **The deployed code is target-native.** An Arm target receives Arm
   instructions; an Xtensa or RISC-V target receives its own instructions.
3. **The firmware is a stable real-time kernel.** It owns boot, hardware
   initialization, transport, machine time, storage, resource admission,
   safety, interrupts, logs, and the module loader.
4. **Machine behavior is deployable.** OpenAMS workflows, printer-specific
   recovery, control algorithms, and other applications may be replaced
   atomically without a firmware flash.
5. **Portable modules do not use peripheral addresses.** They call a
   versioned, capability-scoped HELIX API and receive typed resource handles.
6. **Storage is not an execution-time pager.** A complete module or bounded
   overlay is read, verified, relocated, and placed in executable memory
   before it may run.
7. **Control flow is native, while trajectories remain data.** Precomputed
   motion chapters, coefficients, configuration, and evidence records remain
   immutable capsule objects consumed by native code and existing firmware
   executors.
8. **There are distinct execution profiles.** Event-driven machine
   applications and high-rate hard-real-time control kernels have different
   APIs, admission proofs, privileges, and failure policies.
9. **Capability compatibility is explicit.** Source portability never implies
   that every MCU has the PWM, ADC, timing, memory, or compute resources
   required by every algorithm.
10. **Native speed does not weaken physical safety.** Hardware break inputs,
    heater cutoffs, watchdogs, emergency stop, and kernel-owned terminal
    policies remain outside uploaded application authority.

## Product model

The resulting printer contains three independently versioned layers:

```text
+-------------------------------------------------------------+
| Job capsule                                                 |
| motion chapters, material plan, application version pins,   |
| configuration/calibration hashes, recovery/checkpoint data  |
+-------------------------------------------------------------+
| Native machine applications                                |
| OpenAMS, tool change, printer policy, control algorithms,   |
| calibration and bounded recovery                            |
+-------------------------------------------------------------+
| Stable HELIX kernel and target drivers                      |
| time, motion executor, storage, fabric, safety, loader, HAL |
+-------------------------------------------------------------+
| MCU, timers, ADC, DMA, PWM, buses, storage, power hardware  |
+-------------------------------------------------------------+
```

The firmware changes when the kernel, target port, safety substrate, or stable
ABI changes. A machine application changes when printer behavior changes. A
job capsule changes for each print. Treating these as one flash image would
discard the main benefit of the architecture.

## Terminology

**HELIX native module (`.hmod`)**
: A bounded, target-specific, content-addressed container holding native code,
  data, restricted relocations, imports, exports, budgets, compatibility
  requirements, and integrity metadata.

**Machine application**
: A persistent deployable behavior such as OpenAMS, a tool changer, a
  calibration procedure, or printer recovery policy.

**Job module**
: A module bundled or pinned by one job capsule because the job requires an
  exact behavior version.

**Hard-real-time control module**
: A native module admitted into a fixed-rate or hardware-event control domain
  under stricter memory, call, and worst-case execution-time rules.

**HELIX kernel**
: The flashed, trusted firmware that owns hardware, safety, loading,
  scheduling, communication, and resource authority.

**Target class**
: The complete native-code compatibility contract: ISA, instruction features,
  ABI, endianness, floating-point convention, relocation set, executable
  memory properties, and HELIX module ABI.

**Capability graph**
: The running printer's typed inventory of resources, rates, limits,
  identities, topology, safety constraints, and ABI generations.

**Control domain**
: A kernel-owned hardware execution environment such as synchronized
  ADC/PWM motor control. It supplies fixed input frames and accepts bounded
  output frames from a module.

## Authoring language and compilation

### One source, several products

Portable source remains ordinary Python so it can be imported by tests and
the live Klipper compatibility executor. Decorators declare compilation and
execution intent. The exact spelling is an implementation decision; the
following illustrates the semantic distinction:

```python
@machine_program(resources=("toolhead", "material.primary"))
async def change_material(machine, requested: MaterialId) -> ChangeResult:
    if await machine.material.current() == requested:
        return ChangeResult.ALREADY_LOADED

    async with machine.resources():
        await machine.track("park_and_retract")
        await machine.call("cutter.cut_and_verify")
        await machine.call("material.unload")
        await machine.call("material.load", material=requested)
        await machine.track("prime_clean_and_restore")
    return ChangeResult.COMPLETE
```

For live Klipper, the compatibility executor drives these awaitables through
the reactor and existing MCU drivers. For autonomous HELIX, the compiler
lowers the coroutine into a native resumable function with an explicit,
fixed-size context:

```c
enum helix_status change_material_resume(
    struct change_material_context *context,
    const struct helix_event *event,
    const struct helix_machine_api_v1 *api);
```

`await` becomes a state transition and return to the kernel. Resumption calls
the native entry point again with the matching completion, observation, or
deadline event. No Python frame or interpreter exists on the target.

### Compiler pipeline

The proposed `helixc` pipeline is:

1. import or parse the selected portable source without executing host effects;
2. resolve annotations, explicit types, topology inputs, and operation ABIs;
3. reject unsupported Python constructs and hidden dependencies;
4. build typed control-flow, effect, resource, and state-lifetime IR;
5. prove finite continuations, bounded retries, declared waits, and context
   size;
6. emit a statechart for inspection and trace mapping;
7. lower the executable paths to C or a compiler IR;
8. invoke the target's ordinary cross-compiler and linker;
9. reduce the relocatable result to the allowlisted module sections and
   relocations;
10. attach capability requirements, budgets, provenance, and state schema;
11. produce host reference code and conformance vectors from the same IR; and
12. hash, optionally sign, and package the `.hmod`.

C is a practical initial lowering because every supported firmware target
already has a qualified C toolchain. It is an implementation detail, not a
portable application ABI. The stable contracts are the source semantics,
typed IR, module container, and HELIX import surfaces.

### Language restrictions

Portable source may:

* call another compiled portable function;
* branch on typed Boolean, enum, and bounded numeric values;
* operate on fixed-size value types and records;
* use approved pure mathematical helpers;
* iterate over compile-time-bounded collections;
* await declared machine operations; and
* raise declared machine faults.

It may not use:

* Python objects with dynamic layout;
* reflection, dynamic import, monkey patching, or `eval`;
* arbitrary-precision integer behavior;
* recursion or unbounded loops;
* dynamic allocation after initialization;
* files, sockets, subprocesses, environment variables, or wall-clock time;
* direct Klippy, reactor, pin, MCU, or configuration objects;
* undeclared global mutable state; or
* a target address, peripheral register, interrupt mask, or vector table.

### Numeric semantics

Native equivalence requires stronger numeric rules than normal Python:

```text
u8, u16, u32, u64
i8, i16, i32, i64
q15, q31 and declared fixed-point formats
f32
f64 only when the target and module contract allow it
ticks, duration, frequency, angle, current, voltage, temperature
```

Overflow, saturation, shifts, rounding, fused operations, NaN handling, and
floating-point contraction are defined by the source type and compiler
profile. The host reference implementation must reproduce those semantics.
Python's unbounded integer behavior is never the oracle for an embedded
integer operation.

## Target classes and capability binding

A native binary is admitted only when all of these agree:

```text
instruction architecture and minimum feature set
procedure-call ABI and stack alignment
integer and floating-point calling convention
endianness
allowlisted relocation model
executable-memory and cache rules
HELIX module ABI major/minor range
required semantic operations
required control domains and performance limits
configuration and calibration generations
```

A target class need not name one board SKU. If two boards implement the same
HELIX ABI and expose compatible semantic resources, the same binary may run
on both. Conversely, two MCUs sharing an instruction set are not compatible
when their floating-point ABI, executable-memory policy, module ABI, or
required capabilities differ.

The compiler may create a fat application package containing several `.hmod`
variants, but the printer selects and verifies exactly one variant per
execution node. The preferred deployment flow queries the real printer first
and compiles only the necessary variants.

## The `.hmod` container

The wire and storage format is deliberately smaller and more constrained than
a general ELF dynamic object:

```text
header
    magic and container version
    module identity and semantic version
    target-class hash
    source and compiler identity
requirements
    HELIX ABI range
    operation and control-domain imports
    resource/capability predicates
    configuration/calibration bindings
sections
    executable text
    read-only data
    initialized writable data
    zero-initialized data size
    explicit persistent context schema
linkage
    numeric import slots
    exported lifecycle entry points
    allowlisted relocation records
budgets
    code/data/context/stack maxima
    invocation and execution-cycle limits
    allowed rates and priorities
    telemetry limits
integrity
    per-section hashes
    content root
    signer and signature policy
```

Development tooling may consume ordinary relocatable ELF internally. The
printer never accepts arbitrary ELF symbols, constructors, dynamic libraries,
debuggers, or unrestricted relocation types. Conversion to `.hmod` rejects
everything the loader does not need.

Imports are numeric, versioned slots rather than firmware symbol addresses.
Modules never assume a function's linked address:

```c
struct helix_machine_api_v1 {
    struct helix_api_header header;
    helix_result (*resource_acquire)(...);
    helix_result (*resource_release)(...);
    helix_result (*operation_start)(...);
    helix_result (*observation_read)(...);
    helix_result (*deadline_arm)(...);
    helix_result (*checkpoint_commit)(...);
    helix_result (*track_start)(...);
    helix_result (*fault_raise)(...);
};
```

Additive slots extend a minor ABI. Incompatible semantics require a new major
ABI or operation version. A module cannot be armed merely because its machine
instructions happen to decode on the CPU.

## Layered module APIs

One raw syscall table is not an adequate authority boundary. HELIX exposes
three layers.

### Semantic machine-application API

Ordinary applications receive typed handles for resources already configured
and qualified by the kernel:

```text
resource leases and generations
typed node operations
sensor observations with time and quality
trajectory-track submission and barriers
heater/fan/material semantic commands
deadlines, cancellation, and safe hold
checkpoints, journal events, and terminal results
```

They cannot configure a timer register, claim arbitrary DMA, disable an IRQ,
or write an unowned GPIO.

### Hard-real-time control-domain API

A hard-real-time module receives fixed-layout input frames and returns
fixed-layout output frames. It does not call the general machine API from its
cycle callback. Initialization, parameter updates, telemetry draining, and
fault reporting use separate non-cycle entry points.

### Privileged target-driver API

The existing [board syscall ABI](13-Syscall_API.md) remains the substrate for
firmware and separately authorized system extensions. It is too powerful for
ordinary job content. A dynamically deployed target driver, if supported at
all, requires a system-publisher signature and a maintenance activation
boundary equivalent to a kernel update.

This separation lets one portable BLDC algorithm use a safe motor-control
domain without exposing `irq_disable()` or target timer registers to it.

## Module lifecycle

### Stage

The host pushes a module or job capsule through an authenticated transport.
The mainboard writes it under a temporary content identity. Interrupted
transfers resume by hash; incomplete content is never executable.

### Verify

Before allocation, the kernel verifies:

* the container and every section hash;
* signature policy and publisher authority;
* target-class and ABI compatibility;
* capability and resource requirements;
* code, data, stack, context, and cycle budgets;
* relocation and import allowlists;
* state/checkpoint schema compatibility; and
* coexistence with the currently admitted real-time load.

### Load

The loader allocates fixed-lifetime regions, copies all active code and data
from storage, applies restricted relocations, constructs the import table, and
performs target-specific instruction/data cache maintenance. Writable
relocation is complete before executable permission is granted where the
target supports such enforcement.

An SD card is never demand-paged. A target with insufficient RAM must reject
the module or use compiler-defined overlays loaded only at explicit workflow
barriers. The next overlay is fully resident before the current one releases
execution authority.

### Initialize

Initialization runs with actuators inert. It validates parameters, binds
semantic resource handles, constructs fixed context, and performs pure
self-tests. It cannot silently energize an output.

### Activate

Activation is atomic and occurs only while idle or at an explicit safe
workflow barrier. The old version remains available for rollback until the
new version completes its activation gate. A module controlling a running
heater, motor, or distributed workflow is never hot-swapped in place.

### Execute

The kernel records module identity, generation, entry point, event, start/end
ticks, budget status, state transition, emitted operation, and fault outcome
at the appropriate decimated or transition boundary.

### Deactivate and rollback

Deactivation cancels waits, revokes resource leases, applies the declared safe
state, and releases memory only after no callback can still reference it. A
faulting candidate may be quarantined and the prior known-good version
reactivated after the machine is physically requalified.

## Memory and isolation

Native modules are more dangerous than declarative bytecode. The architecture
does not disguise that fact.

On capable targets, the loader should enforce:

* read-only executable text;
* non-executable writable state;
* a private stack with guard region;
* private context/data regions;
* read-only imports and immutable constants;
* no module access to kernel, peripheral, DMA-descriptor, or another module's
  memory; and
* unprivileged thread-mode execution for ordinary machine applications.

The exact MPU/cache layout is target-specific. `W^X` and MPU isolation are
capabilities, not assumptions.

Some supported MCUs cannot provide useful fault isolation for arbitrary
native code. On those targets, a module is fully trusted code. Policy may:

* permit only firmware-publisher-signed modules;
* permit only compiler-produced modules whose content hash is allowlisted;
* restrict deployment to fixed leaf primitives;
* or disable runtime native modules entirely.

Static validation cannot make an arbitrary native blob safe. Production
loaders therefore accept the constrained `.hmod` format and authorized
compiler provenance, not arbitrary user-supplied machine code.

## Failure containment

The kernel owns failure behavior:

* a module budget overrun is a prompt fault and revokes further callbacks;
* a MemManage, BusFault, UsageFault, or HardFault records the active module,
  entry point, generation, and machine time;
* the affected resources enter their kernel-defined safe hold or shutdown;
* coordinated motion receives the appropriate group stop/hold event;
* electrical protection remains active even if module code never returns;
* the faulting module is quarantined across restart until explicitly cleared
  or replaced; and
* Atlas receives the module provenance and last journaled state.

On Cortex-M, handler mode is privileged. An uploaded callback invoked directly
from an ISR cannot honestly be described as MPU-isolated in the same way as an
unprivileged thread. HELIX therefore distinguishes isolated application
modules from trusted hard-ISR modules and never treats a source annotation as
a security boundary.

## State, checkpoints, and restart

Portable asynchronous code never persists a native stack image. The compiler
creates an explicit context record:

```text
program counter / logical state
typed local values live across a suspension
operation ids and generations
retry and deadline state
resource leases
application-specific durable fields
context schema version
```

Only declared checkpoint fields enter the durable journal. After reset, the
kernel reloads the exact module hash and reconstructs its context from the
matching schema. Physical state is then reconciled from sensors and device
generations before execution continues. A valid checksum is not permission to
assume position, filament location, or thermal state survived.

Changing a module with live persisted state requires one of:

* an exact context-schema match;
* an explicit, separately verified migration function executed while inert;
* forward reconciliation into a declared state;
* or invalidation of the old checkpoint and operator requalification.

## Hard-real-time control modules

### Why a second profile is necessary

An event-driven tool-change program may yield for seconds. A current-control
loop may have only microseconds between an ADC sample and the next PWM update.
Giving both the same scheduler contract would either make workflows needlessly
hostile or make motor control unqualified.

A hard-real-time module declares:

```text
control-domain type and instance
trigger source and phase
nominal and maximum invocation rate
deadline from sample to committed output
maximum cycles and stack
fixed state and parameter size
allowed pure intrinsics
telemetry decimation
safe output on fault or missed deadline
```

The compiler rejects dynamic allocation, waits, general syscalls, recursion,
unbounded iteration, and data-dependent call graphs from the cycle callback.

### Invocation models

HELIX may qualify two target-dependent modes:

1. **Isolated high-priority thread:** the ISR publishes a control frame and
   wakes an unprivileged, highest-priority native callback. This provides a
   stronger memory boundary but consumes measurable dispatch latency.
2. **Trusted IRQ-tail callback:** a minimal kernel ISR captures the hardware
   state and calls an authorized bounded native function before committing its
   output. This minimizes latency but runs in privileged handler context and
   therefore requires stronger provenance and static evidence.

The selected mode is part of the control-domain capability and qualification
record. It is never chosen silently by a module.

### Parameter and telemetry planes

Parameters are double-buffered. A non-cycle API validates a new parameter
block and publishes it atomically at a control-cycle boundary. A high-rate
callback never parses a message or observes a partially updated PID gain,
motor constant, or limit.

Telemetry is written to a fixed local ring and drained at a declared decimated
rate. A full telemetry ring drops telemetry and increments a counter; it does
not delay the control cycle.

## Portable BLDC/FOC control

BLDC control is the reference case that justifies the hard-real-time module
profile.

### Algorithm/hardware split

The target binding owns:

* complementary or independent phase PWM;
* center/edge alignment, dead time, and polarity;
* gate-driver enable and hardware break/fault inputs;
* ADC trigger placement relative to PWM edges;
* one-, two-, or three-shunt acquisition and reconstruction;
* DMA/FIFO ownership and sample timestamping;
* encoder, Hall, resolver, or observer input acquisition;
* bus-voltage and temperature safety inputs; and
* atomic application of the next phase command.

The portable algorithm owns:

* Clarke and Park transforms;
* current-loop policy;
* velocity and position loops where selected;
* feed-forward;
* observers and sensorless estimation;
* field weakening;
* modulation selection;
* anti-windup and saturation policy; and
* algorithm telemetry and typed diagnostic state.

This is the same architectural split as trajectory core versus actuator
backend: target code realizes hardware timing, while portable code decides
control behavior.

### Control-frame API

An illustrative current-loop contract is:

```python
@helix_realtime(
    domain="bldc.current_loop",
    rate_hz=20_000,
    deadline_us=20,
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

    phase_a, phase_b, phase_c = inverse_svpwm(
        v_d, v_q, sample.electrical_angle)
    return MotorCommand(phase_a, phase_b, phase_c)
```

The ABI uses fixed-layout records:

```text
MotorSample
    phase currents and reconstruction-valid mask
    DC bus voltage
    mechanical/electrical position and quality
    local and machine timestamp
    gate-driver and hardware fault bits

MotorCommand
    normalized phase commands or timer-independent duty representation
    requested enable state
    algorithm fault/status bits
    decimated diagnostic values
```

The module returns a command; it does not write compare registers. The kernel
validates finite values, range, slew/voltage limits, current safety state, and
domain generation before committing the command.

### Capability contract

A BLDC module may require:

```text
phase_pwm_channels >= 3
complementary_pwm = true
center_aligned_pwm = true
programmable_deadtime = true
hardware_break_input = true
synchronized_adc_channels >= 2
adc_trigger_from_pwm = true
current_topology in {two_shunt, three_shunt}
position_source in {encoder, hall, sensorless}
control_rate >= 20 kHz
sample_to_update_deadline <= 20 us
numeric_profile in {f32, q31}
```

These are examples, not universal BLDC requirements. The compiler and
capability matcher bind an algorithm variant to the actual topology.

"Write once" means one source and semantic contract can produce several
native targets. It does not mean a low-end MCU without synchronized ADC/PWM,
sufficient cycles, or safe gate-driver hardware is magically qualified.

### Multiple-rate loops

The inner current loop, estimator, velocity loop, position loop, and trajectory
sampler need not all run at one frequency. A motor-control domain declares
integer-related rates and fixed handoff points:

```text
PWM/current loop       20-40 kHz
observer/estimator     target-dependent
velocity loop          1-5 kHz
position loop          100 Hz-2 kHz
HELIX trajectory       sampled into the declared outer-loop rate
telemetry              decimated, typically 10-200 Hz
```

The actual admitted values come from measured target capacity. Total
utilization includes every active motor, communication ISR, DMA completion,
trajectory backend, heater controller, and safety monitor.

### Safety invariants

No BLDC module may:

* disable the timer break path or gate-driver fault;
* widen board current, voltage, temperature, speed, or duty ceilings;
* continue after stale/invalid current samples;
* reconfigure PWM/ADC timing while armed;
* claim an unqualified rotor-position source;
* suppress kernel deadline or tracking faults; or
* authorize torque merely because a host remains connected.

On deadline miss or module fault, hardware output follows the domain's
prequalified safe action: immediate break, zero-voltage command, controlled
torque ramp, or servo hold according to the actuator and fault class.

## Compute and timing admission

Each hard-real-time target build produces both static and measured evidence:

```text
code and constant bytes
context and stack high-water
worst observed cycles by entry point
compiler-derived loop and call bounds
control-domain invocation rate
interrupt/preemption assumptions
cache placement and cold/warm behavior
numeric profile
telemetry and memory bandwidth
```

The kernel reserves a utilization budget rather than merely checking that
each module works alone. Admission must include a fixed safety margin and
reject an axis/controller count that exceeds the MCU's qualified aggregate
capacity.

A module cannot raise its control rate, add axes, or select a more expensive
algorithm after arming without a new admission transaction.

## Distributed deployment

The mainboard owns the authoritative application package even when native
code runs on downstream controllers.

```text
host compiler
     |
     v
mainboard immutable store
     |
     +-- local H7/F7 module -> executable SRAM
     |
     +-- CAN/USB/Ethernet transfer -> leaf staging RAM/storage
                                      |
                                      v
                               verified leaf module
```

Every participant reports:

```text
node identity and boot/session generation
target class and module ABI
module content hash and activation generation
load address class and memory use
capability bindings
admitted execution budget
ready/active/fault/quarantined state
```

A distributed application activates only after every essential node has
staged and acknowledged its exact module generation. Activation uses the same
machine-time and coordinated-horizon principles as motion. Partial activation
cannot leave one MCU speaking a new operation ABI while another executes the
old workflow.

Small leaf MCUs may retain fixed firmware operations instead of accepting
native modules. The mainboard's native application can coordinate those
operations through the same semantic API.

## Relationship to autonomous job capsules

Installed applications and print jobs are distinct but reproducibly linked.
A capsule may:

* require an already installed module by content hash;
* bundle the exact module object;
* carry target variants for several nodes;
* declare an allowed compatible semantic-version range; or
* prohibit substitution and require byte-exact behavior.

The capsule manifest adds:

```text
module table and content roots
target-node assignments
HELIX/module/operation ABI requirements
entry points used by each workflow barrier
state/checkpoint schema hashes
execution and memory budgets
signature/publisher requirements
rollback and terminal policies
```

Motion chapters remain immutable track data. Native programs decide when
preconditions are satisfied, start a chapter through the semantic API, wait
for its execution evidence, and progress to the next workflow barrier.

## Host compatibility and semantic equivalence

The same portable source has two observable executors:

1. the live Klipper compatibility executor; and
2. the target-native HELIX module.

They must produce equivalent semantic traces for the same typed inputs:

```text
state transitions
resource acquisition/release
operation ids and arguments
deadlines
observations accepted or rejected
checkpoints
fault/recovery choices
terminal result
```

They need not have identical internal call stacks or timing. Equivalence is
defined at the semantic operation and evidence boundaries.

The compiler emits conformance vectors consumed by:

* the pure host simulator;
* the Klipper compatibility executor;
* a native module runner on the workstation where possible;
* target firmware in loopback; and
* physical hardware qualification.

Shadow mode may run the host reference without authority while the MCU module
controls the machine, comparing decisions and recording divergence without
duplicating effects.

## Management and observability

The eventual management surface should include:

```text
HELIX_MODULE_LIST
HELIX_MODULE_INSPECT MODULE=<hash-or-name>
HELIX_MODULE_STAGE SOURCE=<object>
HELIX_MODULE_VERIFY MODULE=<hash>
HELIX_MODULE_ACTIVATE MODULE=<hash>
HELIX_MODULE_ROLLBACK NAME=<application>
HELIX_MODULE_REMOVE MODULE=<hash>
HELIX_MODULE_STATUS
HELIX_CONTROL_DOMAIN_STATUS
```

Status includes:

* active and retained module hashes, semantic versions, and publishers;
* source/compiler/target/ABI identities;
* bound resources and control domains;
* code, data, context, stack, and storage use;
* invocation counts, worst cycles, deadline misses, and overruns;
* activation/rollback generation;
* last checkpoint and journal position;
* last fault, hardware exception, and quarantine state; and
* whether the current job can complete without a host or remote repository.

Atlas records lifecycle changes, verification rejection, activation,
rollback, budget violation, native fault, semantic divergence, and terminal
result. High-rate healthy callbacks remain aggregated telemetry.

## Security and trust policy

Authenticating the transport does not authorize arbitrary native code.
Production policy distinguishes:

* **firmware publisher:** may install kernel and privileged target extensions;
* **machine owner:** may install ordinary machine applications within the
  kernel safety envelope;
* **job publisher:** may supply job modules only when allowed by printer
  policy; and
* **observer:** may inspect module identity and status but cannot mutate it.

A farm may pin publisher keys and module roots per printer group. A personal
printer may allow an explicit local developer mode, but that mode is visible,
latched in the execution record, and cannot silently broaden thermal or
electrical safety limits.

Signed native code is still fallible code. Signatures establish provenance
and authorization, not correctness. Bounds, capability admission, hardware
safety, fault handling, and physical qualification remain mandatory.

## Implementation plan

### Phase 0 — freeze contracts

- [ ] Name and version the kernel, machine-operation, control-domain, target,
  context, and module-container ABIs.
- [ ] Separate the raw board syscall table from capability-scoped application
  imports.
- [ ] Define explicit numeric, overflow, floating-point, and async-resumption
  semantics.
- [ ] Define publisher roles, signing policy, developer mode, and revocation.

### Phase 1 — container and workstation toolchain

- [ ] Implement typed portable-source validation and a minimal C lowering.
- [ ] Produce deterministic target-native objects for at least two target
  classes.
- [ ] Implement ELF-to-`.hmod` reduction with restricted sections, imports,
  exports, and relocations.
- [ ] Build human-readable inspect/diff/disassemble tools.
- [ ] Emit source, compiler, ABI, capability, budget, and state-schema hashes.
- [ ] Prove reproducible builds for a pinned toolchain.

### Phase 2 — mainboard loader and storage lifecycle

- [ ] Implement temporary upload, content verification, immutable publication,
  and rollback retention.
- [ ] Add fixed executable/data/context allocators and target cache handling.
- [ ] Apply W^X, stack guards, and unprivileged application execution where
  supported.
- [ ] Implement initialize/activate/deactivate/quarantine state machines.
- [ ] Prove no code fetch or page fault depends on SD after activation.
- [ ] Inject corruption, incompatible targets, invalid relocation, removal,
  reset, and activation power loss.

### Phase 3 — native portable workflows

- [ ] Compile `@machine_program` async functions into native resumable entry
  points and explicit context.
- [ ] Bind the semantic operation API, resource leases, deadlines,
  checkpoints, and forward recovery.
- [ ] Prove simulator, live Klipper, and native target traces agree for the
  OpenAMS golden corpus.
- [ ] Run a hostless native OpenAMS tool change between stored motion
  chapters.

### Phase 4 — hard-real-time control domains

- [ ] Define fixed-frame callback, parameter, telemetry, and fault ABIs.
- [ ] Implement a synthetic ADC/PWM loopback domain using the DMA acquisition
  substrate.
- [ ] Measure isolated-thread and trusted-IRQ-tail dispatch on representative
  targets.
- [ ] Enforce aggregate cycle, stack, memory, and rate admission.
- [ ] Inject overrun, invalid numeric output, stale sample, module fault, and
  watchdog events.

### Phase 5 — portable BLDC reference module

- [ ] Define target bindings for synchronized PWM, ADC/current topology,
  rotor position, gate fault, and hardware break.
- [ ] Implement one typed FOC source with explicit numeric variants.
- [ ] Compile and run it on at least two materially different qualified MCU
  targets without changing the algorithm source.
- [ ] Compare host model, native loopback, current/position traces, cycle
  budgets, and fault behavior.
- [ ] Prove a hardware break remains effective during a hung or faulted
  module.
- [ ] Qualify trajectory consumption, coordinated stop, hold, telemetry, and
  host-loss behavior.

### Phase 6 — distributed modules and product lifecycle

- [ ] Stage and atomically activate one application across a mainboard and
  downstream node.
- [ ] Bind exact module roots into autonomous capsules.
- [ ] Add fleet repository, group policy, rollback, provenance, and Atlas
  support.
- [ ] Complete long autonomous jobs through module update, host loss,
  reconnect, and result collection scenarios.

## Acceptance gates

The architecture is complete only when:

1. a machine application changes on a physical printer without rebuilding,
   reflashing, or resetting the HELIX firmware;
2. inspection proves the active payload contains and executes native target
   instructions rather than interpreted portable bytecode;
3. incompatible target, ABI, capability, configuration, or state-schema
   modules are rejected before activation;
4. incomplete, corrupt, or unauthorized modules can never become executable;
5. activation and rollback are atomic across power interruption;
6. the real-time path performs no SD read after a module is activated;
7. an ordinary module fault cannot write unowned hardware or escape its
   qualified safe-state policy on a target claiming isolation;
8. every admitted hard-real-time module remains inside measured aggregate
   cycle, stack, memory, jitter, and deadline budgets;
9. live Klipper, simulator, and native module semantic traces agree for the
   portable workflow corpus;
10. one BLDC/FOC source runs as target-native code on at least two different
    qualified MCU families through the same control-domain API;
11. hardware electrical protection stops the actuator even when the BLDC
    module faults or never returns;
12. a mainboard and downstream node stage, activate, execute, checkpoint, and
    report one module generation coherently; and
13. a hostless capsule completes with its pinned native applications and
    later supplies complete provenance and execution evidence to Atlas.

## Rejected alternatives

**A universal Java/Wasm-style instruction VM**
: Provides compile-once binary portability by putting an interpreter or JIT
  in the real-time path. HELIX instead recompiles source per target and runs
  native code.

**Compile each application into the firmware image**
: Preserves native speed but couples behavior deployment to a bootloader,
  reset, firmware rollback, and whole-image qualification.

**Execute directly from the SD card**
: Ordinary SD is not memory-mapped executable storage and has unbounded
  latency relative to control deadlines. Modules must be resident before use.

**Accept arbitrary ELF shared objects**
: Exposes an excessive loader and symbol surface and cannot enforce HELIX
  relocation, import, budget, provenance, or lifecycle rules.

**Allow portable modules to access peripheral registers**
: Makes source board-specific and lets application code bypass ownership,
  timing, and safety invariants.

**Treat the board syscall table as the ordinary application API**
: The table includes primitives too powerful and too low-level for
  capability-scoped machine behavior. It remains a kernel/system substrate.

**Allow arbitrary uploaded ISR handlers**
: An unbounded or malicious handler can destroy the entire machine's timing
  and safety. Only a kernel trampoline may invoke a separately qualified,
  bounded hard-real-time callback.

**Assume an MPU makes native modules safe**
: MPU enforcement is target- and execution-mode-dependent, cannot prove
  algorithm correctness, and does not replace hardware protection.

**Ship one native binary for every supported MCU**
: Native portability is conditional on a complete target class. The source
  and semantic API are portable; binaries are not universally so.

## Product consequence

HELIX ceases to be only a motion-enabled Klipper firmware fork. It becomes a
target-compiled native machine-application platform:

* firmware is stable infrastructure;
* machine behavior is a deployable application;
* jobs are immutable executable scores;
* printers remain autonomous after provisioning;
* algorithms are written once at the semantic source level and rebuilt for
  qualified targets; and
* native performance is retained without converting every behavioral change
  into a firmware release.

The same architecture that removes Klippy from the execution dependency graph
also makes high-value embedded algorithms portable across the HELIX hardware
base. OpenAMS is the reference distributed workflow; BLDC/FOC is the reference
hard-real-time control module. Together they test both ends of the model:
seconds-long physical orchestration and microsecond-scale control under one
versioned deployment system.

The defining principle is:

> Virtualize machine capabilities. Compile for the target. Deploy behavior
> without reflashing. Execute natively.
