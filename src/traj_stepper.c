// Stepper backend for trajectory intention segments (FD-0001).
//
// Realizes quadratic position segments as step/dir edges with pure
// integer math: for each step, solve q(t) = q_target for the tick of
// the next microstep boundary crossing with an incremental Newton
// iteration seeded by the previous interval.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <string.h> // memset
#include "autoconf.h" // CONFIG_*
#include "basecmd.h" // oid_alloc
#include "board/gpio.h" // gpio_out_write
#include "board/irq.h" // irq_disable
#include "board/misc.h" // timer_read_time
#include "command.h" // DECL_COMMAND
#include "execlog.h" // execlog_append
#include "sched.h" // struct timer
#include "trajq.h" // trajq_setup
#include "trsync.h" // trsync_add_signal

// One microstep in solver units (Q16.16 sub-units)
#define STEP_Q ((int64_t)1 << 32)
// Maximum scheduling horizon between wakeups within a segment; slow
// motion re-polls rather than trusting long extrapolations.
#define POLL_TICKS (1 << 22)
// Activation guard: a segment stream anchored further than this in
// the past is a protocol error (host must rebase after any gap).
#define PAST_GUARD_TICKS (1 << 26)
// Reciprocal of normalized velocity used by the per-pulse Newton correction.
// With |v| normalized to [2^16,2^17), 2^39/|v| fits in 23 bits and the
// reciprocal Newton product remains within uint64_t.
#define RECIP_Q 39

enum { WK_STEP, WK_UNSTEP, WK_POLL };

struct traj_stepper {
    struct timer time;
    struct trajq tq;
    uint32_t step_pulse_ticks;
    struct gpio_out step_pin, dir_pin;
    // Solver state for the active segment
    uint32_t t_prev;        // ticks into segment of last solve point
    uint32_t last_step_t;   // last scheduled physical pulse in this segment
    uint32_t step_interval; // preceding physical pulse interval (predictor)
    uint32_t first_step_guess; // phase-scaled predictor at a segment boundary
    uint32_t recip_speed, recip_q39;
    // Pre-scaled Horner coefficients for the common int32 quintic path.
    // Preparing these at segment load keeps coefficient range checks and
    // 64-bit constant multiplication out of the precision timer IRQ.
    int32_t poly_c, poly_s5, poly_j20, poly_a60, poly_v120;
    int64_t target16;       // next boundary, Q16.16 rel segment start
    int64_t q16_end;        // segment end position, Q16.16 rel start
    // Pure-cruise crossing recurrence.  Division is paid once per segment;
    // recurring step IRQs advance quotient/remainder with adds only.
    uint64_t cruise_q, cruise_step_q;
    uint32_t cruise_r, cruise_step_r, cruise_speed;
    int64_t cruise_target16;
    int32_t mpos;           // microsteps actually stepped (absolute)
    int8_t dir;             // +1 / -1 for the active segment
    uint8_t wake_kind;
    uint8_t flags;
    uint8_t cruise_valid;
    uint8_t poly_fast_valid;
    struct trsync_signal stop_signal;
};

enum { TSF_INVERT_STEP = 1 << 0, TSF_DIR_HIGH = 1 << 1,
       TSF_INVERT_DIR = 1 << 2 };

static inline uint8_t
traj_stepper_is_pure_cruise(struct trajq *tq)
{
    if (tq->accel)
        return 0;
#if CONFIG_WANT_TRAJECTORY_HIGHER_ORDER
    if ((tq->seg_flags & TSEG_POLY_MASK)
        && (tq->jerk || tq->snap || tq->crackle))
        return 0;
#endif
    return 1;
}

static void
traj_cruise_set_target(struct traj_stepper *s)
{
    int64_t target = s->target16;
    if (s->dir > 0 ? target <= 0 : target >= 0) {
        s->cruise_valid = 0;
        return;
    }
    uint64_t magnitude = target < 0
        ? -(uint64_t)target : (uint64_t)target;
    s->cruise_q = magnitude / s->cruise_speed;
    s->cruise_r = magnitude % s->cruise_speed;
    s->cruise_target16 = target;
    s->cruise_valid = 1;
}

static void
traj_cruise_setup(struct traj_stepper *s)
{
    int32_t velocity = s->tq.velocity;
    uint32_t speed = velocity < 0
        ? -(uint32_t)velocity : (uint32_t)velocity;
    s->cruise_valid = 0;
    s->cruise_speed = speed;
    if (!speed)
        return;
    s->cruise_step_q = (uint64_t)STEP_Q / speed;
    s->cruise_step_r = (uint64_t)STEP_Q % speed;
    traj_cruise_set_target(s);
}

static void
traj_cruise_advance_target(struct traj_stepper *s)
{
    int64_t expected = s->cruise_target16
        + (s->dir > 0 ? STEP_Q : -STEP_Q);
    if (!s->cruise_valid || s->target16 != expected) {
        traj_cruise_set_target(s);
        return;
    }
    s->cruise_q += s->cruise_step_q;
    uint64_t remainder = (uint64_t)s->cruise_r + s->cruise_step_r;
    if (remainder >= s->cruise_speed) {
        s->cruise_q++;
        remainder -= s->cruise_speed;
    }
    s->cruise_r = remainder;
    s->cruise_target16 = s->target16;
}

static uint64_t noinline
traj_udivmod32_pair(uint32_t value, uint32_t divisor)
{
    return ((uint64_t)(value % divisor) << 32) | (value / divisor);
}

static int64_t
traj_signed_shr(int64_t value, uint8_t shift)
{
    if (!shift)
        return value;
    uint64_t magnitude = value < 0
        ? -(uint64_t)value : (uint64_t)value;
    magnitude >>= shift;
    return value < 0 ? -(int64_t)magnitude : (int64_t)magnitude;
}

static void
traj_recip_init(struct traj_stepper *s, uint32_t speed)
{
    s->recip_speed = speed;
    s->recip_q39 = ((uint64_t)1 << RECIP_Q) / speed;
}

// Return -residual/velocity using the incrementally maintained Q39
// reciprocal.  This is the founding-document hot path: one reciprocal Newton
// update and one 64x32 multiply-shift, with no division per step.
static int64_t
traj_recip_correction(struct traj_stepper *s, int64_t residual,
                      int32_t velocity, uint32_t limit)
{
    uint32_t speed = velocity < 0
        ? -(uint32_t)velocity : (uint32_t)velocity;
    uint32_t r = s->recip_q39;
    if (!r || !s->recip_speed
        || speed > s->recip_speed * 2U
        || s->recip_speed > speed * 2U) {
        traj_recip_init(s, speed);
        r = s->recip_q39;
    } else {
        uint64_t vr = (uint64_t)speed * r;
        if (vr >= ((uint64_t)2 << RECIP_Q)) {
            traj_recip_init(s, speed);
            r = s->recip_q39;
        } else {
            uint64_t correction = ((uint64_t)2 << RECIP_Q) - vr;
            r = ((uint64_t)r * correction) >> RECIP_Q;
            if (!r) {
                traj_recip_init(s, speed);
                r = s->recip_q39;
            } else {
                s->recip_speed = speed;
                s->recip_q39 = r;
            }
        }
    }
    uint64_t magnitude = residual < 0
        ? -(uint64_t)residual : (uint64_t)residual;
    // 96-bit magnitude*r, then >>39.  `hi` is product>>32, so the
    // requested integer quotient is hi>>(39-32).
    uint64_t lo = (uint64_t)(uint32_t)magnitude * r;
    uint64_t hi = (magnitude >> 32) * r + (lo >> 32);
    uint64_t quotient = hi >> (RECIP_Q - 32);
    if (quotient > limit)
        quotient = limit;
    uint8_t negative = (residual < 0) == (velocity < 0);
    return negative ? -(int64_t)quotient : (int64_t)quotient;
}

