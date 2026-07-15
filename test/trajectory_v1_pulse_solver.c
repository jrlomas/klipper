// Test adapter that compiles the exact MCU trajectory step solver for use by
// trajectory_v1_pulse_compare.py.  Link-time garbage collection discards the
// command/GPIO paths; unresolved symbols in those discarded paths are never
// loaded.  Include trajq.c as well so both quadratic and quintic vectors use
// the production fixed-point evaluators and exact endpoint arithmetic.
#include <stdint.h>
#include <setjmp.h>
#include <stdlib.h>

// Turn production shutdowns into a controlled adapter return.  The MCU
// implementation is correctly noreturn, but a lazy-loaded workstation shared
// object otherwise attempts to resolve the complete firmware shutdown stack
// only when a bad test vector reaches that path.
#include "../src/command.h"
static jmp_buf helix_test_shutdown_jmp;
static uint8_t helix_test_shutdown_active;
static uint32_t helix_test_shutdown_crossing;
static const char *helix_test_shutdown_reason;

static __attribute__((noreturn)) void
helix_test_shutdown(const char *reason)
{
    (void)reason;
    helix_test_shutdown_reason = reason;
    if (helix_test_shutdown_active)
        longjmp(helix_test_shutdown_jmp, 1);
    abort();
}

#undef shutdown
#define shutdown(msg) helix_test_shutdown(msg)

#include "../src/trajq.c"
#include "../src/traj_stepper.c"

const char *
helix_test_last_shutdown(void)
{
    return helix_test_shutdown_reason;
}

// Return the exact result of traj_solve_step() while exposing only its pure
// mathematical state to Python.  The caller advances t_prev/target after a
// returned crossing exactly as traj_stepper_schedule() does.
int
helix_test_solve_step(uint32_t duration, int32_t velocity, int32_t accel,
                      uint32_t t_prev, int64_t target16, int32_t dir,
                      uint32_t *step_t)
{
    struct traj_stepper s = { };
    s.tq.duration = duration;
    s.tq.velocity = velocity;
    s.tq.accel = accel;
    s.t_prev = t_prev;
    s.target16 = target16;
    s.dir = dir;
    s.q16_end = trajq_end_delta_seg(&s.tq) >> 16;
    return traj_solve_step(&s, step_t);
}

// Exercise the production modulo-phase boundary calculation independently of
// timer/GPIO state so boundary-focused Python vectors can cover phase wrap.
int64_t
helix_test_target16(int64_t acc, int32_t mpos, int32_t dir)
{
    return traj_stepper_calc_target16(acc, mpos, dir);
}

int
helix_test_is_pure_cruise(uint8_t flags, int32_t accel, int32_t jerk,
                          int32_t snap, int32_t crackle)
{
    struct trajq tq = { };
    tq.seg_flags = flags;
    tq.accel = accel;
#if CONFIG_WANT_TRAJECTORY_HIGHER_ORDER
    tq.jerk = jerk;
    tq.snap = snap;
    tq.crackle = crackle;
#endif
    return traj_stepper_is_pure_cruise(&tq);
}

int64_t
helix_test_divide_residual(int64_t residual, int32_t velocity,
                           uint32_t limit)
{
    return traj_divide_residual(residual, velocity, limit);
}

int
helix_test_smul_shr16_s32(int32_t value, uint32_t ticks, int32_t *result)
{
    return traj_smul_shr16_s32(value, ticks, result);
}

