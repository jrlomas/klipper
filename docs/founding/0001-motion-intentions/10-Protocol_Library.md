# FD-0001: The Protocol Library

Status: Implemented in HELIX 0.9 (software complete; hardware bring-up pending)

> For the full, implementation-grounded reference of the v2 wire
> protocol this library implements — framing, codecs, the annotation
> layer, negotiation, datagrams, and session security — see
> [Protocol v2](../../Protocol_v2.md). This document is the library's
> design rationale and scope.

Today the wire protocol is implemented **twice**: once in the firmware
(`src/command.c`, `src/msgblock` constants) and once in the host's C
helper (`klippy/chelper/msgblock.c`, `serialqueue.c`) — two disjoint
codebases that duplicate the framing constants, the CRC, the VLQ
codec, and the sequence-number rules, kept in agreement only by
discipline. Any third party who wants to speak the protocol (a probe
vendor, a custom toolboard, a test harness) gets neither: they
reimplement from documentation.

This document specifies **one protocol library** — a single,
self-contained, MIT-licensed implementation of the entire wire
protocol (freestanding-profile C++ behind a C-linkage API — see the
language decision below), consumed by the host, by our firmware, by
the bootloader ([11-Bootloader.md](11-Bootloader.md)), and by anyone
else's firmware or tooling, open or closed.

The demand is proven, not speculative: Annex Engineering's *Anchor*
is an independent implementation of the MCU side of the Klipper
protocol, written in Rust, built precisely so custom hardware could
join the ecosystem without running Klipper's firmware. This library
serves the same need with a C-linkage API — linkable from any C or
C++ firmware including our own bootloader (building it requires a
C++17 toolchain, which every GCC/Clang on the 32-bit floor provides) —
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
  stable **versioned C API with real headers**
  ([lib/intentproto/include/intentproto/capi.h](../../../lib/intentproto/include/intentproto/capi.h),
  semantic-versioned ABI — `INTENTPROTO_ABI_VERSION` plus a runtime
  `intentproto_abi_version()` the binding checks for a major
  mismatch), and a thin Python binding generated from those headers.
  Both now exist: `capi.h` is a C-linkage shim
  ([src/capi.cpp](../../../lib/intentproto/src/capi.cpp)) over the C++
  core exposing the host session, the framing/VLQ/CRC and framing-v2
  codecs, the datagram tx/rx binding, and the registry /
  extension-descriptor accessors; it is validated from a pure-C
  translation unit
  ([tests/test_capi.c](../../../lib/intentproto/tests/test_capi.c),
  a host-session loopback against the device `rx()`). The Python
  binding
  ([lib/intentproto/python/intentproto](../../../lib/intentproto/python/intentproto/__init__.py))
  is cffi in API mode, building `src/*.cpp` + `capi.cpp` into an
  extension that `#include`s `capi.h` — the header, not a hand-copied
  signature, is the source of truth. `make capi` builds the shared
  object and runs the C and Python round-trip tests.

The host's transmit machinery (today's `serialqueue.c` role) becomes a
consumer of the library rather than a second implementation of the
protocol; the differ ([06-Migration.md](06-Migration.md)) uses the
library as its codec layer, which means the library is exercised by
every validation run from day one. Fuzzing the frame parser and
dictionary parser is part of the library's own test suite — it is the
attack surface of the whole system.

## Language and declarations (decided 2026-07)

The protocol is dictionary-driven and the implementation languages
are non-reflective, so *something* must produce the typed glue
between wire messages and handler functions. Exactly four mechanisms
exist, and the OpenAMS firmware's history is a tour of them:

1. **Scrape the source** (the OpenAMS libclang/lark generators): the
   C++ signature is the single source of truth — the right property,
   bought at the price of owning a compiler front-end and emitting
   the protocol core itself from a template.
2. **An external IDL** with a generator: trivial tooling, but the
   protocol now lives in a second artifact and the application calls
   generated stubs — rejected as trading one indirection for another.
3. **Macro metaprogramming (X-macros/FOR_EACH) in plain C**: no
   tools, but the machinery is the least readable of the four and
   lands in application code.
4. **Static registration in C++**: the annotation *itself* emits the
   metadata when it expands. This is what Java's `@annotation`
   actually is — metadata emitted where the runtime can find it —
   done at compile/link time because there is no VM.

