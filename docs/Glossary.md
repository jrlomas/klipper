# Glossary

> **This is Helix** — an evolution of Klipper. This page defines the terms
> Helix's documentation uses. New to Helix? Start with the
> **[Helix overview](HELIX.md)**.

Helix introduces a handful of new ideas on top of Klipper. This page
explains each in plain language, with a pointer to the deeper design
document where one exists. If a term you need isn't here, the
[Helix overview](HELIX.md) is the gentlest full explanation.

## Core motion concepts

- **Intention** (also **segment**) — the basic unit of Helix's motion
  protocol. Instead of sending a micro-controller a pre-computed list of
  step pulses, Helix sends a short mathematical description of *where a
  joint should be over the next slice of time* — its position as a
  function of time — and lets the board work out the pulses itself. One
  intention says, in effect, "starting now, move like *this* for *this*
  long." It describes *where*, never *how*.
  → [FD-0001 doc 02](founding/0001-motion-intentions/02-Intention_Protocol.md)

- **Joint** (also **actuator**) — one independently driven part of the
  machine — a single stepper, a servo, a heater's power channel. Helix
  works in terms of joints *after* kinematics, not the X/Y/Z axes you
  think in (one axis may be driven by several joints).

- **Backend** — the piece of firmware that turns intentions into real
  hardware action for a *specific* kind of actuator: a **stepper backend**
  makes step/dir pulses, a **PWM/DAC backend** makes an analog level, and
  a future **BLDC/FOC backend** would drive a brushless servo. Because the
  intention says only "where," the same motion queue can drive any of
  them — the backend is swappable. This is what frees Helix from being
  stepper-only.
  → [FD-0001 doc 04](founding/0001-motion-intentions/04-Actuator_Backends.md)

- **Machine time** — a single shared clock the whole machine agrees on, so
  that "do this at time T" means the same instant on the mainboard, a CAN
  toolhead, and a WiFi accessory. One board (the primary micro-controller)
  keeps the master clock; every other board disciplines its own clock to
  match, the way your computer syncs to an internet time server.
  → [FD-0001 doc 01](founding/0001-motion-intentions/01-Time_Model.md)

- **Horizon** — how far into the future a board's queue of intentions
  reaches, measured in machine time. A deep horizon is a shock absorber:
  it lets motion keep flowing smoothly even when the communication link
  stalls for a moment.

- **Underrun** — what happens if a board's intention queue runs dry
  mid-move (for example, because the link dropped). Rather than failing,
  the board smoothly decelerates to a stop (the **underrun ramp**, tuned
  with `motion_underrun_decel`) and reports the event — a pause you can
  resume from, not a crash.

- **Rebase** — re-anchoring a joint's motion to a known position. Helix
  does this automatically at the start of motion, after homing, and after
  an underrun; you rarely touch it directly.

## Failure recovery

- **Pause-and-hold** — Helix's default response to a *recoverable* problem
  (a lost link, a loose connector, a rebooted board). Instead of Klipper's
  `shutdown()` — which kills the heaters, releases the motors, and abandons
  the print — the affected board finishes or gently stops its current
  motion, **holds position with the motors still energized**, keeps heaters
  on their configured policy, and waits for you to fix the cause. This is
  Helix's flagship user-facing feature.
  → [FD-0001 doc 08](founding/0001-motion-intentions/08-Failure_Recovery.md)

- **Execution log** (spelled **execlog** in commands like `EXECLOG_DUMP`) —
  a board's "flight recorder." It is the mirror image of the intention
  queue: a running record of what the board *actually did* (every segment,
  trigger, and hold). On resume, the host reads this log to learn exactly
  where every joint stopped, so it can continue precisely rather than
  guess.

- **Failsafe hold** (heaters) — an opt-in, per-heater policy that keeps a
  heater — usually the bed — at temperature on its own during a
  pause-and-hold, so the part stays stuck to the plate. It runs under a
  hard temperature ceiling, a time limit, and independent runaway checks
  on the board itself.

