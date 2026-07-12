# HELIX

*An evolution of Klipper — rebuilt around motion intentions, machine
time, and networks.*

This document is the whole story in one place: what HELIX is, the
problem it was built to solve, how it solves it, and what that buys you.
If you only read one page, read this one. When you want the rigorous
version, every section points into [the RFC 0001 design
canon](rfcs/0001-motion-intentions/00-Vision.md).

---

## The one idea

Klipper decided, correctly for its time, that a 3D printer's
micro-controller should be a *puppet*: the host pre-computes the exact
sequence of step pulses and the MCU replays it, tick by tick, with no
opinions of its own. That decision was right when MCUs were 8-bit parts
with a few hundred bytes of RAM and no trustworthy clock. It bought
Klipper its legendary precision.

But it has three permanent consequences:

1. **The host owns the truth.** Where your machine actually *is* exists
   only in the host's model. The MCU is holding a script, not a
   position.
2. **The link can't breathe.** A pre-computed step stream is a firehose
   with a deadline; a late packet is a defect, so the transport must be
   a short, quiet, wired one.
3. **The only safe failure is death.** A puppet that loses its
   puppeteer, or drops a line of its script, has no basis to continue —
   so it must `shutdown()`: kill the heaters, release the motors,
   abandon the print.

The micro-controllers on a modern printer are 32-bit computers with real
memory, hardware timers, DMA, and cycles to spare. HELIX changes the one
decision underneath everything:

> **The MCU is a peer, not a puppet.** The host sends *intentions* —
> where each joint should be and how it's moving — and the board owns
> its clock, its position, and its queue, and synthesizes the steps
> itself.

The rest of HELIX is what becomes *possible*, and in several cases
*easy*, once that is true.

## Intentions, not step streams

An intention is a short per-joint polynomial: "from here, move with this
velocity, acceleration (and, if you like, jerk and snap) for this long."
The board keeps a deep queue of them and integrates them against its own
clock to generate steps in real time.

**Why it's better.** The queue is a shock absorber. Communication
latency and jitter — the enemy of a step firehose — become slack that
the queue smooths away, because the board always has hundreds of
microseconds to milliseconds of motion already in hand. Position is now
something the board *knows*, exactly, in a drift-free fixed-point
accumulator — not something the host is guessing on its behalf.

**And it stops being stepper-only.** This is the part that's easy to
miss and matters most. A pre-computed step stream can only speak to a
thing that takes step pulses — a step/dir stepper, full stop. A segment
speaks a level up: *where the joint should be and how it's moving*. What
turns that into motion is a **backend**, and the backend is swappable.
The segment core drives classic steppers today, drives a sampled
**PWM/DAC** actuator today, and is built so a **closed-loop BLDC/FOC**
servo joint — a brushless extruder, a servo axis — is just another
backend on the exact same queue tomorrow. The step firehose held that
door shut by construction; intentions open it. *(The backend contract and
the FOC/BLDC case are specified in
[doc 04](rfcs/0001-motion-intentions/04-Actuator_Backends.md).)*

**What it buys you.** Smoother motion, tolerance to imperfect links,
a board that can answer "where are you?" without the host — and an
actuator model that isn't nailed to stepper motors. HELIX carries
segments all the way up to **cubic and quintic Bézier** curves for jerk-
and snap-limited motion, chained so exactly that thousands of them in a
row accumulate zero positional drift.
→ [doc 02](rfcs/0001-motion-intentions/02-Intention_Protocol.md)

## A machine that agrees on the time

If boards own their own clocks, the machine needs a shared notion of
*when*. HELIX disciplines every secondary board's clock to a single
**machine time** with a beacon and a small control loop, the way NTP
disciplines a computer to a reference.

**What it buys you.** "Do this at time T" means the same instant on the
mainboard, on a CAN toolhead, and on a WiFi accessory — so coordinated
motion and synchronized events survive being spread across a network
instead of a shared backplane. → [doc 01](rfcs/0001-motion-intentions/01-Time_Model.md)

## Pause, hold, resume — not shutdown

This is the change you will feel most.

Klipper's failure model is binary: any recoverable hiccup — a late
timer, a lost message, a loose connector — ends in `shutdown()`, which
turns off every heater, releases every motor, and abandons the print.
For an untrustworthy 8-bit puppet that was the *only* safe answer.

A HELIX board is trustworthy: it owns its position and its queue. So its
default response to a recoverable failure is **pause-and-hold** — finish
or gently ramp out the current motion, hold position with the motors
energized, keep the heaters on their **failsafe policy** (the bed stays
hot, so the part stays stuck), and keep a rolling **execution log** of
everything it actually did.

When the problem clears, resume stops being a guess. The host drains the
board's execution log — the uplink twin of the intention queue — diffs
*what was intended* against *what was executed*, and knows exactly where
every joint stopped and what was already printed. HELIX's recovery model
uses no encoders and no closed-loop feedback: it simply assumes the
joint is still where it was last commanded, with the homing it had, and
continues — asking for a re-home only when that homing was genuinely
lost.

