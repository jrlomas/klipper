# FD-0001: Portable Machine Programs and Dynamic Scores

Status: architecture and implementation proposal. The deterministic HELIX
trajectory path exists, but the portable workflow API, compiler, score IR, and
mainboard score VM described here do not. No autonomous macro or Klippy-Python
compatibility claim is complete until the equivalence and physical gates in
this document pass.

This document extends the
[host architecture](05-Host_Architecture.md),
[failure recovery](08-Failure_Recovery.md),
[protocol library](10-Protocol_Library.md),
[autonomous job execution](21-Autonomous_Job_Execution.md), and
[coordinated execution horizon](22-Coordinated_Execution_Horizon.md).

## Problem

A print is not defined by sliced G-code alone.

Klipper installations commonly place machine behavior in:

* Jinja/G-code macros;
* Python extras that register G-code commands;
* reactor timers and callbacks;
* mutable `printer` object state;
* MCU command/reply handlers;
* configuration reads and runtime persistence;
* user-supplied macros such as nozzle cleaning; and
* pause, runout, tool-change, calibration, and recovery procedures.

Pre-expanding a G-code file cannot preserve behavior that depends on a future
sensor value. Compiling only macro text cannot preserve behavior implemented
by a Python command handler. Porting all of Klippy, CPython, and Jinja onto the
mainboard would retain accidental host dependencies and would not produce a
bounded, verifiable execution contract.

HELIX therefore needs a source-compatible way to express **machine programs**:
distributed, event-driven operations that can execute either through live
Klippy or as part of an autonomous job capsule.

## Central decision

One ordinary-Python workflow source targets two execution backends:

1. a Klipper compatibility executor; and
2. a HELIX dynamic-score compiler.

The same workflow must not be rewritten separately for each backend.

```text
typed configuration + domain state + event
                    |
                    v
         portable machine program
                    |
          semantic operation stream
             /              \
            v                v
   Klipper executor     HELIX compiler
     live reactor       capsule score IR
                              |
                              v
                       mainboard score VM
```

The Python is an authoring language. The H7 does not run Klippy or arbitrary
Python. It executes a compact, versioned, bounded score representation.

## Terminology

**Machine program**
: A portable workflow written in the restricted Python subset and declared
  with `@machine_program`.

**Machine operation**
: A typed semantic effect such as `track.run`, `sensor.wait`,
  `device.command`, or `checkpoint.commit`.

**Dynamic score**
: The compiled state/transition graph containing operations, guards, bounded
  retries, deadlines, resource ownership, and local trajectory tracks.

**Trajectory chapter**
: An uninterrupted set of precompiled timestamped HELIX tracks admitted
  against one local time anchor.

**Workflow barrier**
: A point between trajectory chapters at which a variable-duration machine
  program must establish postconditions before the following chapter is
  rebased.

## Source compatibility

Portable workflows remain valid Python and ship with their compatibility
runtime. An unmodified upstream Klipper installation can import the same file.

```python
@machine_program(
    coordinator="mainboard",
    resources=("motion.xy", "extruder", "cutter", "material_lane"),
    timeout=45.0,
    on_failure="safe_pause",
)
async def change_material(machine, requested):
    if await machine.material.current() == requested:
        return
    async with machine.resources():
        await machine.track("park_and_retract")
        await machine.call("cutter.cut_and_verify")
        await machine.call("material.unload")
        await machine.call("material.load", material=requested)
        await machine.track("prime_clean_and_restore")
```

`async` and `await` identify suspension and evidence points; they do not
require asyncio on Klipper. The decorator returns a normal callable wrapper.
The host executor drives its operation awaitables with the Klippy reactor.
The compiler consumes the same awaitables to produce score nodes.

If an annotation is added only as metadata to an existing synchronous Klippy
function, the normal-host implementation may return that function unchanged.
Once a function uses portable operations, the compatibility decorator must
execute those operations rather than merely ignore the annotation.

## Logic and execution boundary

A portable domain module may contain:

* immutable typed configuration and topology;
* immutable authoritative state;
* commands, observations, completions, and timeout events;
* pure reducers and policy functions;
* portable workflow definitions; and
* declared fault and recovery types.

It may not import or directly access:

* Klippy `printer`, reactor, G-code, configfile, MCU, or pin objects;
* files, sockets, subprocesses, or dynamically imported modules;
* wall-clock time or randomness;
* mutable global control state; or
* an unbounded loop, recursion, wait, retry, or resource acquisition.

Every external dependency becomes either an explicit typed input or an
emitted operation.

```python
new_state, effects = reduce(state, event, observations)
```

Reducers do not schedule timers. They emit `deadline.arm`. A deadline expiry
returns later as `DeadlineExpired`. Reducers do not read sensor driver
objects. They receive timestamped observations with provenance and quality.

## Machine-operation ABI

The first ABI requires four operation families.

### Coordination

```text
resource.acquire
resource.release
checkpoint.commit
deadline.arm
deadline.cancel
workflow.pause
workflow.cancel
event.emit
```

