// Trajectory segment payload codec (FD-0001 doc 02).
//
// Helpers a THIRD-PARTY trajectory peer needs to speak the motion-intent
// protocol without reimplementing the two things that must be bit-exact:
//
//   * coefficient QUANTIZATION - true per-tick polynomial derivatives to
//     the fixed-point wire ints (velocity x 2^16, accel x 2^32, jerk x
//     2^48, snap x 2^64, crackle x 2^80), round-half-away-from-zero and
//     clamped to int32; and
//   * chained-position BOOKKEEPING - the exact Q32.32 sub-unit end-of-
//     segment delta, so a peer tracks where each segment ends identically
//     to how the MCU integrates it.
//
// The integer routines here are, by contract, bit-for-bit identical to
// src/trajq.c (trajq_end_delta_seg), klippy/chelper/segfit.c
// (segfit_end_delta_ho) and klippy/extras/trajectory_queuing.py
// (py_end_delta_ho). test/traj_higher_order_test.py and the library's
// own test guard that identity. Do not "optimize" the arithmetic.
//
// This is a bare, freestanding subset (no heap, no exceptions, no STL) so
// it links into the same builds as the datagram codec. The quantizer is
// the one routine that touches floating point; it is a host/peer emitter
// convenience and is never on an MCU hot path.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.
// (The intentproto library is offered under the MIT license per its
// README; this file follows the library's licensing.)

#ifndef INTENTPROTO_SEGMENT_HPP
#define INTENTPROTO_SEGMENT_HPP

#include <stddef.h>
#include <stdint.h>

namespace intentproto {

// ---- segment flags (mirror src/trajq.h TSEG_*) ----
constexpr uint8_t SEG_HOLD_AT_END   = 1 << 0;
constexpr uint8_t SEG_POLY_MASK     = 3 << 6;
constexpr uint8_t SEG_POLY_QUADRATIC = 0 << 6;
constexpr uint8_t SEG_POLY_CUBIC     = 1 << 6;
constexpr uint8_t SEG_POLY_QUINTIC   = 2 << 6;

// 1 native unit = 2^16 sub-units; the chained accumulator is Q32.32
// sub-units (32 fractional bits below the integer sub-unit).
constexpr int TRAJ_SUBUNIT_SHIFT = 16;
constexpr uint32_t TRAJ_MAX_DURATION = 1u << 26;

// ---- coefficient quantization ----
// Quantize a true per-tick derivative to its wire int32. order_k is the
// derivative order (1=velocity .. 5=crackle); the scale is 2^(16*k),
// matching segfit.c's quantize()/bezier_to_wire(). Rounds half away from
// zero and saturates to the int32 range.
int32_t segment_quantize(double true_value, unsigned order_k);

// ---- exact chained-position bookkeeping ----
// The Q32.32 sub-unit position change over one segment of the given
// duration (ticks) and quantized coefficients. Unused higher orders pass
// as 0. Bit-identical to trajq_end_delta_seg / segfit_end_delta_ho.
int64_t segment_end_delta(uint32_t duration, int32_t velocity, int32_t accel,
                          int32_t jerk = 0, int32_t snap = 0,
                          int32_t crackle = 0);

// A running chained-position accumulator (the peer twin of the MCU's
// tq->acc). Q32.32 sub-units. POD - copy/serialize freely.
struct SegmentChain {
    int64_t acc;  // Q32.32 sub-units
};

// Anchor the chain to an absolute integer sub-unit position (as a
// trajectory_rebase would). Fractional bits are cleared.
inline void segment_chain_set(SegmentChain *ch, int64_t pos_subunits) {
    ch->acc = pos_subunits << 32;
}
// Advance the chain by one segment; returns the new integer sub-unit
// position (acc >> 32).
int64_t segment_chain_advance(SegmentChain *ch, uint32_t duration,
                              int32_t velocity, int32_t accel,
                              int32_t jerk = 0, int32_t snap = 0,
                              int32_t crackle = 0);
// Current integer sub-unit position (drops the 32 fractional bits).
inline int64_t segment_chain_position(const SegmentChain *ch) {
    return ch->acc >> 32;
}

// ---- payload codec ----
// Decoded segment fields; kind selects which message was parsed.
enum SegmentKind {
    SEG_KIND_NONE = 0,
    SEG_KIND_SEGMENT = 1,  // queue_traj_segment (order in flags)
    SEG_KIND_HOLD = 2,     // traj_hold
};
struct SegmentPayload {
    int kind;
    uint8_t oid;
    uint8_t flags;
    uint32_t duration;
    int32_t velocity, accel, jerk, snap, crackle;
};

// Encode a queue_traj_segment payload (msgid + oid + flags + duration +
// coefficients) into out. The number of coefficients emitted follows the
// polynomial-order bits of flags (quadratic: v,a; cubic: +jerk; quintic:
// +snap,crackle). Returns bytes written, or 0 if cap is too small.
size_t segment_encode(uint8_t *out, size_t cap, uint8_t oid, uint8_t flags,
                      uint32_t duration, int32_t velocity, int32_t accel,
                      int32_t jerk = 0, int32_t snap = 0,
                      int32_t crackle = 0);
// Encode a traj_hold payload (msgid + oid + duration). hold_at_end sets
// the SEG_HOLD_AT_END flag semantics on the peer's own chain (not carried
// on the traj_hold wire, which has no flags field - it is implicit).
size_t segment_encode_hold(uint8_t *out, size_t cap, uint8_t oid,
                           uint32_t duration);

// Decode a queue_traj_segment / traj_hold payload. Fills *seg and returns
// its kind, or SEG_KIND_NONE on a msgid that is neither, a truncated
// payload, or a bad polynomial order. Unused coefficients are zeroed.
int segment_decode(const uint8_t *in, size_t len, SegmentPayload *seg);

} // namespace intentproto

#endif // INTENTPROTO_SEGMENT_HPP
