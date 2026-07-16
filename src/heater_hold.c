// Heater failsafe hold: autonomous bang-bang hold policy (FD-0001
// doc 08).
//
// An opt-in, per-heater policy that keeps a heater (typically the
// bed) at its last target while the machine is in pause-and-hold and
// the host may be gone. Deliberately minimal on-MCU capability:
// hysteresis control on the heater's ADC channel, the existing ADC
// sanity limits, an on-MCU deviation (runaway) check, a hard
// temperature ceiling, and an unconditional duration cap. Where a
// heater is configured for hold, the host substitutes this bounded
// envelope for its blanket max_duration watchdog — that trade is the
// user's explicit, per-heater decision in the printer config, never
// a default.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "autoconf.h" // CONFIG_*
#include "basecmd.h" // oid_alloc
#include "board/gpio.h" // gpio_out_setup, gpio_adc_setup
#include "board/irq.h" // irq_disable
#include "board/misc.h" // timer_read_time
#include "command.h" // DECL_COMMAND
#include "execlog.h" // execlog_append
#include "gpiocmds.h" // digital_out_takeover_pin, digital_out_release_pin
#include "heater_hold_math.h" // heater_hold_at_or_above_ceiling
#include "sched.h" // DECL_TASK

enum {
    HH_DISABLED,   // configured, no policy armed
    HH_ARMED,      // watching host pings; engages on silence
    HH_ENGAGED,    // autonomously holding temperature
    HH_EXPIRED,    // hold ended (duration/runaway/ceiling): heater off
};

struct heater_hold {
    struct timer time;
    struct gpio_adc adc;
    struct gpio_out heater_out;
    uint32_t heater_pin;
    uint32_t sample_ticks;    // controller period
    uint32_t ping_timeout;    // ticks of host silence before engage
    uint32_t last_ping;
    uint32_t max_samples;     // hard duration cap while engaged
    uint32_t engaged_samples;
    uint16_t target_adc;      // bang-bang setpoint (raw counts)
    uint16_t ceiling_adc;     // hard over-temperature cutoff
    uint16_t band_adc;        // deviation band for the runaway check
    uint16_t min_valid, max_valid; // ADC sanity range (sensor fault)
    uint16_t last_adc;
    uint8_t deviation_count, max_deviation_count;
    uint8_t state;
    uint8_t invert;           // 1 = higher ADC means hotter
    uint8_t heater_on;
    uint8_t adc_pending;
    uint8_t oid;
    uint8_t event_pending;
};

static struct task_wake heater_hold_wake;
static void heater_hold_transition(struct heater_hold *h, uint8_t state);

static uint_fast8_t
heater_takeover(struct heater_hold *h)
{
    // The normal software-PWM object may have an active toggle timer even
    // when no future host updates remain.  Quiesce it before the autonomous
    // holder writes the shared pin, otherwise it can re-energize the heater
    // after a ceiling/duration cutoff reports output off.
    if (digital_out_takeover_pin(h->heater_pin, 0)) {
        h->heater_out = gpio_out_setup(h->heater_pin, 0);
        h->heater_on = 0;
        heater_hold_transition(h, HH_EXPIRED);
        return 0;
    }
    h->heater_out = gpio_out_setup(h->heater_pin, 0);
    h->heater_on = 0;
    return 1;
}

static void
heater_output(struct heater_hold *h, uint8_t on)
{
    h->heater_on = on;
    gpio_out_write(h->heater_out, on);
}

static void
heater_hold_transition(struct heater_hold *h, uint8_t state)
{
    h->state = state;
    h->event_pending = 1;
    execlog_append(EL_HEATER, h->oid, timer_read_time()
                   , h->last_adc, state);
    sched_wake_task(&heater_hold_wake);
}

