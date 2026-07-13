// intentproto trajectory segment codec tests (FD-0001 doc 02).
//
// The end-delta goldens below are the SAME vectors the firmware self-test
// (src/self_test.c traj_kernel) asserts against src/trajq.c, so this test
// proves the library's segment_end_delta() is bit-identical to the MCU's
// integration for the quadratic case. The higher-order and cross-language
// bit-identity (vs klippy's segfit.c / trajectory_queuing.py) is guarded
// by test/segment_lib_test.py.

#include "intentproto/segment.hpp"
#include "intentproto/core_ids.hpp"
#include "intentproto/proto.hpp" // vlq_encode

#include <stdio.h>
#include <string.h>

static int g_failures = 0;
#define CHECK(cond)                                                     \
    do {                                                                \
        if (!(cond)) {                                                  \
            printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);      \
            g_failures++;                                               \
        }                                                               \
    } while (0)

using namespace intentproto;

static void test_end_delta_goldens() {
    // {duration, velocity, accel, expected} - identical to the firmware
    // self-test's traj_kernel golden vectors.
    struct { uint32_t d; int32_t v; int32_t a; int64_t exp; } g[] = {
        {1000u, 65536, 0, 4294967296000LL},
        {48000u, 123456, -789, 387450067968000LL},
        {65536u, -2000000, 4096, -8581138498977792LL},
        {1048576u, 7, 12345, 6787216558784512LL},
    };
    for (auto& t : g)
        CHECK(segment_end_delta(t.d, t.v, t.a) == t.exp);
}

static void test_quantize() {
    CHECK(segment_quantize(1.0, 1) == 65536);          // 2^16
    CHECK(segment_quantize(0.5, 1) == 32768);
    CHECK(segment_quantize(-0.25, 1) == -16384);
    CHECK(segment_quantize(1.0 / 65536.0, 1) == 1);    // half-away rounding
    // round half away from zero (not banker's): 1.5 sub-unit -> 2
    CHECK(segment_quantize(1.5 / 65536.0, 1) == 2);
    CHECK(segment_quantize(-1.5 / 65536.0, 1) == -2);
    // saturation to int32 rails
    CHECK(segment_quantize(1.0, 2) == 2147483647);     // 2^32 clamps
    CHECK(segment_quantize(-1.0, 2) == -2147483647 - 1);
}

static void test_chain() {
    SegmentChain ch;
    segment_chain_set(&ch, 0);
    // Two identical quadratic segments accumulate exactly twice one delta.
    int64_t one = segment_end_delta(1000, 65536, 0);
    segment_chain_advance(&ch, 1000, 65536, 0);
    CHECK(ch.acc == one);
    segment_chain_advance(&ch, 1000, 65536, 0);
    CHECK(ch.acc == 2 * one);
    // Anchored position resets the fractional bits.
    segment_chain_set(&ch, 12345);
    CHECK(segment_chain_position(&ch) == 12345);
    CHECK(ch.acc == (int64_t)12345 << 32);
}

static void test_payload_roundtrip() {
    uint8_t buf[64];
    // Quadratic
    size_t n = segment_encode(buf, sizeof buf, 3, SEG_POLY_QUADRATIC,
                              48000, 123456, -789);
    CHECK(n > 0);
    SegmentPayload sp;
    CHECK(segment_decode(buf, n, &sp) == SEG_KIND_SEGMENT);
    CHECK(sp.oid == 3 && sp.duration == 48000);
    CHECK(sp.velocity == 123456 && sp.accel == -789);
    CHECK(sp.jerk == 0 && sp.snap == 0 && sp.crackle == 0);
    // Quintic carries all five coefficients
    n = segment_encode(buf, sizeof buf, 7,
                       (uint8_t)(SEG_POLY_QUINTIC | SEG_HOLD_AT_END),
                       9000, 10, -20, 30, -40, 50);
    CHECK(n > 0);
    CHECK(segment_decode(buf, n, &sp) == SEG_KIND_SEGMENT);
    CHECK((sp.flags & SEG_POLY_MASK) == SEG_POLY_QUINTIC);
    CHECK(sp.flags & SEG_HOLD_AT_END);
    CHECK(sp.velocity == 10 && sp.accel == -20 && sp.jerk == 30);
    CHECK(sp.snap == -40 && sp.crackle == 50);
    // Hold
    n = segment_encode_hold(buf, sizeof buf, 2, 250000);
    CHECK(n > 0);
    CHECK(segment_decode(buf, n, &sp) == SEG_KIND_HOLD);
    CHECK(sp.oid == 2 && sp.duration == 250000);
    // The first byte is the msgid VLQ.
    uint8_t idbuf[5];
    size_t idn = (size_t)(vlq_encode(idbuf, v2::MSGID_TRAJ_HOLD) - idbuf);
    CHECK(idn == 1 && buf[0] == idbuf[0]);
    // A foreign msgid decodes as NONE.
    uint8_t foreign[4];
    size_t fn = (size_t)(vlq_encode(foreign, 99) - foreign);
    CHECK(segment_decode(foreign, fn, &sp) == SEG_KIND_NONE);
}

int main() {
    test_end_delta_goldens();
    test_quantize();
    test_chain();
    test_payload_roundtrip();
    if (g_failures) {
        printf("%d FAILURE(S)\n", g_failures);
        return 1;
    }
    printf("all tests passed\n");
    return 0;
}
