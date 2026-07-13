# intentproto

The single protocol library of the motion-intentions fork — see
[FD-0001, doc 10](../../docs/founding/0001-motion-intentions/10-Protocol_Library.md).
For the full wire-protocol reference this library implements, see
[Protocol v2](../../docs/Protocol_v2.md).

**Status: prototype.** This is the working skeleton that validates the
library's two load-bearing decisions on real code:

1. **Freestanding C++ core** ("bare" subset: no heap, no exceptions,
   no RTTI, no virtual dispatch, no STL containers) implementing the
   legacy wire protocol: CRC16 framing, VLQ codec, frame RX state
   machine, ack/nack, block dispatch, identify serving, the
   dictionary builder, and the host-side retransmit-window session
   (`host.hpp` — sequence assignment, in-flight window, go-back-N).
   Both sessions negotiate framing v2 (BCH t=3 FEC trailer) per
   [FD-0001 doc 07](../../docs/founding/0001-motion-intentions/07-Link_Transport.md):
   the device advertises `FRAMING_V2` in its dictionary and latches
   on the first valid v2 frame; the host probes after
   `session_enable_v2()` and falls back to legacy automatically
   (`v2_rejected`) when a legacy peer keeps nak'ing the probe.
   `send_command()` records a traffic class per in-flight frame for
   the datagram transport binding, with per-class `ClassStats`.