### Motion and output

```text
track.run
trajectory.barrier
device.command
follower.set
heater.set_profile
fan.set
gpio.set
```

### Observation

```text
sensor.read
sensor.wait
operation.wait
device.capabilities
state.reconcile
```

### Terminal control

```text
workflow.complete
workflow.fail
workflow.safe_hold
workflow.forward_recovery
```

Each operation carries:

```text
workflow_id
operation_id
operation type and ABI version
semantic target role
configuration/calibration generation
typed arguments
deadline
idempotency and replay policy
expected completion/evidence type
failure and cancellation policy
```

Operation ids are not logging decorations. A leaf device must either execute
an unseen id once, return the recorded result for an allowed replay, or report
a typed conflict. It must never repeat a cutter stroke or feeder command
because a completion was lost.

## Capability manifest

Every portable workflow compiles to explicit requirements:

```text
score_vm >= 1
machine_program_abi >= 1
operations:
    track.run@2
    sensor.wait@1
    resource.acquire@1
    device.command@2
roles:
    toolhead.cutter
    toolhead.extruder
    material_lane.primary
bounds:
    max_duration
    maximum retries by phase
    maximum state and journal storage
```

The capsule verifier compares these requirements with the printer-fabric
capability graph. Missing or incompatible behavior rejects the capsule before
arming.

## Restricted Python compilation

Portable code may:

* call another portable function;
* branch on typed Boolean, enum, and bounded numeric predicates;
* use immutable values and pure approved helpers;
* iterate over compile-time-bounded collections;
* await machine operations; and
* raise declared workflow faults.

It may not rely on general Python reflection or unrestricted dynamic behavior.
The compiler rejects unsupported syntax with the function and source
location.

Compilation is effect-oriented. It does not translate arbitrary CPython
bytecode instruction by instruction. It executes or analyzes portable
functions until they request a declared machine effect or unknown future
observation, then records the effect and finite continuations.

Boolean observations form two branches. Enums form their declared finite
cases. Numeric observations form intervals from declared predicates rather
than one branch per possible value.

## Time model

Variable-duration workflows do not stop machine time.

```text
chapter N, anchored at C0
    -> workflow barrier
    -> sensor waits / retries / local tracks
    -> postconditions committed at C1
    -> chapter N+1 rebased at C1 + lead time
```

Every local motion inside a workflow remains an ordinary HELIX trajectory
track. The score VM waits for its execution checkpoint before advancing.

The following print chapter is not assigned an absolute timestamp until the
workflow establishes its postconditions. This preserves deterministic motion
without pretending that filament arrival or operator recovery duration is
known during slicing.

## Distributed ownership

The mainboard coordinates a composite workflow. Leaf MCUs execute bounded
local primitives.

A material change may lease:

```text
motion.xy
motion.z
toolhead.extruder
toolhead.cutter
sensor.extruder_in
sensor.extruder_out
material_lane.<id>
heater.hotend
```

The resource set is acquired atomically and in a canonical order. Runout,
operator commands, and another tool change cannot race for the same material
lane or extruder.

An OpenAMS unit does not own `OAMS_CUT` merely because it supplies filament.
The mainboard owns the distributed tool-change program and dispatches leaf
operations to the local axes, toolhead MCU, and OpenAMS nodes.

## State and evidence

Host mirrors are caches, not physical truth.

Every observation used to commit workflow state contains:

```text
sensor identity and semantic role
value
machine-time observation
quality and staleness
device and session generation
```

Startup, cancellation, reconnection, and ambiguous completion enter a
reconciliation workflow. The runtime obtains a coherent observation snapshot
and maps it to exactly one state. Contradictory evidence enters `UNKNOWN` or
`FAULT`; it does not guess `LOADED`.

## Forward recovery

Physical work cannot be rolled back. Recovery proceeds forward:

* retry feed;
* back off to a known sensor;
* recut;
* retract;
* select a replacement source;
* purge;
* or enter a safe hold for operator judgment.

Every recovery edge has an explicit attempt and duration bound, expected
evidence, resource set, and cancellation path. The score journal records the
failed operation and recovery history for Atlas.

## Klipper executor

The compatibility layer maps the semantic API onto existing mechanisms:

| Machine operation | Live Klipper adapter |
| --- | --- |
| `track.run` | Toolhead/motion command followed by the necessary barrier |
| `device.command` | Typed MCU driver call |
| `sensor.wait` | Sensor callback and reactor deadline |
| `deadline.arm` | Reactor timer |
| `checkpoint.commit` | Host runtime state and structured event |
| `workflow.pause` | Registered `PAUSE` command |
| `resource.acquire` | Workflow resource manager |

The domain never owns a `ReactorCompletion` or loops on `reactor.pause()`.
The adapter may wait for a workflow from a synchronous G-code command, but its
implementation remains event-driven.

Legacy entry macros reduce to wrappers:

```ini
[gcode_macro T0]
gcode:
    CHANGE_MATERIAL MATERIAL=T0
```

