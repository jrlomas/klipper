# Coming from Klipper

> **This is Helix** — an evolution of Klipper. If you know Klipper, this
> page is your fastest path in. New to Helix? Start with the
> **[Helix overview](HELIX.md)**.

Picture the failure that ruins prints today: a toolhead cable works
loose mid-print. In Klipper that ends in `shutdown()` — cold bed,
released motors, a detached part. In Helix the same event becomes a
**pause-and-hold**: the board finishes or gently ramps out its current
motion, holds position with the motors energized, keeps the bed hot on a
failsafe policy so the part stays stuck, and waits. You walk over, replug
the cable, and resume. That is possible because Helix changes one thing
underneath everything else: instead of the host pre-computing a stream of
step pulses for the micro-controller to replay, the host sends
**intentions** — short per-joint polynomial *segments* ("from here, move
with this velocity and acceleration for this long") — and the board owns
its own clock, position, and queue and synthesizes the steps itself. Your
existing Klipper printer already runs Helix unchanged. Everything new
here is **opt-in**, one actuator or one board at a time.

## What stays exactly the same

Helix is source-compatible with upstream Klipper. If you don't turn a
Helix feature on, it behaves like the Klipper you already know — because
it *is*. Nothing in this list changes:

* Your **`printer.cfg`** — same sections, same options.
* Your **G-code** — `G0`/`G1` and every macro work identically. Ordinary
  moves need no coordinate repair.
* Your **macros**, **kinematics** (cartesian, corexy, delta, and the
  rest), **input shaping**, and **pressure advance** — all still run
  host-side, exactly as in Klipper.
* **Mainsail, Fluidd, OctoPrint**, the JSON API, and your **slicer**
  (SuperSlicer, Cura, PrusaSlicer, …) — unchanged.
* Your install layout. The on-disk paths stay **`~/klipper`**, and the
  log stays **`klippy.log`**.

That last point surprises people, so let's be plain about it: the product
is **Helix**, but the directory is still `klipper` and the host process
is still `klippy`. That is deliberate — it's the mechanism that keeps
Helix source-compatible with upstream and lets it absorb future Klipper
releases cleanly. Seeing `klipper` in your paths is not a mistake; it's
the compatibility promise working. (No G-code, config-file, or macro
breakage is a stated **non-goal** of the project — see
[FD-0001](founding/0001-motion-intentions/00-Vision.md#non-goals).)

## What you gain

Each of these is opt-in and covered in depth elsewhere; here's the
benefit-first version. For the full list see
[Features](Features.md#what-helix-adds); for the *why*, the
[Helix overview](HELIX.md).

* **Prints that survive hiccups.** A lost link, a loose connector, or a
  rebooted secondary board becomes a pause you can recover from, not an
  aborted print. → [pause-and-hold](Features.md#what-helix-adds)
* **Motion that tolerates a worse link.** A deep on-board segment queue
  absorbs communication latency and jitter, which is what makes WiFi and
  Ethernet boards realistic rather than a novelty.
* **A board that knows where it is.** Position lives in a drift-free
  fixed-point accumulator on the MCU, not in a host-side guess.
* **More repeatable homing and probing.** Endstop and probe detection can
  run off on-chip hardware events (edge interrupts, comparators) instead
  of a polled software timer — microsecond stop latency, hardware-latched
  trigger position, no config change.
* **Beyond stepper-only.** A segment describes *motion*, not step pulses,
  so the actuator becomes a swappable **backend** (a subsystem that turns
  a segment into real movement). Classic step/dir steppers and sampled
  PWM/DAC actuators work today; a closed-loop BLDC/FOC servo joint is
  built to be just another backend on the same queue tomorrow.

## Your first Helix feature in 5 minutes

Let's turn on the flagship user-facing feature: **pause-and-hold**, so a
lost link on a secondary board becomes a recoverable pause. (If you'd
rather start with trajectory motion, jump to
[Enabling trajectory motion](Helix_User_Guide.md#enabling-trajectory-motion)
in the User Guide — it's a one-line `motion_protocol: trajectory` on a
single stepper.)

**1. Add a `[failure_recovery]` section and a heater failsafe policy.**
This tells the machine to keep the bed hot through a fault instead of
killing it:

```
[failure_recovery]

[heater_bed]
# ... your existing bed heater config ...
failure_policy: hold        # keep the bed hot through a fault
hold_max_temp: 110
hold_max_duration: 3600
```

**2. Tell a secondary board to pause instead of shut down** on a lost
link:

```
[mcu toolhead]
canbus_uuid: ...
on_comm_timeout: pause      # pause-and-hold instead of shutdown
```

**3. Verify it's live.** Restart, then in the G-code console:

```
FAILURE_RECOVERY_STATUS
```

This lists your configured heater holds (policy, ceiling, duration) and
any micro-controllers currently in the paused-link state. You can also
ask the whole machine what its firmware was actually built to support:

```
HELIX_STATUS
```

`HELIX_STATUS` reads each board's served capability dictionary and tells
you which Helix features are compiled in (trajectory motion, heater hold,
hardware triggers, and more) and which host subsystems are loaded — the
fastest answer to "what does this printer support, and what's turned on?"

**What to expect.** If that toolhead's link later drops, Helix finishes
queued motion, holds position with the motors energized, keeps the bed on
its hold policy, and keeps a rolling **execution log** — a record of
everything the board actually did (the uplink twin of the intention
queue). Once you've fixed the cause:

* `RECONNECT_MCU MCU=toolhead` — re-handshake the board.
* `RESUME_MOTION` — reconcile every joint from its execution log and
  resume the print. It only blocks for a joint whose homing was genuinely
  lost.

Full walkthrough, including how to mark an axis whose homing can't be
trusted across a board reset (`motion_homing_volatile: True`), is in
[Surviving failures](Helix_User_Guide.md#surviving-failures).

## What's still maturing

Helix is **0.9**, and it's honest about what has run on silicon versus
what is awaiting hardware bring-up. The core is implemented; several
subsystems are validated off-silicon (host tests plus firmware that
compiles and links for the target) but still carry a "needs a devkit"
banner in their design docs. Treat the **network, security, and signing**
features (UDP transport, the DTLS-class secure session, Ed25519 firmware
signing) as opt-in until you've validated them on your own bench.

None of this puts your printer at risk, because opting into a Helix
feature is a deliberate act. When in doubt, the classic Klipper paths are
all still there, untouched — the legacy `queue_step` path is the
permanent fallback, not a deprecated one. See
[A note on maturity](Helix_User_Guide.md#a-note-on-maturity) for the
project's own framing.

## Where to go next

* **[Helix User Guide](Helix_User_Guide.md)** — every knob, with the
  config to turn it on. Start here to go further than this page.
* **[Helix overview](HELIX.md)** — the whole story: puppet vs peer,
  firehose vs intentions, why pause-and-hold replaces shutdown.
* **[Features](Features.md)** — what Helix adds and what it inherits from
  Klipper unchanged.
* **[Helix command & feature reference](Helix_Commands.md)** — every
  command, config option, and firmware capability in one table.
* **[Glossary](Glossary.md)** — intention, segment, backend, execution
  log, pause-and-hold, and the rest, defined in one place.
