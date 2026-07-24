// Trajectory intention segment core (FD-0001).
//
// Actuator-independent half of the intention protocol: the segment
// queue, exact chained-position accounting, underrun ramp synthesis,
// and hold/rebase state. Backends (traj_stepper.c) execute the
// active segment and call trajq_advance() at each segment boundary.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "autoconf.h" // CONFIG_WANT_TRACE, CONFIG_CLOCK_FREQ
#include "basecmd.h" // move_alloc
#include "board/irq.h" // irq_disable
#include "board/misc.h" // timer_read_time
#include "command.h" // shutdown
#include "execlog.h" // execlog_append
#include "sched.h" // sched_wake_task
#include "timesync.h" // machine-time to local execution conversion
#include "trajq.h" // trajq_setup
#if CONFIG_WANT_TRACE
#include "trace.h" // LOG*
#else
#define LOG1(sub, lvl, ev, a0) do { } while (0)
#define LOG2(sub, lvl, ev, a0, a1) do { } while (0)
#endif

static struct task_wake traj_event_wake;

static inline uint32_t
trajq_horizon_us(struct trajq *tq)
{
    int32_t ticks = tq->horizon_clock - timer_read_time();
    if (ticks <= 0)
        return 0;
    return (uint64_t)(uint32_t)ticks * 1000000 / CONFIG_CLOCK_FREQ;
}

// Instantaneous velocity (Q16.16 sub-units/tick) at tick t of a segment.
int32_t
trajq_velocity_at(int32_t velocity, int32_t accel, uint32_t t)
{
    return velocity + (int32_t)(((int64_t)accel * t) >> 16);
}

// Relative position (Q16.16 sub-units) at tick t of a segment.
// Intermediates stay in range for any segment accepted by
// trajq_end_delta()'s guards.
int64_t
trajq_pos_at(int32_t velocity, int32_t accel, uint32_t t)
{
    int64_t vterm = (int64_t)velocity * t;
    int64_t w = (int64_t)accel * t;
    int64_t aterm = ((w >> 16) * (int64_t)t) >> 1;
    return vterm + aterm;
}

// Multiply a 64-bit value by a 32-bit tick count, halve, with a
// 96-bit intermediate. Both the host fitter and the MCU use this
// exact convention (truncation toward zero), which is what makes
// chained integration drift-free.
static int64_t
mul64x32_half(int64_t a, uint32_t b)
{
    int neg = a < 0;
    uint64_t ua = neg ? -(uint64_t)a : (uint64_t)a;
    uint64_t lo = (ua & 0xffffffff) * b;
    uint64_t hi = (ua >> 32) * b;
    hi += lo >> 32;
    lo &= 0xffffffff;
    // result = (hi:lo) >> 1
    if (hi >> 62)
        shutdown("traj segment overflow");
    uint64_t r = (hi << 31) | (lo >> 1);
    return neg ? -(int64_t)r : (int64_t)r;
}

// Exact Q32.32 position delta over a whole segment.
int64_t
trajq_end_delta(uint32_t duration, int32_t velocity, int32_t accel)
{
    int64_t dv = (int64_t)velocity * duration;
    if (dv >= (1LL << 47) || dv <= -(1LL << 47))
        shutdown("traj segment overflow");
    int64_t delta = trajq_q16_to_acc(dv);
    if (accel) {
        int64_t w = (int64_t)accel * duration;
        delta += mul64x32_half(w, duration);
    }
    return delta;
}

#if CONFIG_WANT_TRAJECTORY_HIGHER_ORDER
// ---- higher-order (cubic / quintic) segment evaluation ----
//
// The polynomial (true derivatives at t=0, sub-units vs ticks):
//   q(t) = q0 + v*t + (1/2)a*t^2 + (1/6)j*t^3 + (1/24)s*t^4 + (1/120)c*t^5
//
// FIXED-POINT SCALING (extends the existing v=Q16.16, a=Q0.32 ladder:
// each higher derivative multiplies t once more, so it carries 16 more
// fractional bits than the one below it):
//   jerk    j : stored int32 = j_true * 2^48   (sub-units/tick^3)
//   snap    s : stored int32 = s_true * 2^64   (sub-units/tick^4)
//   crackle c : stored int32 = c_true * 2^80   (sub-units/tick^5)
//
// RANGE ANALYSIS (proving int32 storage suffices with headroom).
// Take the most aggressive physically reachable move and the worst
// (largest per-tick) parameters a trajectory MCU runs at:
//   J<=1e6 mm/s^3, S<=1e8 mm/s^4, C<=1e10 mm/s^5 (extreme S-curve),
//   sub-units/mm <= ~1e7 (fine microstepping), CLOCK_FREQ >= 12 MHz.
// Per-tick true values scale as (rate * su_per_mm / F^order):
//   j_true <= 1e6 *1e7 / (12e6)^3 = 5.8e-9 su/tick^3
//   s_true <= 1e8 *1e7 / (12e6)^4 = 4.8e-14 su/tick^4
//   c_true <= 1e10*1e7 / (12e6)^5 = 4.0e-19 su/tick^5
// Stored integers:
//   |j| <= 1.63e6, |s| <= 8.90e5, |c| <= 4.86e5
// all far inside int32 (+-2.1e9): >10 bits of headroom. No
// int32 wire field is needed. (At very high F the small higher-order
// corrections quantize to a few LSB; that is a resolution, not a range,
// limit and is harmless because the host fitter keeps the *quantized*
// polynomial inside its deviation tolerance regardless.)
//
// Evaluation stays in int64 using staged 96-bit multiply-shifts with an
// explicit overflow guard (shutdown), the same truncate-toward-zero
// convention as mul64x32_half. Any unphysical coeff/duration product
// that would exceed int64 is rejected rather than silently wrapped.
// The quadratic (v, a) terms are left byte-identical to the code above;
// only the j/s/c corrections use these helpers, so a segment with
// j=s=c=0 evaluates bit-for-bit the same whether or not this feature is
// compiled in.

