---
hide:
  - toc
  - navigation
title: HELIX
---

![HELIX](img/helix-logo.svg){ .center-image width="360" }

<h1 style="text-align:center; font-size:3.4em; letter-spacing:0.32em; font-weight:800; margin:0.1em 0 0; padding-left:0.32em;">HELIX</h1>

<p style="text-align:center; font-size:1.2em; color:var(--md-default-fg-color--light); margin:0.4em 0 1.6em;">
Motion firmware that trusts its micro-controllers.
</p>

HELIX is an evolution of [Klipper](https://www.klipper3d.org/) — rebuilt
around motion **intentions**, machine time, and networks. Instead of
streaming a pre-computed firehose of step pulses to a micro-controller
that does exactly as it's told, HELIX sends *intentions* — where each
joint should be and how it's moving — and lets the board own its clock,
its position, and its queue. It knows where it is, and when the world
misbehaves it holds its ground instead of falling on its sword.

That one change unlocks the rest: an **actuator-agnostic** motion model
(steppers, PWM/DAC, and a future closed-loop BLDC/FOC joint are all
backends behind one protocol), **pause-and-hold** recovery instead of
shutdown-everything, network-native transports, **hardware-event**
sensing that makes things polling never could, and one firmware shared
across the STM32 and ESP32 families.

## Start here

- **[HELIX overview](HELIX.md)** — the whole story in one page: the idea,
  and each capability as problem → solution → payoff.
- **[User Guide](Helix_User_Guide.md)** — from a stock-Klipper mental
  model to turning on HELIX's new capabilities.
- **[Developer Guide](Helix_Developer_Guide.md)** — the architecture, the
  protocol library, and how to build or port HELIX.
- **[Command &amp; feature reference](Helix_Commands.md)** — every new
  command, config option, and firmware capability in one place.
- **[Features](Features.md)** · **[FD-0001 design canon](founding/0001-motion-intentions/00-Vision.md)**

Start running it with the [installation guide](Installation.md); the
inherited Klipper documentation lives under the [Overview](Overview.md).

---

*HELIX is a friendly fork of Klipper, created by Kevin O'Connor and its
community. It is Free Software under the [GNU GPLv3](../COPYING),
preserves Klipper's copyrights and attribution throughout the source, and
builds on that work with deep respect. HELIX is an evolution of Klipper,
and no longer Klipper itself.*