// Steady-state Newton correction using interval_last/STEP_Q as recip(v).
// residual120>>7 is 120/128 of the Q16.16 position residual; that deliberate
// 6.25% under-correction is followed by the next pulse's refinement and keeps
// the captured quintic well inside the 1/8-microstep execution bound.
static int64_t
traj_interval_correction(int64_t residual120, uint32_t interval, int32_t dir,
                         uint32_t limit)
{
    int64_t residual = traj_signed_shr(residual120, 7);
    uint64_t magnitude = residual < 0
        ? -(uint64_t)residual : (uint64_t)residual;
    uint64_t lo = (uint64_t)(uint32_t)magnitude * interval;
    uint64_t hi = (magnitude >> 32) * interval + (lo >> 32);
    uint64_t quotient = hi; // (magnitude * interval) >> 32
    if (quotient > limit)
        quotient = limit;
    uint8_t negative = (residual < 0) == (dir < 0);
    return negative ? -(int64_t)quotient : (int64_t)quotient;
}

#if CONFIG_WANT_TRAJECTORY_HIGHER_ORDER
// Exact trunc-toward-zero (value*ticks)/2^16 for a signed int32 value.  The
// result is returned only when it still fits int32.  Cortex-M0 lacks a
// 32x32->64 instruction; retaining only the shifted result lets this use four
// native limb multiplies without the general long-multiply helper or int64
// temporaries in the caller.
static uint_fast8_t noinline
traj_smul_shr16_s32(int32_t value, uint32_t ticks, int32_t *result)
{
    uint8_t negative = value < 0;
    uint32_t magnitude = negative ? -(uint32_t)value : (uint32_t)value;
    uint32_t a0 = magnitude & 0xffff, a1 = magnitude >> 16;
    uint32_t t0 = ticks & 0xffff, t1 = ticks >> 16;
    uint32_t p0 = a0 * t0;
    uint32_t p1 = a0 * t1;
    uint32_t p2 = a1 * t0;
    uint32_t middle = (p0 >> 16) + (p1 & 0xffff) + (p2 & 0xffff);
    uint32_t product_lo = (p0 & 0xffff) | (middle << 16);
    uint32_t product_hi = a1 * t1 + (p1 >> 16) + (p2 >> 16)
        + (middle >> 16);
    if (product_hi >> 16)
        return 0;
    uint32_t shifted = (product_hi << 16) | (product_lo >> 16);
    if (shifted > (negative ? 0x80000000U : (uint32_t)INT32_MAX))
        return 0;
    *result = negative
        ? (shifted == 0x80000000U ? INT32_MIN : -(int32_t)shifted)
        : (int32_t)shifted;
    return 1;
}

static uint_fast8_t
traj_poly_stage(int32_t coefficient, int32_t h, uint32_t ticks,
                int32_t *result)
{
    int32_t product;
    if (!traj_smul_shr16_s32(h, ticks, &product)
        || __builtin_add_overflow(coefficient, product, result))
        return 0;
    return 1;
}

static void
traj_poly_fast_setup(struct traj_stepper *s)
{
    struct trajq *tq = &s->tq;
    s->poly_fast_valid = 0;
    if ((tq->seg_flags & TSEG_POLY_MASK) != TSEG_POLY_QUINTIC)
        return;
    int64_t s5 = (int64_t)tq->snap * 5;
    int64_t j20 = (int64_t)tq->jerk * 20;
    int64_t a60 = (int64_t)tq->accel * 60;
    int64_t v120 = (int64_t)tq->velocity * 120;
    if (s5 > INT32_MAX || s5 < INT32_MIN
        || j20 > INT32_MAX || j20 < INT32_MIN
        || a60 > INT32_MAX || a60 < INT32_MIN
        || v120 > INT32_MAX || v120 < INT32_MIN)
        return;
    s->poly_c = tq->crackle;
    s->poly_s5 = s5;
    s->poly_j20 = j20;
    s->poly_a60 = a60;
    s->poly_v120 = v120;
    s->poly_fast_valid = 1;
}

static uint_fast8_t
traj_poly_pos120(struct traj_stepper *s, uint32_t ticks, int64_t *position120)
{
    int32_t h = s->poly_c;
    if (!s->poly_fast_valid
        || !traj_poly_stage(s->poly_s5, h, ticks, &h)
        || !traj_poly_stage(s->poly_j20, h, ticks, &h)
        || !traj_poly_stage(s->poly_a60, h, ticks, &h)
        || !traj_poly_stage(s->poly_v120, h, ticks, &h))
        return 0;
    *position120 = (int64_t)h * ticks;
    return 1;
}
#endif

// Exact -residual/velocity quotient, saturated to limit, without pulling the
// compiler's general signed 64-bit division helper into the precision-timer
// IRQ.  Accepting the residual before negation also makes INT64_MIN safe.
//
// At a normal pulse boundary the residual is at most one microstep plus a
// tick of polynomial quantization, so its magnitude has a high word of zero
// or one.  Those two common cases take one or two 32-bit divisions.  The
// bitwise fallback handles a larger residual (for example after polling away
// from zero velocity) and remains bounded to 32 iterations.  The preceding
// product comparison both applies the solver's duration clamp and guarantees
// that the fallback quotient fits uint32_t.
static int64_t
traj_divide_residual(int64_t numerator, int32_t denominator, uint32_t limit)
{
    if (!denominator || !limit)
        return 0;
    uint8_t negative = (numerator < 0) == (denominator < 0);
    uint64_t magnitude = numerator < 0
        ? -(uint64_t)numerator : (uint64_t)numerator;
    uint32_t divisor = denominator < 0
        ? -(uint32_t)denominator : (uint32_t)denominator;
    uint64_t bound = (uint64_t)divisor * limit;
    uint32_t quotient;
    if (magnitude >= bound) {
        quotient = limit;
    } else {
        uint32_t hi = magnitude >> 32;
        uint32_t lo = magnitude;
        if (!hi) {
            quotient = lo / divisor;
        } else {
            // Since magnitude < divisor*limit and limit is uint32, hi is
            // smaller than divisor.  Decompose each 2^32 high-word unit into
            // a quotient/remainder pair, then account for lo.  The common
            // quintic residual has a small hi, so its correction fits 32 bits
            // and needs only three 32-bit divisions in total.
            uint64_t base_qr = traj_udivmod32_pair(UINT32_MAX, divisor);
            uint32_t base_q = base_qr;
            uint32_t base_r = (base_qr >> 32) + 1U;
            if (base_r == divisor) {
                base_q++;
                base_r = 0;
            }
            uint64_t lo_qr = traj_udivmod32_pair(lo, divisor);
            uint32_t lo_q = lo_qr;
            uint32_t lo_r = lo_qr >> 32;
            uint64_t correction_wide = (uint64_t)hi * base_r + lo_r;
            if (correction_wide <= UINT32_MAX) {
                uint32_t correction = correction_wide;
                quotient = hi * base_q + lo_q + correction / divisor;
            } else {
                // Rare exact fallback for a large adversarial residual.
                quotient = 0;
                int8_t bit;
                for (bit = 31; bit >= 0; bit--) {
                    uint64_t term = (uint64_t)divisor << bit;
                    if (magnitude >= term) {
                        magnitude -= term;
                        quotient |= 1U << bit;
                    }
                }
            }
        }
    }
    return negative ? -(int64_t)quotient : (int64_t)quotient;
}