// trunc_toward_zero(a * t) >> sh, with a 96-bit intermediate.
static int64_t
smul_shr(int64_t a, uint32_t t, unsigned sh)
{
    int neg = a < 0;
    uint64_t ua = neg ? -(uint64_t)a : (uint64_t)a;
    uint64_t lo = (ua & 0xffffffff) * t;
    uint64_t hi = (ua >> 32) * t;
    hi += lo >> 32;
    lo &= 0xffffffff;
    if (hi >> (31 + sh))
        shutdown("traj segment overflow");
    uint64_t r = sh ? ((hi << (32 - sh)) | (lo >> sh)) : ((hi << 32) | lo);
    return neg ? -(int64_t)r : (int64_t)r;
}

// Deadline-path specialization.  Physical fitted segments overwhelmingly
// keep each Horner intermediate in int32; in that range a single signed
// 32x32->64 multiply is sufficient.  Preserve the generic guarded 96-bit
// path for extreme-but-valid intermediates.
static inline int64_t
smul_shr_deadline(int64_t a, uint32_t t, unsigned sh)
{
    if (a > INT32_MAX || a < INT32_MIN)
        return smul_shr(a, t, sh);
    uint32_t magnitude32 = a < 0
        ? -(uint32_t)(int32_t)a : (uint32_t)a;
    uint32_t a0 = magnitude32 & 0xffff, a1 = magnitude32 >> 16;
    uint32_t t0 = t & 0xffff, t1 = t >> 16;
    uint32_t p0 = a0 * t0;
    uint32_t p1 = a0 * t1;
    uint32_t p2 = a1 * t0;
    uint32_t middle = (p0 >> 16) + (p1 & 0xffff) + (p2 & 0xffff);
    uint32_t product_lo = (p0 & 0xffff) | (middle << 16);
    uint32_t product_hi = a1 * t1 + (p1 >> 16) + (p2 >> 16)
        + (middle >> 16);
    uint64_t magnitude = ((uint64_t)product_hi << 32) | product_lo;
    if (!sh)
        return a < 0 ? -(int64_t)magnitude : (int64_t)magnitude;
    magnitude >>= sh;
    return a < 0 ? -(int64_t)magnitude : (int64_t)magnitude;
}

static inline int64_t
scale_i32_deadline(int32_t value, uint32_t factor)
{
    uint32_t magnitude = value < 0
        ? -(uint32_t)value : (uint32_t)value;
    if (magnitude <= UINT32_MAX / factor) {
        uint32_t product = magnitude * factor;
        return value < 0 ? -(int64_t)product : (int64_t)product;
    }
    return (int64_t)value * factor;
}

// Exact constant divisions used by the deadline-oriented Horner evaluators.
// A general signed 64-bit divide is particularly costly on Cortex-M0/M0+.
// Decomposing at 2^32 (whose remainder is 16 for both 24 and 120) reduces the
// work to one or two unsigned 32-bit divides and a very small correction loop.
// Keep quotient and remainder in one result so ARM EABI targets make a single
// __aeabi_uidivmod call.  noinline prevents LTO from splitting it back into
// separate quotient and remainder operations.
static uint64_t noinline
udivmod32_pair(uint32_t value, uint32_t divisor)
{
    return ((uint64_t)(value % divisor) << 32) | (value / divisor);
}

static int64_t
sdiv64_120(int64_t value)
{
    int negative = value < 0;
    uint64_t magnitude = negative ? -(uint64_t)value : (uint64_t)value;
    uint32_t hi = magnitude >> 32;
    uint32_t lo = magnitude;
    uint64_t hi_qr = udivmod32_pair(hi, 120);
    uint64_t lo_qr = udivmod32_pair(lo, 120);
    uint32_t qhi = hi_qr;
    uint32_t rhi = hi_qr >> 32;
    uint32_t qlo = lo_qr;
    uint32_t rlo = lo_qr >> 32;
    qlo += rhi * 35791394U; // floor(2^32 / 120)
    uint32_t correction = rhi * 16U + rlo;
    while (correction >= 120) {
        qlo++;
        correction -= 120;
    }
    uint64_t quotient = ((uint64_t)qhi << 32) | qlo;
    return negative ? -(int64_t)quotient : (int64_t)quotient;
}

