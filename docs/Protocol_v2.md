# HELIX Protocol v2 (intentproto wire protocol)

This is the authoritative reference for **protocol v2**, the wire
protocol of the HELIX motion-intentions fork. It consolidates material
that was previously scattered across the founding document —
[07-Link_Transport](founding/0001-motion-intentions/07-Link_Transport.md),
[10-Protocol_Library](founding/0001-motion-intentions/10-Protocol_Library.md),
[03-Traffic_Classes](founding/0001-motion-intentions/03-Traffic_Classes.md) —
and grounds every claim in the reference implementation, the
`intentproto` library
([lib/intentproto/](../lib/intentproto/), and its
[README](../lib/intentproto/README.md)).

The audience is someone implementing or debugging the protocol on
either side of the wire. Where this document and a founding-document section disagree on a
concrete value, this document (and the code it cites) is correct: the
The founding-document sections recorded *proposals*; the numbers below are what shipped.

For the legacy Klipper MCU protocol that v2 extends, see the companion
document [Protocol.md](Protocol.md). Protocol v2 is a strict,
backwards-compatible superset of it.

> **Status (HELIX 0.9):** implemented and host-tested; not yet
> hardware-validated. See [Status and what is proven](#status-and-what-is-proven).

> **As-built — how klippy speaks v2 (read this before the details).**
> The application and host keep stock Klipper's v1 command path (so upstream
> merges cleanly); intentproto adds transport/auth/FEC as an **envelope
> around unchanged v1 frames** — a stateless **framing transform**, not
> intentproto's `HostSession` (which *replaces* v1's ARQ and is only for
> `proto.cpp`-cored peers like the bootloader). Two envelope modes:
>
> 1. **Datagram transport (network links)** — authenticated, erasure-FEC UDP
>    datagrams (`datagram.cpp`) wrapping whole v1 frames. **End-to-end
>    today:** MCU side `udp_console.c`; host side the klippy transport
>    bridge (`klippy/intentproto_transport.py`,
>    `[intentproto_transport]`). This is where v2's value lives (lossy /
>    untrusted WiFi/Ethernet).
> 2. **Console framing (byte-stream UART links)** — each v1 frame re-framed
>    with a BCH trailer (`frame_v2_encode/decode`). **Host side implemented
>    and tested** (`BchConsoleCodec`, loopback round-trip); **MCU side is
>    Kconfig-gated (`WANT_CONSOLE_FRAMING_V2`, off by default) and
>    compile-checked but hardware-unproven** (`src/generic/console_v2.c`).
>
> The `HostSession` / `proto.cpp` `rx()` dual-dispatch path described in
> parts of this document is the **library/bootloader** engine, *not* the
> app/host path. The app path is the framing transform above. See
> [Upstream_Tracking](Upstream_Tracking.md).

---

## 1. Overview and design goals

Protocol v2 is not a new protocol. It is a **backwards-compatible
extension of Klipper's existing frame**, negotiated per link, with the
legacy framing retained as the permanent bootstrap format and fallback.
That single property is load-bearing and everything else is built to
preserve it:

* A v2-capable device accepts **both** framings at all times. A legacy
  host talking to a v2 device never knows the difference.
* A legacy device talking to a v2 host **provably rejects** a v2 frame
  (it naks it), so a v2 host can *probe* an unknown peer without ever
  corrupting the session — the worst case is a few retransmits before
  it falls back to legacy.
* The choice is **per link**. A machine may run its main MCU over USB
  in legacy framing and a WiFi toolboard in v2 simultaneously; nothing
  couples them.

The concrete additions v2 brings over legacy are:

