// ESP32 RMT-backed stepper backend (FD-0001 doc 12: the "RMT escape
// hatch" for step generation, made real).
//
// This is a drop-in replacement for the portable src/stepper.c,
// selected at build time by CONFIG_WANT_ESP32_RMT_STEP (the esp32
// CMake compiles this file *instead of* stepper.c).  It registers the
// identical command surface - config_stepper / queue_step /
// set_next_step_dir / reset_step_clock / stepper_get_position /
// stepper_stop_on_trigger - so klippy is unchanged, but instead of
// toggling a step GPIO inside a sched-timer event it feeds
// (interval, count, add) triples into the RMT peripheral
// (src/esp32/rmt_step.c), which emits the pulse train in hardware.
// Edge timing is then immune to the WiFi-stack timer-IRQ and
// flash-cache jitter that makes tick-precise stepping on this chip
// hard (doc 07).
//
// The three open problems doc 12 / docs/ESP32.md flagged are solved
// here:
//
//  * Dir-change fencing.  An RMT channel drives only the step pin, so
//    a direction change cannot happen mid-train.  Consecutive moves
//    of one direction are grouped into a train; a direction change
//    closes the train and opens a new one whose start clock is the
//    old train's exact end clock.  The dir GPIO is flipped in the
//    anchor timer callback, which first fences on rmt_step_is_busy()
//    so the flip never lands under live pulses.
//
//  * Clock anchoring.  Each train's first pulse is armed from a sched
//    timer (rs_anchor_event) firing at the train's absolute start
//    clock, correlating tx_start to timer_read_time().  Train N+1's
//    start clock is chained from N via rmt_step_move_ticks(), so the
//    timeline stays anchored across an arbitrarily long motion and
//    every direction change re-synchronizes exactly.  Residual error
//    is documented in docs/ESP32.md ("RMT step generation").
//
//  * Wrap-mode underrun.  rmt_step.c watermarks the transmitter read
//    cursor against the ring write cursor at each refill and latches
//    an underrun (force-stopping the channel) rather than silently
//    re-emitting stale items.  rmt_stepper_task() polls the latch and
//    escalates to shutdown() - lost steps mean a desynchronized axis,
//    which is a hard fault, not something to paper over.
//
// Homing/trsync stop ceases pulses immediately (rmt_step_abort in the
// trsync callback) and freezes an exact stopped position computed
// from the clock (rmt_step_move_emitted) - no pulse counter needed.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "autoconf.h" // CONFIG_CLOCK_FREQ
#include "basecmd.h" // oid_alloc
#include "board/gpio.h" // gpio_out_setup
#include "board/irq.h" // irq_disable
#include "board/misc.h" // timer_read_time
#include "command.h" // DECL_COMMAND
#include "sched.h" // struct timer
#include "stepper.h" // stepper_event
#include "trsync.h" // trsync_add_signal
#include "rmt_step.h" // rmt_step_setup

// Software move queue depth (moves buffered ahead of the RMT channel's
// own 16-move ring; a direction change parks the next train's moves
// here until the current train drains).
#define RS_QUEUE 48
// Bias so an all-forward move history stays a positive 32-bit value
// with the top bit free for the reverse-direction flag (matches
// src/stepper.c's scheme so the host decodes positions identically).
enum { POSITION_BIAS = 0x40000000 };
// If the previous train has not drained by the time the next train's
// anchor fires, back off this long between fence rechecks, and give up
// (shutdown) after the deadline - a channel stuck busy past its
// computed end clock is a hardware fault.
#define RS_FENCE_BACKOFF_US 5
#define RS_FENCE_DEADLINE_US 200

enum {
    RSF_INVERT_STEP = 1 << 0,
    RSF_NEED_RESET  = 1 << 1,
    RSF_ARMED       = 1 << 2, // anchor timer scheduled for a pending train
    RSF_RUNNING     = 1 << 3, // a train has been started on the channel
    RSF_FENCING     = 1 << 4, // anchor is backing off on a busy channel
};

struct rs_move {
    uint32_t interval;
    uint16_t count;
    int16_t add;
    uint8_t dir;    // absolute dir-pin level for this move
};