// Solve for the tick (relative to segment start) where the active
// segment next crosses s->target16, strictly after s->t_prev.
// Returns 1 with *step_t set, 0 if no crossing before segment end.
static int
traj_solve_step(struct traj_stepper *s, uint32_t *step_t)
{
    struct trajq *tq = &s->tq;
    int32_t dir = s->dir;
    // A boundary wake may enter here with the segment already exhausted.
    // In particular, a pure hold has zero velocity throughout.  Treating
    // that state as another zero-velocity poll would reschedule the timer at
    // the same (now expired) segment-end clock forever instead of allowing
    // traj_stepper_schedule() to advance the queue.
    if (s->t_prev >= tq->duration)
        return 0;
    // Does this segment reach the target at all?
    if (dir > 0 ? s->q16_end < s->target16 : s->q16_end > s->target16)
        return 0;
    // Cruise is overwhelmingly the common case.  Avoid running the generic
    // Newton solver (and its repeated signed 64-bit divisions) in the timer
    // IRQ for every pulse.  q(t)=v*t crossings form a quotient/remainder
    // recurrence: division is paid once when the segment loads, and each
    // following pulse needs only additions.  This keeps precision timer
    // clients such as the TMC software UART inside their bit sampling window
    // on division-poor MCUs (RP2040 M0+).
    if (traj_stepper_is_pure_cruise(tq)) {
        int64_t target = s->target16;
        uint32_t t;
        if ((dir > 0 && target <= 0) || (dir < 0 && target >= 0)) {
            t = s->t_prev + 1;
        } else {
            uint32_t speed = tq->velocity < 0
                ? -(uint32_t)tq->velocity : (uint32_t)tq->velocity;
            if (!speed)
                return 0;
            if (s->cruise_speed != speed)
                traj_cruise_setup(s);
            if (!s->cruise_valid || s->cruise_target16 != target)
                traj_cruise_advance_target(s);
            if (!s->cruise_valid)
                return 0;
            uint64_t crossing = s->cruise_q + !!s->cruise_r;
            if (crossing > tq->duration)
                return 0;
            t = (uint32_t)crossing;
            if (t <= s->t_prev)
                t = s->t_prev + 1;
        }
        if (t > tq->duration)
            return 0;
        *step_t = t;
        return 1;
    }
#if CONFIG_WANT_TRAJECTORY_HIGHER_ORDER
    // At a continuous same-direction segment boundary, preserve the prior
    // full-step interval and scale it by the remaining microstep phase.  The
    // fitter's bounded derivatives make this first estimate substantially
    // tighter than the 1/8-step execution tolerance, so scheduling it
    // directly avoids paying a complete Horner evaluation in the boundary
    // IRQ.  The following pulse resumes the corrected interval recurrence.
    if ((tq->seg_flags & TSEG_POLY_MASK) && s->step_interval
        && !s->last_step_t && s->first_step_guess > s->t_prev
        && s->first_step_guess <= tq->duration) {
        *step_t = s->first_step_guess;
        return 1;
    }
#endif
    uint32_t t = s->t_prev;
    uint32_t tmax = tq->duration;
    int i;
#if CONFIG_WANT_TRAJECTORY_HIGHER_ORDER
    // Smooth fitted curves change their pulse interval gradually.  Seed from
    // the preceding interval when it remains ahead of any intervening poll;
    // Newton then corrects only the interval-to-interval curvature error.
    if ((tq->seg_flags & TSEG_POLY_MASK) && s->step_interval) {
        uint32_t predicted = s->last_step_t
            ? s->last_step_t + s->step_interval : s->first_step_guess;
        if (predicted > t && predicted <= tmax)
            t = predicted;
    }
#endif
    // A recurring quintic needs one Newton correction at microstep scale,
    // after which
    // the bounded crossing check below removes any tiny undershoot. Further
    // corrections do not materially improve its pulse clock because the
    // fixed-point polynomial is quantized, and the former eight-iteration
    // oscillation could consume more than the next pulse interval on M0+.
    // Quadratic acceleration profiles retain the convergent correction loop
    // needed to match Klipper V1 edge timing; their evaluator is much cheaper.
    int corrections = 8;
#if CONFIG_WANT_TRAJECTORY_HIGHER_ORDER
    if (tq->seg_flags & TSEG_POLY_MASK) {
        corrections = 1;
        // A path may legitimately begin at zero velocity.  POLL_TICKS is a
        // queue-idle horizon, not an appropriate first-crossing estimate: on
        // a short acceleration segment it can jump directly to the endpoint
        // and turn every earlier edge into catch-up pulses.  Seed inside the
        // first sixteenth of the segment and permit the full convergence loop
        // for this one cold crossing.  Once two pulses establish an interval,
        // all subsequent crossings use the single-correction recurrence.
        if (!s->step_interval) {
            // The second edge also precedes establishment of a full-step
            // interval, so retain full convergence until recurrence state is
            // available.
            corrections = 8;
            if (!t && tq->duration) {
                int64_t v0 = trajq_velocity24_at_seg_fast(tq, 0);
                if (dir > 0 ? v0 <= 0 : v0 >= 0) {
                    t = tq->duration >> 4;
                    if (!t)
                        t = 1;
                }
            }
        }
    }
#endif
    for (i = 0; i < corrections; i++) {
        int32_t v;
        int64_t err;
        uint8_t use_recip = 0;
#if CONFIG_WANT_TRAJECTORY_HIGHER_ORDER
        if (tq->seg_flags & TSEG_POLY_MASK) {
            // Horner returns 24*v and 120*q.  Newton's
            // -(120*q-120*target)/(5*24*v) has the same ratio as
            // -(q-target)/v, but pays no constant 64-bit divisions.
            int64_t position120;
            if (!traj_poly_pos120(s, t, &position120))
                position120 = trajq_pos120_at_seg_fast(tq, t);
            err = position120 - s->target16 * 120;
            if (s->step_interval) {
                // The previous full-step interval is an excellent velocity
                // reciprocal during steady motion, but it can lag sharply at
                // the first few edges of an acceleration ramp.  Refine only
                // while the reconstructed spatial error exceeds 1/8 step;
                // steady EBB extrusion remains a single cheap correction.
                uint8_t refine;
                for (refine = 0; refine < 4; refine++) {
                    int64_t dt = traj_interval_correction(
                        err, s->step_interval, dir, tmax);
                    int64_t nt = (int64_t)t + dt;
                    if (nt < 0)
                        nt = 0;
                    else if (nt > (int64_t)tmax)
                        nt = tmax;
                    t = nt;
                    if (!traj_poly_pos120(s, t, &position120))
                        position120 = trajq_pos120_at_seg_fast(tq, t);
                    err = position120 - s->target16 * 120;
                    uint64_t magnitude = err < 0
                        ? -(uint64_t)err : (uint64_t)err;
                    if (magnitude <= STEP_Q * 15)
                        break;
                }
                break;
            }
            int64_t v120 = trajq_velocity24_at_seg_fast(tq, t) * 5;
            // Normalize the common scale while retaining at least 16 bits of
            // velocity magnitude.  This keeps quotient quantization far
            // below one timer tick and brings high-rate residuals into the
            // quotient helper's single-word fast path.
            uint64_t speed120 = v120 < 0
                ? -(uint64_t)v120 : (uint64_t)v120;
            uint8_t scale_shift = 0;
            while (speed120 > 131071) {
                speed120 >>= 1;
                scale_shift++;
            }
            v120 = traj_signed_shr(v120, scale_shift);
            err = traj_signed_shr(err, scale_shift);
            v = (int32_t)v120;
            use_recip = 1;
        } else
#endif
        {
            v = trajq_velocity_at_seg_fast(tq, t);
            err = trajq_pos_at_seg_fast(tq, t) - s->target16;
        }
        if (dir > 0 ? v <= 0 : v >= 0) {
            // Not approaching at this tick (v ~= 0 while accelerating
            // from rest): step forward and retry.
            t += (tmax - t > POLL_TICKS ? POLL_TICKS : tmax - t);
            if (t >= tmax) {
                *step_t = tmax;
                return 2; // re-poll at segment end
            }
            continue;
        }
        int64_t dt = use_recip
            ? traj_recip_correction(s, err, v, tmax)
            : traj_divide_residual(err, v, tmax);
        int64_t nt = (int64_t)t + dt;
        if (nt < 0)
            nt = 0;
        else if (nt > (int64_t)tmax)
            nt = tmax;
        uint32_t prev = t;
        t = (uint32_t)nt;
        // Converged when the guess stops moving by more than a tick
        if (t == prev || (t > prev ? t - prev : prev - t) <= 1)
            break;
    }
    // Ensure monotonic progress.  The fitter supplies smooth bounded curves;
    // the on-silicon deadline regression independently checks that the
    // one-correction approximation remains within 1/8 microstep spatially.
    if (t <= s->t_prev)
        t = s->t_prev + 1;
    if (t > tmax)
        return 0;
    *step_t = t;
    return 1;
}

