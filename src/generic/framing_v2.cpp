// C-callable shim over lib/intentproto's stateless framing-v2 (BCH) codec.
//
// Mirrors udp_datagram.cpp: it exposes intentproto's frame_v2_encode /
// frame_v2_decode (the BCH error-correcting console framing) to the C
// serial console-v2 glue (console_v2.c). The BCH code itself lives in the
// already-linked lib/intentproto (datagram.cpp + bch.cpp); nothing is
// reimplemented here.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "intentproto/datagram.hpp"

extern "C" {
#include "framing_v2.h"
}

static_assert(FV2_FLAG == (int)intentproto::FRAME_V2_FLAG,
              "v2 flag mismatch");
static_assert(FV2_OVERHEAD == (int)intentproto::FRAME_V2_OVERHEAD,
              "v2 overhead mismatch");

extern "C" uint32_t
fv2_encode(uint8_t *out, const uint8_t *payload, uint32_t len, uint8_t seq)
{
    return (uint32_t)intentproto::frame_v2_encode(out, payload, len, seq);
}

extern "C" int32_t
fv2_decode(uint8_t *frame, uint32_t len, const uint8_t **payload, uint8_t *seq)
{
    return (int32_t)intentproto::frame_v2_decode(frame, len, payload, seq,
                                                 nullptr);
}