static int32_t
sdiv64_24_to_s32(int64_t value)
{
    int negative = value < 0;
    uint64_t magnitude = negative ? -(uint64_t)value : (uint64_t)value;
    if (magnitude > (uint64_t)INT32_MAX * 24U)
        shutdown("traj segment overflow");
    uint32_t hi = magnitude >> 32; // at most 11 after the range check
    uint32_t lo = magnitude;
    uint32_t quotient = hi * 178956970U; // floor(2^32 / 24)
    uint64_t lo_qr = udivmod32_pair(lo, 24);
    uint32_t lo_q = lo_qr;
    uint32_t lo_r = lo_qr >> 32;
    quotient += lo_q;
    uint32_t correction = hi * 16U + lo_r;
    while (correction >= 24) {
        quotient++;
        correction -= 24;
    }
    return negative ? -(int32_t)quotient : (int32_t)quotient;
}

// coeff * t^nmul, with a >>16 after the first nsh multiplies, then a
// truncate-toward-zero divide by fact. Shifts are applied early so the
// running magnitude tracks the (small) true term value.
static int64_t
poly_term(int64_t coeff, uint32_t t, int nmul, int nsh, uint32_t fact)
{
    int64_t p = coeff;
    int i;
    for (i = 0; i < nmul; i++)
        p = smul_shr(p, t, i < nsh ? 16 : 0);
    if (fact > 1) {
        int neg = p < 0;
        uint64_t up = neg ? -(uint64_t)p : (uint64_t)p;
        up /= fact;
        p = neg ? -(int64_t)up : (int64_t)up;
    }
    return p;
}
#endif // CONFIG_WANT_TRAJECTORY_HIGHER_ORDER

// Instantaneous velocity (Q16.16) at tick t including j/s/c terms.
int32_t
trajq_velocity_at_seg(struct trajq *tq, uint32_t t)
{
    int32_t v = trajq_velocity_at(tq->velocity, tq->accel, t);
#if CONFIG_WANT_TRAJECTORY_HIGHER_ORDER
    uint8_t order = tq->seg_flags & TSEG_POLY_MASK;
    if (order) {
        // d/dt of (1/6)j t^3 = (1/2)j t^2, etc.
        v += (int32_t)poly_term(tq->jerk, t, 2, 2, 2);
        if (order == TSEG_POLY_QUINTIC) {
            v += (int32_t)poly_term(tq->snap, t, 3, 3, 6);
            v += (int32_t)poly_term(tq->crackle, t, 4, 4, 24);
        }
    }
#endif
    return v;
}

// Relative position (Q16.16) at tick t including j/s/c terms.
int64_t
trajq_pos_at_seg(struct trajq *tq, uint32_t t)
{
    int64_t p = trajq_pos_at(tq->velocity, tq->accel, t);
#if CONFIG_WANT_TRAJECTORY_HIGHER_ORDER
    uint8_t order = tq->seg_flags & TSEG_POLY_MASK;
    if (order) {
        p += poly_term(tq->jerk, t, 3, 2, 6);
        if (order == TSEG_POLY_QUINTIC) {
            p += poly_term(tq->snap, t, 4, 3, 24);
            p += poly_term(tq->crackle, t, 5, 4, 120);
        }
    }
#endif
    return p;
}

// Interrupt-deadline versions of the higher-order evaluators. Combining the
// Taylor terms over one denominator permits Horner evaluation with four
// multiply-shifts instead of independently constructing t^2 through t^5.
// The result may differ from the exact term-by-term evaluator by a tiny
// fraction of one microstep because truncation occurs after the combined
// numerator; it is used only to find pulse crossing clocks. Exact segment
// chaining and endpoint authority remain with trajq_end_delta_seg().
int64_t
trajq_pos120_at_seg_fast(struct trajq *tq, uint32_t t)
{
#if CONFIG_WANT_TRAJECTORY_HIGHER_ORDER
    uint8_t order = tq->seg_flags & TSEG_POLY_MASK;
    if (order) {
        // 120*q(t)/t = 120*v + x*(60*a + x*(20*j
        //                   + x*(5*s + x*c)))), x=t/2^16.
        int64_t h = order == TSEG_POLY_QUINTIC ? tq->crackle : 0;
        h = (order == TSEG_POLY_QUINTIC
             ? scale_i32_deadline(tq->snap, 5) : 0)
            + smul_shr_deadline(h, t, 16);
        h = scale_i32_deadline(tq->jerk, 20)
            + smul_shr_deadline(h, t, 16);
        h = scale_i32_deadline(tq->accel, 60)
            + smul_shr_deadline(h, t, 16);
        h = scale_i32_deadline(tq->velocity, 120)
            + smul_shr_deadline(h, t, 16);
        return smul_shr_deadline(h, t, 0);
    }
#endif
    return trajq_pos_at(tq->velocity, tq->accel, t) * 120;
}

