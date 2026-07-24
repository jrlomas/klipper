// Event-driven trigger sources feeding trsync (FD-0001 doc 09).
//
// Detection moves from polled software timers to hardware events
// (GPIO edge interrupts, analog comparators); what happens after a
// trigger (trsync fan-out, actuator stops) is unchanged. Noise is
// handled by qualify-after-event: the edge starts a short bounded
// confirmation instead of every sample paying a standing cost — a
// false edge costs one brief re-read burst and never fires trsync.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "autoconf.h" // CONFIG_*
#include "basecmd.h" // oid_alloc
#include "board/irq.h" // irq_disable
#include "board/misc.h" // timer_read_time
#include "command.h" // DECL_COMMAND
#include "execlog.h" // execlog_append
#include "sched.h" // DECL_SHUTDOWN
#include "trigger_source.h" // trigger_source_notify
#include "trsync.h" // trsync_do_trigger
#if CONFIG_WANT_TRACE
#include "trace.h" // LOG2
#else
#define LOG2(sub, lvl, ev, a0, a1) do { } while (0)
#endif

// Cap on the busy-wait qualification window (about 100us at typical
// clock rates would already be generous; this is a hard safety cap).
#define QUALIFY_MAX_TICKS (CONFIG_CLOCK_FREQ / 2000)

void command_config_trigger_gpio(uint32_t *args);
static uint_fast8_t trigger_source_arm_event(struct timer *t);

struct trigger_source *
trigger_source_alloc(uint8_t oid, uint8_t kind)
{
    // All kinds share one oid namespace so arm/disarm/query commands
    // work uniformly.
    struct trigger_source *tsrc = oid_alloc(
        oid, command_config_trigger_gpio, sizeof(*tsrc));
    tsrc->oid = oid;
    tsrc->kind = kind;
    tsrc->arm_timer.func = trigger_source_arm_event;
    return tsrc;
}

void
command_config_trigger_gpio(uint32_t *args)
{
    struct trigger_source *tsrc = trigger_source_alloc(
        args[0], TS_KIND_GPIO);
    tsrc->pin = args[1];
    tsrc->edge = !!args[2];
    tsrc->pin_in = gpio_in_setup(args[1], args[3]);
    tsrc->qualify_ticks = args[4];
    tsrc->qualify_count = args[5];
    tsrc->flags = TSRC_CAN_QUALIFY;
    tsrc->hw_arm = board_edge_trigger_arm;
    if ((uint64_t)tsrc->qualify_ticks * tsrc->qualify_count
        > QUALIFY_MAX_TICKS)
        shutdown("Trigger qualify window too long");
    int ret = board_edge_trigger_setup(tsrc);
    if (ret)
        shutdown("Pin unavailable as edge trigger");
    // Optionally route the same edge to a timer input-capture channel
    // for a hardware-exact timestamp (FD-0001 doc 09 sec 3). Availability
    // is per-pin/per-port; if unwired the ISR-entry read is used.
    if (board_timer_capture_setup(tsrc))
        tsrc->flags |= TSRC_CAN_CAPTURE;
}
DECL_COMMAND(command_config_trigger_gpio,
             "config_trigger_gpio oid=%c pin=%u edge=%c pull_up=%c"
             " qualify_ticks=%u qualify_count=%c");

// Analog watchdog trigger source (FD-0001 doc 09 sec 2): the ADC
// free-runs on one channel and hardware auto-compares each sample
// against high/low thresholds, raising an event with no ADC polling.
// This is the event-not-poll fallback on families without COMP.
void
command_config_trigger_adc_watchdog(uint32_t *args)
{
    struct trigger_source *tsrc = trigger_source_alloc(
        args[0], TS_KIND_ADC_WATCHDOG);
    tsrc->pin = args[1];
    tsrc->hw[0] = args[2];      // high threshold (ADC counts)
    tsrc->hw[1] = args[3];      // low threshold (ADC counts)
    // No gpio-style qualify: the confirmation "re-read" for an analog
    // source is the ADC's own next hardware compare against the same
    // thresholds (plus the AWD's inherent threshold band), so
    // TSRC_CAN_QUALIFY stays clear and the digital pin_in re-read in
    // trigger_source_notify is skipped for this kind.
    tsrc->hw_arm = board_adc_watchdog_arm;
    int ret = board_adc_watchdog_setup(tsrc);
    if (ret)
        shutdown("ADC watchdog unavailable for pin");
}
DECL_COMMAND(command_config_trigger_adc_watchdog,
             "config_trigger_adc_watchdog oid=%c pin=%u high=%hu low=%hu");

