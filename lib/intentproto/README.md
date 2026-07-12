# intentproto

The single protocol library of the motion-intentions fork — see
[RFC 0001, doc 10](../../docs/rfcs/0001-motion-intentions/10-Protocol_Library.md).

**Status: prototype.** This is the working skeleton that validates the
library's two load-bearing decisions on real code:

1. **Freestanding C++ core** ("bare" subset: no heap, no exceptions,
   no RTTI, no virtual dispatch, no STL containers) implementing the
   legacy wire protocol: CRC16 framing, VLQ codec, frame RX state
   machine, ack/nack, block dispatch, identify serving, the
   dictionary builder, and the host-side retransmit-window session
   (`host.hpp` — sequence assignment, in-flight window, go-back-N).
   Both sessions negotiate framing v2 (BCH t=3 FEC trailer) per
   [RFC 0001 doc 07](../../docs/rfcs/0001-motion-intentions/07-Link_Transport.md):
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

The **host profile** (RFC 0001 doc 10) is exposed through a versioned,
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

## Not yet implemented (tracked in RFC 0001 doc 10)

* Segment payload codecs (`queue_traj_segment` coefficient
  quantization, chained-position bookkeeping) per
  [02](../../docs/rfcs/0001-motion-intentions/02-Intention_Protocol.md).
* Binding the datagram/HMAC transport (`datagram.hpp`) to the
  sessions' framed byte streams (framing v2 and traffic classes are
  wired into the negotiation path; the UDP datagram layer still
  rides standalone).
* v2 extension self-description (the >= 0x80 id space of
  `core_ids.hpp`) and the connect-time host binding.

## Caveats

* Declare each method/response/constant in exactly one translation
  unit (the descriptors are internal-linkage definitions; declaring
  in a header would register duplicates).
* Dictionary version strings are emitted unescaped and must be
  JSON-safe.