**What it buys you.** The single most print-destroying event in the old
world — a toolhead cable working loose — becomes a pause you walk over
and fix. → [doc 08](rfcs/0001-motion-intentions/08-Failure_Recovery.md)

## Networks as first-class transports

Deep intention queues absorb link jitter, which quietly removes the
reason printers were tethered to USB and short CAN runs. So HELIX treats
**Ethernet and WiFi** as first-class: the same authenticated datagram
protocol runs over UDP, CAN, USB, and UART alike, and a network-native
**ESP32** becomes a real toolhead target rather than a novelty.

Because a network is a hostile place in ways a USB cable is not, the
transport is built to be trusted:

* **Authenticated by default** — every datagram carries a truncated
  HMAC over a static pre-shared key; that floor is mandatory.
* **An optional secure session** — a DTLS-class handshake adds rotating
  per-session keys and per-board identity, defending against forgery and
  replay of motion commands, without the weight of full IETF DTLS.
* **Forward error correction** — a negotiable BCH trailer repairs bit
  errors on lossy links instead of forcing a retransmit.
* **Signed firmware** — images can be **Ed25519-signed** and verified by
  the bootloader before they are allowed to run.

→ [doc 07](rfcs/0001-motion-intentions/07-Link_Transport.md),
[doc 11](rfcs/0001-motion-intentions/11-Bootloader.md),
[doc 12](rfcs/0001-motion-intentions/12-ESP32_Architecture.md)

## Stops that happen in hardware

Homing and probing used to *poll*: a software timer sampled the endstop
pin thousands of times a second and hoped to catch the trigger between
samples. HELIX arms an on-chip **edge interrupt** (or analog comparator,
or ADC watchdog) that fires the coordinated stop the instant the pin
changes and latches the exact trigger time in hardware — falling back to
polling only where the silicon can't do it.

**What it buys you.** Microsecond stop latency and a hardware-exact
trigger position — more repeatable homing and probing, with zero config
change and automatic graceful degradation. But the latency is the
*surface*. Moving sensing off the timer list and onto interrupts,
comparators, and **DMA** unlocks a class of things polling made
structurally impossible in a real-time motion loop: catching an
**overrun or fault the instant it occurs** instead of at the next
sample, **DMA-driven ADC oversampling** at rates a scheduled poll could
never reach without starving step generation, and analog window
triggers. Those uses aren't all built yet — the point is the substrate
now makes them *reachable* rather than a fight with the scheduler.
→ [doc 09](rfcs/0001-motion-intentions/09-Hardware_Triggers.md)

## One protocol, one library, every board the same

Under the hood, HELIX speaks a single protocol implemented **once** as a
freestanding C++ library (`lib/intentproto`). Two decisions define it:

* **Annotation, not code generation.** A command is declared with a
  macro next to its handler (`KLIPPER_METHOD(...)`) and registers itself
  before `main()` — no external code generator, no build step that
  parses your source, no generated files to drift out of sync. The
  device's data dictionary is a *serialization of the live registry*,
  served, not scraped.
* **A unified board syscall surface.** STM32 and ESP32 already implement
  the same board primitives; HELIX gathers them into one **versioned,
  capability-advertised syscall table** so a module is written once
  against the API, not once per chip — the foundation for pushing
  modules to a board without reflashing it (the idea; the VM that would
  have run them was deliberately left out as low-value).

→ [doc 10](rfcs/0001-motion-intentions/10-Protocol_Library.md),
[doc 13](rfcs/0001-motion-intentions/13-Syscall_API.md)

## What HELIX is not

HELIX is **not a drop-in Klipper release** and does not pretend to be.
The trajectory path is opt-in per actuator; the network, security, and
signing features are opt-in per board; and the whole thing is honest
about what has run on silicon and what is still awaiting hardware
bring-up. It is **not** trying to be merged upstream — several of its
choices (an on-die WiFi blob on the ESP32, a different failure
philosophy) are deliberate departures. The goal is not to *replace*
Klipper but to *evolve* from it, in the open, for people who want the
newer bargain.

## Where to go next

* **Run it:** the [HELIX User Guide](Helix_User_Guide.md).
* **Build it:** the [HELIX Developer Guide](Helix_Developer_Guide.md).
* **Understand it deeply:** the [RFC 0001
  canon](rfcs/0001-motion-intentions/00-Vision.md).
* **Its Klipper roots:** the inherited [documentation
  overview](Overview.md).

---

*HELIX is a friendly fork of [Klipper](https://www.klipper3d.org/), Free
Software under the [GNU GPLv3](../COPYING). It stands on the shoulders of
Kevin O'Connor and the Klipper community, whose work it preserves,
credits, and builds upon.*
