// Trajectory intention segment core (RFC 0001).
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
#include "sched.h" // sched_wake_task
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

// Load the given coefficients as the active segment
static void
trajq_load(struct trajq *tq, uint8_t flags, uint32_t duration
           , int32_t velocity, int32_t accel)
{
    tq->seg_flags = flags;
    tq->duration = duration;
    tq->velocity = velocity;
    tq->accel = accel;
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
    tq->acc += trajq_end_delta(tq->duration, tq->velocity, tq->accel);
    tq->seg_start_clock += tq->duration;
    int32_t v_end = trajq_velocity_at(tq->velocity, tq->accel, tq->duration);

    if (tq->flags & TQF_RAMPING) {
        // Emergency ramp in progress: keep ramping until stopped,
        // then latch the underrun.
        if (trajq_synth_ramp(tq, v_end))
            return TQ_ADV_SEG;
        tq->flags &= ~TQF_RAMPING;
        tq->flags |= TQF_UNDERRUN | TQF_NEED_REBASE | TQF_EVENT_PENDING;
        tq->event_clock = tq->seg_start_clock;
        tq->event_pos = (int32_t)(tq->acc >> 32);
        sched_wake_task(&traj_event_wake);
        tq->flags &= ~TQF_ACTIVE;
        return TQ_ADV_IDLE;
    }

    if (!move_queue_empty(&tq->mq)) {
        struct move_node *mn = move_queue_pop(&tq->mq);
        struct traj_segment *seg = container_of(
            mn, struct traj_segment, node);
        trajq_load(tq, seg->flags, seg->duration, seg->velocity, seg->accel);
        move_free(seg);
        tq->queued--;
        return TQ_ADV_SEG;
    }

    // Queue ran dry. Stopped (or told to hold): idle at position.
    if (!v_end || tq->seg_flags & TSEG_HOLD_AT_END) {
        tq->flags &= ~TQF_ACTIVE;
        return TQ_ADV_IDLE;
    }
    // Moving: synthesize the underrun deceleration ramp.
    if (trajq_synth_ramp(tq, v_end))
        return TQ_ADV_SEG;
    // No ramp possible (unconfigured or negligible velocity): latch.
    tq->flags |= TQF_UNDERRUN | TQF_NEED_REBASE | TQF_EVENT_PENDING;
    tq->event_clock = tq->seg_start_clock;
    tq->event_pos = (int32_t)(tq->acc >> 32);
    sched_wake_task(&traj_event_wake);
    tq->flags &= ~TQF_ACTIVE;
    return TQ_ADV_IDLE;
}

void
trajq_queue_segment(struct trajq *tq, uint8_t flags, uint32_t duration
                    , int32_t velocity, int32_t accel)
{
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

void
trajq_rebase(struct trajq *tq, uint32_t clock, int32_t pos)
{
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