2. **Annotation-style static registration** instead of code
   generation. Declaring a command is one macro plus the body — no
   table, no registration call, no build step, and nothing ever
   parses your source code:

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
KLIPPER_ENUMERATION(static_string_id, oams_jammed, 1);
```

Buffer parameters (the legacy `%.*s` wire type) are declared as
`(intentproto::buf, data)` — a VLQ length prefix followed by raw
bytes; inside a handler the data pointer aliases the receive buffer
for the duration of the call.

Parameter **types** are deduced from the function signature by a
template (they can never drift from the code); parameter **names**
appear once in the macro (C++ has no reflection over parameter
names). Each macro drops a plain static descriptor next to the
function; its constructor links it into an intrusive registry before
`main()`. `intentproto::init()` freezes the registry and assigns wire
ids in definition order.

The identify dictionary is a **serialization of that registry**
(`build_dictionary()` — data to data), not a scrape of the source: a
legacy klippy host needs the output zlib-compressed at build time
into `Config::identify_blob`. `tools/mkdict.py` performs that step:
it runs a binary built from `tools/dump_dict.cpp` linked with the
firmware's declaration TUs and writes `identify_blob.h` plus the
`identify.json` for inspection (`make dict` demonstrates it with
example declarations).

The fixed ids of the v2 core command set are documented as a header
in `include/intentproto/core_ids.hpp` (ids >= 0x80 are reserved for
extension self-description).

## Build and test

```
make test        # desktop build + test suite (any g++/clang++)
make capi        # host-profile C ABI + cffi binding round-trip tests
make embedded    # Cortex-M0 compile + size report (arm-none-eabi-g++)
make dict        # example identify-blob build (tools/mkdict.py)
```

The **host profile** (FD-0001 doc 10) is exposed through a versioned,
C-linkage ABI in `include/intentproto/capi.h`
(`INTENTPROTO_ABI_VERSION` + `intentproto_abi_version()`), implemented
as a thin `extern "C"` shim in `src/capi.cpp` over the C++ core: host
session, framing/VLQ/CRC and framing-v2 codecs, datagram tx/rx, and
the registry / extension-descriptor accessors. `tests/test_capi.c`
compiles as C and drives a host-session loopback against the device
`rx()` to prove the header is valid C and the ABI links. The Python
binding under `python/intentproto/` is **cffi, API mode** (doc 10's
resolved open question): it builds `src/*.cpp` + `capi.cpp` into an
extension that `#include`s `capi.h`, so the header is the single
source of truth. `make capi` runs both round-trip tests and skips the
Python one politely when cffi is absent.

The tests port a slice of the OpenAMS firmware's command set as the
working example.

## CAN transport (FD-0001 doc 07)

`can_transport.hpp` binds the framed byte stream to a CAN bus the way
legacy Klipper does — CAN is a byte-stream carrier *below* the
CRC16/VLQ framing. `CanCarrier::write_frame()` (plugged into
`Config::write` via `can_write_thunk`) splits an outgoing frame into
≤8-byte CAN data frames on the device's tx id; `on_can_frame()`
forwards incoming data frames straight to `rx()`, which already
reassembles across CAN-frame boundaries, so there is no receive buffer.
Node addressing mirrors Klipper's UUID admin handshake
(`query-unassigned` → UUID reply → 1-byte node-id → data on
`0x100+2n` / `0x100+2n+1`), making an intentproto device a drop-in CAN
peer. `test_can_transport` drives the admin assignment, the frame
chunking, and a full host-command → CAN → dispatch → reply → CAN →
host-decode round trip. The carrier is 362 bytes of Cortex-M0 code and
is transport-agnostic (the caller supplies the `send` hook).

## Session security (optional)

The datagram transport authenticates every packet with a truncated
HMAC-SHA256 keyed by a **static** PSK — that is the mandatory floor
(FD-0001 doc 07) and the default, and it is untouched. On top of it,
`session_sec.hpp` adds the *negotiated* upgrade FD-0001 doc 07 had deferred as
"heavier machinery (DTLS, key rotation, per-board identities)":

* **HKDF-SHA256** (`hmac.hpp`, RFC 5869, built from the existing HMAC)
  derives per-session traffic keys from the PSK plus exchanged nonces,
  so the raw PSK never rides on a data packet.
* A **3-message PSK handshake** (`ClientHello` / `ServerHello` /
  `ClientFinished`) establishes independent tx/rx keys and carries a
  **per-board identity**. It is a pure state machine like `host.hpp`:
  no heap, no I/O, no clock, no RNG — the caller feeds it its own
  nonce and bytes; it emits bytes and reaches `Established`.
* **Key rotation** by an epoch bump (a datagram-count threshold or an
  explicit `rekey()`), **replay protection** by a 64-entry sliding
  window over the per-epoch sequence, and **downgrade** to the static
  path when a peer does not answer the offer.

Scope and threat model are argued at the top of `session_sec.hpp`:
this is a purpose-built authenticated session, **not** full IETF DTLS
1.3 (still deferred — it would be unverifiable in a freestanding,
no-heap library), and it is **auth-only** — like the static path it
authenticates but does not encrypt, because the stated threat is
forgery/replay of motion commands, not payload secrecy. Session
datagrams are flagged with `DGF_SESSION` (flags bit 4); the static-PSK
codec never sets or inspects it. A live `SecureSession` (both
directions, keys, epochs and the replay window) costs 264 bytes of
RAM per link on the STM32F072 floor.

## Trajectory segment codec (FD-0001 doc 02)

`segment.hpp` gives a **third-party trajectory peer** the two pieces of
the motion-intent protocol that must be bit-exact, so it can emit
`queue_traj_segment` payloads and track position identically to the MCU
without vendoring klippy:

* **Coefficient quantization** — `segment_quantize(true_value, order_k)`
  maps a true per-tick polynomial derivative to its wire int32 (scale
  `2^(16k)`, round half away from zero, saturated), matching
  `segfit.c`'s `quantize()`/`bezier_to_wire()`.
* **Chained-position bookkeeping** — `segment_end_delta()` and the
  `SegmentChain` accumulator reproduce the exact truncate-toward-zero
  Q32.32 integration of `src/trajq.c:trajq_end_delta_seg()`, so a peer's
  end position never drifts from where the board integrates the segment.
* **Payload codec** — `segment_encode()`/`segment_encode_hold()`/
  `segment_decode()` build and parse the VLQ payloads (coefficient count
  follows the flags' polynomial-order bits).

The bit-identity against both klippy references (`segfit.c` and
`trajectory_queuing.py`) is asserted by `test/segment_lib_test.py`; the
firmware's own `traj_kernel` golden vectors are re-checked by
`tests/test_segment.cpp`. Exposed through the C ABI (`ip_segment_*`) and
the Python binding (`intentproto.segment_*` / `SegmentChain`).

## Connect-time extension binding (FD-0001 doc 10)

A v2 peer serves its command/response/constant registry as **data** over
two library-owned meta-commands (`list_extensions` / `list_constants`,
`proto.cpp` + `core_ids.hpp`) — no zlib dictionary round-trip. The host
binds to it at connect: `intentproto.ExtBinding` streams the descriptors
into typed encoders and parsers, so after `query()` a caller does
`ext.encode_command(name, **kwargs)` and `ext.parse_response(payload)`,
and reads `ext.constants` / `ext.enums`. Extension commands live in the
`>= 0x80` id space (`MSGID_EXTENSION_BASE`); the meta-commands self-
describe all the way down.

The packaged API (`python/intentproto/extbind.py`) reuses the C-backed
VLQ codec and adds `HostSessionTransport` / `bind_host_session()` to drive
the enumeration over a real `HostSession`; `python/test_extbind_py.py`
proves it against an in-process device. `tools/extbind.py` remains as a
dependency-free (stdlib-only) reference of the same protocol.

## Feature-complete

Every FD-0001 doc-10 library surface is now implemented and tested:
framing v2 (BCH), traffic classes, the UDP datagram layer with its
truncated-HMAC authentication and XOR erasure FEC, the DTLS-class
session upgrade, extension self-description with the host `ExtBinding`,
the trajectory segment codec, Ed25519 signed images in `boot/bootcore`,
the `extern "C"`/cffi host binding, and the CAN carrier. The datagram
transport is bound to a `HostSession`'s framed byte stream by
`DatagramCarrier` (`datagram_carrier.hpp`) — the host-side complement of
the device's `udp_console.c` — so a host can run a full ARQ session over
UDP without re-implementing the datagram accounting (test:
`tests/test_datagram_carrier.cpp`).

## Caveats

* Declare each method/response/constant in exactly one translation
  unit (the descriptors are internal-linkage definitions; declaring
  in a header would register duplicates).
* Dictionary version strings are emitted unescaped and must be
  JSON-safe.