struct rmt_stepper {
    struct timer anchor;        // train start / dir-fence timer
    struct rmt_step_chan *chan;
    struct gpio_out dir_pin;
    uint32_t position;          // POSITION_BIAS + reverse-flag scheme
    uint16_t high_ticks;
    // Software move queue (moves not yet handed to the RMT channel)
    struct rs_move q[RS_QUEUE];
    uint8_t qhead, qtail;
    // Absolute klipper clock of the next not-yet-committed step edge;
    // chains train start clocks.
    uint32_t next_clock;
    // Active train (moves already fed to the channel), kept so a stop
    // can reconstruct how many of their steps have physically emitted.
    struct rs_move train[RS_QUEUE];
    uint8_t train_n;
    uint32_t train_start_clock;
    uint32_t fence_deadline;
    uint8_t run_dir;            // dir loaded in the running train
    uint8_t cur_fed_dir;        // dir of the last move fed (train grouping)
    uint8_t req_dir;            // pending set_next_step_dir level
    uint8_t flags;
    uint8_t oid;
    struct trsync_signal stop_signal;
};

// RMT channels are handed out sequentially as steppers are configured.
static uint8_t rmt_next_chan;

/****************************************************************
 * Software queue helpers (caller holds irqs disabled)
 ****************************************************************/

static inline uint_fast8_t
rs_has_move(struct rmt_stepper *s)
{
    return s->qhead != s->qtail;
}

static inline struct rs_move *
rs_head(struct rmt_stepper *s)
{
    return &s->q[s->qtail % RS_QUEUE];
}

static inline struct rs_move *
rs_pop(struct rmt_stepper *s)
{
    struct rs_move *m = &s->q[s->qtail % RS_QUEUE];
    s->qtail++;
    return m;
}

/****************************************************************
 * Position bookkeeping
 ****************************************************************/

// Steps fed to the channel but not yet physically emitted.  The
// dir-change fence guarantees every in-flight (fed) move shares one
// direction, so these are simply subtracted (as in src/stepper.c,
// where only the single active move contributes remaining steps).
static uint32_t
rs_unemitted(struct rmt_stepper *s)
{
    if (!s->train_n)
        return 0;
    uint32_t elapsed = timer_read_time() - s->train_start_clock;
    if ((int32_t)elapsed < 0)
        elapsed = 0; // train has not started emitting yet
    uint32_t unemitted = 0;
    for (uint_fast8_t i = 0; i < s->train_n; i++) {
        struct rs_move *m = &s->train[i];
        uint32_t span = rmt_step_move_ticks(m->interval, m->count, m->add);
        if (elapsed >= span) {
            elapsed -= span;
            continue; // fully emitted
        }
        uint16_t done = rmt_step_move_emitted(m->interval, m->count, m->add
                                              , elapsed);
        unemitted += (uint32_t)(m->count - done);
        // Everything after this move is entirely unemitted
        for (uint_fast8_t j = i + 1; j < s->train_n; j++)
            unemitted += s->train[j].count;
        break;
    }
    return unemitted;
}

// Logical position (reverse-flag applied), mirroring src/stepper.c's
// stepper_get_position(): count out the steps not yet taken.
static uint32_t
rs_get_position(struct rmt_stepper *s)
{
    uint32_t position = s->position - rs_unemitted(s);
    if (position & 0x80000000)
        return -position;
    return position;
}

/****************************************************************
 * Feeding / train scheduling (caller holds irqs disabled)
 ****************************************************************/

// Commit one move to the RMT channel: update the position total with
// src/stepper.c's flip-then-add convention, record it for emitted()
// accounting, advance the clock accumulator.
static void
rs_feed_move(struct rmt_stepper *s, struct rs_move *m)
{
    // cur_fed_dir starts 0 (matching the dir pin's reset level and
    // src/stepper.c's SF_LAST_DIR), so a first move with dir=1 flips
    // exactly as the classic backend does - position encoding parity.
    if (m->dir != s->cur_fed_dir)
        s->position = -s->position;
    s->cur_fed_dir = m->dir;
    s->position += m->count;
    rmt_step_queue(s->chan, m->interval, m->count, m->add);
    if (s->train_n < RS_QUEUE)
        s->train[s->train_n++] = *m;
    s->next_clock += rmt_step_move_ticks(m->interval, m->count, m->add);
}

// Top up a running train with queued same-direction moves (safe from
// any context - no timer-list mutation).
static void
rs_feed(struct rmt_stepper *s)
{
    if (!(s->flags & RSF_RUNNING))
        return;
    if (!rmt_step_is_busy(s->chan)) {
        s->flags &= ~RSF_RUNNING; // train finished; idle path re-arms
        return;
    }
    while (rs_has_move(s) && rs_head(s)->dir == s->run_dir
           && rmt_step_queue_space(s->chan))
        rs_feed_move(s, rs_pop(s));
}