// Exercise one crossing of the production quintic solver.  Python retains
// the per-segment recurrence fields between calls exactly as the IRQ path
// does, while this adapter keeps the ABI compact and avoids emulating any
// solver arithmetic outside the firmware source.
int
helix_test_solve_step_ho(uint32_t duration, int32_t velocity, int32_t accel,
                         int32_t jerk, int32_t snap, int32_t crackle,
                         uint32_t t_prev, int64_t target16, int32_t dir,
                         uint32_t step_interval, uint32_t last_step_t,
                         uint32_t first_step_guess, uint32_t *step_t)
{
    struct traj_stepper s = { };
    s.tq.duration = duration;
    s.tq.velocity = velocity;
    s.tq.accel = accel;
    s.tq.jerk = jerk;
    s.tq.snap = snap;
    s.tq.crackle = crackle;
    s.tq.seg_flags = TSEG_POLY_QUINTIC | TSEG_LOCAL_TIME;
    s.t_prev = t_prev;
    s.target16 = target16;
    s.dir = dir;
    s.step_interval = step_interval;
    s.last_step_t = last_step_t;
    s.first_step_guess = first_step_guess;
    s.q16_end = trajq_end_delta_seg(&s.tq) >> 16;
    traj_poly_fast_setup(&s);
    return traj_solve_step(&s, step_t);
}

// Expand one complete segment while retaining every production solver state
// field across crossings.  The smaller helpers above are useful for focused
// vectors, but reconstructing traj_stepper for every pulse cannot reproduce
// the reciprocal/predictor recurrence of a long real G-code segment.
int
helix_test_expand_segment(uint8_t flags, uint32_t duration, int32_t velocity,
                          int32_t accel, int32_t jerk, int32_t snap,
                          int32_t crackle, int64_t acc, int32_t *mpos_io,
                          uint32_t *interval_io, int32_t *dir_io,
                          uint32_t *pulses, uint32_t max_pulses)
{
    struct traj_stepper s = { };
    s.tq.duration = duration;
    s.tq.velocity = velocity;
    s.tq.accel = accel;
    s.tq.jerk = jerk;
    s.tq.snap = snap;
    s.tq.crackle = crackle;
    s.tq.seg_flags = flags;
    s.tq.acc = acc;
    s.mpos = *mpos_io;
    s.step_interval = *interval_io;
    s.dir = *dir_io;

    // traj_stepper_load() also updates the physical direction GPIO when its
    // cached state differs.  Prime that cache to the direction it will select
    // so this pure test adapter never touches a board GPIO implementation.
    int32_t vend = trajq_velocity_at_seg(&s.tq, duration);
    int8_t direction = velocity ? (velocity > 0 ? 1 : -1)
        : (accel ? (accel > 0 ? 1 : -1) : (vend > 0 ? 1 : -1));
    if (direction > 0)
        s.flags |= TSF_DIR_HIGH;
    // Segment validation and endpoint setup can also reject an overflowing
    // polynomial.  Keep that production shutdown inside the adapter boundary
    // so a bad named vector is reported to Python instead of aborting the
    // entire test process.
    if (setjmp(helix_test_shutdown_jmp)) {
        helix_test_shutdown_active = 0;
        pulses[max_pulses - 1] = helix_test_shutdown_crossing;
        return -2;
    }
    helix_test_shutdown_crossing = 0;
    helix_test_shutdown_active = 1;
    traj_stepper_load(&s);
    helix_test_shutdown_active = 0;

    uint32_t count = 0;
    for (;;) {
        uint32_t step_t;
        if (setjmp(helix_test_shutdown_jmp)) {
            helix_test_shutdown_active = 0;
            pulses[max_pulses - 1] = helix_test_shutdown_crossing;
            return -2;
        }
        helix_test_shutdown_crossing = count;
        helix_test_shutdown_active = 1;
        int result = traj_solve_step(&s, &step_t);
        helix_test_shutdown_active = 0;
        if (!result)
            break;
        if (result == 2) {
            s.t_prev = step_t;
            continue;
        }
        if (count >= max_pulses)
            return -1;
        if (s.last_step_t)
            s.step_interval = step_t - s.last_step_t;
        s.last_step_t = step_t;
        s.t_prev = step_t;
        pulses[count++] = step_t;
        s.mpos += s.dir;
        s.target16 += s.dir > 0 ? STEP_Q : -STEP_Q;
    }
    *mpos_io = s.mpos;
    *interval_io = s.step_interval;
    *dir_io = s.dir;
    return count;
}
