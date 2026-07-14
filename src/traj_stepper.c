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

enum { WK_STEP, WK_UNSTEP, WK_POLL };

struct traj_stepper {
    struct timer time;
    struct trajq tq;
    uint32_t step_pulse_ticks;
    struct gpio_out step_pin, dir_pin;
    // Solver state for the active segment
    uint32_t t_prev;        // ticks into segment of last solve point
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
    struct trsync_signal stop_signal;
};

enum { TSF_INVERT_STEP = 1 << 0, TSF_DIR_HIGH = 1 << 1,
       TSF_INVERT_DIR = 1 << 2 };

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
    if (!tq->accel && !(tq->seg_flags & TSEG_POLY_MASK)) {
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
    uint32_t t = s->t_prev;
    uint32_t tmax = tq->duration;
    int i;
    for (i = 0; i < 8; i++) {
        int32_t v = trajq_velocity_at_seg(tq, t);
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
        int64_t err = trajq_pos_at_seg(tq, t) - s->target16;
        int64_t dt = -err / v; // ticks (err Q16.16 / v Q16.16)
        if (dt > (int64_t)tmax)
            dt = tmax;
        else if (dt < -(int64_t)tmax)
            dt = -(int64_t)tmax;
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
    // Ensure monotonic progress and crossing
    if (t <= s->t_prev)
        t = s->t_prev + 1;
    // Walk forward over any residual undershoot (bounded)
    for (i = 0; i < 4 && t < tmax; i++) {
        int64_t q = trajq_pos_at_seg(tq, t);
        if (dir > 0 ? q >= s->target16 : q <= s->target16)
            break;
        t++;
    }
    if (t > tmax)
        return 0;
    *step_t = t;
    return 1;
}

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
#endif

// Set up solver state when a segment becomes active
static void
traj_stepper_load(struct traj_stepper *s)
{
    struct trajq *tq = &s->tq;
    int32_t v0 = tq->velocity;
    int32_t vend = trajq_velocity_at_seg(tq, tq->duration);
    int8_t dir = v0 ? (v0 > 0 ? 1 : -1)
        : (tq->accel ? (tq->accel > 0 ? 1 : -1) : (vend > 0 ? 1 : -1));
    s->dir = dir;
    s->t_prev = 0;
    s->q16_end = trajq_end_delta_seg(tq) >> 16;
    // Match Klipper's legacy itersolve quantizer: the physical integer
    // microstep position changes when the continuous commanded position
    // crosses the half-step boundary around it.  mpos is deliberately
    // independent of tq->acc -- a logical coordinate rebase must not invent
    // physical pulses or reset the MCU's step count.
    int64_t boundary = dir > 0 ? ((2LL * s->mpos + 1) << 47)
                               : ((2LL * s->mpos - 1) << 47);
    s->target16 = (boundary - tq->acc) >> 16;
    if (!tq->accel && !(tq->seg_flags & TSEG_POLY_MASK))
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
        tq->acc += trajq_pos_at_seg(tq, t) << 16;
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
    if (tq->flags & TQF_ACTIVE) {
        uint32_t t = now - tq->seg_start_clock;
        if (!timer_is_before(now, tq->seg_start_clock)) {
            if (t > tq->duration)
                t = tq->duration;
            acc += trajq_pos_at_seg(tq, t) << 16;
        }
    }
    irq_enable();
    sendf("traj_position oid=%c clock=%u pos=%i"
          , oid, now, (int32_t)(acc >> 32));
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
