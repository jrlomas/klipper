// Test adapter that compiles the exact MCU trajectory step solver for use by
// trajectory_v1_pulse_compare.py.  Link-time garbage collection discards the
// command/GPIO paths; unresolved symbols in those discarded paths are never
// loaded.  Include trajq.c as well so both quadratic and quintic vectors use
// the production fixed-point evaluators and exact endpoint arithmetic.
#include <stdint.h>

#include "../src/trajq.c"
#include "../src/traj_stepper.c"

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
