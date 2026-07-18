// Commands for controlling GPIO analog-to-digital input pins
//
// Copyright (C) 2016-2026  Kevin O'Connor <kevin@koconnor.net>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "basecmd.h" // oid_alloc
#include "board/gpio.h" // struct gpio_adc
#include "board/irq.h" // irq_disable
#include "board/misc.h" // timer_read_time
#include "command.h" // DECL_COMMAND
#include "sched.h" // DECL_TASK
#include "trigger_analog.h" // trigger_analog_update

struct analog_in {
    struct timer timer;
    uint32_t rest_time, sample_time, next_begin_time;
    uint16_t value, min_value, max_value;
    struct gpio_adc pin;
    uint8_t invalid_count, range_check_count;
    uint8_t state, sample_count;
    uint8_t bytes_per_report, data_count;
    uint8_t data[48];
    struct trigger_analog *ta;
#if CONFIG_ADC_PROFILE
    uint32_t timer_invocations, conversion_retries, conversions, reports;
    uint32_t event_ticks, event_ticks_max;
#endif
};

static struct task_wake analog_wake;

static uint_fast8_t
analog_in_event(struct timer *timer)
{
    struct analog_in *a = container_of(timer, struct analog_in, timer);
#if CONFIG_ADC_PROFILE
    uint32_t profile_start = timer_read_time();
    a->timer_invocations++;
#define ADC_PROFILE_DONE() do {                                      \
        uint32_t elapsed = timer_read_time() - profile_start;         \
        a->event_ticks += elapsed;                                    \
        if (elapsed > a->event_ticks_max)                             \
            a->event_ticks_max = elapsed;                             \
    } while (0)
#else
#define ADC_PROFILE_DONE() do { } while (0)
#endif
    uint32_t sample_delay = gpio_adc_sample(a->pin);
    if (sample_delay) {
#if CONFIG_ADC_PROFILE
        a->conversion_retries++;
#endif
        a->timer.waketime += sample_delay;
        ADC_PROFILE_DONE();
        return SF_RESCHEDULE;
    }
    uint16_t value = gpio_adc_read(a->pin);
#if CONFIG_ADC_PROFILE
    a->conversions++;
#endif
    uint8_t state = a->state;
    if (state >= a->sample_count) {
        state = 0;
    } else {
        value += a->value;
    }
    a->value = value;
    a->state = state+1;
    if (a->state < a->sample_count) {
        a->timer.waketime += a->sample_time;
        ADC_PROFILE_DONE();
        return SF_RESCHEDULE;
    }
    if (likely(a->value >= a->min_value && a->value <= a->max_value)) {
        a->invalid_count = 0;
    } else {
        a->invalid_count++;
        if (a->invalid_count >= a->range_check_count) {
            try_shutdown("ADC out of range");
            a->invalid_count = 0;
        }
    }
    sched_wake_task(&analog_wake);
    a->next_begin_time += a->rest_time;
    a->timer.waketime = a->next_begin_time;
    ADC_PROFILE_DONE();
    return SF_RESCHEDULE;
#undef ADC_PROFILE_DONE
}

void
command_config_analog_in(uint32_t *args)
{
    struct gpio_adc pin = gpio_adc_setup(args[1]);
    struct analog_in *a = oid_alloc(
        args[0], command_config_analog_in, sizeof(*a));
    a->timer.func = analog_in_event;
    a->pin = pin;
    a->state = 1;
}
DECL_COMMAND(command_config_analog_in, "config_analog_in oid=%c pin=%u");

void
command_query_analog_in(uint32_t *args)
{
    struct analog_in *a = oid_lookup(args[0], command_config_analog_in);
    sched_del_timer(&a->timer);
    gpio_adc_cancel_sample(a->pin);
    a->next_begin_time = args[1];
    a->timer.waketime = a->next_begin_time;
    a->sample_time = args[2];
    a->sample_count = args[3];
    a->state = a->sample_count + 1;
    a->rest_time = args[4];
    a->bytes_per_report = args[5];
    a->data_count = 0;
    a->min_value = args[6];
    a->max_value = args[7];
    a->range_check_count = args[8];
#if CONFIG_ADC_PROFILE
    a->timer_invocations = a->conversion_retries = a->conversions = 0;
    a->reports = a->event_ticks = a->event_ticks_max = 0;
#endif
    if (! a->sample_count)
        return;
    if (a->bytes_per_report > ARRAY_SIZE(a->data))
        shutdown("Invalid analog_in bytes_per_report");
    sched_add_timer(&a->timer);
}
DECL_COMMAND(command_query_analog_in,
             "query_analog_in oid=%c clock=%u sample_ticks=%u sample_count=%c"
             " rest_ticks=%u bytes_per_report=%c"
             " min_value=%hu max_value=%hu range_check_count=%c");

void
command_analog_in_attach_trigger_analog(uint32_t *args) {
    struct analog_in *a = oid_lookup(args[0], command_config_analog_in);
    a->ta = trigger_analog_oid_lookup(args[1]);
}
#if CONFIG_WANT_TRIGGER_ANALOG
DECL_COMMAND(command_analog_in_attach_trigger_analog,
    "analog_in_attach_trigger_analog oid=%c trigger_analog_oid=%c");
#endif

#define BYTES_PER_SAMPLE 2

void
analog_in_task(void)
{
    if (!sched_check_wake(&analog_wake))
        return;
    uint8_t oid;
    struct analog_in *a;
    foreach_oid(oid, a, command_config_analog_in) {
        if (a->state != a->sample_count)
            continue;
        irq_disable();
        if (a->state != a->sample_count) {
            irq_enable();
            continue;
        }
        uint16_t value = a->value;
        uint32_t next_begin_time = a->next_begin_time;
        a->state++;
        irq_enable();
        trigger_analog_update(a->ta, value);
        uint8_t *d = &a->data[a->data_count];
        d[0] = value;
        d[1] = value >> 8;
        a->data_count += BYTES_PER_SAMPLE;
        if (a->data_count + BYTES_PER_SAMPLE > a->bytes_per_report) {
            sendf("analog_in_state oid=%c next_clock=%u values=%*s"
                  , oid, next_begin_time, a->data_count, a->data);
#if CONFIG_ADC_PROFILE
            a->reports++;
#endif
            a->data_count = 0;
        }
    }
}
DECL_TASK(analog_in_task);

#if CONFIG_ADC_PROFILE
void
command_analog_in_get_profile(uint32_t *args)
{
    struct analog_in *a = oid_lookup(args[0], command_config_analog_in);
    sendf("analog_in_profile oid=%c timer_invocations=%u retries=%u"
          " conversions=%u reports=%u event_ticks=%u event_ticks_max=%u",
          args[0], a->timer_invocations, a->conversion_retries,
          a->conversions, a->reports, a->event_ticks, a->event_ticks_max);
}
DECL_COMMAND_FLAGS(command_analog_in_get_profile, HF_IN_SHUTDOWN,
                   "analog_in_get_profile oid=%c");
#endif

void
analog_in_shutdown(void)
{
    uint8_t i;
    struct analog_in *a;
    foreach_oid(i, a, command_config_analog_in) {
        gpio_adc_cancel_sample(a->pin);
        a->ta = NULL;
        if (a->sample_count) {
            a->state = a->sample_count + 1;
            a->next_begin_time += a->rest_time;
            a->timer.waketime = a->next_begin_time;
            sched_add_timer(&a->timer);
        }
    }
}
DECL_SHUTDOWN(analog_in_shutdown);
