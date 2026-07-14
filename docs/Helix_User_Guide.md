# HELIX User Guide

This guide is for the person running a printer. It assumes you know
Klipper — a `printer.cfg`, a `[mcu]`, homing, a G-code console — and
walks you into the things HELIX adds and how to turn them on. Nothing
here is mandatory: every HELIX capability is **opt-in**, and a config
that doesn't ask for them behaves like the Klipper you already know.

If you want the *why* behind any of this, the
[HELIX overview](HELIX.md) tells the story; this guide tells you which
knobs to turn. New to a term like *intention*, *segment*, or *execution
log*? The [glossary](Glossary.md) defines them in plain language, and
[Coming from Klipper](Coming_From_Klipper.md) is the five-minute tour.

## The mental model shift

In stock Klipper the host computes step pulses and streams them to a
micro-controller that replays them. In HELIX you can instead let an
actuator receive **motion intentions** — short polynomial segments — and
let the board synthesize the steps and track its own position. You opt
in *per actuator*, so a single machine can mix classic steppers and
trajectory steppers on the same board while you gain confidence. The
practical upshots you'll notice: motion tolerates a worse link, and your
printer can *survive and resume* failures that used to end the print.

## Enabling trajectory motion

On any stepper you want to move via intentions:

```
[stepper_x]
# ... your normal pins, rotation_distance, etc ...
motion_protocol: trajectory
```

Optional tuning (all have sensible defaults — see the
[Config Reference](Config_Reference.md#stepper)):

* `motion_tolerance`, `motion_sample_time` — how faithfully the host
  segment fitter approximates the commanded path.
* `motion_underrun_decel` — the deceleration the board uses to ramp to a
  safe stop if its queue ever runs dry (for example on link loss).
* `motion_homing_volatile` — see [recovery](#surviving-failures) below.

Check what's live at any time:

```
TRAJECTORY_STATUS
```

## Surviving failures

This is the flagship user-facing feature — **pause-and-hold** recovery. Add a `[failure_recovery]`
section and set per-heater failsafe policies so a stumble becomes a
pause instead of a ruined print.

```
[failure_recovery]

[heater_bed]
# ...
failure_policy: hold        # keep the bed hot through a fault
hold_max_temp: 110
hold_max_duration: 3600
```

Then, on a secondary micro-controller you want to survive a lost link:

```
[mcu toolhead]
canbus_uuid: ...
on_comm_timeout: pause      # pause-and-hold instead of shutdown
```

When that board's link drops, HELIX finishes queued motion, holds
position with heaters on their policy, and waits. After you fix the
cause:

* `RECONNECT_MCU MCU=toolhead` — re-handshake the board.
* `RESUME_MOTION` — reconcile every joint from its execution log and
  resume the print.
* `FAILURE_RECOVERY_STATUS` — see holds and paused links at a glance.

**Homing after a board reset.** HELIX assumes a joint is still where it
was last commanded, with the homing it had, and continues — no encoders
required. If a particular axis genuinely cannot be trusted across a board
reset, mark it `motion_homing_volatile: True` and HELIX will require a
re-home for that axis before resuming. Full rationale in
[FD-0001 doc 08](founding/0001-motion-intentions/08-Failure_Recovery.md).

## Faster, more repeatable homing

If your board's firmware is built with hardware trigger support, HELIX
uses an on-chip **edge interrupt** for endstop and probe detection
automatically — an interrupt-latched trigger position with no config change.
Boards with timer input capture can timestamp the physical edge to a hardware
tick; other ports use the ISR-entry timer read and may add a short
qualification window. It falls back to the classic polled path where the
hardware or firmware can't. To force the legacy polled path on a given board:

```
[mcu]
hardware_endstop_trigger: False
```

## Networked and CAN boards

HELIX speaks the same authenticated protocol over UDP (Ethernet/WiFi),
CAN, USB, and UART. CAN boards work as they do in Klipper. For Ethernet
and the ESP32 network target, see [Ethernet](Ethernet.md) and
[ESP32](ESP32.md), and the transport design in
[FD-0001 doc 07](founding/0001-motion-intentions/07-Link_Transport.md).

## Knowing what your machine can do

```
HELIX_STATUS
```

reads each micro-controller's served capability dictionary and tells you
which HELIX features that board's firmware was actually built with
(trajectory motion, cubic/quintic segments, hardware triggers, heater
hold, framing v2, the unified syscall API) and which host subsystems are
loaded. It's the fastest answer to "what does this printer support, and
what's turned on?"

## Advanced / commissioning commands

Normal `G0`/`G1` commands are the production quintic motion path: Klippy still
owns Cartesian lookahead and the displayed toolhead position, while each
trajectory MCU receives joint polynomials and synthesizes its pulses locally.
No coordinate repair is required after an ordinary move.

* `BEZIER_MOVE` — drive a single trajectory joint along a cubic/quintic
  Bézier curve. Like `FORCE_MOVE`, it bypasses the kinematic planner and
  is **disabled by default**; enable it with
  `[trajectory_queuing] enable_bezier_move: True` and use it only on an
  idle machine for testing. It leaves the toolhead position stale —
  follow with `SET_KINEMATIC_POSITION`.

Every HELIX command, config option, and firmware capability is collected
in one place in the [HELIX command &amp; feature reference](Helix_Commands.md).
The full per-command detail lives in the
[G-Code reference](G-Codes.md) (see the `[failure_recovery]`,
`[timesync]`, `[trajectory_queuing]`, and `[helix_status]` sections);
all config options in the [Config Reference](Config_Reference.md).

## A note on maturity

HELIX is honest about what has run on hardware and what is awaiting
bring-up. Several subsystems are validated off-silicon (host tests,
firmware that compiles and links for the target) but still carry a
"needs a devkit" banner in their design docs. Read those banners, and
treat network, security, and signing features as opt-in until you've
validated them on your own bench. When in doubt, the classic Klipper
paths are all still there, untouched.