static int64_t traj_stepper_calc_target16(int64_t acc, int32_t mpos,
                                          int32_t dir);

#if CONFIG_WANT_SELF_TEST
// Exercise the exact state reached at a pure-hold boundary without touching
// GPIO or the live queue.  The built-in trajectory kernel self-test calls
// this on real silicon as a regression for same-clock timer livelock.
uint_fast8_t
traj_stepper_test_hold_boundary(void)
{
    struct traj_stepper s = { };
    uint32_t step_t = 0;
    s.tq.duration = 12000;
    s.t_prev = s.tq.duration;
    s.dir = -1;
    return traj_solve_step(&s, &step_t) == 0;
}

uint_fast8_t
traj_stepper_test_halfstep_phase(void)
{
    // The two nearest V1 quantization thresholds around physical mpos=0.
    int64_t positive = ((2LL * 0 + 1) << 47) >> 16;
    int64_t negative = ((2LL * 0 - 1) << 47) >> 16;
    return positive == STEP_Q / 2 && negative == -STEP_Q / 2;
}

uint_fast8_t
traj_stepper_test_cruise_recurrence(void)
{
    // Compare the division-free recurring path with the closed-form crossing
    // for enough pulses to exercise many remainder carries in both directions.
    int8_t dir;
    for (dir = -1; dir <= 1; dir += 2) {
        struct traj_stepper s = { };
        s.tq.duration = 600000;
        s.tq.velocity = dir * 1145325;
        s.dir = dir;
        s.target16 = dir * (STEP_Q / 2);
        s.q16_end = (int64_t)s.tq.velocity * s.tq.duration;
        traj_cruise_setup(&s);
        uint32_t i;
        for (i = 0; i < 128; i++) {
            uint64_t magnitude = s.target16 < 0
                ? -(uint64_t)s.target16 : (uint64_t)s.target16;
            uint32_t want = (magnitude + 1145325 - 1) / 1145325;
            uint32_t got;
            if (traj_solve_step(&s, &got) != 1 || got != want)
                return 0;
            s.t_prev = got;
            s.target16 += dir > 0 ? STEP_Q : -STEP_Q;
        }
    }
    return 1;
}