// Periodic controller. Runs from the timer irq at sample_ticks; the
// per-wake work is one ADC state-machine step or one control step.
static uint_fast8_t
heater_hold_event(struct timer *t)
{
    struct heater_hold *h = container_of(t, struct heater_hold, time);

    if (h->state == HH_ARMED) {
        if (timer_read_time() - h->last_ping > h->ping_timeout) {
            // Host went silent: take the heater over
            if (!heater_takeover(h)) {
                h->time.waketime += h->sample_ticks;
                return SF_RESCHEDULE;
            }
            h->engaged_samples = 0;
            h->deviation_count = 0;
            heater_hold_transition(h, HH_ENGAGED);
        }
        h->time.waketime += h->sample_ticks;
        return SF_RESCHEDULE;
    }
    if (h->state != HH_ENGAGED) {
        h->time.waketime += h->sample_ticks;
        return SF_RESCHEDULE;
    }

    // Engaged: sample the sensor
    uint32_t sample_delay = gpio_adc_sample(h->adc);
    if (sample_delay) {
        h->time.waketime += sample_delay;
        return SF_RESCHEDULE;
    }
    uint16_t adc = gpio_adc_read(h->adc);
    h->last_adc = adc;

    // Sensor sanity (out-of-range = fault: off, permanently)
    if (adc < h->min_valid || adc > h->max_valid) {
        heater_output(h, 0);
        heater_hold_transition(h, HH_EXPIRED);
        h->time.waketime += h->sample_ticks;
        return SF_RESCHEDULE;
    }
    // Hard ceiling
    if (heater_hold_at_or_above_ceiling(
            h->invert, adc, h->ceiling_adc)) {
        heater_output(h, 0);
        heater_hold_transition(h, HH_EXPIRED);
        h->time.waketime += h->sample_ticks;
        return SF_RESCHEDULE;
    }
    // Deviation (on-MCU runaway check): sustained excursion outside
    // the band around the hold target turns the heater off for good.
    uint16_t dev = adc > h->target_adc ? adc - h->target_adc
                                       : h->target_adc - adc;
    if (dev > h->band_adc) {
        if (++h->deviation_count >= h->max_deviation_count) {
            heater_output(h, 0);
            heater_hold_transition(h, HH_EXPIRED);
            h->time.waketime += h->sample_ticks;
            return SF_RESCHEDULE;
        }
    } else {
        h->deviation_count = 0;
    }
    // Unconditional duration cap
    if (++h->engaged_samples >= h->max_samples) {
        heater_output(h, 0);
        heater_hold_transition(h, HH_EXPIRED);
        h->time.waketime += h->sample_ticks;
        return SF_RESCHEDULE;
    }
    // Hysteresis control
    heater_output(h, heater_hold_colder_than(
                      h->invert, adc, h->target_adc));

    h->time.waketime += h->sample_ticks;
    return SF_RESCHEDULE;
}

void
command_config_heater_hold(uint32_t *args)
{
    struct heater_hold *h = oid_alloc(
        args[0], command_config_heater_hold, sizeof(*h));
    h->oid = args[0];
    h->heater_pin = args[1];
    h->adc = gpio_adc_setup(args[2]);
    h->invert = args[3];
    h->state = HH_DISABLED;
    h->time.func = heater_hold_event;
}
DECL_COMMAND(command_config_heater_hold,
             "config_heater_hold oid=%c heater_pin=%u sensor_pin=%u"
             " invert_sense=%c");

static struct heater_hold *
heater_hold_oid_lookup(uint8_t oid)
{
    return oid_lookup(oid, command_config_heater_hold);
}

// Arm (or retune) the policy. All thresholds are raw ADC counts (the
// host owns the temperature conversion) and land in the data
// dictionary via the host config, so the machine's failure behavior
// is inspectable.
void
command_heater_hold_setup(uint32_t *args)
{
    struct heater_hold *h = heater_hold_oid_lookup(args[0]);
    irq_disable();
    sched_del_timer(&h->time);
    h->target_adc = args[1];
    h->ceiling_adc = args[2];
    h->band_adc = args[3];
    h->min_valid = args[4];
    h->max_valid = args[5];
    h->ping_timeout = args[6];
    h->sample_ticks = args[7];
    h->max_samples = args[8];
    h->max_deviation_count = args[9];
    if (!h->sample_ticks || !h->max_samples || !h->max_deviation_count) {
        h->state = HH_DISABLED;
        irq_enable();
        return;
    }
    h->last_ping = timer_read_time();
    h->state = HH_ARMED;
    h->time.waketime = timer_read_time() + h->sample_ticks;
    sched_add_timer(&h->time);
    irq_enable();
}
DECL_COMMAND(command_heater_hold_setup,
             "heater_hold_setup oid=%c target=%hu ceiling=%hu band=%hu"
             " min_valid=%hu max_valid=%hu ping_timeout=%u sample_ticks=%u"
             " max_samples=%u max_deviation=%c");