int64_t
trajq_velocity24_at_seg_fast(struct trajq *tq, uint32_t t)
{
#if CONFIG_WANT_TRAJECTORY_HIGHER_ORDER
    uint8_t order = tq->seg_flags & TSEG_POLY_MASK;
    if (order) {
        // 24*q'(t) = 24*v + x*(24*a + x*(12*j
        //                    + x*(4*s + x*c))), x=t/2^16.
        int64_t h = order == TSEG_POLY_QUINTIC ? tq->crackle : 0;
        h = (order == TSEG_POLY_QUINTIC
             ? scale_i32_deadline(tq->snap, 4) : 0)
            + smul_shr_deadline(h, t, 16);
        h = scale_i32_deadline(tq->jerk, 12)
            + smul_shr_deadline(h, t, 16);
        h = scale_i32_deadline(tq->accel, 24)
            + smul_shr_deadline(h, t, 16);
        h = scale_i32_deadline(tq->velocity, 24)
            + smul_shr_deadline(h, t, 16);
        if ((h < 0 ? -(uint64_t)h : (uint64_t)h)
            > (uint64_t)INT32_MAX * 24U)
            shutdown("traj segment overflow");
        return h;
    }
#endif
    return (int64_t)trajq_velocity_at(tq->velocity, tq->accel, t) * 24;
}

int64_t
trajq_pos_at_seg_fast(struct trajq *tq, uint32_t t)
{
    return sdiv64_120(trajq_pos120_at_seg_fast(tq, t));
}

int32_t
trajq_velocity_at_seg_fast(struct trajq *tq, uint32_t t)
{
    return sdiv64_24_to_s32(trajq_velocity24_at_seg_fast(tq, t));
}

// Exact Q32.32 end-of-segment delta including j/s/c terms.
int64_t
trajq_end_delta_seg(struct trajq *tq)
{
    int64_t d = trajq_end_delta(tq->duration, tq->velocity, tq->accel);
#if CONFIG_WANT_TRAJECTORY_HIGHER_ORDER
    uint8_t order = tq->seg_flags & TSEG_POLY_MASK;
    if (order) {
        uint32_t D = tq->duration;
        d += poly_term(tq->jerk, D, 3, 1, 6);
        if (order == TSEG_POLY_QUINTIC) {
            d += poly_term(tq->snap, D, 4, 2, 24);
            d += poly_term(tq->crackle, D, 5, 3, 120);
        }
    }
#endif
    return d;
}

// Correct local velocity by the smallest integer wire amount that makes the
// locally executed polynomial land on the authoritative machine-time wire
// endpoint. Higher derivatives retain their rate-scaled shape; this removes
// their accumulated coefficient-rounding residue without cross-segment drift.
static int32_t
endpoint_velocity_correction(int64_t wire_delta, int64_t local_delta,
                             uint32_t local_duration)
{
    int64_t residual = wire_delta - local_delta;
    int64_t denominator = (int64_t)local_duration << 16;
    int64_t correction = residual / denominator;
    int64_t remainder = residual % denominator;
    uint64_t magnitude = remainder < 0
        ? -(uint64_t)remainder : (uint64_t)remainder;
    if (magnitude * 2 >= (uint64_t)denominator)
        correction += residual < 0 ? -1 : 1;
    if (correction > INT32_MAX || correction < INT32_MIN)
        shutdown("traj segment overflow");
    return (int32_t)correction;
}

void
trajq_setup(struct trajq *tq, uint8_t oid, const struct trajq_backend_ops *ops
            , uint32_t underrun_decel)
{
    tq->oid = oid;
    tq->ops = ops;
    tq->underrun_decel = underrun_decel;
    tq->flags = TQF_NEED_REBASE;
    move_queue_setup(&tq->mq, sizeof(struct traj_segment));
}

static void
trajq_apply_rebase(struct trajq *tq, uint32_t clock, int32_t pos, int32_t aux)
{
    tq->acc = (int64_t)((uint64_t)(uint32_t)pos << 32);
    tq->seg_start_clock = clock;
    if (tq->ops->rebase)
        tq->ops->rebase(tq, aux);
    execlog_append(EL_REBASE, tq->oid, clock, pos, 0);
    LOG1(TRACE_SUB_MOTION, TRACE_LVL_INFO, TRACE_EV_rebase, clock);
}