**Decision: option 4.** The library is implemented in *bare* C++ — a
freestanding subset: no heap, no exceptions, no RTTI, no virtual
dispatch, no STL containers; templates and `constexpr` are allowed as
compile-time machinery only. Declaring a device command is one
annotation plus the body, at the definition site, with nothing else
anywhere:

```cpp
KLIPPER_RESPONSE(oams_action_status,
                 (uint8_t, action), (uint8_t, code), (uint32_t, value));

KLIPPER_METHOD(oams_cmd_load_spool, (uint8_t, spool)) {
    if (busy) {
        intentproto::reply(oams_action_status{ACTION_LOAD, ERR_BUSY, 0});
        return;
    }
    start_load(spool);
}
```

The macro defines the function exactly as written and drops a plain
static descriptor next to it; the descriptor self-registers into an
intrusive, heap-free list before `main()`. Parameter **types** are
deduced from the function's real signature by a template — they
cannot drift from the code, which preserves the property that made
the source-scraping approach attractive. Parameter **names** appear
once in the annotation, because no usable C++ standard has reflection
over parameter names. Wire ids are assigned at `init()` in definition
order. The descriptors are ordinary structs, visible in a debugger.

Consequences:

* **The dictionary is served, not scraped.** The identify JSON is a
  *serialization of the live registry* — data to data — compressed at
  build time by a tool that links the same tables the firmware ships.
  Nothing in any build ever parses application source code.
* **In the v2 protocol the dictionary is demoted further:** the core
  command set gets fixed ids in the spec (VLQ encoding makes dense
  per-build numbering worthless anyway), so a v2 peer needs no
  dictionary round-trip at all; device-specific extension commands
  self-describe over a fixed meta-command, and the host — Python,
  where dynamism is native — binds to them at connect. The full JSON
  dictionary remains only for the legacy protocol.
* **Core protocol is implemented, not declared.** Commands that every
  board must answer (clock, uptime, config, stats, identify) are
  library code with semantics, not application boilerplate — the
  annotation layer is only for what is genuinely device-specific.
* **C consumers keep a C API**: the core is exposed behind
  `extern "C"` headers for the host binding and for third-party C
  firmware; the annotation layer is a C++-only convenience, and the
  same registry can be filled by hand with plain structs.

A working skeleton implementing this — legacy framing, VLQ, dispatch,
identify, dictionary builder, and the annotation layer, with a
desktop test suite that ports a slice of the OpenAMS command set —
lives in [lib/intentproto/](../../../lib/intentproto/).

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
* **No linker magic, no source scraping.** Registration is static
  data: self-registering descriptor records created at the
  declaration site (see the language decision above), visible to the
  compiler, the debugger, and a newcomer. No linker-section string
  tables, and no build step that parses application source.
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
  practical because the v2 protocol is new work specified by this
  founding document, and because **a complete original C implementation of the
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
* ~~Whether the Python binding is cffi against the installed headers or
  a generated ctypes shim (proposed: cffi, API mode).~~ **Resolved
  (2026-07): cffi, API mode.** Implemented in
  [lib/intentproto/python/intentproto](../../../lib/intentproto/python/intentproto/__init__.py):
  cffi compiles a real C++ extension that `#include`s `capi.h`, so a
  drifting ABI is a build error rather than a silently wrong signature.
  The binding mirrors klippy/chelper's build-on-demand pattern (mtime
  check, compile, cached module) but replaces its stringly-typed
  ABI-mode `cdef` with the versioned header as the single source of
  truth. Callbacks cross the boundary via cffi's `extern "Python"`.
* ~~Whether legacy-format support belongs in the MIT library at all, or
  only v2.~~ **Resolved: legacy framing is included** — it is what makes
  the library useful to vendors *today*, before v2 hardware exists.
* Dictionary generation for non-C firmwares (Rust/MicroPython
  vendors): provide a JSON schema for the dictionary format.
* Declaration layer: string/buffer parameters (`%.*s`) and enumerations
  are **implemented** (`intentproto::buf`; the `KLIPPER_ENUMERATION*`
  macros). A guard against declaring the same method in two translation
  units is deliberately not added — documented as a caveat instead.
* ~~The exact fixed-id allocation for the v2 core command set.~~
  **Resolved: frozen in
  [core_ids.hpp](../../../lib/intentproto/include/intentproto/core_ids.hpp)**
  — ids 2..36 are the spec-frozen core space, ≥ `0x80` is the
  self-describing extension space.