uint_fast8_t
traj_stepper_test_quintic_deadline(uint32_t *max_elapsed_out)
{
#if !CONFIG_WANT_TRAJECTORY_HIGHER_ORDER
    *max_elapsed_out = 0;
    return 1;
#else
    // A real fitted acceleration ramp begins with v=0.  Qualify the two cold
    // crossings before a recurring interval exists; these are the only
    // quintic edges that intentionally use the full convergence loop.
    struct traj_stepper cold = { };
    cold.tq.seg_flags = TSEG_POLY_QUINTIC | TSEG_LOCAL_TIME;
    cold.tq.duration = 792000;
    cold.tq.accel = 469125;
    cold.dir = 1;
    cold.q16_end = trajq_end_delta_seg(&cold.tq) >> 16;
    cold.target16 = STEP_Q / 2;
    traj_poly_fast_setup(&cold);
    uint32_t cold_max = 0;
    uint8_t cold_edge;
    for (cold_edge = 0; cold_edge < 2; cold_edge++) {
        uint32_t step_t, before = timer_read_time();
        int result = traj_solve_step(&cold, &step_t);
        uint32_t elapsed = timer_read_time() - before;
        if (elapsed > cold_max)
            cold_max = elapsed;
        if (result != 1 || step_t <= cold.t_prev) {
            *max_elapsed_out = 0x00800000 | cold_edge;
            return 0;
        }
        uint32_t interval = step_t - cold.t_prev;
        int64_t error = trajq_pos120_at_seg_fast(
            &cold.tq, step_t) - cold.target16 * 120;
        uint64_t magnitude = error < 0
            ? -(uint64_t)error : (uint64_t)error;
        if (magnitude > STEP_Q * 15) {
            *max_elapsed_out = 0x00900000 | cold_edge;
            return 0;
        }
        if (elapsed >= interval - interval / 4) {
            *max_elapsed_out = 0x00a00000 | (elapsed & 0x000fffff);
            return 0;
        }
        if (cold.last_step_t)
            cold.step_interval = step_t - cold.last_step_t;
        cold.last_step_t = step_t;
        cold.t_prev = step_t;
        cold.target16 += STEP_Q;
    }

    // The first non-degenerate segment from the EBB36 hot-extrusion
    // qualification.  The old eight-correction Newton loop oscillated on
    // these quantized coefficients and overran the STM32G0 step timer.  Time
    // compress the same geometric curve to exercise 1x through 16x pulse
    // rates without changing its distance: duration scales by 1/k and its
    // nth derivative by k^n.  The last case is approximately 20k extruder
    // steps/s.  This is a performance qualification, so do not lower the
    // maximum scale merely to make a slower implementation pass.
    uint32_t overall_max = cold_max;
    uint8_t scale, rate_stage = 0;
    for (scale = 1; scale <= 16; scale <<= 1) {
        rate_stage++;
        uint32_t scale2 = (uint32_t)scale * scale;
        uint32_t scale3 = scale2 * scale;
        uint32_t scale4 = scale3 * scale;
        uint32_t scale5 = scale4 * scale;
        struct traj_stepper s = { };
        s.tq.seg_flags = TSEG_POLY_QUINTIC | TSEG_LOCAL_TIME;
        s.tq.duration = 1856000 / scale;
        s.tq.velocity = 77024 * scale;
        s.tq.accel = -2721 * scale2;
        s.tq.jerk = 1426 * scale3;
        s.tq.snap = -245 * scale4;
        s.tq.crackle = 17 * scale5;
        s.dir = 1;
        s.q16_end = trajq_end_delta_seg(&s.tq) >> 16;
        traj_poly_fast_setup(&s);
        // Segment begins at 404429 sub-units with physical position 6.
        int64_t acc = (int64_t)404429 << 32;
        s.target16 = traj_stepper_calc_target16(acc, 6, s.dir);
        // Model a continuous segment boundary: the prior segment supplies a
        // full-step interval, while this segment's first crossing is only the
        // remaining fraction of a microstep away.
        s.step_interval = 56912 / scale;
        uint64_t first_magnitude = s.target16 < 0
            ? -(uint64_t)s.target16 : (uint64_t)s.target16;
        s.first_step_guess = (first_magnitude * s.step_interval) >> 32;
        if (!s.first_step_guess)
            s.first_step_guess = 1;
        int64_t initial_v120 = trajq_velocity24_at_seg_fast(&s.tq, 0) * 5;
        uint64_t initial_speed = initial_v120 < 0
            ? -(uint64_t)initial_v120 : (uint64_t)initial_v120;
        while (initial_speed > 131071) {
            initial_speed >>= 1;
            initial_v120 = traj_signed_shr(initial_v120, 1);
        }
        traj_recip_init(&s, initial_v120 < 0
                        ? -(uint32_t)initial_v120 : (uint32_t)initial_v120);
        uint32_t count = 0, max_elapsed = 0;
        for (;;) {
            uint32_t step_t;
            uint32_t before = timer_read_time();
            int result = traj_solve_step(&s, &step_t);
            uint32_t elapsed = timer_read_time() - before;
            if (elapsed > max_elapsed)
                max_elapsed = elapsed;
            if (!result)
                break;
            if (result != 1 || step_t <= s.t_prev
                || step_t > s.tq.duration) {
                *max_elapsed_out = 0x40000000 | count;
                return 0;
            }
            uint32_t interval = step_t - s.t_prev;
            int64_t spatial_error = trajq_pos120_at_seg_fast(
                &s.tq, step_t) - s.target16 * 120;
            uint64_t error_magnitude = spatial_error < 0
                ? -(uint64_t)spatial_error : (uint64_t)spatial_error;
            if (error_magnitude > STEP_Q * 15) { // 120 / 8
                *max_elapsed_out = 0x10000000 | count;
                return 0;
            }
            if (elapsed >= interval - interval / 4) {
                *max_elapsed_out = ((uint32_t)rate_stage << 20)
                    | (elapsed & 0x000fffff);
                return 0;
            }
            if (s.last_step_t)
                s.step_interval = step_t - s.last_step_t;
            s.last_step_t = step_t;
            s.t_prev = step_t;
            s.target16 += STEP_Q;
            if (++count > 64) {
                *max_elapsed_out = 0x20000000 | count;
                return 0;
            }
        }
        if (max_elapsed > overall_max)
            overall_max = max_elapsed;
        // The next boundary must be the first one beyond the exact endpoint.
        // The captured 1x vector additionally fixes its known pulse count.
        if (s.q16_end >= s.target16
            || s.q16_end < s.target16 - STEP_Q
            || (scale == 1 && count != 38)) {
            // Encode log2(rate)+1 in bits 20..23 for field diagnosis; the
            // low 20 bits retain the measured worst solve time.
            *max_elapsed_out = ((uint32_t)rate_stage << 20)
                | (max_elapsed & 0x000fffff);
            return 0;
        }
    }
    *max_elapsed_out = overall_max;
    return 1;
#endif
}

// Status values returned by the computation-only throughput probe.  Keep
// these stable: scripts/helix_traj_benchmark.py renders them by number.
enum {
    TB_PASS,
    TB_BAD_ARGS,
    TB_SETUP,
    TB_SOLVER,
    TB_SPATIAL,
    TB_DEADLINE,
};

#define TRAJ_BENCH_MAX_AXES 8
#define TRAJ_BENCH_PULSES 32

static struct traj_stepper traj_bench_axes[TRAJ_BENCH_MAX_AXES];

static uint_fast8_t
traj_bench_advance(struct traj_stepper *s, uint32_t step_t)
{
    if (step_t <= s->t_prev || step_t > s->tq.duration)
        return 0;
    if (s->last_step_t)
        s->step_interval = step_t - s->last_step_t;
    s->last_step_t = step_t;
    s->t_prev = step_t;
    s->target16 += STEP_Q;
    return 1;
}

