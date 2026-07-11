# RFC 0001: The Protocol Library

Status: Draft / Discussion

Today the wire protocol is implemented **twice**: once in the firmware
(`src/command.c`, `src/msgblock` constants) and once in the host's C
helper (`klippy/chelper/msgblock.c`, `serialqueue.c`) — two disjoint
codebases that duplicate the framing constants, the CRC, the VLQ
codec, and the sequence-number rules, kept in agreement only by
discipline. Any third party who wants to speak the protocol (a probe
vendor, a custom toolboard, a test harness) gets neither: they
reimplement from documentation.

This document specifies **one protocol library** — a single,
self-contained, MIT-licensed C implementation of the entire wire
protocol, consumed by the host, by our firmware, by the bootloader
([11-Bootloader.md](11-Bootloader.md)), and by anyone else's firmware
or tooling, open or closed.

The demand is proven, not speculative: Annex Engineering's *Anchor*
is an independent implementation of the MCU side of the Klipper
protocol, written in Rust, built precisely so custom hardware could
join the ecosystem without running Klipper's firmware. This library
serves the same need in plain C — no toolchain opinion imposed on
adopters, linkable from any firmware including our own bootloader —
and covers both sides of the wire (device *and* host) plus the v2
protocol. Its seed has the same lineage: the author's own C
implementation of the legacy protocol, written for the OpenAMS
boards' firmware
([OpenAMSOrg/mainboard-firmware](https://github.com/OpenAMSOrg/mainboard-firmware)),
which is also how the protocol's actual wire behavior — as opposed to
its documentation — is known first-hand here.

## Scope

The library implements everything that crosses the wire, and nothing
that doesn't:

**In scope**

* Framing: legacy CRC16 format and framing v2 with the BCH trailer,
  including negotiation state
  ([07-Link_Transport.md](07-Link_Transport.md)).
* The BCH encoder/decoder and the CRC16.
* VLQ argument encoding/decoding; message packing/unpacking against
  command descriptors.
* Sequence numbers, ack/nak, retransmit window state (as a pure state
  machine — the caller owns timers and I/O).
* Traffic-class tagging and per-class queue accounting
  ([03-Traffic_Classes.md](03-Traffic_Classes.md)).
* Segment payload codecs: `queue_traj_segment` coefficient
  quantization helpers, chained-position bookkeeping
  ([02-Intention_Protocol.md](02-Intention_Protocol.md)).
* Datagram transport helpers: datagram sequencing, erasure-FEC
  encode/decode, HMAC authentication
  ([07-Link_Transport.md](07-Link_Transport.md)).
* Data dictionary: format definition, generation helpers, and parser.

**Out of scope** (these live in the applications): scheduling, timers,
actuator backends, kinematics, storage, and all I/O — the library
never calls `read`, `write`, `malloc` (embedded profile), or sleeps.
It is a codec and a set of state machines; bytes in, bytes out,
caller-owned buffers.

## Two profiles, one source

* **Embedded profile**: no heap, no libc beyond `memcpy`-class
  functions, static or caller-provided buffers, const tables placed in
  flash, every function annotated for ISR-safety. Sized for the
  smallest fleet target — STM32F072 (16 KB RAM): the BCH GF(2¹⁰)
  log/antilog tables (~4 KB) live in flash, and working state per link
  fits in well under 1 KB.
* **Host profile**: the same core plus convenience allocation, a
  stable **versioned C API with real headers** (`intent_proto.h`,
  semantic-versioned ABI), and a thin Python binding generated from
  those headers.

The host's transmit machinery (today's `serialqueue.c` role) becomes a
consumer of the library rather than a second implementation of the
protocol; the differ ([06-Migration.md](06-Migration.md)) uses the
library as its codec layer, which means the library is exercised by
every validation run from day one. Fuzzing the frame parser and
dictionary parser is part of the library's own test suite — it is the
attack surface of the whole system.

## Design values (a deliberate contrast)

The current codebase makes protocol changes a specialist activity:
command registration happens through `DECL_*` linker-section
metaprogramming expanded by a build-time code generator, the host
binding is stringly-typed FFI with no versioned header, and the same
concept exists in two implementations that must be kept mentally
synchronized. Whatever the historical reasons, the effect is gating:
understanding the boundary requires apprenticeship in one codebase's
private idioms.

The library takes the opposite position, stated as rules:

* **One implementation of every concept.** If host and firmware both
  need it, it is in the library, once.
* **No linker magic.** Command tables are plain `const` arrays of
  plain structs, written where the compiler, the debugger, and a
  newcomer can see them. Registration is data, not macro expansion.
* **Boring, documented, replaceable interfaces.** Real headers, doc
  comments on every public symbol, a written wire specification that
  the implementation follows rather than *being*. A competent
  embedded developer should speak the protocol from the header and
  the spec in an afternoon, without reading our firmware.
* **The state machines are pure.** No I/O, no time, no allocation
  inside the library core — which is what makes it testable on a
  desktop, fuzzable in CI, and portable to anyone's RTOS or
  bare-metal loop.

## Licensing

* **The protocol library is MIT.** Deliberately: closed-source
  innovation on this ecosystem is welcome. A vendor can ship a
  closed-firmware sensor, drive, or toolboard that speaks the
  protocol natively — today's ecosystem shows the demand (commercial
  probes integrate via reimplementation and shims), and a permissive
  first-party library is the difference between "possible with
  lawyers" and "an afternoon with a header file".
* **The fork remains GPL** — both the host (`klippy/`) and the
  firmware (`src/`). The GPL applications link the MIT library; that
  direction is unconditionally fine.
* **The clean-room rule (hard constraint):** the library contains
  only original code. Nothing is copied or mechanically derived from
  the existing GPL sources — not a table, not a comment. This is
  practical because the v2 protocol is new work specified by these
  RFCs, and because **a complete original C implementation of the
  legacy protocol already exists**: the author wrote one
  independently for the OpenAMS boards' own (non-Klipper) firmware,
  working from the wire behavior rather than the GPL sources. That
  library is the seed of this one; the v2 features (framing v2/BCH,
  classes, segments, HMAC, dictionary v2) extend it rather than
  starting from zero. **Provenance confirmed by the author
  (2026-07):** the seed was implemented from observed wire behavior
  and is structurally unrelated to the GPL implementation — clearing
  it for MIT relicensing by its author. The format itself is not copyrightable; the
  implementation's originality is what the rule protects, and any
  future contribution is reviewed under it.
* The bootloader, being built on the library and similarly
  original, can also be MIT ([11-Bootloader.md](11-Bootloader.md)) —
  vendors shipping closed boards need a bootloader they can ship too.

## Repository layout

The library lives in the fork's tree (working name `lib/intentproto/`)
with its own LICENSE file, no include-path dependencies on `src/` or
`klippy/`, and a standalone build (single Makefile/CMake) proving it
compiles with nothing but a C compiler. CI builds it three ways: host
test/fuzz build, arm-none-eabi embedded build sized against the F072
budget, and as part of the firmware images.

## Open questions

* Final name and header prefix.
* Whether the Python binding is cffi against the installed headers or
  a generated ctypes shim (proposed: cffi, API mode).
* Whether legacy-format support belongs in the MIT library at all, or
  only v2 (proposed: include it — it is what makes the library useful
  to vendors *today*, before v2 hardware exists).
* Dictionary generation for non-C firmwares (Rust/MicroPython
  vendors): provide a JSON schema for the dictionary format.
