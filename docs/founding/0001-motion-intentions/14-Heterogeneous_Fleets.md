# FD-0001: Heterogeneous Fleets — Firehose and Intent Boards Together

Status: Implemented off-silicon in HELIX 0.9: the shared timing substrate,
single-paradigm config validator, and classic-step escape hatch all exist.

A real machine will not convert all at once. On the same printer you will
have boards running Klipper's classic **step firehose** (the host
pre-computes every pulse) next to boards running HELIX **motion
intentions** (the board owns its queue and synthesizes steps). This
document defines what that means and how the two stay in lockstep — the
motion-side companion to the wire-side coexistence in
[Protocol v2](../../Protocol_v2.md) and
[Upstream_Tracking](../../Upstream_Tracking.md).

A firehose board is simply a board the host speaks **stock v1** to; an
intent board is one it speaks **segments** to. The envelope architecture
already makes one host fluent in both dialects. The question this document
answers is the motion one: **how do coordinated moves and timed events
stay aligned across boards that own their motion differently?**

## One coordination timeline

There is exactly **one** timeline that coordinates the machine: the host's
**print-time**. Everything else is a way to realize it on a board.

- **Firehose boards** are aligned by `clocksync`: the host maintains a
  precise affine map (frequency + offset) from print-time to each MCU's
  clock, and places every step at a print-time instant. This is stock
  Klipper, unchanged.
- **Intent boards** are aligned by **machine time** ([doc 01](01-Time_Model.md)):
  the board integrates segments in its own ticks, the beacon keeps
  secondaries disciplined to the primary, and the host holds the master
  map from print-time to machine time.

Machine time is therefore **not a competing clock** — it is print-time
*realized on an intent board*. `clocksync` and the machine-time beacon are
two disciplining mechanisms for the same timeline, and the host knows both
maps. The consequence is the property the whole machine depends on:

> "Toolhead at position **P** at instant **T**" means the same physical
> moment whether **T** is realized as a pre-computed step on a firehose
> board or a segment boundary on an intent board — because the host
> anchors both to print-time.

The host stays the planner and arbiter it has always been. It computes the
one coordinated kinematic plan and emits it in two encodings.

## Two regimes

The paradigms share a timeline but **not** their failure and buffering
semantics — an intent board can pause-and-hold and absorb wire jitter from
a deep queue; a firehose board cannot skip a beat and shuts the machine
down on a fault. That difference splits mixed-fleet motion into two cases.

### Regime 1 — independent / accessory motion (common, supported today)

A firehose board and an intent board drive **different, non-coordinated**
actuators: an OpenAMS unit feeding filament beside a firehose XYZ; an
extruder-only toolboard; a chamber or accessory board. Nothing has to
interleave step-for-step. The boards share only the timeline, and any
event that must align across them ("retract at T", "fire the cutter at T")
uses the shared print-time.

This is the overwhelmingly common topology — most notably, **filament
motion is not part of the coordinated toolhead kinematics**, which is
exactly why an OpenAMS board can run stock Klipper beside HELIX intent
boards with nothing more than the clock discipline that already exists. It
works today.

### Regime 2 — one coordinated move spanning both paradigms (guarded)

A single kinematic move whose steps must land on **both** a firehose board
and an intent board (say, X on an intent mainboard and a coordinated Z on
a firehose toolboard). Here the semantic mismatch bites: a move that spans
both degrades to the **least-capable** board — you cannot pause-and-hold a
motion a firehose board is already mid-executing, and you cannot let an
intent board's deep queue run ahead of a firehose board that must be fed
beat-by-beat.

The rule, therefore:

> **A coordination group must be single-paradigm.** All actuators that
> participate in one coordinated kinematic motion should be driven by
> boards of the same kind — all firehose, or all intent. The host
> validates this at config time and refuses a topology that splits a
> coordination group across paradigms (rather than silently producing a
> move with two different failure behaviours).

This is not a limitation of the timeline — it is honesty about semantics.
Mixed *machines* are first-class; mixed *coordination groups* are not.

### The escape hatch: classic-step compatibility

If a machine genuinely must split a coordination group, an intent-capable
board can drive a specific axis in **classic-step mode** — leave that
stepper's `motion_protocol` at its default instead of setting it to
`trajectory`. The board already speaks stock v1, so it accepts the host's
pre-computed step times for that axis and joins a firehose coordination
group. The whole spanning move is then planned in the firehose paradigm,
and that axis **forgoes HELIX
recovery** (pause-and-hold, deep buffering) for the duration. This is an
explicit, opt-in fallback, never the default — offered so no machine is
*impossible* to wire, not because it is a good place to be.

## What exists

- **Exists today:** the timing substrate. `clocksync` disciplines firehose
  boards; the machine-time beacon ([doc 01](01-Time_Model.md)) disciplines
  intent boards; the host holds both maps. Regime-1 machines already run.
  **The config-time validator is implemented**: at connect,
  `trajectory_queuing` rejects a kinematic rail that mixes trajectory and
  legacy steppers, and rejects a partial conversion of coupled kinematics
  (corexy/delta-class, whose rails move as one group) — with an error that
  names the offending steppers and cites this document.
- **Classic-step escape hatch:** firmware keeps both the stock `queue_step`
  and trajectory command surfaces, and the host selects them per stepper
  with `motion_protocol`. An all-classic coordination group on an
  intent-capable board therefore works without a third execution mode.

## Relationship to other documents

- [doc 01 — Time Model](01-Time_Model.md): machine time, the primary-MCU
  authority, and beacon discipline — the intent-board half of the shared
  timeline.
- [doc 02 — Intention Protocol](02-Intention_Protocol.md): what an intent
  board executes.
- [doc 06 — Migration](06-Migration.md): the incremental path on which
  mixed fleets are the steady-state reality, not a transient.
- [Protocol v2](../../Protocol_v2.md) and
  [Upstream_Tracking](../../Upstream_Tracking.md): the wire-side
  coexistence — one host speaking stock v1 to firehose boards and the
  additive envelope to intent boards.