1. **Forward error correction in the frame** (framing v2): a shortened
   binary BCH trailer replaces the CRC-16, correcting small bit-error
   bursts in place instead of forcing a nak/retransmit round-trip
   ([§5](#5-framing-v2-the-bch-fec-trailer)).
2. **A single implementation** of the whole wire protocol — device and
   host — as a freestanding C++ library
   ([§4](#4-message-ids-the-annotation-layer-and-the-served-dictionary)),
   replacing the two hand-synchronised codebases (firmware `command.c`
   and host `msgblock.c`/`serialqueue.c`) that legacy Klipper keeps in
   agreement by discipline alone.
3. **A datagram transport** (UDP over WiFi/Ethernet) with datagram
   sequencing, XOR erasure FEC, and mandatory per-packet authentication
   ([§8](#8-the-datagram-transport-udp--ethernet--wifi)).
4. **Traffic classes** distinguished by failure semantics, so a late
   LED update can no longer shut down a print
   ([§7](#7-traffic-classes-0--1--2)).
5. **Session security** as an optional, negotiated upgrade over the
   authentication floor ([§9](#9-session-security-the-dtls-class-upgrade)).

The core library is *freestanding* C++: no heap, no exceptions, no
RTTI, no virtual dispatch, no STL containers. Bytes in, bytes out,
caller-owned buffers; the caller owns all timers and I/O. This is what
lets the identical code run in the host, the firmware, and the
bootloader, and lets the whole thing be unit-tested on a desktop.

---

## 2. The legacy frame and the compatibility hook

The legacy Klipper frame — reproduced exactly by v2 — is
([lib/intentproto/include/intentproto/proto.hpp](../lib/intentproto/include/intentproto/proto.hpp)):

```
<1 byte len> <1 byte seq> <payload…> <2 byte CRC16> <1 byte sync 0x7E>
```

with these constants (`proto.hpp`):

| Constant | Value | Meaning |
| --- | --- | --- |
| `MESSAGE_MAX` | 64 | maximum whole-frame length |
| `MESSAGE_MIN` | 5 | len + seq + crc16 (2) + sync |
| `HEADER_SIZE` | 2 | len, seq |
| `TRAILER_SIZE` | 3 | crc16 (2) + sync |
| `PAYLOAD_MAX` | 59 | `MESSAGE_MAX - MESSAGE_MIN` |
| `MESSAGE_DEST` | `0x10` | fixed bit in the seq byte |
| `MESSAGE_SEQ_MASK` | `0x0f` | 4-bit sequence field |
| `MESSAGE_SYNC` | `0x7E` | frame terminator |

`len` counts the *whole* frame including itself and the trailer. The
sequence byte is `MESSAGE_DEST | (sequence & MESSAGE_SEQ_MASK)`, i.e.
bit 4 always set, bits 0–3 the 4-bit sequence. **Bits 5, 6, 7 (`0xE0`)
are reserved and legacy firmware requires them to be zero** — it
rejects and naks any frame whose seq byte has them set.

That reservation is the compatibility hook. Protocol v2 marks a v2
frame by setting the top bit of the seq byte:

```cpp
// lib/intentproto/include/intentproto/datagram.hpp
constexpr uint8_t FRAME_V2_FLAG = 0x80;   // seq-byte bit 7
```

Because legacy firmware treats `0x80` as an illegal seq byte and naks
it, a v2 frame can *never* be misread by a legacy peer as a valid
legacy frame — it is rejected cleanly and the ARQ layer retransmits.
This is what makes probing safe ([§6](#6-negotiation-and-fallback)).

### The receive state machine

Both sides run the same three-state byte-at-a-time frame parser
([src/proto.cpp](../lib/intentproto/src/proto.cpp) `rx()`, mirrored in
[src/host.cpp](../lib/intentproto/src/host.cpp) `on_rx()`):

* **Length** — the first non-sync byte is the frame length; it must be
  in `[MESSAGE_MIN, MESSAGE_MAX]` or it is a framing error and the
  parser drops to resync. Idle `0x7E` bytes between frames are skipped.
* **Body** — accumulate `len` bytes.
* On a complete frame the trailer is checked. The last byte must be
  `MESSAGE_SYNC`; then the seq byte's `FRAME_V2_FLAG` selects which
  trailer to verify — legacy CRC-16 or the v2 BCH parity. A bad frame
  is nak'd (device) or dropped for ARQ (host); a good frame's payload
  is dispatched.

The sync byte is a *trailer*, so a fresh link starts in the Length
state ready to read a length byte; garbage resynchronises through the
error path (`RxState::Sync`, which waits for the next `0x7E`).

---

## 3. CRC-16 and the VLQ codec

### The CRC-16 (reflected MCRF4XX)

The frame check used by legacy framing is the **reflected CRC-16/MCRF4XX**:
polynomial `0x8408` (the bit-reversed `0x1021`), initial value `0xFFFF`,
LSB-first (reflected) processing, no final XOR. Its check value over
the ASCII string `"123456789"` is **`0x6F91`**. This is the variant
Klipper's wire actually uses — it is **not** MSB-first CCITT-FALSE.

```cpp
// lib/intentproto/src/proto.cpp
uint16_t crc16_ccitt(const uint8_t* buf, size_t len) {
    uint16_t crc = 0xffff;
    while (len--) {
        crc ^= *buf++;
        for (int i = 0; i < 8; i++)
            crc = (crc & 1) ? (uint16_t)((crc >> 1) ^ 0x8408)
                            : (uint16_t)(crc >> 1);
    }
    return crc;
}
```

The CRC covers `len`, `seq`, and the payload (everything except the two
CRC bytes and the sync). It is transmitted **big-endian**: `frame[len-3]
= crc >> 8`, `frame[len-2] = crc & 0xff`.

> Implementers copying from the header should note the function is
> *named* `crc16_ccitt` for historical continuity, and one stale header
> comment still describes it as MSB-first CCITT-FALSE. The **body in
> `proto.cpp` is authoritative** and is MCRF4XX (poly `0x8408`, reflected,
> check `0x6F91`), matching the Klipper wire. Compute your check value
> against `0x6F91`, not the CCITT-FALSE `0x29B1`.

### The VLQ integer codec

All integer arguments are encoded with Klipper's variable-length
quantity (VLQ): 7 data bits per byte, the high bit set on every byte
except the last, most-significant group first. The leading group is
**sign-extended** — a decoder that sees bits 5–6 of the first group
both set treats the value as negative — so the encoder always picks the
shortest length whose leading group round-trips the sign:

| bytes | signed range |
| --- | --- |
| 1 | `[-2^5, 3·2^5)` = `[-32, 96)` |
| 2 | `[-2^12, 3·2^12)` |
| 3 | `[-2^19, 3·2^19)` |
| 4 | `[-2^26, 3·2^26)` |
| 5 | everything else (full 32-bit) |

```cpp
// lib/intentproto/src/proto.cpp — vlq_encode / vlq_decode
```

**Worked example — encode `1000`:** `1000` needs 2 bytes (it is
≥ `3·2^5 = 96` but < `3·2^12`). High group: `(1000 >> 7) & 0x7f = 7`,
OR the continuation bit → `0x87`. Low group: `1000 & 0x7f = 104 = 0x68`.
So `1000` → **`87 68`**. Decoding: `0x87` has the continuation bit, value
`7`; `0x68` clears it, value `(7 << 7) | 0x68 = 1000`.

**Worked example — encode `-1`:** fits in 1 byte; the byte is
`(-1) & 0x7f = 0x7F`. Decoding `0x7F`: value `0x7F`, and because bits
5–6 of the leading group are set the decoder sign-extends
(`v |= 0xFFFFFFE0`), giving `0xFFFFFFFF = -1`.

`vlq_encode` writes at most 5 bytes; `vlq_decode` is bounded (returns
false on truncated input, guarding at 5 bytes) so a malformed frame can
never run the parser off the end of the buffer.

A buffer parameter (the legacy `%.*s` wire type) is a VLQ length prefix
followed by that many raw bytes. Inside a command handler the data
pointer aliases the receive buffer and is valid only for the duration
of the call.

---

## 4. Message ids, the annotation layer, and the served dictionary

### Declaring a command

Legacy Klipper registers commands with `DECL_COMMAND` linker-section
metaprogramming expanded by a build-time code generator. `intentproto`
replaces that with **annotation-style static registration**: the
annotation *itself* emits the metadata when it expands, so declaring a
command is one macro plus the body, with nothing else anywhere — no
table, no registration call, no build step, and nothing that ever
parses your source
([include/intentproto/method.hpp](../lib/intentproto/include/intentproto/method.hpp)):

```cpp
#include <intentproto/method.hpp>

KLIPPER_RESPONSE(oams_action_status,
                 (uint8_t, action), (uint8_t, code), (uint32_t, value));

KLIPPER_METHOD(oams_cmd_load_spool, (uint8_t, spool)) {
    if (busy) {
        intentproto::reply(oams_action_status{ACTION_LOAD, ERR_BUSY, 0});
        return;
    }
    start_load(spool);
}

KLIPPER_CONSTANT(CLOCK_FREQ, 48000000);
KLIPPER_ENUMERATION(oams_error, jammed, 1);
```

The macro family is:

| Macro | Emits |
| --- | --- |
| `KLIPPER_METHOD(name, (type,arg)…) { … }` | a command handler + descriptor |
| `KLIPPER_METHOD0(name) { … }` | a zero-parameter command |
| `KLIPPER_RESPONSE(name, (type,field)…);` | a response struct + packer |
| `KLIPPER_RESPONSE0(name);` | a zero-field response (ack-style) |
| `KLIPPER_CONSTANT(NAME, int)` / `KLIPPER_CONSTANT_STR(NAME, "s")` | dictionary constant |
| `KLIPPER_ENUMERATION(enum, value, n)` | one enumeration value |
| `KLIPPER_ENUMERATION_STR(enum, uid, "display name", n)` | enum value whose display name is not a token |
| `KLIPPER_ENUMERATION_EMPTY(enum)` | a value-less enumeration group (`"pin":{}`) |

Parameter **types** are deduced from the function's real signature by
the `Thunk<&fn>` template (`method.hpp`), so they can never drift from
the code. Parameter **names** appear once in the macro, because C++ has
no reflection over parameter names. Supported wire types are
`uint8_t / int8_t / uint16_t / int16_t / uint32_t / int32_t / bool /
intentproto::buf`; anything else is a compile error.

### Self-registration and id assignment

Each macro drops a plain `static` descriptor (`Command`, `Response`,
`Constant`, or `Enumeration` from `proto.hpp`) next to the function.
The descriptor's constructor links it into an intrusive, heap-free
registry list **before `main()`** (static initialisation). Nothing runs
a registration pass; the linked list simply exists by the time
`init()` is called.

`intentproto::init()` freezes the registry, reverses the lists back
into definition order (static init builds them head-first), and assigns
wire ids in definition order starting at `MSGID_FIRST_FREE = 2` —
commands first, then responses. Ids 0 and 1 are fixed by the legacy
protocol (`identify_response` = 0, `identify` = 1). `init()` also
registers, through the ordinary registry, the library-owned
`FRAMING_V2 = 1` capability constant and the five extension
self-description meta-messages, so they appear in the dictionary like
any other declaration.

### The served dictionary

The identify dictionary is a **serialization of that live registry** —
data to data, never a scrape of source
([src/dict.cpp](../lib/intentproto/src/dict.cpp) `build_dictionary()`).
For the `oams_cmd_load_spool` command above, with id 2, the dictionary
entry is:

```json
"commands": { "oams_cmd_load_spool spool=%c": 2, … }
```

and `oams_action_status` (a response) appears under `"responses"` as
`"oams_action_status action=%c code=%c value=%u": <id>`. The key string
(`name field=%fmt …`) is built by the shared `message_key()` so the
JSON dictionary and the extension-descriptor stream can never disagree.
Format specifiers come from `format_of()`: `%c` (u8/i8/bool), `%hu`/`%hi`
(u16/i16), `%u`/`%i` (u32/i32), `%.*s` (buf).

A legacy klippy host wants this dictionary zlib-compressed into
`Config::identify_blob` at build time; `tools/mkdict.py` runs a binary
linked against the firmware's declaration TUs to produce
`identify_blob.h` plus an `identify.json` for inspection. The library
serves the blob in chunks in response to the core `identify` command.

### The v2 core id space and extension self-description

In v2 the dictionary is **demoted**. The core command set every board
must answer is frozen at fixed ids in the spec header
([include/intentproto/core_ids.hpp](../lib/intentproto/include/intentproto/core_ids.hpp)),
so a v2 peer needs no dictionary round-trip at all (VLQ encoding makes
dense per-build numbering worthless anyway):

| id range | contents |
| --- | --- |
| 0–1 | `identify_response`, `identify` (legacy, retained) |
| 2–9 | clock / uptime / config / stats |
| 10–17 | trajectory intentions (doc 02) |
| 18–21 | execution log (doc 08) |
| 22–23 | heater failure-policy hold (doc 08) |
| 24–26 | hardware trigger sources (doc 09) |
| 27–31 | bootloader / in-band update (doc 11) |
| 32–36 | extension self-description meta-commands (doc 10) |
| 37–0x7F | reserved for future core commands |
| ≥ `0x80` | `MSGID_EXTENSION_BASE` — device-specific extension space |

Device-specific commands live in the extension space and describe
themselves as *data* over two library-owned meta-commands (so a v2 host
needs no JSON dictionary):

* `list_extensions start=%u count=%c` → one `extension_desc kind=%c
  id=%u desc=%.*s` per registered command (kind 0) and response (kind 1)
  in the requested window; `desc` is the same `message_key()` string.
* `list_constants start=%u count=%c` → one `constant_desc kind=%c
  desc=%.*s` per constant (kind 0 int / 1 str) and enumeration value
  (kind 2).

`count` is clamped to `EXTDESC_COUNT_MAX = 8` per call; when the window
reaches the end of the registry the entries are followed by
`extension_done total=%u`, and the host paginates `start += count`
until it sees it. On a legacy link these same messages carry
`init()`-assigned ids; the two numbering schemes never mix on one link.
The host-side reference binding is `tools/extbind.py`.

---

## 5. Framing v2: the BCH FEC trailer

### Why FEC

The CRC-16 only *detects* errors; every hit forces a nak → retransmit
round-trip. On a high-latency link (WiFi, a long serial run) each
round-trip is a bite out of the refill horizon. Framing v2 replaces the
CRC with a code that *corrects* small errors in place, so a lightly
damaged frame is accepted immediately and the retransmit path is used
only for damage beyond the code's reach. ARQ is **retained** — FEC
reduces its use, it does not replace it.

### The frame

```
<len> <seq | 0x80> <payload…> <BCH parity: 4 bytes> <sync 0x7E>
```

```cpp
// lib/intentproto/include/intentproto/datagram.hpp
constexpr uint8_t FRAME_V2_FLAG = 0x80;
constexpr size_t  FRAME_V2_OVERHEAD = 7;  // len, seq, parity[4], sync
```

Framing v2 spends `FRAME_V2_OVERHEAD = 7` bytes instead of legacy's
`MESSAGE_MIN = 5` — two more bytes of trailer (4 parity bytes vs 2 CRC
bytes) — leaving `MESSAGE_MAX - 7 = 57` bytes of payload.

### The code

The trailer is a **shortened binary BCH code over GF(2¹⁰)**, natural
length n = 1023 bits, shortened to the frame length, correcting
**t = 3** bit errors
([include/intentproto/bch.hpp](../lib/intentproto/include/intentproto/bch.hpp),
[src/bch.cpp](../lib/intentproto/src/bch.cpp)):

| Parameter | Value |
| --- | --- |
| Field | GF(2¹⁰), primitive poly `p(x)=x¹⁰+x³+1` (`0x409`) |
| Generator | `g(x) = lcm(m₁, m₃, m₅)`, degree 30, `= 0x50A91113` |
| Design distance | 7 (corrects t = 3) |
| Parity | 30 bits, packed MSB-first into 4 bytes (`BCH_PARITY_BYTES = 4`); the 2 low bits of the last byte are spare zeros |
| Max protected data | `BCH_DATA_MAX = 61` bytes |

The codeword is systematic: it covers `len`, `seq`, and the payload
(the parity protects the header too), so a bit error anywhere in the
frame — including the parity bytes themselves — is correctable. The
generator polynomial, the GF log/antilog tables (~4 KB), and the
nibble-at-a-time division table are all `constexpr`-generated at build
time from the field mathematics; nothing is transcribed from an
external script. A `static_assert` pins `g(x) = 0x50A91113` so a drift
is a compile error.

### Encode / decode cost

* **Encode** and the **fast decode path** are table-driven `mod g(x)`
  division — two nibble lookups per byte, comparable to a table-driven
  CRC. On decode, `s(x) = v(x) mod g(x)` is computed the same way; `s ==
  0` means the frame is a valid codeword and it is accepted immediately.
  This is the dominant path (a few µs per frame).
* **The correction path runs only on damaged frames**, in task context,
  where a few hundred µs is irrelevant against the multi-millisecond
  retransmit it avoids. It computes syndromes S₁,S₃,S₅ (evens follow
  from the binary Frobenius identity S₂ⱼ = Sⱼ²), runs Berlekamp–Massey
  for the error-locator polynomial, does a Chien search over the
  shortened bit positions, **verifies the candidate pattern reproduces
  the received syndromes before touching the frame**, then flips the
  located data bits. If the locator degree, root count, or verification
  fails, the frame is declared uncorrectable and returns −1.

On the wire that −1 becomes: device side naks (`send_nack()`), host
side drops the frame for ARQ to recover. `LinkStats` on the device
tracks both outcomes — `bch_errors` (uncorrectable, nak'd) and
`bch_corrected` (bit errors repaired in accepted frames).

`frame_v2_encode` / `frame_v2_decode` in
[src/datagram.cpp](../lib/intentproto/src/datagram.cpp) wrap the codec
into a frame: encode places `len`, `seq | 0x80`, the payload, then BCH
parity over `len+seq+payload`, then the sync byte; decode validates the
sync, BCH-decodes-and-corrects in place, and re-checks that `frame[0]`
still equals the length and the v2 flag is set before delivering the
payload.

---

## 6. Negotiation and fallback

> **Scope note.** This section describes negotiation between intentproto's
> **host session** (`host.cpp`, cffi-reachable) and a **device that links
> intentproto's core** (`rx()`) — i.e. the bootloader, third-party
> firmware, and the desktop tests. It is **not** wired into the klippy
> host or the stock application firmware in 0.9. Making a *klippy app
> board* negotiate console-v2 is the envelope-shim work in
> [§12](#12-status-and-what-is-proven) — the host side hangs the same
> `session_enable_v2()` machinery *below* `serialhdl` (wrapping stock
> command blocks), and the MCU side generalizes `udp_console.c`'s
> de-frame→stock-dispatch pattern to the console. Until then the text
> below is the library's behaviour, not a running printer's.

Negotiation has four moving parts and is driven entirely from the host;
the device is passive.

1. **Advertisement.** The device advertises framing v2 as the
   dictionary constant `FRAMING_V2 = 1`, registered by `init()` itself,
   which rides the identify dictionary transferred in legacy framing.
   (The founding document originally sketched a string constant `LINK_FEC="bch10_t3"`;
   the implementation ships the integer `FRAMING_V2 = 1`.)
2. **Probe.** After reading `FRAMING_V2` from the dictionary the caller
   promotes the host session with `session_enable_v2()`, which enters
   the `Probing` state. From then on the host's frames carry the BCH
   trailer and the `FRAME_V2_FLAG` seq bit. The session never parses
   dictionaries itself — the caller decides when to promote.
3. **Latch.** The **device** latches to v2 on the **first valid v2
   frame** it decodes: `g_link.v2 = true`, and every subsequent transmit
   (acks, naks, responses, identify) switches to the BCH trailer. The
   device never auto-downgrades — only `init()` resets it. The **host**
   confirms the upgrade when the peer answers with any valid v2 frame,
   moving `Probing → V2`, which is likewise sticky.
4. **Automatic fallback.** A legacy peer naks every v2 frame and can
   never answer in v2. The host counts consecutive rejections (naks, or
   RTO expiries with no v2 reply) while `Probing`; after
   `HOST_V2_PROBE_LIMIT = 4` of them it falls back to `Legacy`, latches
   `v2_rejected` for the caller to inspect, and retransmits the same
   window in legacy framing.

The host's tx framing is a three-state machine
([include/intentproto/host.hpp](../lib/intentproto/include/intentproto/host.hpp)):

```
Legacy --session_enable_v2()--> Probing --valid v2 rx--> V2
  ^                                 |
  +-- HOST_V2_PROBE_LIMIT rejects --+   (v2_rejected latched)
```

In-flight frames are stored as **payloads** and re-framed per the
current tx framing at every (re)transmit, which is exactly what lets a
probe fallback resend the same window in legacy framing. Probing is
safe precisely because of the compatibility hook ([§2](#2-the-legacy-frame-and-the-compatibility-hook)):
a legacy peer keeps the payload flowing via retransmit even while it
rejects the v2 trailer. Both sides accept **both** framings at all
times; the seq byte's `FRAME_V2_FLAG` tells the receiver which trailer
to check per frame.

Because the choice is per link and negotiated independently in each
direction from the same PSK-free mechanism, a mixed fleet (USB legacy
mainboard + WiFi v2 toolboard) just works.

---

## 7. Traffic classes (0 / 1 / 2)

Protocol v2 distinguishes three traffic classes **by failure
semantics**, not priority
([03-Traffic_Classes.md](founding/0001-motion-intentions/03-Traffic_Classes.md)).
Class is a **static property of the message id** — it costs zero wire
bytes because both ends know each id's class from the dictionary /
spec.

| Class | Name | Traffic | Delivery guarantee |
| --- | --- | --- | --- |
| 0 | **Scheduled** | trajectory segments/rebase, trsync arm, endstop sampling config — anything whose correctness *is* its timing | acked, retransmitted, ordered; the only class allowed to insert hard timers; a missed schedule is still a shutdown |
| 1 | **Prompt** | pin/PWM/fan/LED writes, config, queries and replies, MCU→host events (underrun, faults) | acked and reliable, executed on arrival from task context; **no failure mode shuts the machine down** (late-OK) |
| 2 | **Telemetry** | ADC/temperature reports, trajectory status, diagnostics, live execution-log stream | best-effort; rate-limited at the source, droppable under congestion; every producer keeps a visible drop counter |

The mapping onto transports is the point:

* On the framed/acked byte-stream transport (UART/USB/CAN and the
  legacy window), all three classes share the one seq/ack/retransmit
  machinery.
* On the datagram transport, **Class 0 and 1 are acked and
  erasure-protected**; **Class 2 may be sent as unacked datagrams** and
  simply lost under congestion — the class semantics were designed for
  exactly this.

The library carries the class as a tag per in-flight frame and
accounts it per class. `TrafficClass { Scheduled=0, Prompt=1,
Telemetry=2 }` and `ClassStats { tx_msgs, tx_bytes, rx_msgs, rx_bytes,
dropped }` live in
[datagram.hpp](../lib/intentproto/include/intentproto/datagram.hpp);
the host session records a `TrafficClass` per window slot
(`HostSession::class_of(seq)`) so a datagram binding can map each frame
to its datagram class, and keeps `class_stats[3]`.

---

## 8. The datagram transport (UDP / Ethernet / WiFi)

The datagram transport makes WiFi- and Ethernet-attached MCUs
first-class citizens
([src/datagram.cpp](../lib/intentproto/src/datagram.cpp)). An honest
note frames its design: on a UDP/WiFi path the 802.11 MAC already
CRC-checks and locally retransmits every radio frame, so a datagram
that *arrives* rarely has bit errors — the dominant impairments are
**whole-datagram loss and reordering**. Intra-frame BCH ([§5](#5-framing-v2-the-bch-fec-trailer))
and packet-level erasure FEC are therefore *independent* layers for
*different* failure modes.

### Datagram layout

A datagram is **one or more whole frames** (batching amortises
per-packet overhead the way 64-byte blocks amortise per-byte overhead):

```
[u16 seq][u8 flags][payload: whole frames][8-byte HMAC tag]
```

```cpp
// datagram.hpp
constexpr size_t  DATAGRAM_HEADER = 3;    // u16 seq + u8 flags
constexpr size_t  DATAGRAM_TAG    = 8;    // truncated HMAC-SHA256
constexpr size_t  DATAGRAM_MAX    = 1472; // typical UDP payload MTU
constexpr uint8_t DGF_CLASS_MASK  = 0x03; // flags bits 0-1: traffic class
constexpr uint8_t DGF_PARITY      = 0x04; // bit 2: XOR parity datagram
constexpr uint8_t DGF_AUTH        = 0x08; // bit 3: authenticated
constexpr uint8_t DGF_SESSION     = 0x10; // bit 4: session-protected (§9)
constexpr uint8_t DGF_PARITY_LENGTHS = 0x20; // bit 5: length-aware parity
// bits 6-7 reserved
```

* **Datagram sequence.** A 16-bit sequence prepended per datagram
  detects loss and reorder at the transport layer and widens the
  effective window. The in-frame 4-bit sequence is untouched (it was
  sized for wired RTTs). The receiver syncs on the first datagram, then
  counts `lost` (positive gaps) and `reordered` (a stale/duplicate
  sequence, dropped).
* **XOR erasure FEC.** With `fec_k > 0`, the tx side folds every data
  datagram (header + frames, pre-auth) into a running XOR accumulator
  and, after each block of `k`, emits a **parity datagram** (`DGF_PARITY`).
  Its body is `[u16 xor_of_protected_lengths][xor_bytes...]`; the explicit
  `DGF_PARITY_LENGTHS` format bit makes older parity bodies degrade cleanly
  to ARQ instead of being misparsed by a new receiver. FEC-enabled data
  bodies are limited to 1459 bytes so the two-byte length field and HMAC
  remain within the 1472-byte UDP payload ceiling.
  If exactly one datagram of a block is lost, the receiver XORs the
  survivors it held against the parity to **reconstruct the missing
  datagram without waiting out a retransmit timeout** — trading
  bandwidth (1/k) for latency (an RTO). This is single-loss recovery
  (`datagram_parity_flush`, and the `rx->held` survivor buffer);
  `datagram_take_recovered()` fetches the rebuilt datagram.
* **Authentication floor.** Outside an explicit `trust_network` mode
  (selected by passing `psk_len == 0`), **every datagram is
  authenticated**: `seal()` appends a truncated 8-byte HMAC-SHA256 over
  the whole datagram and sets `DGF_AUTH`. On receive, a datagram
  without `DGF_AUTH`, or one whose tag fails a constant-time compare, is
  rejected and counted in `auth_failures`. The tag kills both forgery
  and blind replay; it costs a few µs on any 32-bit MCU, negligible
  against WiFi latency. The unauthenticated `trust_network` mode is a
  deliberate confession for lab benches and isolated VLANs, not a
  default.

Ethernet is the preferred wired network transport — the same UDP
binding runs over it unchanged, differing from WiFi only in the loss
model (the erasure layer can typically be negotiated off). UART, USB
and CAN remain fully supported.

---

## 9. Session security (the DTLS-class upgrade)

The static-PSK HMAC floor ([§8](#8-the-datagram-transport-udp--ethernet--wifi))
is mandatory and the default. Layered *over* it, and entirely optional,
is a negotiated session-security upgrade
([include/intentproto/session_sec.hpp](../lib/intentproto/include/intentproto/session_sec.hpp),
[src/session_sec.cpp](../lib/intentproto/src/session_sec.cpp)) that
delivers the four properties FD-0001 doc 07 named for "heavier machinery":
session keys, key rotation, per-board identity, and replay protection.

### Scope and threat model — AUTH-ONLY

This is **not** IETF DTLS 1.3. Full DTLS in a freestanding, no-heap,
no-STL library would drag in X.509/ASN.1, an AEAD suite, cookie
exchange and a full key schedule — thousands of lines this fork could
not honestly audit. Instead the layer is purpose-built from the
primitives already in the library and is **authentication-only, not
confidentiality**: like the static path it authenticates each datagram
(now with a rotating per-session key) but does **not** encrypt the
payload. That is deliberate — the stated threat is **forgery and blind
replay of motion/heater commands** by anything on the network segment,
not secrecy of the commands themselves. A confidentiality layer (an
HKDF-derived keystream XORed over the payload) could be added later on
the same schedule without re-framing, but it buys nothing against the
stated threat while adding a nonce-reuse footgun to unverifiable
freestanding crypto.

Like `host.hpp`, `SecureSession` is a pure state machine: no heap, no
I/O, no clock, no RNG. Entropy enters only as an argument — the caller
draws this peer's 16-byte nonce from its own RNG and passes it to
`init()`.

### Key schedule (HKDF-SHA256)

HKDF-SHA256 (RFC 5869), built entirely from the library's own
HMAC-SHA256 ([src/hmac.cpp](../lib/intentproto/src/hmac.cpp)), turns the
static PSK plus the exchanged nonces into independent, rotatable traffic
keys — none of which is the raw PSK, so the PSK never rides on a data
packet:

* `PRK = HKDF-Extract(salt = client_random ‖ server_random, IKM = PSK)`.
* `tx_key / rx_key = HKDF-Expand(PRK, info = label ‖ epoch)` with
  direction labels `"intentproto c2s v1"` / `"intentproto s2c v1"` and
  the 32-bit epoch appended. Selecting the label by role makes one
  peer's tx key equal the other's rx key at the same epoch.
* The handshake proof key uses the `"intentproto finished v1"` label.

### The 3-message PSK handshake

```
Initiator ── ClientHello  (type 0x51, ver=2, id_len, client_random, board_id, PSK proof) ──▶ Responder
Initiator ◀── ServerHello (type 0x52, …, server_random, board_id, Finished MAC) ── Responder
Initiator ── ClientFinished (type 0x53, Finished MAC) ──▶ Responder
```

Both hellos carry a **per-board identity** (`SEC_ID_MAX = 24` bytes),
exposed to the caller via `peer_id()`. ClientHello ends with a 16-byte
HMAC-SHA256 proof over its complete prefix under the configured PSK; the
responder verifies it in constant time before copying the nonce/identity,
deriving keys, or emitting a reply. The ServerHello's 16-byte
Finished MAC binds both nonces and both identities under the finished
key; the initiator verifies it constant-time before deriving traffic
keys, and the ClientFinished MAC proves the initiator to the responder.
A peer that does not support the session layer simply never answers
with a ServerHello, so the initiator never reaches `Established`;
`downgrade()` records that the caller fell back to the static-PSK path.

### Session datagrams, epochs, and replay

Session-protected datagrams set `DGF_SESSION` (flags bit 4); the
static-PSK codec never sets or inspects it. The session datagram header
is 6 bytes — `flags`, an epoch byte, and a 32-bit per-epoch sequence —
followed by the frames and an 8-byte HMAC tag keyed by the current
epoch's `tx_key`.

* **Key rotation** happens on an epoch bump: automatically when the
  per-epoch sequence crosses `rekey_threshold` (default
  `SEC_DEFAULT_REKEY = 2²⁰` datagrams), or on an explicit `rekey()`. The
  tx side increments its epoch and re-derives `tx_key`; the peer follows
  on the epoch byte of the next datagram.
* **Replay protection** is a **64-entry sliding window** over the
  per-epoch sequence (`rx_window_top` + a 64-bit `rx_window_bits`
  mask). A sequence at or below the window that is already set, or
  further back than 64, is rejected (`replays_rejected`).
* **Epoch safety.** The receiver verifies the tag under the key for the
  datagram's *stated* epoch **before** trusting anything in the header,
  so a forged high epoch cannot reset the replay window. An older epoch
  is stale and rejected (`old_epoch_rejected`); an authenticated newer
  epoch adopts the new key and restarts the window on that datagram.

A live `SecureSession` (both directions, keys, epochs, replay window)
costs **264 bytes of RAM per link** on the STM32F072 floor.

### Handshake hardening (as built)

The responder's handshake surface is hardened against unauthenticated
UDP traffic. Protocol version 2 requires a ClientHello PSK proof, so a
spoofed/random hello receives no reply and cannot occupy half-open state or
move the reply peer. A valid ClientHello can never reset a **live** session:
while established, it drives a *pending* handshake that replaces the live
keys only when ClientFin completes (this is also how a restarted klippy
reconnects without a board reboot). A repeated valid hello is idempotent, a
different hello cannot replace an active candidate, every accepted hello
uses a fresh responder nonce, and incomplete state expires after two seconds.
A peer that holds the PSK can still deny service, but it already has authority
to authenticate command traffic; unauthenticated on-segment traffic no longer
has that leverage. All of this is exercised live by
[test/datagram_session_live_test.py](../test/datagram_session_live_test.py)
(hostile PSK-invalid hello against a live session, then an immediate
legitimate adopted re-handshake).

### As built: both ends are wired

The session is no longer library-only. **On the board**, a datagram
console built with `CONFIG_WANT_DATAGRAM_SESSION` (Kconfig option
"DTLS-class session over the UDP datagram transport") runs the responder:
`udp_console.c` classifies each inbound datagram (handshake / session /
static), answers a `ClientHello` with a `ServerHello`, and once
established seals all replies as session datagrams — a host that never
opens a session keeps using the static-PSK path, so the upgrade is
strictly additive. Once a session is established, the board rejects static
data until reboot or an authenticated re-handshake, preventing downgrade
around identity, replay protection, and rotating keys. Each board carries a
distinct identity via
`CONFIG_DATAGRAM_SESSION_ID`. **On the host**, `[intentproto_transport]`
with `session: True` runs the initiator: the bridge completes the
3-message handshake at `open()` before its pump starts, then routes the
v1 byte stream through the session's `encode`/`decode` instead of the
static codec. Both directions were exercised end to end over a real UDP
socket against `linuxprocess` firmware
([test/datagram_session_live_test.py](../test/datagram_session_live_test.py))
and in a firmware-free host loopback
([test/session_bridge_test.py](../test/session_bridge_test.py)).

---

## 10. The CAN carrier

CAN is a **byte-stream carrier below the framing**, exactly as UART and
USB are ([include/intentproto/can_transport.hpp](../lib/intentproto/include/intentproto/can_transport.hpp),
[src/can_transport.cpp](../lib/intentproto/src/can_transport.cpp)).
Because protocol v2 reproduces the legacy CRC16/VLQ framing, it rides
CAN the same way legacy Klipper does: a whole protocol frame is split
into ≤8-byte CAN data frames on transmit, and on receive the frames are
forwarded straight to `intentproto::rx()`, which already reassembles
across CAN-frame boundaries — so there is **no receive reassembly
buffer**.

Two consequences, both realised:

* **The fork's own micro-controller commands ride CAN with no new
  code.** `trajq`, `execlog`, `trigger_source`, `heater_hold`,
  `timesync` and the rest are ordinary `DECL_COMMAND`s; they traverse a
  CAN link through the existing `canserial.c` reassembly and
  `command_dispatch` path unchanged.
* **The library gains a matching CAN carrier.** `CanCarrier::write_frame()`
  (plugged into `Config::write` via `can_write_thunk`) chunks outgoing
  frames onto the device's tx id; `on_can_frame()` forwards incoming
  data frames to `rx()`.

Node addressing mirrors Klipper's UUID admin handshake so an
`intentproto` device is a drop-in CAN peer: the host queries unassigned
nodes on `CAN_ID_ADMIN` (`0x3F0`), the device answers on
`CAN_ID_ADMIN_RESP` (`0x3F1`) with its 6-byte UUID, the host assigns a
1-byte node id, and data then flows on the derived identifiers —
host→device on `0x100 + 2n`, device→host on `0x100 + 2n + 1`. The
carrier is 362 bytes of Cortex-M0 code and is transport-agnostic (the
caller supplies the `send` hook). On CAN, the traffic classes
([§7](#7-traffic-classes-0--1--2)) can optionally map onto CAN-ID
priority bits, giving Class 0 physical-layer arbitration precedence.

---

## 11. Signed images (a different guarantee)

Transport authentication ([§8](#8-the-datagram-transport-udp--ethernet--wifi),
[§9](#9-session-security-the-dtls-class-upgrade)) proves *who sent a
datagram*. **Image signing** proves *what firmware a board is allowed to
run* — a different guarantee, at a different layer. It lives in the
bootloader ([11-Bootloader.md](founding/0001-motion-intentions/11-Bootloader.md)),
above the transport.

The bootloader verifies an **Ed25519 (RFC 8032)** signature over the
application image before boot
([lib/intentproto/src/ed25519.cpp](../lib/intentproto/src/ed25519.cpp),
`sha512.cpp`); `BOOT_INFO_FLAG_SIGNED` marks a signed image and the
64-byte signature is stored in the image info block. This closes the
"unsigned image swap over the update channel" threat — an attacker who
can reach the flash commands cannot make the board run an image the
fleet operator did not authorise. Transport auth and image signing are
complementary: one guards the channel, the other guards the payload's
provenance.

---

## 12. The host session

The host side of the link is a single retransmit-window state machine
([include/intentproto/host.hpp](../lib/intentproto/include/intentproto/host.hpp),
[src/host.cpp](../lib/intentproto/src/host.cpp)) — the counterpart to
the device side in `proto.cpp`. It is pure: no heap, no I/O, no time
reads. Time enters only as the `now_ticks` argument to
`need_retransmit()`.

* **Sequence assignment.** Frame sequence numbers are 4-bit on the wire,
  extended to 64 bits internally. `send_seq` is the next frame to send,
  `receive_seq` the lowest unacked. The device acks with the next
  sequence it *expects*, so one ack covers everything before it.
* **In-flight window.** Up to `HOST_WINDOW = 12` unacked frames (kept
  below the 16-value sequence space so acks stay unambiguous), stored as
  payloads in a ring indexed by `seq % HOST_WINDOW` and re-framed at
  every transmit.
* **Go-back-N.** A nak — signalled by the device rewinding its ack
  sequence, or a duplicate empty-frame ack — arms `nak_pending`; on the
  next `need_retransmit()` poll, or when the RTO expires, **every**
  unacked frame is retransmitted in order, preceded by a lone sync byte
  so a receiver stuck mid-frame can resynchronise.
* **Framing negotiation** ([§6](#6-negotiation-and-fallback)) is driven
  from here: `session_enable_v2()`, the `Legacy/Probing/V2` machine, and
  the `v2_rejected` fallback latch.
* **Per-class accounting.** `class_stats[3]` and a `TrafficClass` per
  window slot feed the datagram class mapping.

The device side (`proto.cpp`) is the mirror: it tracks `last_rx_seq`,
acks with `(last_rx_seq + 1)` (`send_ack()`), naks by rewinding
(`send_nack()`), and dispatches each message in a validated block to the
registered handler.

---

## 13. A worked round-trip

Trace one command end to end: the host sends `oams_cmd_load_spool
spool=3` (id 2) over a link that has negotiated framing v2, and the
device replies `oams_action_status`.

1. **Host annotation → payload.** The caller VLQ-encodes the message:
   msgid `2` → `0x02`, arg `spool=3` → `0x03`. Payload = `02 03`.
2. **Frame (v2).** `HostSession::send_command(payload, 2,
   TrafficClass::Prompt)` copies the payload into window slot
   `send_seq % 12`, records the class, and calls `xmit()`. In the `V2`
   framing state that is `frame_v2_encode`: `len = 2 + 7 = 9`,
   `seq = 0x10 | seq_nibble | 0x80`, payload `02 03`, then 4 BCH parity
   bytes over `len+seq+payload`, then `7E`. The 9-byte frame goes out
   the write hook.
3. **Wire → device.** Bytes arrive in any chunking at
   `intentproto::rx()`. The state machine reads the length, accumulates
   the body, checks the trailing `7E`, sees `FRAME_V2_FLAG` set, and
   calls `frame_v2_decode` — a single fast `mod g(x)` pass (`s == 0`),
   no correction needed. First valid v2 frame would latch the device to
   v2; here it already is.
4. **Dispatch.** `process_block()` records `last_rx_seq`, then
   `dispatch_one()` VLQ-decodes msgid `2`, finds the `Command` for
   `oams_cmd_load_spool`, decodes its one `u8` argument into an
   `ArgWord`, and calls the `Thunk` trampoline, which narrows the word
   and invokes the handler `oams_cmd_load_spool(3)`.
5. **Response.** The handler calls `intentproto::reply(oams_action_status{…})`.
   `send_response()` writes the response id and packs the fields with
   the descriptor's `pack()` function, then `tx_frame()` emits it in the
   device's latched v2 framing (BCH trailer), with
   `seq = (last_rx_seq + 1) | MESSAGE_DEST`.
6. **Ack.** After dispatching the block the device also `send_ack()`s —
   an empty frame whose seq byte is `(last_rx_seq + 1)`. Back at the
   host, `on_rx()` decodes the device frames: the response frame is
   delivered through `on_response(payload, len)`, and the ack advances
   `receive_seq`, freeing the window slot and clearing the retransmit
   deadline. If the frame had instead failed BCH decode, the device
   would have nak'd (rewound seq) and the host's go-back-N would resend
   the window.

The same trace in legacy framing differs only in steps 2/3/5: a 2-byte
CRC-16 trailer instead of 4 BCH parity bytes, and no `FRAME_V2_FLAG`.

---

## 14. Status and what is proven

Protocol v2 is **implemented and host-tested; not yet hardware-validated**
(HELIX 0.9). The library is a freestanding C++ core that compiles for
the desktop (any g++/clang++), for a Cortex-M0 size report
(arm-none-eabi), and behind a versioned C ABI ([capi.h](../lib/intentproto/include/intentproto/capi.h))
with a cffi Python binding.

The desktop test suite exercises every layer described here
([lib/intentproto/tests/](../lib/intentproto/tests/)):

| Test | Covers |
| --- | --- |
| `test_proto` | codecs, registry, framing, dispatch, identify serving, the dictionary builder (a real OpenAMS command slice) |
| `test_bch` | BCH encode/syndrome/Berlekamp–Massey/Chien, correction up to t = 3, uncorrectable detection |
| `test_host` | host session loopback against the device `rx()`: sequence assignment, window, ack/nak, go-back-N |
| `test_datagram` | framing v2 round-trip, datagram sequencing, XOR erasure recovery, HMAC auth |
| `test_negotiate` | the doc-07 negotiation path: legacy default, dictionary-driven upgrade, BCH avoiding retransmits, ARQ fallback, `v2_rejected` legacy-peer fallback |
| `test_hmac` | SHA-256 (FIPS 180-4 vectors), HMAC-SHA256 (RFC 4231), HKDF (RFC 5869) |
| `test_session_sec` | PSK handshake, session datagram round-trip, replay rejection, epoch rotation, per-board identity, downgrade, forgery rejection |
| `test_can_transport` | UUID admin handshake, ≤8-byte frame chunking, full host→CAN→dispatch→reply→CAN→host round trip |

The **live v2 wiring into the running system is now implemented** as the
envelope framing transform (see the as-built callout):

- **Host:** the klippy transport bridge
  ([klippy/intentproto_transport.py](../klippy/intentproto_transport.py),
  configured by `[intentproto_transport]`) sits *below* the serial fd via a
  PTY and re-frames each stock v1 frame to v2 on the wire (datagram or BCH
  console), leaving `serialqueue`/`serialhdl.py`/`msgproto.py`
  byte-identical to upstream. Tested by loopback
  ([test/intentproto_transport_test.py](../test/intentproto_transport_test.py)):
  exact v1 reconstruction both directions, chunked input, resync bytes,
  datagram auth tamper-rejection, and a real-PTY bridge round-trip.
- **MCU:** the datagram side ships in `udp_console.c` (network links,
  end-to-end). The **console-BCH** side (`src/generic/console_v2.c`) is
  `WANT_CONSOLE_FRAMING_V2`-gated, off by default, and **LIVE-tested in
  emulation**: the linuxprocess console carries the same hooks, and
  [test/console_v2_live_test.py](../test/console_v2_live_test.py) proves
  dual-accept (v1 in → v1 out), the v2 latch (v2 in → v2 out), and BCH
  correction of 3 flipped bits against real firmware over a PTY. The
  silicon UART call sites (`serial_irq.c`) still await a devkit.

The capability handshake exists: a console-BCH board advertises
`FRAMING_V2 = 1` in the identify dictionary (`DECL_CONSTANT`), which
`helix_status.py` and the host bridge read. The founding-document items
formerly listed as remaining are now **implemented and host-tested**: the
segment payload codecs
([02-Intention_Protocol.md](founding/0001-motion-intentions/02-Intention_Protocol.md))
live in the library as `segment.hpp` (bit-identity guarded by
[test/segment_lib_test.py](../test/segment_lib_test.py)), and the
extension-space connect-time host binding ships as
`intentproto.ExtBinding`/`bind_host_session()` (see the library README).
What remains is **hardware bring-up** — running the already-tested paths
on silicon. PSK provisioning is handled by
[scripts/gen_psk.py](../scripts/gen_psk.py) (per-board printable keys
shared between the host `psk_file` and the board's build config). See the
intentproto [README](../lib/intentproto/README.md) for the current
caveats.

---

## See also

* [Protocol.md](Protocol.md) — the legacy Klipper MCU protocol that v2 extends.
* [lib/intentproto/README.md](../lib/intentproto/README.md) — the reference implementation.
* [FD-0001 doc 03 — Traffic Classes](founding/0001-motion-intentions/03-Traffic_Classes.md)
* [FD-0001 doc 07 — Link Transport](founding/0001-motion-intentions/07-Link_Transport.md)
* [FD-0001 doc 10 — Protocol Library](founding/0001-motion-intentions/10-Protocol_Library.md)
* [FD-0001 doc 11 — Bootloader](founding/0001-motion-intentions/11-Bootloader.md)
</content>
</invoke>