## Networking, security, and sensing

- **Trigger source** — a hardware event (a pin changing, an analog
  comparator, an ADC watchdog) that Helix uses to detect endstop and probe
  hits the *instant* they happen, instead of a software timer sampling the
  pin thousands of times a second and hoping to catch it. The result is
  microsecond-precise, more repeatable homing and probing.
  → [FD-0001 doc 09](founding/0001-motion-intentions/09-Hardware_Triggers.md)

- **Pre-shared key (PSK)** — a secret both the host and a board know in
  advance. Helix uses it to authenticate every message on a network link,
  so a stranger on your WiFi can't forge motion commands. See
  [Secure Networking](Secure_Networking.md).

- **Framing v2** — Helix's upgraded packet format for links that need it.
  It replaces Klipper's simple error-*detecting* checksum with an
  error-*correcting* code (see BCH below), so a few corrupted bits on a
  noisy WiFi link get repaired instead of forcing a resend.
  → [FD-0001 doc 07](founding/0001-motion-intentions/07-Link_Transport.md)

- **Protocol library** (`intentproto`) — the single, freestanding piece of
  C++ that implements Helix's wire protocol. The firmware, the bootloader,
  and even other people's boards all use the same library, so they all
  speak the protocol identically.
  → [FD-0001 doc 10](founding/0001-motion-intentions/10-Protocol_Library.md)

## Project and process terms

- **Quarantine** — this word means **two different things** in Helix, so
  watch the context:
    1. **Feature quarantine** — a new feature ships as opt-in and
       experimental first, proves itself on real machines, and is
       *promoted* to the mainline only once it's earned it. See
       [Contributing](CONTRIBUTING.md#our-philosophy-pragmatic-and-you-maintain-what-you-add).
    2. **Radio quarantine** (ESP32 only) — on a dual-core ESP32, the WiFi
       radio stack is confined to one CPU core so it can never disturb the
       real-time motion running on the other. See [ESP32](ESP32.md).

- **Friendly fork** — Helix is a permanent, independent project built from
  Klipper's code, keeping Klipper's licensing and attribution, and
  continuously absorbing upstream Klipper's improvements — but free to
  follow its own design. It is *not* trying to be merged back into Klipper.
  → [Upstream Tracking](Upstream_Tracking.md)

- **Legacy path** (the **v1** protocol, sometimes the **"firehose"**) — the
  classic Klipper way of driving motion: the host pre-computes every step
  pulse and streams them to the board. Helix keeps this path intact and
  unchanged as a permanent fallback (and the only option on 8-bit AVR
  boards), so every existing printer still works.

## Acronyms

| Acronym | Stands for | In Helix |
| --- | --- | --- |
| **MCU** | Micro-Controller Unit | the printer's control board(s) |
| **SBC** | Single-Board Computer | the Linux host, e.g. a Raspberry Pi |
| **CAN** | Controller Area Network | a wiring bus for toolhead boards |
| **PWM** | Pulse-Width Modulation | how a digital pin fakes an analog level |
| **DAC** | Digital-to-Analog Converter | a true analog output |
| **FOC / BLDC** | Field-Oriented Control / Brushless DC | closed-loop servo motors |
| **PSK** | Pre-Shared Key | the secret that authenticates a network link |
| **HMAC** | Hash-based Message Authentication Code | proves a message is genuine and untampered |
| **DTLS** | Datagram Transport Layer Security | the model for Helix's optional secure session |
| **FEC** | Forward Error Correction | repairing bit errors without a resend |
| **BCH** | Bose–Chaudhuri–Hocquenghem | the error-correcting code framing v2 uses |
| **Ed25519** | (an elliptic-curve signature scheme) | how signed firmware is verified |
| **execlog** | Execution Log | the board's flight recorder |
