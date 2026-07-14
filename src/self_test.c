// Built-in self test: the software verification gates, runnable LIVE on
// the board as part of the protocol (FD-0001; docs/Helix_Test_Plan.md).
//
// A host connects and runs `run_self_test id=%c` for each advertised test;
// each answers `self_test_result id=%c status=%c value=%u`. The tests are
// the same invariants the desktop suites enforce — executed on the real
// silicon, so hardware verification is built into the protocol itself and,
// once green, doubles as a field diagnostic (the host side is
// [helix_self_test] / HELIX_SELF_TEST).
//
// Kept deliberately cheap and safe: no I/O is touched, nothing moves, no
// state is disturbed; every test runs from a task context in bounded time.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <string.h> // memset
#include "board/misc.h" // timer_read_time, crc16_ccitt
#include "command.h" // DECL_COMMAND
#include "sched.h" // sched_shutdown
#include "autoconf.h" // CONFIG_WANT_TRAJECTORY
#if CONFIG_WANT_TRAJECTORY
#include "trajq.h" // trajq_end_delta
uint_fast8_t traj_stepper_test_hold_boundary(void);
uint_fast8_t traj_stepper_test_halfstep_phase(void);
uint_fast8_t traj_stepper_test_cruise_recurrence(void);
uint_fast8_t traj_stepper_test_slow_reciprocal(void);
uint_fast8_t traj_stepper_test_prestart_stop(void);
uint_fast8_t traj_stepper_test_quintic_deadline(uint32_t *max_elapsed);
uint_fast8_t traj_stepper_benchmark(uint32_t step_rate, uint_fast8_t axes,
                                    uint32_t *pulses, uint32_t *max_elapsed,
                                    uint32_t *min_interval,
                                    uint32_t *max_error);
uint_fast8_t traj_stepper_probe_captured_quintic(
    uint_fast8_t scale, uint32_t *pulses, uint32_t *max_elapsed,
    uint32_t *min_interval, uint32_t *max_error);
#endif

enum {
    ST_CRC_WIRE = 0,     // wire CRC check vector (the interop trap)
    ST_TIMER_MONOTONIC,  // timer strictly advances
    ST_TIMER_RATE,       // ticks for a fixed spin (perf fingerprint)
    ST_RAM_PATTERN,      // bus/RAM pattern walk
    ST_TRAJ_KERNEL,      // fixed-point kernel vs host golden vectors
    ST_COUNT,
};

enum { ST_PASS = 0, ST_FAIL = 1, ST_SKIP = 2 };

DECL_CONSTANT("SELF_TEST_COUNT", ST_COUNT);
DECL_ENUMERATION("self_test", "crc_wire", ST_CRC_WIRE);
DECL_ENUMERATION("self_test", "timer_monotonic", ST_TIMER_MONOTONIC);
DECL_ENUMERATION("self_test", "timer_rate", ST_TIMER_RATE);
DECL_ENUMERATION("self_test", "ram_pattern", ST_RAM_PATTERN);
DECL_ENUMERATION("self_test", "traj_kernel", ST_TRAJ_KERNEL);

// The one CRC vector that catches every framing/CRC misport: the wire
// CRC over "123456789" is 0x6f91 (reflected CRC-16/MCRF4XX), NOT 0x29b1.
static uint_fast8_t
test_crc_wire(uint32_t *value)
{
    static const uint8_t check[9] = "123456789";
    uint16_t crc = crc16_ccitt((uint8_t *)check, sizeof(check));
    *value = crc;
    return crc == 0x6f91 ? ST_PASS : ST_FAIL;
}

static uint_fast8_t
test_timer_monotonic(uint32_t *value)
{
    uint32_t last = timer_read_time(), maxd = 0;
    for (int i = 0; i < 64; i++) {
        uint32_t now = timer_read_time();
        uint32_t d = now - last; // wrap-safe unsigned delta
        if (d > maxd)
            maxd = d;
        if ((int32_t)d < 0)
            return *value = d, ST_FAIL; // time went backwards
        last = now;
    }
    *value = maxd;
    return ST_PASS;
}

static uint_fast8_t
test_timer_rate(uint32_t *value)
{
    // Elapsed ticks over a fixed 256-read spin: a stable perf fingerprint
    // for a given chip/clock config (informational; always passes).
    uint32_t start = timer_read_time();
    for (int i = 0; i < 256; i++)
        (void)timer_read_time();
    *value = timer_read_time() - start;
    return ST_PASS;
}

static uint_fast8_t
test_ram_pattern(uint32_t *value)
{
    static uint8_t buf[128];
    for (int pass = 0; pass < 3; pass++) {
        for (unsigned i = 0; i < sizeof(buf); i++)
            buf[i] = pass == 0 ? 0x55 : pass == 1 ? 0xaa : (uint8_t)i;
        for (unsigned i = 0; i < sizeof(buf); i++) {
            uint8_t want = pass == 0 ? 0x55 : pass == 1 ? 0xaa : (uint8_t)i;
            if (buf[i] != want) {
                *value = (uint32_t)(pass << 16) | i;
                return ST_FAIL;
            }
        }
    }
    *value = 0;
    return ST_PASS;
}

