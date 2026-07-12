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

#include "basecmd.h" // move_alloc
#include "board/irq.h" // irq_disable
#include "command.h" // shutdown
#include "execlog.h" // execlog_append
#include "sched.h" // sched_wake_task
#include "timesync.h" // timesync_ticks_to_local
#include "trajq.h" // trajq_setup

static struct task_wake traj_event_wake;

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
    int64_t delta = dv << 16;
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
//   sub-units/mm <= ~1e7 (fine microstepping), CLOCK_FREQ >= 64 MHz.
// Per-tick true values scale as (rate * su_per_mm / F^order):
//   j_true <= 1e6 *1e7 / (64e6)^3 = 3.8e-11 su/tick^3
//   s_true <= 1e8 *1e7 / (64e6)^4 = 6.0e-17 su/tick^4
//   c_true <= 1e10*1e7 / (64e6)^5 = 9.3e-23 su/tick^5
// Stored integers:
//   |j| <= 3.8e-11 * 2^48 ~= 1.1e4
//   |s| <= 6.0e-17 * 2^64 ~= 1.1e3
//   |c| <= 9.3e-23 * 2^80 ~= 1.1e2
// all far inside int32 (+-2.1e9): >=17 bits of headroom on jerk. No
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
    tq->acc += trajq_end_delta_seg(tq);
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
        sched_wake_task(&traj_event_wake);
        tq->flags &= ~TQF_ACTIVE;
        return TQ_ADV_IDLE;
    }

    if (!move_queue_empty(&tq->mq)) {
        struct move_node *mn = move_queue_pop(&tq->mq);
        struct traj_segment *seg = container_of(
            mn, struct traj_segment, node);
        trajq_load(tq, seg->flags, seg->duration, seg->velocity, seg->accel);
#if CONFIG_WANT_TRAJECTORY_HIGHER_ORDER
        tq->jerk = seg->jerk;
        tq->snap = seg->snap;
        tq->crackle = seg->crackle;
#endif
        move_free(seg);
        tq->queued--;
        return TQ_ADV_SEG;
    }

    // Queue ran dry. Stopped (or told to hold): idle at position.
    if (!v_end || tq->seg_flags & TSEG_HOLD_AT_END) {
        tq->flags &= ~TQF_ACTIVE;
        execlog_append(EL_HOLD, tq->oid, tq->seg_start_clock
                       , (int32_t)(tq->acc >> 32), 0);
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
    // Segment durations arrive in machine time (identity when
    // timesync is unconfigured)
    duration = timesync_ticks_to_local(duration);
    if (!duration)
        shutdown("Invalid traj segment");
    if (duration > TRAJ_MAX_DURATION && (velocity || accel))
        // Only pure holds (v=0, a=0) may exceed the duration cap
        shutdown("Invalid traj segment");
    if (flags & TSEG_POLY_MASK)
        // Polynomial orders beyond quadratic are not negotiated
        shutdown("Invalid traj segment");
    // Validate coefficient ranges up front (also computed on advance)
    trajq_end_delta(duration, velocity, accel);

    struct traj_segment *seg = move_alloc();
    seg->flags = flags;
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
        irq_enable();
        return;
    }
    // Idle: this segment starts at the current anchor clock
    trajq_load(tq, seg->flags, seg->duration, seg->velocity, seg->accel);
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
    duration = timesync_ticks_to_local(duration);
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

    struct traj_segment *seg = move_alloc();
    seg->flags = flags;
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
    irq_enable();
}
#endif // CONFIG_WANT_TRAJECTORY_HIGHER_ORDER

void
trajq_rebase(struct trajq *tq, uint32_t clock, int32_t pos)
{
    // The anchor is a machine-time instant (FD-0001 doc 01);
    // identity when timesync is unconfigured
    clock = timesync_clock_to_local(clock);
    irq_disable();
    if (tq->flags & TQF_ACTIVE) {
        irq_enable();
        shutdown("Can't rebase active trajectory");
    }
    tq->acc = (int64_t)pos << 32;
    tq->seg_start_clock = clock;
    tq->horizon_clock = clock;
    tq->flags &= ~(TQF_NEED_REBASE | TQF_UNDERRUN | TQF_RAMPING);
    tq->dropped = 0;
    execlog_append(EL_REBASE, tq->oid, clock, pos, 0);
    irq_enable();
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