uint_fast8_t
traj_stepper_benchmark(uint32_t step_rate, uint_fast8_t axes,
                       uint32_t *pulses_out, uint32_t *max_elapsed_out,
                       uint32_t *min_interval_out, uint32_t *max_error_out)
{
    *pulses_out = *max_elapsed_out = *min_interval_out = *max_error_out = 0;
#if !CONFIG_WANT_TRAJECTORY_HIGHER_ORDER
    return TB_SETUP;
#else
    // Leave enough ticks for a meaningful timer measurement and keep the
    // bounded segment below TRAJ_MAX_DURATION.  The polynomial fast path
    // also requires 120*velocity to fit int32.
    if (!axes || axes > TRAJ_BENCH_MAX_AXES || step_rate < 1000
        || step_rate > CONFIG_CLOCK_FREQ / 128)
        return TB_BAD_ARGS;
    uint32_t nominal_interval = CONFIG_CLOCK_FREQ / step_rate;
    uint32_t duration = nominal_interval * 48;
    if (!nominal_interval || duration >= TRAJ_MAX_DURATION)
        return TB_BAD_ARGS;
    uint64_t v64 = ((uint64_t)STEP_Q * step_rate
                    + CONFIG_CLOCK_FREQ / 2) / CONFIG_CLOCK_FREQ;
    if (!v64 || v64 > INT32_MAX / 120)
        return TB_SETUP;
    int32_t velocity = v64;

    // Increase velocity by roughly 10 percent over the segment.  Successive
    // derivatives are chosen so their endpoint contributions form a bounded
    // series; at practical H7 rates all five polynomial coefficients are
    // non-zero, while lower-rate quantization degrades safely to cubic or
    // quadratic acceleration through the identical quintic execution path.
    uint64_t a64 = ((v64 / 16) << 16) / duration;
    if (!a64)
        a64 = 1;
    uint64_t j64 = (a64 << 16) / duration;
    uint64_t s64 = (j64 << 16) / duration;
    uint64_t c64 = (s64 << 16) / duration;
    if (a64 > INT32_MAX / 60 || j64 > INT32_MAX / 20
        || s64 > INT32_MAX / 5 || c64 > INT32_MAX)
        return TB_SETUP;

    uint_fast8_t axis;
    for (axis = 0; axis < axes; axis++) {
        struct traj_stepper *s = &traj_bench_axes[axis];
        memset(s, 0, sizeof(*s));
        s->tq.seg_flags = TSEG_POLY_QUINTIC | TSEG_LOCAL_TIME;
        s->tq.duration = duration;
        s->tq.velocity = velocity;
        s->tq.accel = a64;
        s->tq.jerk = j64;
        s->tq.snap = s64;
        s->tq.crackle = c64;
        s->dir = 1;
        s->q16_end = trajq_end_delta_seg(&s->tq) >> 16;
        s->target16 = STEP_Q / 2;
        traj_poly_fast_setup(s);
        if (!s->poly_fast_valid)
            return TB_SETUP;

        // Warm the two startup crossings just as the live backend does.  The
        // fixed self-test above separately qualifies their cold deadline;
        // this probe measures sustained recurring synthesis capacity.
        uint_fast8_t warm;
        for (warm = 0; warm < 2; warm++) {
            uint32_t step_t;
            if (traj_solve_step(s, &step_t) != 1
                || !traj_bench_advance(s, step_t))
                return TB_SOLVER;
        }
    }

    uint32_t max_elapsed = 0, min_interval = UINT32_MAX;
    uint32_t max_error = 0;
    uint_fast8_t pulse;
    for (pulse = 0; pulse < TRAJ_BENCH_PULSES; pulse++) {
        uint32_t step_times[TRAJ_BENCH_MAX_AXES];
        uint32_t before = timer_read_time();
        for (axis = 0; axis < axes; axis++) {
            if (traj_solve_step(&traj_bench_axes[axis],
                                &step_times[axis]) != 1)
                return TB_SOLVER;
        }
        uint32_t elapsed = timer_read_time() - before;
        if (elapsed > max_elapsed)
            max_elapsed = elapsed;

        uint32_t round_interval = UINT32_MAX;
        for (axis = 0; axis < axes; axis++) {
            struct traj_stepper *s = &traj_bench_axes[axis];
            uint32_t interval = step_times[axis] - s->t_prev;
            if (!interval || !traj_bench_advance(s, step_times[axis]))
                return TB_SOLVER;
            if (interval < round_interval)
                round_interval = interval;
            int64_t error120 = trajq_pos120_at_seg_fast(
                &s->tq, step_times[axis]) - (s->target16 - STEP_Q) * 120;
            uint64_t magnitude120 = error120 < 0
                ? -(uint64_t)error120 : (uint64_t)error120;
            uint64_t error = magnitude120 / 120;
            if (error > UINT32_MAX)
                error = UINT32_MAX;
            if (error > max_error)
                max_error = error;
            if (magnitude120 > STEP_Q * 15) {
                *pulses_out = pulse;
                *max_elapsed_out = max_elapsed;
                *min_interval_out = round_interval;
                *max_error_out = max_error;
                return TB_SPATIAL;
            }
        }
        if (round_interval < min_interval)
            min_interval = round_interval;
        if (elapsed >= round_interval - round_interval / 4) {
            *pulses_out = pulse + 1;
            *max_elapsed_out = max_elapsed;
            *min_interval_out = min_interval;
            *max_error_out = max_error;
            return TB_DEADLINE;
        }
    }
    *pulses_out = TRAJ_BENCH_PULSES;
    *max_elapsed_out = max_elapsed;
    *min_interval_out = min_interval;
    *max_error_out = max_error;
    return TB_PASS;
#endif
}
#endif

// Set up solver state when a segment becomes active
static int64_t
traj_stepper_calc_target16(int64_t acc, int32_t mpos, int32_t dir)
{
    int32_t phase_mpos = (uint16_t)mpos;
    if (phase_mpos & 0x8000)
        phase_mpos -= 0x10000;
    int32_t half_phase = 2 * phase_mpos + (dir > 0 ? 1 : -1);
    uint64_t boundary = (uint64_t)(int64_t)half_phase << 47;
    return (int64_t)(boundary - (uint64_t)acc) >> 16;
}

static void
traj_stepper_load(struct traj_stepper *s)
{
    struct trajq *tq = &s->tq;
    uint32_t prior_interval = s->step_interval;
    int8_t prior_dir = s->dir;
    int32_t v0 = tq->velocity;
    int32_t vend = trajq_velocity_at_seg(tq, tq->duration);
    int8_t dir = v0 ? (v0 > 0 ? 1 : -1)
        : (tq->accel ? (tq->accel > 0 ? 1 : -1) : (vend > 0 ? 1 : -1));
    s->dir = dir;
    s->t_prev = 0;
    s->last_step_t = 0;
    if (prior_dir != dir)
        prior_interval = 0;
    s->step_interval = prior_interval;
    s->first_step_guess = 0;
    s->q16_end = trajq_end_delta_seg(tq) >> 16;
#if CONFIG_WANT_TRAJECTORY_HIGHER_ORDER
    traj_poly_fast_setup(s);
#endif
    // Match Klipper's legacy itersolve quantizer: the physical integer
    // microstep position changes when the continuous commanded position
    // crosses the half-step boundary around it.  mpos is deliberately
    // independent of tq->acc -- a logical coordinate rebase must not invent
    // physical pulses or reset the MCU's step count.
    // tq->acc is a modulo-2^64 phase.  Reduce the physical counter to the
    // matching 65536-microstep phase and subtract as unsigned arithmetic so
    // crossing +/-2^31 sub-units remains a small, well-defined delta.
    s->target16 = traj_stepper_calc_target16(tq->acc, s->mpos, dir);
#if CONFIG_WANT_TRAJECTORY_HIGHER_ORDER
    if ((tq->seg_flags & TSEG_POLY_MASK) && prior_interval) {
        uint64_t magnitude = s->target16 < 0
            ? -(uint64_t)s->target16 : (uint64_t)s->target16;
        if (magnitude > STEP_Q)
            magnitude = STEP_Q;
        s->first_step_guess = (magnitude * prior_interval) >> 32;
        if (!s->first_step_guess)
            s->first_step_guess = 1;
    }
#endif
#if CONFIG_WANT_TRAJECTORY_HIGHER_ORDER
    if (tq->seg_flags & TSEG_POLY_MASK) {
        int64_t v120 = trajq_velocity24_at_seg_fast(tq, 0) * 5;
        uint64_t speed = v120 < 0 ? -(uint64_t)v120 : (uint64_t)v120;
        uint8_t shift = 0;
        while (speed > 131071) {
            speed >>= 1;
            shift++;
        }
        v120 = traj_signed_shr(v120, shift);
        uint32_t normalized = v120 < 0
            ? -(uint32_t)v120 : (uint32_t)v120;
        if (normalized && (!s->recip_speed
            || normalized > s->recip_speed * 2U
            || s->recip_speed > normalized * 2U))
            // Initial division is paid while loading the segment, not in the
            // recurring pulse path. Continuous segments reuse the prior r.
            traj_recip_init(s, normalized);
    } else
        s->recip_speed = s->recip_q39 = 0;
#endif
    if (traj_stepper_is_pure_cruise(tq))
        traj_cruise_setup(s);
    else
        s->cruise_valid = s->cruise_speed = 0;
    uint8_t dirstate = (dir > 0) ^ !!(s->flags & TSF_INVERT_DIR);
    if (dirstate != !!(s->flags & TSF_DIR_HIGH)) {
        s->flags ^= TSF_DIR_HIGH;
        gpio_out_write(s->dir_pin, dirstate);
    }
}

