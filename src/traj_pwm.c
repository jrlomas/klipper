// PWM/DAC sampled actuator backend for trajectory segments (FD-0001).
//
// Non-stepper backend: "position" is an output level, not step edges.
// Unlike the stepper backend it does NOT solve for microstep boundary
// crossings.  It SAMPLES the segment polynomial at a fixed loop rate
// (FD-0001 doc 02 "Sampled realization", doc 04 "PWM / DAC backend"):
// each tick it evaluates q(dt) = q0 + v*dt + 1/2*a*dt^2 directly (two
// 64-bit multiply-accumulates, FPU-free) via trajq_pos_at(), maps the
// resulting sub-unit position to a duty cycle, and writes it to a hard
// PWM (or DAC) output.  Gives hobby servos a native trajectory
// interface and lets laser power track commanded position-derived
// motion precisely without a special-purpose module.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "autoconf.h" // CONFIG_*
#include "basecmd.h" // oid_alloc
#include "board/gpio.h" // gpio_pwm_setup
#include "board/irq.h" // irq_disable
#include "board/misc.h" // timer_read_time
#include "command.h" // DECL_COMMAND
#include "execlog.h" // execlog_append
#include "sched.h" // struct timer
#include "trajq.h" // trajq_setup
#include "trsync.h" // trsync_add_signal

// Activation guard: a segment stream anchored further than this in the
// past is a protocol error (the host must rebase after any gap).
#define PAST_GUARD_TICKS (1 << 26)

struct traj_pwm {
    struct timer time;
    struct trajq tq;
    struct gpio_pwm out;
    uint32_t sample_ticks;   // cadence between output samples
    uint32_t scale;          // sub-units mapping to full-scale duty
    uint16_t max_value;      // PWM/DAC full-scale duty (0..PWM_MAX)
    uint16_t shutdown_value; // duty written on machine shutdown
    struct trsync_signal stop_signal;
};

// Pure sub-unit position -> duty mapping (clamped to [0, max_value]).
// duty = pos_su * max_value / scale.  The host mirror of this function
// (klippy/extras/trajectory_pwm.py:subunit_to_duty) is unit tested.
static uint16_t
traj_pwm_duty(int64_t pos_su, uint32_t scale, uint16_t max_value)
{
    if (pos_su <= 0 || !scale)
        return 0;
    uint64_t duty = ((uint64_t)pos_su * max_value) / scale;
    if (duty >= max_value)
        return max_value;
    return (uint16_t)duty;
}

// Write the output level for an absolute Q32.32 sub-unit position.
static void
traj_pwm_write(struct traj_pwm *p, int64_t acc_q32)
{
    gpio_pwm_write(p->out, traj_pwm_duty(acc_q32 >> 32, p->scale
                                         , p->max_value));
}

// Sampling loop: evaluate the active segment at the scheduled sample
// instant, advancing across any segment boundaries reached, then write
// the mapped duty and re-arm.  Runs in timer (irq) context.
static uint_fast8_t
traj_pwm_event(struct timer *t)
{
    struct traj_pwm *p = container_of(t, struct traj_pwm, time);
    struct trajq *tq = &p->tq;
    for (;;) {
        uint32_t elapsed = p->time.waketime - tq->seg_start_clock;
        if (elapsed < tq->duration) {
            // Sample q(dt) directly and write the mapped output level
            int64_t acc = trajq_acc_add(
                tq->acc, trajq_q16_to_acc(
                    trajq_pos_at_seg(tq, elapsed)));
            traj_pwm_write(p, acc);
            p->time.waketime += p->sample_ticks;
            return SF_RESCHEDULE;
        }
        // Reached the end of the active segment: load the next one (or
        // a synthesized underrun ramp), or idle.  The core keeps the
        // ramp going down to v=0; we just keep sampling it.
        int advance = trajq_advance(tq);
        if (advance == TQ_ADV_REBASE) {
            // A queued rebase may intentionally leave a gap between two
            // paths.  Resume sampling at the new absolute start instead of
            // interpreting the unsigned pre-start delta as an elapsed span.
            p->time.waketime = tq->seg_start_clock;
            return SF_RESCHEDULE;
        }
        if (advance == TQ_ADV_SEG)
            continue;
        // Idle (hold-at-end or latched underrun): freeze the output at
        // the exact end position and stop sampling.
        traj_pwm_write(p, tq->acc);
        return SF_DONE;
    }
}

