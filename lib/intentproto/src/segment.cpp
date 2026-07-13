// Trajectory segment payload codec (FD-0001 doc 02). See segment.hpp.
//
// The integer chaining below is a line-for-line port of the truncate-
// toward-zero fixed-point arithmetic in src/trajq.c and
// klippy/chelper/segfit.c. It MUST stay bit-identical to both; the
// library and firmware tests assert exactly that.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "intentproto/segment.hpp"
#include "intentproto/core_ids.hpp"
#include "intentproto/proto.hpp" // vlq_encode / vlq_decode

namespace intentproto {

// ---- exact integer chaining (identical to trajq.c / segfit.c) ----

static int64_t
mul64x32_half(int64_t a, uint32_t b)
{
    int neg = a < 0;
    uint64_t ua = neg ? -(uint64_t)a : (uint64_t)a;
    uint64_t lo = (ua & 0xffffffff) * b;
    uint64_t hi = (ua >> 32) * b;
    hi += lo >> 32;
    lo &= 0xffffffff;
    uint64_t r = (hi << 31) | (lo >> 1);
    return neg ? -(int64_t)r : (int64_t)r;
}

static int64_t
traj_end_delta(uint32_t duration, int32_t velocity, int32_t accel)
{
    int64_t delta = ((int64_t)velocity * duration) << 16;
    if (accel)
        delta += mul64x32_half((int64_t)accel * duration, duration);
    return delta;
}

static int64_t
smul_shr(int64_t a, uint32_t t, unsigned sh)
{
    int neg = a < 0;
    uint64_t ua = neg ? -(uint64_t)a : (uint64_t)a;
    uint64_t lo = (ua & 0xffffffff) * t;
    uint64_t hi = (ua >> 32) * t;
    hi += lo >> 32;
    lo &= 0xffffffff;
    uint64_t r = sh ? ((hi << (32 - sh)) | (lo >> sh)) : ((hi << 32) | lo);
    return neg ? -(int64_t)r : (int64_t)r;
}

static int64_t
poly_term(int64_t coeff, uint32_t t, int nmul, int nsh, uint32_t fact)
{
    int64_t p = coeff;
    for (int i = 0; i < nmul; i++)
        p = smul_shr(p, t, i < nsh ? 16 : 0);
    if (fact > 1) {
        int neg = p < 0;
        uint64_t up = neg ? -(uint64_t)p : (uint64_t)p;
        up /= fact;
        p = neg ? -(int64_t)up : (int64_t)up;
    }
    return p;
}

int64_t
segment_end_delta(uint32_t duration, int32_t velocity, int32_t accel,
                  int32_t jerk, int32_t snap, int32_t crackle)
{
    int64_t d = traj_end_delta(duration, velocity, accel);
    d += poly_term(jerk, duration, 3, 1, 6);
    d += poly_term(snap, duration, 4, 2, 24);
    d += poly_term(crackle, duration, 5, 3, 120);
    return d;
}

int64_t
segment_chain_advance(SegmentChain *ch, uint32_t duration, int32_t velocity,
                      int32_t accel, int32_t jerk, int32_t snap,
                      int32_t crackle)
{
    ch->acc += segment_end_delta(duration, velocity, accel, jerk, snap,
                                 crackle);
    return ch->acc >> 32;
}

// ---- coefficient quantization ----
int32_t
segment_quantize(double true_value, unsigned order_k)
{
    // scale = 2^(16*k). Use ldexp-free doubling to keep this freestanding.
    double scale = 1.0;
    for (unsigned i = 0; i < 16u * order_k; i++)
        scale *= 2.0;
    double x = true_value * scale;
    // Saturate to int32 on the double BEFORE any integer cast: for a
    // coefficient already at/over the rail, rounding only pushes it
    // further out, and the pre-round value could overflow int64. This
    // reproduces trajectory_queuing.py's traj_round()+_clamp_i32() at the
    // boundaries (round then clamp) without the intermediate big-int.
    if (x >= 2147483647.0)
        return 2147483647;
    if (x <= -2147483648.0)
        return (int32_t)(-2147483647 - 1);
    // |x| < 2^31 now, so the int64 round is safe. Round half away from
    // zero, matching C round() used by the fitter.
    if (x >= 0)
        return (int32_t)(int64_t)(x + 0.5);
    return -(int32_t)(int64_t)(-x + 0.5);
}

// ---- payload codec ----

static unsigned
order_ncoeff(uint8_t flags)
{
    switch (flags & SEG_POLY_MASK) {
    case SEG_POLY_QUADRATIC: return 2;  // v, a
    case SEG_POLY_CUBIC:     return 3;  // v, a, j
    case SEG_POLY_QUINTIC:   return 5;  // v, a, j, s, c
    default:                 return 0;  // reserved order
    }
}

size_t
segment_encode(uint8_t *out, size_t cap, uint8_t oid, uint8_t flags,
               uint32_t duration, int32_t velocity, int32_t accel,
               int32_t jerk, int32_t snap, int32_t crackle)
{
    unsigned nc = order_ncoeff(flags);
    if (!nc)
        return 0;  // reserved / invalid polynomial order
    // Worst case: msgid + oid + flags + duration + 5 coeffs, 5 bytes each.
    uint8_t buf[5 * 9];
    uint8_t *p = buf;
    p = vlq_encode(p, v2::MSGID_QUEUE_TRAJ_SEGMENT);
    p = vlq_encode(p, oid);
    p = vlq_encode(p, flags);
    p = vlq_encode(p, duration);
    const int32_t coeffs[5] = {velocity, accel, jerk, snap, crackle};
    for (unsigned i = 0; i < nc; i++)
        p = vlq_encode(p, (uint32_t)coeffs[i]);
    size_t n = (size_t)(p - buf);
    if (n > cap)
        return 0;
    for (size_t i = 0; i < n; i++)
        out[i] = buf[i];
    return n;
}

size_t
segment_encode_hold(uint8_t *out, size_t cap, uint8_t oid, uint32_t duration)
{
    uint8_t buf[5 * 3];
    uint8_t *p = buf;
    p = vlq_encode(p, v2::MSGID_TRAJ_HOLD);
    p = vlq_encode(p, oid);
    p = vlq_encode(p, duration);
    size_t n = (size_t)(p - buf);
    if (n > cap)
        return 0;
    for (size_t i = 0; i < n; i++)
        out[i] = buf[i];
    return n;
}

int
segment_decode(const uint8_t *in, size_t len, SegmentPayload *seg)
{
    const uint8_t *p = in, *end = in + len;
    uint32_t msgid;
    if (!vlq_decode(&p, end, &msgid))
        return SEG_KIND_NONE;
    seg->kind = SEG_KIND_NONE;
    seg->oid = seg->flags = 0;
    seg->duration = 0;
    seg->velocity = seg->accel = seg->jerk = seg->snap = seg->crackle = 0;
    if (msgid == v2::MSGID_TRAJ_HOLD) {
        uint32_t oid, dur;
        if (!vlq_decode(&p, end, &oid) || !vlq_decode(&p, end, &dur))
            return SEG_KIND_NONE;
        seg->oid = (uint8_t)oid;
        seg->duration = dur;
        seg->kind = SEG_KIND_HOLD;
        return SEG_KIND_HOLD;
    }
    if (msgid != v2::MSGID_QUEUE_TRAJ_SEGMENT)
        return SEG_KIND_NONE;
    uint32_t oid, flags, dur;
    if (!vlq_decode(&p, end, &oid) || !vlq_decode(&p, end, &flags)
        || !vlq_decode(&p, end, &dur))
        return SEG_KIND_NONE;
    unsigned nc = order_ncoeff((uint8_t)flags);
    if (!nc)
        return SEG_KIND_NONE;
    int32_t *coeffs[5] = {&seg->velocity, &seg->accel, &seg->jerk,
                          &seg->snap, &seg->crackle};
    for (unsigned i = 0; i < nc; i++) {
        uint32_t v;
        if (!vlq_decode(&p, end, &v))
            return SEG_KIND_NONE;
        *coeffs[i] = (int32_t)v;
    }
    seg->oid = (uint8_t)oid;
    seg->flags = (uint8_t)flags;
    seg->duration = dur;
    seg->kind = SEG_KIND_SEGMENT;
    return SEG_KIND_SEGMENT;
}

} // namespace intentproto