#if CONFIG_WANT_TRAJECTORY
// Golden vectors computed by the HOST's implementation of the same
// quantized kernel (py_end_delta_ho in klippy/extras/
// trajectory_queuing.py). Equality here proves the board's fixed-point
// trajectory math matches the host bit-for-bit on this silicon/compiler.
static const struct {
    uint32_t duration;
    int32_t velocity, accel;
    int64_t want;
} traj_golden[] = {
    { 1000u, 65536, 0, 4294967296000LL },
    { 48000u, 123456, -789, 387450067968000LL },
    { 65536u, -2000000, 4096, -8581138498977792LL },
    { 1048576u, 7, 12345, 6787216558784512LL },
};

static uint_fast8_t
test_traj_kernel(uint32_t *value)
{
    for (unsigned i = 0; i < ARRAY_SIZE(traj_golden); i++) {
        int64_t got = trajq_end_delta(traj_golden[i].duration,
                                      traj_golden[i].velocity,
                                      traj_golden[i].accel);
        if (got != traj_golden[i].want) {
            *value = (uint32_t)(i << 16) | (uint32_t)(got & 0xffff);
            return ST_FAIL;
        }
    }
    if (!traj_stepper_test_hold_boundary()) {
        *value = 0x80000000;
        return ST_FAIL;
    }
    if (!traj_stepper_test_halfstep_phase()) {
        *value = 0x80000001;
        return ST_FAIL;
    }
    if (!traj_stepper_test_cruise_recurrence()) {
        *value = 0x80000002;
        return ST_FAIL;
    }
    if (!traj_stepper_test_slow_reciprocal()) {
        *value = 0x80000003;
        return ST_FAIL;
    }
    if (!traj_stepper_test_prestart_stop()) {
        *value = 0x80000004;
        return ST_FAIL;
    }
    uint32_t solver_elapsed;
    if (!traj_stepper_test_quintic_deadline(&solver_elapsed)) {
        *value = 0x81000000 | (solver_elapsed & 0x00ffffff);
        return ST_FAIL;
    }
    *value = ARRAY_SIZE(traj_golden);
    return ST_PASS;
}
#endif

void
command_run_self_test(uint32_t *args)
{
    uint_fast8_t id = args[0], status = ST_SKIP;
    uint32_t value = 0;
    switch (id) {
    case ST_CRC_WIRE:        status = test_crc_wire(&value); break;
    case ST_TIMER_MONOTONIC: status = test_timer_monotonic(&value); break;
    case ST_TIMER_RATE:      status = test_timer_rate(&value); break;
    case ST_RAM_PATTERN:     status = test_ram_pattern(&value); break;
#if CONFIG_WANT_TRAJECTORY
    case ST_TRAJ_KERNEL:     status = test_traj_kernel(&value); break;
#endif
    default: break; // unknown/not-built -> ST_SKIP
    }
    sendf("self_test_result id=%c status=%c value=%u", id, status, value);
}
DECL_COMMAND(command_run_self_test, "run_self_test id=%c");

#if CONFIG_WANT_TRAJECTORY
// Computation-only trajectory throughput probe.  This exercises the real
// crossing solver from task context but never allocates an oid, configures a
// pin, queues motion, or changes live trajectory state.  Multiple axes are
// independent virtual solver states executed back-to-back, approximating the
// worst case in which several step deadlines coincide.
void
command_run_traj_benchmark(uint32_t *args)
{
    uint32_t step_rate = args[0];
    uint_fast8_t axes = args[1];
    uint32_t pulses = 0, max_elapsed = 0, min_interval = 0, max_error = 0;
    uint_fast8_t status = traj_stepper_benchmark(
        step_rate, axes, &pulses, &max_elapsed, &min_interval, &max_error);
    sendf("traj_benchmark_result rate=%u axes=%c status=%c pulses=%u"
          " max_elapsed=%u min_interval=%u max_error=%u",
          step_rate, axes, status, pulses, max_elapsed, min_interval,
          max_error);
}
DECL_COMMAND(command_run_traj_benchmark,
             "run_traj_benchmark rate=%u axes=%c");

void
command_run_captured_quintic_probe(uint32_t *args)
{
    uint_fast8_t scale = args[0];
    uint32_t pulses = 0, max_elapsed = 0, min_interval = 0, max_error = 0;
    uint_fast8_t status = traj_stepper_probe_captured_quintic(
        scale, &pulses, &max_elapsed, &min_interval, &max_error);
    sendf("captured_quintic_probe_result scale=%c status=%c pulses=%u"
          " max_elapsed=%u min_interval=%u max_error=%u",
          scale, status, pulses, max_elapsed, min_interval, max_error);
}
DECL_COMMAND(command_run_captured_quintic_probe,
             "run_captured_quintic_probe scale=%c");
#endif
