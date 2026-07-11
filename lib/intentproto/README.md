# intentproto

The single protocol library of the motion-intentions fork — see
[RFC 0001, doc 10](../../docs/rfcs/0001-motion-intentions/10-Protocol_Library.md).

**Status: prototype.** This is the working skeleton that validates the
library's two load-bearing decisions on real code:

1. **Freestanding C++ core** ("bare" subset: no heap, no exceptions,
   no RTTI, no virtual dispatch, no STL containers) implementing the
   legacy wire protocol: CRC16 framing, VLQ codec, frame RX state
   machine, ack/nack, block dispatch, identify serving, and the
   dictionary builder.
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
```

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
into `Config::identify_blob`.

## Build and test

```
make test        # desktop build + test suite (any g++/clang++)
make embedded    # Cortex-M0 compile + size report (arm-none-eabi-g++)
```

The tests port a slice of the OpenAMS firmware's command set as the
working example.

## Not yet implemented (tracked in RFC 0001 doc 10)

* String/buffer parameters in the declaration layer (`%.*s`) —
  supported in identify serving only.
* Enumerations in the dictionary.
* Retransmit-window state for the host side; this skeleton is the
  device side.
* Framing v2 (BCH), traffic-class accounting, datagram/HMAC
  transport, segment codecs — the v2 features arrive per
  [07](../../docs/rfcs/0001-motion-intentions/07-Link_Transport.md)
  and [02](../../docs/rfcs/0001-motion-intentions/02-Intention_Protocol.md).
* `extern "C"` API surface for C consumers and the host cffi binding.

## Caveats

* Declare each method/response/constant in exactly one translation
  unit (the descriptors are internal-linkage definitions; declaring
  in a header would register duplicates).
* Dictionary version strings are emitted unescaped and must be
  JSON-safe.