static struct trigger_source *
trigger_gpio_oid_lookup(uint8_t oid)
{
    return oid_lookup(oid, command_config_trigger_gpio);
}

// Prepare a source for a new arm transaction while leaving its peripheral
// masked.  The scheduled command uses this state later from arm_timer; the
// legacy command activates it immediately.
static void
trigger_source_prepare(struct trigger_source *tsrc, struct trsync *ts,
                       uint8_t reason, uint8_t capture)
{
    irq_disable();
    // Keep cancellation and peripheral masking in one critical section.  In
    // particular, do not leave a window in which an old edge can fire after
    // its arm timer was removed but before the source itself is masked.
    sched_del_timer(&tsrc->arm_timer);
    if (tsrc->hw_arm)
        tsrc->hw_arm(tsrc, 0);
    tsrc->ts = ts;
    tsrc->reason = reason;
    tsrc->flags &= ~(TSRC_TRIGGERED | TSRC_CAPTURE_ON | TSRC_OBSERVER
                     | TSRC_ARMED | TSRC_ARM_PENDING);
    // Use the hardware-captured edge tick when the host requests it
    // and the board actually wired a capture channel for this source.
    if (capture && (tsrc->flags & TSRC_CAN_CAPTURE))
        tsrc->flags |= TSRC_CAPTURE_ON;
    irq_enable();
}

// Activate a prepared source. Called with interrupts disabled either directly
// by the legacy command or from the scheduler timer IRQ.
static void
trigger_source_arm_now(struct trigger_source *tsrc)
{
    tsrc->flags &= ~TSRC_ARM_PENDING;
    if (!tsrc->ts)
        return;
    tsrc->flags |= TSRC_ARMED;
    if (tsrc->hw_arm)
        tsrc->hw_arm(tsrc, 1);
    // Edge peripherals do not report a level that was already active before
    // they were armed.  Check the GPIO after unmasking while interrupts are
    // still disabled: an active level is a valid immediate trigger, and an
    // edge racing this read remains latched for delivery on irq_enable().
    // At the scheduled arm boundary this makes a switch that genuinely
    // remained asserted after retract an immediate, diagnosable trigger. It
    // also closes the query-to-arm race without reverting normal detection
    // to polling.
    if (tsrc->kind == TS_KIND_GPIO
        && gpio_in_read(tsrc->pin_in) == tsrc->edge)
        trigger_source_notify(tsrc, timer_read_time());
}

static uint_fast8_t
trigger_source_arm_event(struct timer *t)
{
    struct trigger_source *tsrc = container_of(
        t, struct trigger_source, arm_timer);
    trigger_source_arm_now(tsrc);
    return SF_DONE;
}

// Legacy immediate arm. Retained so old hosts can control new firmware;
// production homing prefers trigger_source_arm_at below.
void
command_trigger_source_arm(uint32_t *args)
{
    struct trigger_source *tsrc = trigger_gpio_oid_lookup(args[0]);
    trigger_source_prepare(tsrc, trsync_oid_lookup(args[1]),
                           args[2], args[3]);
    irq_disable();
    trigger_source_arm_now(tsrc);
    irq_enable();
}
DECL_COMMAND(command_trigger_source_arm,
             "trigger_source_arm oid=%c trsync_oid=%c reason=%c capture=%c");

// Scheduled arm: attach to trsync now, but do not expose the current GPIO
// level or unmask its edge IRQ until the supplied MCU clock.  In particular,
// a second homing pass cannot stop a retract that is queued for that same
// future motion boundary merely because this command arrived first.
void
command_trigger_source_arm_at(uint32_t *args)
{
    struct trigger_source *tsrc = trigger_gpio_oid_lookup(args[0]);
    trigger_source_prepare(tsrc, trsync_oid_lookup(args[2]),
                           args[3], args[4]);
    irq_disable();
    tsrc->flags |= TSRC_ARM_PENDING;
    tsrc->arm_timer.waketime = args[1];
    irq_enable();
    sched_add_timer(&tsrc->arm_timer);
}
DECL_COMMAND(command_trigger_source_arm_at,
             "trigger_source_arm_at oid=%c clock=%u trsync_oid=%c"
             " reason=%c capture=%c");