All decisions after that call occur in one workflow. A Jinja template never
tries to inspect the result of a G-code command that has not executed.

## Score IR and mainboard VM

The score IR is a bounded statechart containing:

* typed states and variables;
* event and completion transitions;
* symbolic guards over typed observations;
* operation nodes and argument records;
* resource leases;
* deadlines;
* bounded retry counters;
* checkpoint nodes;
* local track references;
* forward-recovery branches; and
* terminal complete, safe-hold, cancelled, and failed states.

The verifier proves before arming:

* all references and ABI versions resolve;
* every reachable wait has a deadline or a declared indefinite safe-hold
  policy;
* loops and retries are bounded;
* state and stack memory fit;
* resource ordering is valid;
* every non-terminal path has a successor;
* terminal paths release resources and apply safe-state policy; and
* required trajectory tracks and content hashes exist.

The VM journals state transitions and operation results, not every interpreter
instruction.

## OpenAMS reference extraction

OpenAMS is the first reference case because its current behavior spans all
relevant layers:

* configuration and topology;
* Klippy Python;
* reactor timers;
* MCU protocol commands and completions;
* local firmware coroutines and PID follower behavior;
* Jinja tool-change macros;
* mainboard and toolhead motion;
* runout and sensor branches; and
* forward physical recovery.

The current implementation is not one enum. It is a composite of host manager,
runout, tool-change macro, firmware load, firmware unload, and firmware
supervision/follower machines.

The OpenAMS repository holds the detailed local as-built extraction and future
proposal while that redesign remains private. The reusable HELIX contract is
recorded here without publishing unfinished OpenAMS implementation work.

## Implementation plan

### Phase 0 — freeze behavior

- [ ] Record semantic golden traces for load, unload, cancel, follower, runout,
  tool change, calibration, sensor failure, and no-spare recovery.
- [ ] Include state, observations, operations, deadlines, physical effects,
  and user-visible results.
- [ ] Record ambiguities and defects rather than normalizing them away.

### Phase 1 — schema and pure domain

- [ ] Version the statechart, configuration, event, observation, effect, and
  capability schemas.
- [ ] Extract immutable domain state and topology with no Klippy imports.
- [ ] Add explicit unknown, held, and fault states.
- [ ] Implement reachability, bounds, resource, and terminal-path validation.
- [ ] Build deterministic simulation and replay before either real executor.

### Phase 2 — portable API and Klipper executor

- [ ] Implement `@machine_program` and machine-operation awaitables.
- [ ] Implement the reactor-backed compatibility executor without a Klipper
  core patch.
- [ ] Preserve existing public G-code commands through thin adapters.
- [ ] Prove upstream-Klipper trace equivalence for the golden corpus.

### Phase 3 — complete distributed workflow

- [ ] Express OpenAMS load, unload, follow, calibration, runout, cutter,
  toolhead checks, purge, cleaning, and restore as portable functions.
- [ ] Add operation ids, generations, deadlines, resource leases, checkpoints,
  and forward recovery.
- [ ] Physically qualify the new workflow through live Klipper.

### Phase 4 — compiler and score VM

- [ ] Compile portable functions to a bounded score IR.
- [ ] Add score requirements to the capsule manifest and verifier.
- [ ] Implement the mainboard VM and structured journal.
- [ ] Prove simulator, Klipper executor, and score VM semantic equivalence.

### Phase 5 — autonomous multi-material gate

- [ ] Stage and arm a configuration-bound multi-material capsule.
- [ ] Disconnect the host before at least one tool change.
- [ ] Complete cutter, unload, load, sensor verification, purge, restore, and
  the following print chapter locally.
- [ ] Inject no-spare, jam, missing sensor, stale completion, and cancellation.
- [ ] Reconnect Atlas and reconstruct every workflow decision and recovery.

## Acceptance

The architecture is qualified only when:

1. upstream Klipper executes the rewritten workflow source without a core
   patch;
2. the same source compiles into a HELIX score;
3. domain and workflow modules import without Klipper installed;
4. unsupported host-only behavior fails compilation before arming;
5. every physical operation is bounded and generation-matched;
6. no sensor-failure branch emits dependent motion after requesting pause;
7. cancel and reconnect reconcile physical evidence instead of guessing;
8. resource races between runout, tool change, and operator commands are
   rejected deterministically;
9. simulator, Klipper, and VM semantic traces agree; and
10. a physical hostless multi-material print completes safely.

## Rejected alternatives

**Port all of Klippy onto the mainboard**
: Retains the coupling and does not create a bounded machine contract.

**Compile arbitrary Python bytecode**
: Cannot safely bound general Python I/O, reflection, memory, or control flow.

**Maintain one macro implementation and one HELIX implementation**
: Behavior and recovery will drift.

**Flatten workflows onto one absolute timeline**
: Future sensor and recovery durations are not knowable at compile time.

**Assign the entire workflow to one accessory MCU**
: Composite operations own resources across several controllers and must be
  coordinated by the mainboard.