// Backend ops

static void
traj_pwm_start(struct trajq *tq)
{
    struct traj_pwm *p = container_of(tq, struct traj_pwm, tq);
    uint32_t now = timer_read_time();
    if (timer_is_before(tq->seg_start_clock + PAST_GUARD_TICKS, now))
        shutdown("Trajectory anchor in past");
    p->time.waketime = tq->seg_start_clock;
    if (timer_is_before(p->time.waketime, now))
        p->time.waketime = now;
    sched_add_timer(&p->time);
}

static void
traj_pwm_stop(struct trajq *tq, uint32_t clock)
{
    struct traj_pwm *p = container_of(tq, struct traj_pwm, tq);
    sched_del_timer(&p->time);
    // Freeze the output (leave the last written level untouched) and
    // record the live sub-unit position back into the anchor, so the
    // host can recover exact state (FD-0001 doc 04 stop table).
    if ((tq->flags & TQF_ACTIVE)
        && !timer_is_before(clock, tq->seg_start_clock)) {
        uint32_t t = clock - tq->seg_start_clock;
        if (t > tq->duration)
            t = tq->duration;
        tq->acc = trajq_acc_add(
            tq->acc, trajq_q16_to_acc(trajq_pos_at_seg(tq, t)));
        tq->seg_start_clock += t;
    }
}

static const struct trajq_backend_ops traj_pwm_ops = {
    .start = traj_pwm_start,
    .stop = traj_pwm_stop,
};

// Commands

void
command_config_traj_pwm(uint32_t *args)
{
    struct gpio_pwm out = gpio_pwm_setup(args[1], args[2], args[5]);
    struct traj_pwm *p = oid_alloc(
        args[0], command_config_traj_pwm, sizeof(*p));
    p->out = out;
    p->sample_ticks = args[3] ? args[3] : 1;
    p->scale = args[4];
    p->max_value = args[6];
    p->shutdown_value = args[5];
    p->time.func = traj_pwm_event;
    trajq_setup(&p->tq, args[0], &traj_pwm_ops, args[7]);
}
DECL_COMMAND(command_config_traj_pwm,
             "config_traj_pwm oid=%c pin=%u cycle_ticks=%u sample_ticks=%u"
             " scale=%u shutdown_value=%hu max_value=%hu underrun_decel=%u");

static struct traj_pwm *
traj_pwm_oid_lookup(uint8_t oid)
{
    return oid_lookup(oid, command_config_traj_pwm);
}

void
command_queue_traj_pwm_segment(uint32_t *args)
{
    struct traj_pwm *p = traj_pwm_oid_lookup(args[0]);
    trajq_queue_segment(&p->tq, args[1], args[2], args[3], args[4]);
}
DECL_COMMAND(command_queue_traj_pwm_segment,
             "queue_traj_pwm_segment oid=%c flags=%c duration=%u"
             " velocity=%i accel=%i");

#if CONFIG_WANT_TRAJECTORY_HIGHER_ORDER
void
command_queue_traj_pwm_segment_cubic(uint32_t *args)
{
    struct traj_pwm *p = traj_pwm_oid_lookup(args[0]);
    trajq_queue_segment_ho(&p->tq, args[1] | TSEG_POLY_CUBIC, args[2]
                           , args[3], args[4], args[5], 0, 0);
}
DECL_COMMAND(command_queue_traj_pwm_segment_cubic,
             "queue_traj_pwm_segment_cubic oid=%c flags=%c duration=%u"
             " velocity=%i accel=%i jerk=%i");