// Commissioning-only passive observer: latch and log the hardware edge but
// leave the legacy endstop timer solely responsible for firing trsync.
void
command_trigger_source_observe(uint32_t *args)
{
    struct trigger_source *tsrc = trigger_gpio_oid_lookup(args[0]);
    irq_disable();
    sched_del_timer(&tsrc->arm_timer);
    if (tsrc->hw_arm)
        tsrc->hw_arm(tsrc, 0);
    tsrc->ts = NULL;
    tsrc->reason = 0;
    tsrc->flags &= ~(TSRC_TRIGGERED | TSRC_CAPTURE_ON | TSRC_ARM_PENDING);
    if (args[1] && (tsrc->flags & TSRC_CAN_CAPTURE))
        tsrc->flags |= TSRC_CAPTURE_ON;
    tsrc->flags |= TSRC_ARMED | TSRC_OBSERVER;
    if (tsrc->hw_arm)
        tsrc->hw_arm(tsrc, 1);
    irq_enable();
}
DECL_COMMAND(command_trigger_source_observe,
             "trigger_source_observe oid=%c capture=%c");

void
command_trigger_source_disarm(uint32_t *args)
{
    struct trigger_source *tsrc = trigger_gpio_oid_lookup(args[0]);
    irq_disable();
    sched_del_timer(&tsrc->arm_timer);
    if (tsrc->hw_arm)
        tsrc->hw_arm(tsrc, 0);
    tsrc->flags &= ~(TSRC_ARMED | TSRC_OBSERVER | TSRC_ARM_PENDING);
    tsrc->ts = NULL;
    irq_enable();
}
DECL_COMMAND(command_trigger_source_disarm, "trigger_source_disarm oid=%c");

void
command_trigger_source_query(uint32_t *args)
{
    struct trigger_source *tsrc = trigger_gpio_oid_lookup(args[0]);
    irq_disable();
    uint8_t flags = tsrc->flags;
    uint32_t clock = tsrc->trigger_clock;
    irq_enable();
    sendf("trigger_source_state oid=%c flags=%c clock=%u"
          , tsrc->oid, flags, clock);
}
DECL_COMMAND(command_trigger_source_query, "trigger_source_query oid=%c");

// Deliver a hardware event. Runs in the peripheral's IRQ context;
// qualification is a short bounded busy-wait (the configured window
// is validated at config time).
void
trigger_source_notify(struct trigger_source *tsrc, uint32_t clock)
{
    if (!(tsrc->flags & TSRC_ARMED)
        || (!tsrc->ts && !(tsrc->flags & TSRC_OBSERVER)))
        return;
    uint8_t observer = tsrc->flags & TSRC_OBSERVER;
    // The active path qualifies before firing trsync. The observer records
    // the first edge immediately: doing the 20us busy-wait there would
    // materially perturb the polling latency it is intended to measure.
    if (!observer && tsrc->flags & TSRC_CAN_QUALIFY
        && tsrc->qualify_count) {
        uint32_t start = timer_read_time();
        uint32_t elapsed_target = 0;
        uint8_t i;
        for (i = 0; i < tsrc->qualify_count; i++) {
            elapsed_target += tsrc->qualify_ticks;
            while (timer_read_time() - start < elapsed_target)
                ;
            if (gpio_in_read(tsrc->pin_in) != tsrc->edge) {
                // False edge: swallow it and stay armed
                if (tsrc->hw_arm)
                    tsrc->hw_arm(tsrc, 1);
                return;
            }
        }
    }
    // Mask the peripheral as part of the successful IRQ transaction.  The
    // host will disarm the source too, but that command arrives later and a
    // bouncing switch must not keep re-entering an already-fired source in
    // the meantime.
    if (tsrc->hw_arm)
        tsrc->hw_arm(tsrc, 0);
    tsrc->flags &= ~(TSRC_ARMED | TSRC_OBSERVER);
    tsrc->flags |= TSRC_TRIGGERED;
    tsrc->trigger_clock = clock;
    struct trsync *ts = tsrc->ts;
    tsrc->ts = NULL;
    if (!observer)
        trsync_do_trigger(ts, tsrc->reason);
    execlog_append(observer ? EL_EDGE_OBSERVED : EL_TRIGGER,
                   tsrc->oid, clock, 0, observer ? 0 : tsrc->reason);
    LOG2(TRACE_SUB_TRIGGER, TRACE_LVL_INFO, TRACE_EV_trigger_fire,
         tsrc->oid, tsrc->reason);
}

void
trigger_source_shutdown(void)
{
    uint8_t oid;
    struct trigger_source *tsrc;
    foreach_oid(oid, tsrc, command_config_trigger_gpio) {
        sched_del_timer(&tsrc->arm_timer);
        if (tsrc->hw_arm)
            tsrc->hw_arm(tsrc, 0);
        tsrc->flags &= ~(TSRC_ARMED | TSRC_OBSERVER | TSRC_ARM_PENDING);
        tsrc->ts = NULL;
    }
}
DECL_SHUTDOWN(trigger_source_shutdown);