// Load the given coefficients as the active segment. Higher-order
// coefficients default to zero (a plain quadratic / ramp); the
// higher-order load path fills them in afterwards.
static void
trajq_load(struct trajq *tq, uint8_t flags, uint32_t duration
           , int32_t velocity, int32_t accel)
{
    tq->seg_flags = flags;
    tq->duration = duration;
    tq->velocity = velocity;
    tq->accel = accel;
#if CONFIG_WANT_TRAJECTORY_HIGHER_ORDER
    tq->jerk = tq->snap = tq->crackle = 0;
#endif
    // Internal locally synthesized segments (underrun ramps) use their
    // local polynomial as the authoritative delta. Wire segments replace
    // this with their retained machine-time delta after loading.
    tq->wire_delta = trajq_end_delta(duration, velocity, accel);
}

// Synthesize a deceleration-to-zero ramp from velocity v_end.
// Returns zero if v_end is already (effectively) zero.
static int
trajq_synth_ramp(struct trajq *tq, int32_t v_end)
{
    if (!v_end)
        return 0;
    uint32_t decel = tq->underrun_decel;
    if (!decel)
        // No emergency ramp configured - stop dead where we are
        return 0;
    uint32_t mag = v_end < 0 ? -(uint32_t)v_end : (uint32_t)v_end;
    // T = v / a : (Q16.16 << 16) / Q0.32 = ticks
    uint64_t ticks = ((uint64_t)mag << 16) / decel;
    int32_t a = v_end > 0 ? -(int32_t)decel : (int32_t)decel;
    if (!ticks)
        return 0;
    if (ticks >= TRAJ_MAX_DURATION) {
        // Ramp in bounded chunks; trajq_advance() re-enters here
        // with the remaining velocity.
        trajq_load(tq, 0, TRAJ_MAX_DURATION - 1, v_end, a);
    } else {
        trajq_load(tq, 0, (uint32_t)ticks, v_end, a);
    }
    tq->flags |= TQF_RAMPING;
    return 1;
}

// Complete the active segment and load the next one. Called by the
// backend (irqs off) when execution reaches the segment end. Returns
// TQ_ADV_SEG with a new active segment loaded, or TQ_ADV_IDLE.
int
trajq_advance(struct trajq *tq)
{
    // Chain: advance the exact anchor across the finished segment
    tq->acc = trajq_acc_add(tq->acc, tq->wire_delta);
    tq->seg_start_clock += tq->duration;
    int32_t v_end = trajq_velocity_at_seg(tq, tq->duration);
    execlog_append(EL_SEG_DONE, tq->oid, tq->seg_start_clock
                   , (int32_t)(tq->acc >> 32), 0);

    if (tq->flags & TQF_RAMPING) {
        // Emergency ramp in progress: keep ramping until stopped,
        // then latch the underrun.
        if (trajq_synth_ramp(tq, v_end))
            return TQ_ADV_SEG;
        tq->flags &= ~TQF_RAMPING;
        tq->flags |= TQF_UNDERRUN | TQF_NEED_REBASE | TQF_EVENT_PENDING;
        tq->event_clock = tq->seg_start_clock;
        tq->event_pos = (int32_t)(tq->acc >> 32);
        execlog_append(EL_UNDERRUN, tq->oid, tq->event_clock
                       , tq->event_pos, 0);
        LOG2(TRACE_SUB_MOTION, TRACE_LVL_WARN, TRACE_EV_step_underrun,
             trajq_horizon_us(tq), tq->queued);
        sched_wake_task(&traj_event_wake);
        tq->flags &= ~TQF_ACTIVE;
        return TQ_ADV_IDLE;
    }

    if (!move_queue_empty(&tq->mq)) {
        struct traj_segment *seg;
        uint8_t rebased = 0;
        for (;;) {
            struct move_node *mn = move_queue_pop(&tq->mq);
            seg = container_of(mn, struct traj_segment, node);
            tq->queued--;
            if (seg->kind != TSEGK_REBASE)
                break;
            // A rebase terminates one planned path and starts another.  The
            // old path must have explicitly reached a hold; otherwise this
            // barrier would conceal a real moving-queue underrun.
            if (v_end && !(tq->seg_flags & TSEG_HOLD_AT_END)) {
                move_free(seg);
                shutdown("Rebase after moving trajectory");
            }
            uint32_t clock = seg->duration;
            if (timer_is_before(clock, tq->seg_start_clock)) {
                move_free(seg);
                shutdown("Invalid trajectory rebase clock");
            }
            trajq_apply_rebase(tq, clock, seg->velocity, seg->accel);
            move_free(seg);
            rebased = 1;
            v_end = 0;
            if (move_queue_empty(&tq->mq)) {
                tq->flags &= ~TQF_ACTIVE;
                execlog_append(EL_HOLD, tq->oid, tq->seg_start_clock,
                               (int32_t)(tq->acc >> 32), 0);
                return TQ_ADV_IDLE;
            }
        }
        trajq_load(tq, seg->flags, seg->duration, seg->velocity, seg->accel);
        tq->wire_delta = seg->wire_delta;
#if CONFIG_WANT_TRAJECTORY_HIGHER_ORDER
        tq->jerk = seg->jerk;
        tq->snap = seg->snap;
        tq->crackle = seg->crackle;
#endif
        move_free(seg);
        return rebased ? TQ_ADV_REBASE : TQ_ADV_SEG;
    }

    // Queue ran dry. Stopped (or told to hold): idle at position.
    if (!v_end || tq->seg_flags & TSEG_HOLD_AT_END) {
        tq->flags &= ~TQF_ACTIVE;
        execlog_append(EL_HOLD, tq->oid, tq->seg_start_clock
                       , (int32_t)(tq->acc >> 32), 0);
        LOG1(TRACE_SUB_CORE, TRACE_LVL_INFO, TRACE_EV_hold_enter,
             tq->seg_flags & TSEG_HOLD_AT_END);
        return TQ_ADV_IDLE;
    }
    // Moving: synthesize the underrun deceleration ramp.
    if (trajq_synth_ramp(tq, v_end))
        return TQ_ADV_SEG;
    // No ramp possible (unconfigured or negligible velocity): latch.
    tq->flags |= TQF_UNDERRUN | TQF_NEED_REBASE | TQF_EVENT_PENDING;
    tq->event_clock = tq->seg_start_clock;
    tq->event_pos = (int32_t)(tq->acc >> 32);
    execlog_append(EL_UNDERRUN, tq->oid, tq->event_clock, tq->event_pos, 0);
    LOG2(TRACE_SUB_MOTION, TRACE_LVL_WARN, TRACE_EV_step_underrun,
         trajq_horizon_us(tq), tq->queued);
    sched_wake_task(&traj_event_wake);
    tq->flags &= ~TQF_ACTIVE;
    return TQ_ADV_IDLE;
}