void
command_queue_traj_pwm_segment_quintic(uint32_t *args)
{
    struct traj_pwm *p = traj_pwm_oid_lookup(args[0]);
    trajq_queue_segment_ho(&p->tq, args[1] | TSEG_POLY_QUINTIC, args[2]
                           , args[3], args[4], args[5], args[6], args[7]);
}
DECL_COMMAND(command_queue_traj_pwm_segment_quintic,
             "queue_traj_pwm_segment_quintic oid=%c flags=%c duration=%u"
             " velocity=%i accel=%i jerk=%i snap=%i crackle=%i");
#endif

void
command_traj_pwm_hold(uint32_t *args)
{
    struct traj_pwm *p = traj_pwm_oid_lookup(args[0]);
    trajq_queue_segment(&p->tq, TSEG_HOLD_AT_END, args[1], 0, 0);
}
DECL_COMMAND(command_traj_pwm_hold, "traj_pwm_hold oid=%c duration=%u");

void
command_traj_pwm_rebase(uint32_t *args)
{
    struct traj_pwm *p = traj_pwm_oid_lookup(args[0]);
    trajq_rebase(&p->tq, args[1], args[2], 0);
}
DECL_COMMAND(command_traj_pwm_rebase,
             "traj_pwm_rebase oid=%c clock=%u pos=%i");

void
command_traj_pwm_get_position(uint32_t *args)
{
    uint8_t oid = args[0];
    struct traj_pwm *p = traj_pwm_oid_lookup(oid);
    struct trajq *tq = &p->tq;
    irq_disable();
    uint32_t now = timer_read_time();
    int64_t acc = tq->acc;
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
          , oid, now, (int32_t)(acc >> 32), 0);
}
DECL_COMMAND(command_traj_pwm_get_position, "traj_pwm_get_position oid=%c");

void
command_traj_pwm_query(uint32_t *args)
{
    uint8_t oid = args[0];
    struct traj_pwm *p = traj_pwm_oid_lookup(oid);
    struct trajq *tq = &p->tq;
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
DECL_COMMAND(command_traj_pwm_query, "traj_pwm_query oid=%c");

// Homing/probing stop, underrun event reporting, shutdown

static void
traj_pwm_trigger_stop(struct trsync_signal *tss, uint8_t reason)
{
    struct traj_pwm *p = container_of(tss, struct traj_pwm, stop_signal);
    // Value trajectories do not participate in stepper homing's
    // multi-command-queue trigger relay.  Keep their existing rebase ABI.
    trajq_halt(&p->tq, TQF_NEED_REBASE);
    execlog_append(EL_TRIGGER, p->tq.oid, p->tq.seg_start_clock
                   , (int32_t)(p->tq.acc >> 32), reason);
}

void
command_traj_pwm_stop_on_trigger(uint32_t *args)
{
    struct traj_pwm *p = traj_pwm_oid_lookup(args[0]);
    struct trsync *ts = trsync_oid_lookup(args[1]);
    trsync_add_signal(ts, &p->stop_signal, traj_pwm_trigger_stop);
}
DECL_COMMAND(command_traj_pwm_stop_on_trigger,
             "traj_pwm_stop_on_trigger oid=%c trsync_oid=%c");

// The underrun wake is shared across all trajq backends and is
// consumed (cleared) by whichever backend task polls it first, so the
// PWM task does not gate on trajq_check_event_wake() - it sweeps its
// own oids directly with a cheap fast-path so a stepper task cannot
// swallow a PWM actuator's event (or vice versa).
void
traj_pwm_task(void)
{
    uint8_t oid;
    struct traj_pwm *p;
    foreach_oid(oid, p, command_config_traj_pwm) {
        struct trajq *tq = &p->tq;
        if (!(tq->flags & TQF_EVENT_PENDING))
            continue;
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
DECL_TASK(traj_pwm_task);

void
traj_pwm_shutdown(void)
{
    uint8_t oid;
    struct traj_pwm *p;
    foreach_oid(oid, p, command_config_traj_pwm) {
        irq_disable();
        trajq_halt(&p->tq, TQF_NEED_REBASE);
        irq_enable();
        // Machine shutdown: drive the output to its configured
        // shutdown level (FD-0001 doc 04 stop table).
        gpio_pwm_write(p->out, p->shutdown_value);
    }
}
DECL_SHUTDOWN(traj_pwm_shutdown);