// Schedule the next event for the active segment (irqs off, in timer)
static uint_fast8_t
traj_stepper_schedule(struct traj_stepper *s)
{
    struct trajq *tq = &s->tq;
    for (;;) {
        uint32_t step_t;
        int res = traj_solve_step(s, &step_t);
        if (res == 1) {
            uint32_t dt = step_t - s->t_prev;
            if (dt > POLL_TICKS) {
                // Far away: re-poll part way there
                s->t_prev += POLL_TICKS;
                s->wake_kind = WK_POLL;
                s->time.waketime = tq->seg_start_clock + s->t_prev;
                return SF_RESCHEDULE;
            }
            if (s->last_step_t)
                s->step_interval = step_t - s->last_step_t;
            s->last_step_t = step_t;
            s->t_prev = step_t;
            s->wake_kind = WK_STEP;
            s->time.waketime = tq->seg_start_clock + step_t;
            return SF_RESCHEDULE;
        }
        if (res == 2) {
            // v ~= 0 region: re-poll later in the segment
            s->t_prev = step_t;
            s->wake_kind = WK_POLL;
            s->time.waketime = tq->seg_start_clock + step_t;
            return SF_RESCHEDULE;
        }
        // No further steps in this segment: run out its clock, then
        // advance the queue.
        uint32_t remaining = tq->duration - s->t_prev;
        if (remaining > POLL_TICKS) {
            s->t_prev += POLL_TICKS;
            s->wake_kind = WK_POLL;
            s->time.waketime = tq->seg_start_clock + s->t_prev;
            return SF_RESCHEDULE;
        }
        if (remaining) {
            s->t_prev = tq->duration;
            s->wake_kind = WK_POLL;
            s->time.waketime = tq->seg_start_clock + tq->duration;
            return SF_RESCHEDULE;
        }
        // At segment end
        if (trajq_advance(tq) != TQ_ADV_IDLE) {
            traj_stepper_load(s);
            continue;
        }
        return SF_DONE;
    }
}

static uint_fast8_t
traj_stepper_event(struct timer *t)
{
    struct traj_stepper *s = container_of(t, struct traj_stepper, time);
    if (s->wake_kind == WK_STEP) {
        gpio_out_toggle_noirq(s->step_pin);
        s->mpos += s->dir;
        s->target16 += s->dir > 0 ? STEP_Q : -STEP_Q;
        s->wake_kind = WK_UNSTEP;
        s->time.waketime += s->step_pulse_ticks;
        return SF_RESCHEDULE;
    }
    if (s->wake_kind == WK_UNSTEP)
        gpio_out_toggle_noirq(s->step_pin);
    uint_fast8_t ret = traj_stepper_schedule(s);
    if (ret == SF_RESCHEDULE && s->wake_kind == WK_STEP) {
        // Respect the minimum pulse spacing after an unstep
        uint32_t min_next = timer_read_time() + s->step_pulse_ticks;
        if (timer_is_before(s->time.waketime, min_next))
            s->time.waketime = min_next;
    }
    return ret;
}

// Backend ops

static void
traj_stepper_start(struct trajq *tq)
{
    struct traj_stepper *s = container_of(tq, struct traj_stepper, tq);
    uint32_t now = timer_read_time();
    if (timer_is_before(tq->seg_start_clock + PAST_GUARD_TICKS, now))
        shutdown("Trajectory anchor in past");
    traj_stepper_load(s);
    s->wake_kind = WK_POLL;
    s->t_prev = 0;
    s->time.waketime = tq->seg_start_clock;
    if (timer_is_before(s->time.waketime, now))
        s->time.waketime = now;
    sched_add_timer(&s->time);
}

static void
traj_stepper_stop(struct trajq *tq)
{
    struct traj_stepper *s = container_of(tq, struct traj_stepper, tq);
    sched_del_timer(&s->time);
    if (s->wake_kind == WK_UNSTEP)
        // Mid step pulse: complete the edge
        gpio_out_toggle_noirq(s->step_pin);
    s->wake_kind = WK_POLL;
    if (tq->flags & TQF_ACTIVE) {
        // Record the live sub-unit position at the moment of the stop
        uint32_t t = timer_read_time() - tq->seg_start_clock;
        if (t > tq->duration)
            t = tq->duration;
        tq->acc = trajq_acc_add(
            tq->acc, trajq_q16_to_acc(trajq_pos_at_seg(tq, t)));
        tq->seg_start_clock += t;
    }
}

static void
traj_stepper_rebase(struct trajq *tq, int32_t mpos)
{
    struct traj_stepper *s = container_of(tq, struct traj_stepper, tq);
    s->mpos = mpos;
}

static const struct trajq_backend_ops traj_stepper_ops = {
    .start = traj_stepper_start,
    .stop = traj_stepper_stop,
    .rebase = traj_stepper_rebase,
};

// Commands

void
command_config_traj_stepper(uint32_t *args)
{
    struct traj_stepper *s = oid_alloc(
        args[0], command_config_traj_stepper, sizeof(*s));
    if (args[3])
        s->flags = TSF_INVERT_STEP;
    if (args[4])
        s->flags |= TSF_INVERT_DIR;
    s->step_pin = gpio_out_setup(args[1], s->flags & TSF_INVERT_STEP);
    s->dir_pin = gpio_out_setup(args[2], !!(s->flags & TSF_INVERT_DIR));
    if (s->flags & TSF_INVERT_DIR)
        s->flags |= TSF_DIR_HIGH;
    s->step_pulse_ticks = args[5];
    s->time.func = traj_stepper_event;
    trajq_setup(&s->tq, args[0], &traj_stepper_ops, args[6]);
}
DECL_COMMAND(command_config_traj_stepper,
             "config_traj_stepper oid=%c step_pin=%c dir_pin=%c"
             " invert_step=%c invert_dir=%c step_pulse_ticks=%u"
             " underrun_decel=%u");