void
trajq_queue_segment(struct trajq *tq, uint8_t flags, uint32_t duration
                    , int32_t velocity, int32_t accel)
{
    if (!timesync_class0_ok()) {
        // Machine-time discipline stale - refuse ingest (FD-0001 doc 01)
        tq->dropped++;
        return;
    }
    // The normal host fitter quantizes in this actuator's local timer domain
    // so its error proof includes the actual derivative resolution. A
    // machine-domain encoding remains supported for shared/broadcast users.
    uint8_t local_time = flags & TSEG_LOCAL_TIME;
    int64_t wire_delta = trajq_end_delta(duration, velocity, accel);
    if (!local_time) {
        duration = timesync_ticks_to_local(duration);
        velocity = timesync_derivative_to_local(velocity, 1);
        accel = timesync_derivative_to_local(accel, 2);
    }
    if (!duration)
        shutdown("Invalid traj segment");
    if (duration > TRAJ_MAX_DURATION && (velocity || accel))
        // Only pure holds (v=0, a=0) may exceed the duration cap
        shutdown("Invalid traj segment");
    if (flags & TSEG_POLY_MASK)
        // Polynomial orders beyond quadratic are not negotiated
        shutdown("Invalid traj segment");
    if (!local_time) {
        int32_t correction = endpoint_velocity_correction(
            wire_delta, trajq_end_delta(duration, velocity, accel), duration);
        int64_t corrected_velocity = (int64_t)velocity + correction;
        if (corrected_velocity > INT32_MAX
            || corrected_velocity < INT32_MIN)
            shutdown("traj segment overflow");
        velocity = corrected_velocity;
    }
    // Validate coefficient ranges up front (also computed on advance)
    trajq_end_delta(duration, velocity, accel);

    struct traj_segment *seg = move_alloc();
    seg->flags = flags;
    seg->kind = TSEGK_MOTION;
    seg->wire_delta = wire_delta;
    seg->duration = duration;
    seg->velocity = velocity;
    seg->accel = accel;

    irq_disable();
    if (tq->flags & (TQF_NEED_REBASE | TQF_UNDERRUN)) {
        // Stale stream: the anchor is invalid until the host rebases.
        // The host learns of this from traj_underrun/traj_status.
        tq->dropped++;
        irq_enable();
        move_free(seg);
        return;
    }
    tq->horizon_clock += duration;
    if (tq->flags & TQF_ACTIVE) {
        move_queue_push(&seg->node, &tq->mq);
        tq->queued++;
        LOG2(TRACE_SUB_MOTION, TRACE_LVL_INFO, TRACE_EV_queue_refill,
             tq->queued, 1);
        irq_enable();
        return;
    }
    // Idle: this segment starts at the current anchor clock
    trajq_load(tq, seg->flags, seg->duration, seg->velocity, seg->accel);
    tq->wire_delta = seg->wire_delta;
    tq->flags |= TQF_ACTIVE;
    tq->ops->start(tq);
    irq_enable();
    move_free(seg);
}