// Host liveness ping; silence beyond ping_timeout engages the hold.
void
command_heater_hold_ping(uint32_t *args)
{
    struct heater_hold *h = heater_hold_oid_lookup(args[0]);
    irq_disable();
    h->last_ping = timer_read_time();
    irq_enable();
}
DECL_COMMAND(command_heater_hold_ping, "heater_hold_ping oid=%c");

// Explicit engage (host-commanded pause-and-hold with the host still
// present, e.g. a toolhead board elsewhere went quiet).
void
command_heater_hold_engage(uint32_t *args)
{
    struct heater_hold *h = heater_hold_oid_lookup(args[0]);
    irq_disable();
    if (h->state == HH_ARMED) {
        if (!heater_takeover(h)) {
            irq_enable();
            return;
        }
        h->engaged_samples = 0;
        h->deviation_count = 0;
        heater_hold_transition(h, HH_ENGAGED);
    }
    irq_enable();
}
DECL_COMMAND(command_heater_hold_engage, "heater_hold_engage oid=%c");

// Host resumed control (or disarms the policy): stop driving the pin.
void
command_heater_hold_release(uint32_t *args)
{
    struct heater_hold *h = heater_hold_oid_lookup(args[0]);
    irq_disable();
    if (h->state == HH_ENGAGED || h->state == HH_EXPIRED)
        heater_output(h, 0);
    sched_del_timer(&h->time);
    h->state = HH_DISABLED;
    digital_out_release_pin(h->heater_pin);
    irq_enable();
    sendf("heater_hold_state oid=%c state=%c adc=%hu samples=%u"
          , h->oid, h->state, h->last_adc, h->engaged_samples);
}
DECL_COMMAND(command_heater_hold_release, "heater_hold_release oid=%c");

void
command_heater_hold_query(uint32_t *args)
{
    struct heater_hold *h = heater_hold_oid_lookup(args[0]);
    irq_disable();
    uint8_t state = h->state;
    uint16_t adc = h->last_adc;
    uint32_t samples = h->engaged_samples;
    irq_enable();
    sendf("heater_hold_state oid=%c state=%c adc=%hu samples=%u"
          , h->oid, state, adc, samples);
}
DECL_COMMAND(command_heater_hold_query, "heater_hold_query oid=%c");

void
heater_hold_task(void)
{
    if (!sched_check_wake(&heater_hold_wake))
        return;
    uint8_t oid;
    struct heater_hold *h;
    foreach_oid(oid, h, command_config_heater_hold) {
        irq_disable();
        uint8_t pending = h->event_pending;
        h->event_pending = 0;
        uint8_t state = h->state;
        uint16_t adc = h->last_adc;
        uint32_t samples = h->engaged_samples;
        irq_enable();
        if (pending)
            sendf("heater_hold_state oid=%c state=%c adc=%hu samples=%u"
                  , oid, state, adc, samples);
    }
}
DECL_TASK(heater_hold_task);

// Machine shutdown: the hold policy survives *around* a healthy MCU;
// a genuine firmware fault still turns everything off, unchanged.
void
heater_hold_shutdown(void)
{
    uint8_t oid;
    struct heater_hold *h;
    foreach_oid(oid, h, command_config_heater_hold) {
        if (h->state == HH_ENGAGED)
            heater_output(h, 0);
        sched_del_timer(&h->time);
        h->state = HH_DISABLED;
        gpio_adc_cancel_sample(h->adc);
    }
}
DECL_SHUTDOWN(heater_hold_shutdown);