static struct traj_stepper *
traj_stepper_oid_lookup(uint8_t oid)
{
    return oid_lookup(oid, command_config_traj_stepper);
}

#if CONFIG_WANT_BOOTLOADER
// Strong override of the enter_bootloader activity hook: report the
// board as "mid-print" while any stepper has a segment executing or a
// synthesized ramp running, so an unforced enter_bootloader is
// refused until the host drains the queues (doc 08 / doc 11).
int
bootloader_entry_busy(void)
{
    uint8_t i;
    struct traj_stepper *s;
    foreach_oid(i, s, command_config_traj_stepper) {
        if (s->tq.flags & (TQF_ACTIVE | TQF_RAMPING))
            return 1;
    }
    return 0;
}
#endif

void
command_queue_traj_segment(uint32_t *args)
{
    struct traj_stepper *s = traj_stepper_oid_lookup(args[0]);
    trajq_queue_segment(&s->tq, args[1], args[2], args[3], args[4]);
}
DECL_COMMAND(command_queue_traj_segment,
             "queue_traj_segment oid=%c flags=%c duration=%u"
             " velocity=%i accel=%i");

#if CONFIG_WANT_TRAJECTORY_HIGHER_ORDER
void
command_queue_traj_segment_cubic(uint32_t *args)
{
    struct traj_stepper *s = traj_stepper_oid_lookup(args[0]);
    trajq_queue_segment_ho(&s->tq, args[1] | TSEG_POLY_CUBIC, args[2]
                           , args[3], args[4], args[5], 0, 0);
}
DECL_COMMAND(command_queue_traj_segment_cubic,
             "queue_traj_segment_cubic oid=%c flags=%c duration=%u"
             " velocity=%i accel=%i jerk=%i");

void
command_queue_traj_segment_quintic(uint32_t *args)
{
    struct traj_stepper *s = traj_stepper_oid_lookup(args[0]);
    trajq_queue_segment_ho(&s->tq, args[1] | TSEG_POLY_QUINTIC, args[2]
                           , args[3], args[4], args[5], args[6], args[7]);
}
DECL_COMMAND(command_queue_traj_segment_quintic,
             "queue_traj_segment_quintic oid=%c flags=%c duration=%u"
             " velocity=%i accel=%i jerk=%i snap=%i crackle=%i");
#endif

void
command_traj_hold(uint32_t *args)
{
    struct traj_stepper *s = traj_stepper_oid_lookup(args[0]);
    trajq_queue_segment(&s->tq, TSEG_HOLD_AT_END, args[1], 0, 0);
}
DECL_COMMAND(command_traj_hold, "traj_hold oid=%c duration=%u");

void
command_trajectory_rebase(uint32_t *args)
{
    struct traj_stepper *s = traj_stepper_oid_lookup(args[0]);
    trajq_rebase(&s->tq, args[1], args[2], args[3]);
}
DECL_COMMAND(command_trajectory_rebase,
             "trajectory_rebase oid=%c clock=%u pos=%i mcu_pos=%i");

void
command_traj_get_position(uint32_t *args)
{
    uint8_t oid = args[0];
    struct traj_stepper *s = traj_stepper_oid_lookup(oid);
    struct trajq *tq = &s->tq;
    irq_disable();
    uint32_t now = timer_read_time();
    int64_t acc = tq->acc;
    int32_t mpos = s->mpos;
    if (tq->flags & TQF_ACTIVE) {
        uint32_t t = now - tq->seg_start_clock;
        if (!timer_is_before(now, tq->seg_start_clock)) {
            if (t > tq->duration)
                t = tq->duration;
            acc = trajq_acc_add(
                acc, trajq_q16_to_acc(trajq_pos_at_seg(tq, t)));
        }
    }
    irq_enable();
    sendf("traj_position oid=%c clock=%u pos=%i mcu_pos=%i"
          , oid, now, (int32_t)(acc >> 32), mpos);
}
DECL_COMMAND(command_traj_get_position, "traj_get_position oid=%c");

void
command_traj_query(uint32_t *args)
{
    uint8_t oid = args[0];
    struct traj_stepper *s = traj_stepper_oid_lookup(oid);
    struct trajq *tq = &s->tq;
    irq_disable();
    uint8_t flags = tq->flags;
    uint32_t horizon = tq->horizon_clock;
    uint16_t queued = tq->queued;
    uint16_t dropped = tq->dropped;
    int32_t pos = (int32_t)(tq->acc >> 32);
    irq_enable();
    sendf("traj_status oid=%c flags=%c queued=%hu dropped=%hu"
          " horizon_clock=%u pos=%i"
          , oid, flags, queued, dropped, horizon, pos);
}
DECL_COMMAND(command_traj_query, "traj_query oid=%c");

// Homing/probing stop, underrun event reporting, shutdown

static void
traj_stepper_trigger_stop(struct trsync_signal *tss, uint8_t reason)
{
    struct traj_stepper *s = container_of(
        tss, struct traj_stepper, stop_signal);
    trajq_halt(&s->tq, TQF_NEED_REBASE);
    execlog_append(EL_TRIGGER, s->tq.oid, s->tq.seg_start_clock
                   , (int32_t)(s->tq.acc >> 32), reason);
}

void
command_traj_stop_on_trigger(uint32_t *args)
{
    struct traj_stepper *s = traj_stepper_oid_lookup(args[0]);
    struct trsync *ts = trsync_oid_lookup(args[1]);
    trsync_add_signal(ts, &s->stop_signal, traj_stepper_trigger_stop);
}
DECL_COMMAND(command_traj_stop_on_trigger,
             "traj_stop_on_trigger oid=%c trsync_oid=%c");

void
traj_stepper_task(void)
{
    if (!trajq_check_event_wake())
        return;
    uint8_t oid;
    struct traj_stepper *s;
    foreach_oid(oid, s, command_config_traj_stepper) {
        struct trajq *tq = &s->tq;
        irq_disable();
        uint8_t pending = tq->flags & TQF_EVENT_PENDING;
        uint32_t clock = tq->event_clock;
        int32_t pos = tq->event_pos;
        tq->flags &= ~TQF_EVENT_PENDING;
        irq_enable();
        if (pending)
            sendf("traj_underrun oid=%c clock=%u pos=%i", oid, clock, pos);
    }
}
DECL_TASK(traj_stepper_task);

void
traj_stepper_shutdown(void)
{
    uint8_t oid;
    struct traj_stepper *s;
    foreach_oid(oid, s, command_config_traj_stepper) {
        irq_disable();
        trajq_halt(&s->tq, TQF_NEED_REBASE);
        irq_enable();
    }
}
DECL_SHUTDOWN(traj_stepper_shutdown);