#if CONFIG_WANT_TRAJECTORY_HIGHER_ORDER
// Ingest a cubic (jerk) or quintic (jerk/snap/crackle) segment. The
// caller has set the polynomial-order bits in flags. Unused higher
// coefficients must be zero. Mirrors trajq_queue_segment plus the
// higher-order coefficient plumbing and range validation.
void
trajq_queue_segment_ho(struct trajq *tq, uint8_t flags, uint32_t duration
                       , int32_t velocity, int32_t accel, int32_t jerk
                       , int32_t snap, int32_t crackle)
{
    if (!timesync_class0_ok()) {
        tq->dropped++;
        return;
    }
    uint8_t local_time = flags & TSEG_LOCAL_TIME;
    struct trajq wire;
    trajq_load(&wire, flags, duration, velocity, accel);
    wire.jerk = jerk;
    wire.snap = snap;
    wire.crackle = crackle;
    int64_t wire_delta = trajq_end_delta_seg(&wire);
    if (!local_time) {
        duration = timesync_ticks_to_local(duration);
        velocity = timesync_derivative_to_local(velocity, 1);
        accel = timesync_derivative_to_local(accel, 2);
        jerk = timesync_derivative_to_local(jerk, 3);
        snap = timesync_derivative_to_local(snap, 4);
        crackle = timesync_derivative_to_local(crackle, 5);
    }
    if (!duration)
        shutdown("Invalid traj segment");
    if (duration > TRAJ_MAX_DURATION)
        // Higher-order segments never use the extended (hold) range
        shutdown("Invalid traj segment");
    uint8_t order = flags & TSEG_POLY_MASK;
    if (order != TSEG_POLY_CUBIC && order != TSEG_POLY_QUINTIC)
        shutdown("Invalid traj segment");
    if (order == TSEG_POLY_CUBIC && (snap || crackle))
        shutdown("Invalid traj segment");

    if (!local_time) {
        struct trajq local;
        trajq_load(&local, flags, duration, velocity, accel);
        local.jerk = jerk;
        local.snap = snap;
        local.crackle = crackle;
        int32_t correction = endpoint_velocity_correction(
            wire_delta, trajq_end_delta_seg(&local), duration);
        int64_t corrected_velocity = (int64_t)velocity + correction;
        if (corrected_velocity > INT32_MAX
            || corrected_velocity < INT32_MIN)
            shutdown("traj segment overflow");
        velocity = corrected_velocity;
    }

    struct traj_segment *seg = move_alloc();
    seg->flags = flags;
    seg->kind = TSEGK_MOTION;
    seg->wire_delta = wire_delta;
    seg->duration = duration;
    seg->velocity = velocity;
    seg->accel = accel;
    seg->jerk = jerk;
    seg->snap = snap;
    seg->crackle = crackle;

    irq_disable();
    if (tq->flags & (TQF_NEED_REBASE | TQF_UNDERRUN)) {
        tq->dropped++;
        irq_enable();
        move_free(seg);
        return;
    }
    // Validate coefficient ranges up front (also computed on advance);
    // trajq_end_delta_seg() shuts down on any int64 overflow. Load into
    // the (idle) active slot so the shared evaluator sees the coeffs.
    if (!(tq->flags & TQF_ACTIVE)) {
        trajq_load(tq, seg->flags, seg->duration, seg->velocity, seg->accel);
        tq->wire_delta = seg->wire_delta;
        tq->jerk = jerk;
        tq->snap = snap;
        tq->crackle = crackle;
        tq->horizon_clock += duration;
        trajq_end_delta_seg(tq);
        tq->flags |= TQF_ACTIVE;
        tq->ops->start(tq);
        irq_enable();
        move_free(seg);
        return;
    }
    // Active: validate against a scratch load without disturbing the
    // executing segment, then queue.
    struct trajq scratch;
    scratch.seg_flags = seg->flags;
    scratch.duration = seg->duration;
    scratch.velocity = seg->velocity;
    scratch.accel = seg->accel;
    scratch.jerk = jerk;
    scratch.snap = snap;
    scratch.crackle = crackle;
    trajq_end_delta_seg(&scratch);
    tq->horizon_clock += duration;
    move_queue_push(&seg->node, &tq->mq);
    tq->queued++;
    LOG2(TRACE_SUB_MOTION, TRACE_LVL_INFO, TRACE_EV_queue_refill,
         tq->queued, 1);
    irq_enable();
}
#endif // CONFIG_WANT_TRAJECTORY_HIGHER_ORDER

