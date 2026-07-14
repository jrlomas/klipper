// Test adapter that compiles the exact MCU trajectory step solver for use by
// trajectory_v1_pulse_compare.py.  Link-time garbage collection discards the
// command/GPIO paths; unresolved symbols in those discarded paths are never
// loaded.  The compared vectors are quadratic, so the local evaluators below
// intentionally implement that production wire order only.
#include <stdint.h>

#include "../src/traj_stepper.c"

int32_t
trajq_velocity_at_seg(struct trajq *tq, uint32_t t)
{
    return tq->velocity + (int32_t)(((int64_t)tq->accel * t) >> 16);
}

int64_t
trajq_pos_at_seg(struct trajq *tq, uint32_t t)
{
    int64_t vterm = (int64_t)tq->velocity * t;
    int64_t w = (int64_t)tq->accel * t;
    return vterm + ((w >> 16) * (int64_t)t >> 1);
}

int64_t
trajq_end_delta_seg(struct trajq *tq)
{
    int64_t dv = (int64_t)tq->velocity * tq->duration;
    int64_t delta = dv << 16;
    if (tq->accel) {
        int64_t w = (int64_t)tq->accel * tq->duration;
        // The test vectors stay well inside int64; production's guarded
        // 96-bit helper has the same result in this range.
        delta += (w * tq->duration) >> 1;
    }
    return delta;
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
