<p align="center">
  <img src="docs/img/helix-mark.svg" width="104" height="104" alt="HELIX">
</p>

<h1 align="center">HELIX</h1>

<p align="center"><em>Motion firmware that trusts its micro-controllers.</em></p>

<p align="center">
  An evolution of <a href="https://www.klipper3d.org/">Klipper</a> —
  rebuilt around motion <em>intentions</em>, machine time, and networks.
</p>

---

## Why HELIX exists

Klipper is one of the best pieces of software in 3D printing. HELIX
exists because the ideas that made Klipper great also point somewhere
Klipper, for good historical reasons, was never going to go.

Klipper's core bargain is this: the host computer pre-computes an exact
stream of step pulses and feeds it to a micro-controller that does as
it's told, microsecond by microsecond. That bargain was shaped by an
8-bit past — MCUs with no memory, no clock authority, and no business
making decisions. It bought precision, and it bought it honestly. But
it also means the truth about where your machine *is* lives on the host,
the link between them is a firehose that cannot skip a beat, and the
moment anything goes wrong — a late packet, a loose cable, a rebooted
board — the only safe answer the firmware has is to **stop everything**
and throw the print away.

The boards on your printer today are 32-bit computers. They have RAM,
hardware timers, and cycles to spare. HELIX starts from a different
bargain, one those boards can actually keep:

> The host sends **intentions** — where each joint should be and how
> it's moving, as short polynomial segments — and the micro-controller
> owns its own clock, its own position, and its own queue. It
> synthesizes the steps. It knows where it is. And when the world
> misbehaves, it holds its ground instead of falling on its sword.

Everything below follows from that one change.

## What it buys you

**Your print survives a bad moment.** A toolhead cable comes loose
mid-print. Under the old bargain: comms timeout → shutdown → cold bed →
part detaches → hours lost. Under HELIX: the board finishes its queued
motion, **holds position with the heaters still on**, and waits. You
reseat the cable, it re-handshakes, reconciles exactly where it stopped
from its own execution log, and resumes. Pause-and-hold replaces
abort-everything as the default response to recoverable failure. *(See
[RFC 0001 doc 08](docs/rfcs/0001-motion-intentions/08-Failure_Recovery.md).)*

**The link stops being a firehose.** Because each board buffers deep
queues of intentions and integrates them against its own clock, latency
and jitter on the wire become slack the queue absorbs instead of
defects in your surface finish. That is what finally makes **WiFi and
Ethernet** first-class transports — not just USB and a short CAN stub.
A network-native ESP32 toolhead is a real target, not a curiosity.
*(Docs [07](docs/rfcs/0001-motion-intentions/07-Link_Transport.md),
[12](docs/rfcs/0001-motion-intentions/12-ESP32_Architecture.md).)*

**It stops being stepper-only.** This is the deeper point of intentions:
a segment says *where the joint should be*, not which step pulses to
send. The actuator is a swappable backend behind one protocol —
classic step/dir steppers, sampled **PWM/DAC** actuators today, and a
future **closed-loop BLDC/FOC** servo joint tomorrow, all driven by the
same trajectory queue. HELIX carries segments up to **quintic (jerk- and
snap-limited) Bézier** curves, chained with drift-free fixed-point
integration so a thousand in a row still land exactly on target. *(Docs
[02](docs/rfcs/0001-motion-intentions/02-Intention_Protocol.md),
[04](docs/rfcs/0001-motion-intentions/04-Actuator_Backends.md).)*

**Hardware events, not polling — a capability unlock.** Endstop and
probe detection moves off a polled software timer onto on-chip **edge
interrupts, analog comparators, and ADC watchdogs**, firing the
coordinated stop the instant the pin changes and latching the exact
trigger tick in hardware. The µs stop latency is only the surface of it:
event-driven detection with DMA makes a class of things *possible that
polling made impossible* — catching an overrun or fault the moment it
happens, DMA-driven ADC oversampling, comparator-based analog triggers —
with automatic fall back to polling where the silicon can't. *(Doc
[09](docs/rfcs/0001-motion-intentions/09-Hardware_Triggers.md).)*

**A machine that agrees on the time.** Every board disciplines its clock
to a shared **machine time**, so "do this at T" means the same instant
across a mainboard, a CAN toolhead, and a WiFi accessory alike. *(Doc
[01](docs/rfcs/0001-motion-intentions/01-Time_Model.md).)*

**Communication you can trust.** Every datagram is authenticated
(truncated HMAC over a static PSK floor, an optional DTLS-class session
with key rotation and per-board identity on top); framing gains an
optional **forward-error-correction** trailer for lossy links; and
firmware images can be **Ed25519-signed** and verified by the bootloader
before they ever run. *(Docs
[07](docs/rfcs/0001-motion-intentions/07-Link_Transport.md),
[11](docs/rfcs/0001-motion-intentions/11-Bootloader.md).)*

**One firmware across families.** STM32 and ESP32 speak the same
protocol, expose the same versioned
[board syscall surface](docs/rfcs/0001-motion-intentions/13-Syscall_API.md),
and are written against the same board API. A module is written once,
not once per chip.

## How it holds together

HELIX is one protocol, one library, and a family of subsystems built on
the two things the old design couldn't assume — **an MCU with a clock**
and **an MCU with a memory**:

| Pillar | What changed | Where |
| --- | --- | --- |
| Motion intentions | step firehose → per-joint polynomial queues | [doc 02](docs/rfcs/0001-motion-intentions/02-Intention_Protocol.md) |
| Machine time | host-owned time → disciplined shared clock | [doc 01](docs/rfcs/0001-motion-intentions/01-Time_Model.md) |
| Failure recovery | shutdown-everything → pause, hold, resume | [doc 08](docs/rfcs/0001-motion-intentions/08-Failure_Recovery.md) |
| Link & transport | USB/CAN only → network-native, authenticated, FEC | [doc 07](docs/rfcs/0001-motion-intentions/07-Link_Transport.md) |
| Hardware triggers | polled endstops → interrupt-driven stops | [doc 09](docs/rfcs/0001-motion-intentions/09-Hardware_Triggers.md) |
| The protocol library | code generation → annotation static registration | [doc 10](docs/rfcs/0001-motion-intentions/10-Protocol_Library.md) |
| ESP32 as a target | unsupported → network-native, IDF-as-modem | [doc 12](docs/rfcs/0001-motion-intentions/12-ESP32_Architecture.md) |

The full design canon lives in
[the RFC 0001 series](docs/rfcs/0001-motion-intentions/00-Vision.md).

## Start here

* **New to HELIX?** Read the [HELIX overview](docs/HELIX.md) — the whole
  story, in one place.
* **Running a printer?** The [User Guide](docs/Helix_User_Guide.md) takes
  you from a stock Klipper mental model to HELIX's new capabilities.
* **Building or porting HELIX?** The
  [Developer Guide](docs/Helix_Developer_Guide.md) is the map to the
  architecture, the protocol library, and the RFC canon.

## Heritage and license

HELIX is a friendly fork of **Klipper**, created by Kevin O'Connor and
its community — years of brilliant engineering without which none of
this would exist. HELIX keeps faith with that work: it is Free Software
under the [GNU GPLv3](COPYING), it preserves Klipper's copyrights and
attribution throughout the source, and it contributes its ideas back in
the open. HELIX is *an evolution of Klipper, and no longer Klipper
itself* — a different bargain for a newer generation of hardware, built
with deep respect for the one it grew from.