static int
trajq_rebase_at_local_clock(struct trajq *tq, uint32_t clock, int32_t pos,
                            int32_t aux, uint8_t recovery)
{
    struct traj_segment *barrier = move_alloc();
    barrier->kind = TSEGK_REBASE;
    barrier->flags = 0;
    barrier->duration = clock;
    barrier->velocity = pos;
    barrier->accel = aux;
    irq_disable();
    if (tq->flags & TQF_HALT_BARRIER) {
        if (!recovery) {
            // A multi-MCU trigger relay can overtake trajectory commands
            // that were already staged in another command queue.  Those
            // stale rebases must not clear NEED_REBASE and restart the
            // interrupted suffix.  The host acknowledges the stop with the
            // distinct recovery-rebase command after all trsync peers have
            // reached their terminal state.
            tq->dropped++;
            irq_enable();
            move_free(barrier);
            return 0;
        }
        if ((tq->flags & TQF_ACTIVE)
            || !(tq->flags & TQF_NEED_REBASE)) {
            tq->dropped++;
            irq_enable();
            move_free(barrier);
            return 0;
        }
        move_free(barrier);
        trajq_apply_rebase(tq, clock, pos, aux);
        tq->horizon_clock = clock;
        tq->flags &= ~(TQF_NEED_REBASE | TQF_UNDERRUN | TQF_RAMPING
                       | TQF_HALT_BARRIER);
        tq->dropped = 0;
        irq_enable();
        return 1;
    }
    if (recovery && (!(tq->flags & TQF_NEED_REBASE)
                     || tq->flags & TQF_ACTIVE)) {
        // Recovery rebases are one-shot acknowledgements of a stopped or
        // underrun executor, never general moving-path barriers.
        tq->dropped++;
        irq_enable();
        move_free(barrier);
        return 0;
    }
    if (tq->flags & TQF_ACTIVE) {
        // Klipper transmits scheduled commands ahead of their execution
        // time.  A rebase at or beyond the current planned horizon is a
        // queue boundary, not an attempt to mutate the executing segment.
        if (timer_is_before(clock, tq->horizon_clock)) {
            irq_enable();
            move_free(barrier);
            shutdown("Rebase overlaps active trajectory");
        }
        move_queue_push(&barrier->node, &tq->mq);
        tq->queued++;
        tq->horizon_clock = clock;
        irq_enable();
        return 1;
    }
    move_free(barrier);
    trajq_apply_rebase(tq, clock, pos, aux);
    tq->horizon_clock = clock;
    tq->flags &= ~(TQF_NEED_REBASE | TQF_UNDERRUN | TQF_RAMPING
                   | TQF_HALT_BARRIER);
    tq->dropped = 0;
    irq_enable();
    return 1;
}

int
trajq_rebase(struct trajq *tq, uint32_t clock, int32_t pos, int32_t aux)
{
    if (!timesync_class0_ok()) {
        // A rebase is the anchor for subsequent Class-0 segments.  Do not
        // translate it through a mapping that is still moving or stale.
        tq->dropped++;
        return 0;
    }
    // The anchor is a machine-time instant (FD-0001 doc 01);
    // identity when timesync is unconfigured.
    return trajq_rebase_at_local_clock(
        tq, timesync_clock_to_local(clock), pos, aux, 0);
}

int
trajq_rebase_local(struct trajq *tq, uint32_t clock, int32_t pos, int32_t aux)
{
    if (!timesync_class0_ok()) {
        tq->dropped++;
        return 0;
    }
    // TSEG_LOCAL_TIME streams have already committed their complete timing
    // to this board's timer domain.  Their next queue barrier must therefore
    // use the local clock captured with that stream, not re-convert the same
    // machine-time instant through a mapping that may have disciplined since
    // earlier local segments were queued.
    return trajq_rebase_at_local_clock(tq, clock, pos, aux, 0);
}

int
trajq_rebase_recovery(struct trajq *tq, uint32_t clock, int32_t pos,
                      int32_t aux)
{
    if (!timesync_class0_ok()) {
        tq->dropped++;
        return 0;
    }
    return trajq_rebase_at_local_clock(
        tq, timesync_clock_to_local(clock), pos, aux, 1);
}

int
trajq_rebase_recovery_local(struct trajq *tq, uint32_t clock, int32_t pos,
                            int32_t aux)
{
    if (!timesync_class0_ok()) {
        tq->dropped++;
        return 0;
    }
    return trajq_rebase_at_local_clock(tq, clock, pos, aux, 1);
}

// Abort all motion (trsync trigger, shutdown). Callers must have
// irqs disabled. The backend stop hook records the live position in
// tq->acc before the queue is flushed.
void
trajq_halt(struct trajq *tq, uint8_t set_flags)
{
    tq->ops->stop(tq);
    tq->flags &= ~(TQF_ACTIVE | TQF_RAMPING);
    tq->flags |= set_flags;
    tq->horizon_clock = tq->seg_start_clock;
    while (!move_queue_empty(&tq->mq)) {
        struct move_node *mn = move_queue_pop(&tq->mq);
        move_free(container_of(mn, struct traj_segment, node));
    }
    tq->queued = 0;
}

void
trajq_note_underrun_wake(void)
{
    sched_wake_task(&traj_event_wake);
}

// The backend's event task polls this to know if any oid latched an
// event; the wake is shared so the task file owns the iteration.
int
trajq_check_event_wake(void)
{
    return sched_check_wake(&traj_event_wake);
}