// Arm the anchor timer for the next (or first) train when one is
// waiting.  MUST NOT be called from within rs_anchor_event: mutating
// the timer list from a timer's own callback corrupts it.  Callers
// (queue_step, the task) hold irqs disabled, as sched_add_timer wants.
static void
rs_arm(struct rmt_stepper *s)
{
    if ((s->flags & (RSF_ARMED | RSF_NEED_RESET)) || !rs_has_move(s))
        return;
    uint8_t running = !!(s->flags & RSF_RUNNING);
    if (running && rs_head(s)->dir == s->run_dir)
        return; // same-dir continuation - fed into the running train
    // The waiting train begins at the current clock accumulator (the
    // end of everything committed so far), so next_clock reflects all
    // same-dir moves already fed into the current train - an accurate,
    // never-early anchor time for the fence.
    s->anchor.waketime = s->next_clock;
    sched_add_timer(&s->anchor);
    s->flags |= RSF_ARMED;
}

static void
rs_pump(struct rmt_stepper *s)
{
    rs_feed(s);
    rs_arm(s);
}

// Anchor timer: fires at a train's absolute start clock.  Fences the
// dir change on the draining channel, flips the dir GPIO, loads the
// train and starts it - correlating tx_start to the klipper clock.
// Returns SF_DONE and leaves re-arming of any following train to the
// task / queue_step (rs_arm), which must not run from this callback.
static uint_fast8_t
rs_anchor_event(struct timer *t)
{
    struct rmt_stepper *s = container_of(t, struct rmt_stepper, anchor);
    if (rmt_step_is_busy(s->chan)) {
        // Previous train not fully drained: hold the dir flip.  The
        // anchor fired at the current train's computed end clock, so
        // the channel should clear within about one RMT item.
        uint32_t now = timer_read_time();
        if (!(s->flags & RSF_FENCING)) {
            s->flags |= RSF_FENCING;
            s->fence_deadline = now + timer_from_us(RS_FENCE_DEADLINE_US);
        } else if (!timer_is_before(now, s->fence_deadline)) {
            shutdown("RMT dir fence timeout");
        }
        s->anchor.waketime = now + timer_from_us(RS_FENCE_BACKOFF_US);
        return SF_RESCHEDULE;
    }
    s->flags &= ~(RSF_ARMED | RSF_FENCING);
    if (!rs_has_move(s))
        return SF_DONE;
    uint8_t dir = rs_head(s)->dir;
    s->run_dir = dir;
    gpio_out_write(s->dir_pin, dir);
    s->train_n = 0;
    s->train_start_clock = s->next_clock;
    while (rs_has_move(s) && rs_head(s)->dir == dir
           && rmt_step_queue_space(s->chan))
        rs_feed_move(s, rs_pop(s));
    rmt_step_start(s->chan);
    s->flags |= RSF_RUNNING;
    return SF_DONE;
}

/****************************************************************
 * Command surface (parity with src/stepper.c)
 ****************************************************************/

void
command_config_stepper(uint32_t *args)
{
    struct rmt_stepper *s = oid_alloc(args[0], command_config_stepper
                                      , sizeof(*s));
    int_fast8_t invert_step = args[3];
    if (invert_step > 0)
        s->flags = RSF_INVERT_STEP;
    uint32_t hi = args[4];
    if (!hi)
        hi = 2; // RMT needs a nonzero high half; a 0-width pulse is illegal
    s->high_ticks = hi;
    if (rmt_next_chan >= 8)
        shutdown("Out of RMT step channels");
    uint8_t chan = rmt_next_chan++;
    s->chan = rmt_step_setup(chan, args[1], !!(s->flags & RSF_INVERT_STEP)
                             , s->high_ticks);
    if (!s->chan)
        shutdown("Invalid RMT step pin");
    s->dir_pin = gpio_out_setup(args[2], 0);
    s->position = -POSITION_BIAS;
    s->anchor.func = rs_anchor_event;
    s->oid = args[0];
}
DECL_COMMAND(command_config_stepper, "config_stepper oid=%c step_pin=%c"
             " dir_pin=%c invert_step=%c step_pulse_ticks=%u");

static struct rmt_stepper *
stepper_oid_lookup(uint8_t oid)
{
    return oid_lookup(oid, command_config_stepper);
}

