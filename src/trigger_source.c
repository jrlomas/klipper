// Event-driven trigger sources feeding trsync (RFC 0001 doc 09).
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

// Cap on the busy-wait qualification window (about 100us at typical
// clock rates would already be generous; this is a hard safety cap).
#define QUALIFY_MAX_TICKS (CONFIG_CLOCK_FREQ / 2000)

void command_config_trigger_gpio(uint32_t *args);

struct trigger_source *
trigger_source_alloc(uint8_t oid, uint8_t kind)
{
    // All kinds share one oid namespace so arm/disarm/query commands
    // work uniformly.
    struct trigger_source *tsrc = oid_alloc(
        oid, command_config_trigger_gpio, sizeof(*tsrc));
    tsrc->oid = oid;
    tsrc->kind = kind;
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
}
DECL_COMMAND(command_config_trigger_gpio,
             "config_trigger_gpio oid=%c pin=%u edge=%c pull_up=%c"
             " qualify_ticks=%u qualify_count=%c");

static struct trigger_source *
trigger_gpio_oid_lookup(uint8_t oid)
{
    return oid_lookup(oid, command_config_trigger_gpio);
}

// Arm: attach to a trsync session. Fires trsync_do_trigger(reason)
// on the (qualified) hardware event.
void
command_trigger_source_arm(uint32_t *args)
{
    struct trigger_source *tsrc = trigger_gpio_oid_lookup(args[0]);
    struct trsync *ts = trsync_oid_lookup(args[1]);
    irq_disable();
    tsrc->ts = ts;
    tsrc->reason = args[2];
    tsrc->flags &= ~TSRC_TRIGGERED;
    tsrc->flags |= TSRC_ARMED;
    if (tsrc->hw_arm)
        tsrc->hw_arm(tsrc, 1);
    irq_enable();
}
DECL_COMMAND(command_trigger_source_arm,
             "trigger_source_arm oid=%c trsync_oid=%c reason=%c");

void
command_trigger_source_disarm(uint32_t *args)
{
    struct trigger_source *tsrc = trigger_gpio_oid_lookup(args[0]);
    irq_disable();
    if (tsrc->hw_arm)
        tsrc->hw_arm(tsrc, 0);
    tsrc->flags &= ~TSRC_ARMED;
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
    if (!(tsrc->flags & TSRC_ARMED) || !tsrc->ts)
        return;
    if (tsrc->flags & TSRC_CAN_QUALIFY && tsrc->qualify_count) {
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
    tsrc->flags &= ~TSRC_ARMED;
    tsrc->flags |= TSRC_TRIGGERED;
    tsrc->trigger_clock = clock;
    struct trsync *ts = tsrc->ts;
    tsrc->ts = NULL;
    trsync_do_trigger(ts, tsrc->reason);
    execlog_append(EL_TRIGGER, tsrc->oid, clock, 0, tsrc->reason);
}

void
trigger_source_shutdown(void)
{
    uint8_t oid;
    struct trigger_source *tsrc;
    foreach_oid(oid, tsrc, command_config_trigger_gpio) {
        if (tsrc->hw_arm)
            tsrc->hw_arm(tsrc, 0);
        tsrc->flags &= ~TSRC_ARMED;
        tsrc->ts = NULL;
    }
}
DECL_SHUTDOWN(trigger_source_shutdown);