void
command_queue_step(uint32_t *args)
{
    struct rmt_stepper *s = stepper_oid_lookup(args[0]);
    uint32_t interval = args[1];
    uint16_t count = args[2];
    if (!count)
        shutdown("Invalid count parameter");
    int16_t add = args[3];

    irq_disable();
    if (s->flags & RSF_NEED_RESET) {
        irq_enable();
        return; // dropped until reset_step_clock re-anchors
    }
    if ((uint8_t)(s->qhead - s->qtail) >= RS_QUEUE)
        shutdown("RMT step queue overflow");
    struct rs_move *m = &s->q[s->qhead % RS_QUEUE];
    m->interval = interval;
    m->count = count;
    m->add = add;
    m->dir = s->req_dir;
    s->qhead++;
    rs_pump(s);
    irq_enable();
}
DECL_COMMAND(command_queue_step,
             "queue_step oid=%c interval=%u count=%hu add=%hi");

void
command_set_next_step_dir(uint32_t *args)
{
    struct rmt_stepper *s = stepper_oid_lookup(args[0]);
    uint8_t dir = args[1] ? 1 : 0;
    irq_disable();
    s->req_dir = dir;
    irq_enable();
}
DECL_COMMAND(command_set_next_step_dir, "set_next_step_dir oid=%c dir=%c");

void
command_reset_step_clock(uint32_t *args)
{
    struct rmt_stepper *s = stepper_oid_lookup(args[0]);
    uint32_t waketime = args[1];
    irq_disable();
    if ((s->flags & (RSF_RUNNING | RSF_ARMED)) || rs_has_move(s))
        shutdown("Can't reset time when stepper active");
    s->next_clock = waketime;
    s->flags &= ~RSF_NEED_RESET;
    irq_enable();
}
DECL_COMMAND(command_reset_step_clock, "reset_step_clock oid=%c clock=%u");

void
command_stepper_get_position(uint32_t *args)
{
    uint8_t oid = args[0];
    struct rmt_stepper *s = stepper_oid_lookup(oid);
    irq_disable();
    uint32_t position = rs_get_position(s);
    irq_enable();
    sendf("stepper_position oid=%c pos=%i", oid, position - POSITION_BIAS);
}
DECL_COMMAND(command_stepper_get_position, "stepper_get_position oid=%c");

// Homing/probing: cease pulses immediately and freeze position
static void
rs_trsync_stop(struct trsync_signal *tss, uint8_t reason)
{
    struct rmt_stepper *s = container_of(tss, struct rmt_stepper, stop_signal);
    rmt_step_abort(s->chan);        // pulses cease within one item
    sched_del_timer(&s->anchor);
    uint32_t position = rs_get_position(s);
    s->position = -position;
    s->train_n = 0;
    s->qhead = s->qtail = 0;
    s->cur_fed_dir = 0; // dir pin driven to 0 below (matches src/stepper.c)
    s->req_dir = 0;
    s->flags = (s->flags & RSF_INVERT_STEP) | RSF_NEED_RESET;
    gpio_out_write(s->dir_pin, 0);
}

void
command_stepper_stop_on_trigger(uint32_t *args)
{
    struct rmt_stepper *s = stepper_oid_lookup(args[0]);
    struct trsync *ts = trsync_oid_lookup(args[1]);
    trsync_add_signal(ts, &s->stop_signal, rs_trsync_stop);
}
DECL_COMMAND(command_stepper_stop_on_trigger,
             "stepper_stop_on_trigger oid=%c trsync_oid=%c");

// Keep the RMT channels fed from the software queue and surface a
// wrap-underrun as a shutdown (lost steps = desynchronized axis).
void
rmt_stepper_task(void)
{
    uint8_t i;
    struct rmt_stepper *s;
    foreach_oid(i, s, command_config_stepper) {
        if (rmt_step_take_underrun(s->chan))
            shutdown("RMT step underrun");
        irq_disable();
        rs_pump(s);
        irq_enable();
    }
}
DECL_TASK(rmt_stepper_task);

void
stepper_shutdown(void)
{
    uint8_t i;
    struct rmt_stepper *s;
    foreach_oid(i, s, command_config_stepper) {
        rmt_step_abort(s->chan);
        sched_del_timer(&s->anchor);
        s->train_n = 0;
        s->qhead = s->qtail = 0;
        s->cur_fed_dir = 0;
        s->req_dir = 0;
        s->flags = (s->flags & RSF_INVERT_STEP) | RSF_NEED_RESET;
        gpio_out_write(s->dir_pin, 0);
    }
}
DECL_SHUTDOWN(stepper_shutdown);

// The inline-stepper-hack path in sched.c calls stepper_event() for
// any timer left with a NULL func.  This backend schedules only the
// anchor timer (func set), so no such timer exists; provide the symbol
// and drop any stray timer harmlessly rather than mis-stepping.
uint_fast8_t
stepper_event(struct timer *t)
{
    return SF_DONE;
}
