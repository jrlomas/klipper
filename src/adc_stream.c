// DMA-backed ADC acquisition stream.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "adc_stream.h"
#include "basecmd.h" // oid_alloc
#include "board/adc_stream.h" // board_adc_stream_*
#include "board/irq.h" // irq_save
#include "board/misc.h" // timer_read_time
#include "command.h" // DECL_COMMAND
#include "compiler.h" // __aligned
#include "generic/acq_block.h"
#include "generic/acq_capture.h"
#include "generic/acq_ring.h"
#include "generic/adc_filter.h"
#include "generic/adc_safety.h"
#include "generic/dma_resource.h"
#include "sched.h" // DECL_TASK
#include "traj_local.h" // traj_local_hold_all
#include "trigger_analog.h" // trigger_analog_update

enum {
    ADC_STREAM_STOPPED = 0,
    ADC_STREAM_ARMED,
    ADC_STREAM_RUNNING,
    ADC_STREAM_FAULTED,
};

#if CONFIG_MACH_STM32G0 || CONFIG_MACH_STM32H7
#define ADC_STREAM_TARGET_CAPS ADC_STREAM_CAP_HW_OVERSAMPLE
#else
#define ADC_STREAM_TARGET_CAPS 0
#endif

struct adc_stream;

struct adc_stream_subscription {
    struct timer deadline_timer;
    struct adc_filter filter;
    struct adc_safety safety;
    struct adc_stream *stream;
    struct trigger_analog *local_trigger;
    adc_stream_local_callback local_callback;
    void *local_context;
    uint32_t sequence;
    uint8_t id;
    uint8_t channel;
    uint8_t report_class;
    uint8_t deadline_armed;
};

struct adc_stream {
    struct timer start_timer;
    struct gpio_adc pins[ADC_STREAM_MAX_CHANNELS];
    struct acq_block blocks[ADC_STREAM_BLOCK_COUNT];
    struct acq_capture capture;
    struct acq_ring ready_ring;
    struct adc_stream_backend_info info;
    struct adc_stream_subscription subscriptions[ADC_STREAM_MAX_SUBSCRIPTIONS];
    uint16_t *buffer;
    uint32_t first_clock;
    uint32_t sequence;
    uint32_t epoch;
    uint32_t dropped_blocks;
    uint32_t fault_status;
    uint32_t dma_errors;
    uint32_t adc_errors;
    uint32_t overruns;
    uint32_t telemetry_drops;
    uint32_t watchdog_events;
    uint32_t safety_clock;
    uint32_t publication_count, publication_ticks, publication_ticks_max;
    uint32_t consumer_count, consumer_ticks, consumer_ticks_max;
    uint64_t raw_scan_count;
    uint8_t channel_count;
    uint8_t subscription_count;
    uint8_t block_values;
    uint8_t traffic_class;
    uint8_t oid;
    uint8_t raw_output;
    uint16_t hardware_oversample;
    uint8_t hardware_shift;
    uint8_t safety_pending;
    uint8_t safety_sub;
    uint8_t safety_event;
    uint8_t safety_action;
    volatile uint8_t state;
};

// The first implementation deliberately permits one physical ADC engine.
static struct adc_stream *active_stream;
static struct task_wake adc_stream_wake;

void command_config_adc_stream(uint32_t *args);

static struct adc_stream_subscription *
adc_stream_find_subscription(struct adc_stream *s, uint8_t id)
{
    for (uint8_t i = 0; i < s->subscription_count; i++)
        if (s->subscriptions[i].id == id)
            return &s->subscriptions[i];
    return NULL;
}

int
adc_stream_bind_local(uint8_t stream_oid, uint8_t subscription,
                      adc_stream_local_callback callback, void *context)
{
    struct adc_stream *s = oid_lookup(
        stream_oid, command_config_adc_stream);
    struct adc_stream_subscription *sub = adc_stream_find_subscription(
        s, subscription);
    if (s->state != ADC_STREAM_STOPPED || !sub || !callback
        || sub->local_callback)
        return -1;
    sub->local_callback = callback;
    sub->local_context = context;
    return 0;
}

static void
adc_stream_apply_safety(struct adc_stream *s,
                        struct adc_stream_subscription *sub,
                        uint8_t event)
{
    s->watchdog_events++;
    s->fault_status |= event == ADC_SAFETY_EVENT_THRESHOLD
                       ? ACQ_STATUS_THRESHOLD : ACQ_STATUS_DEADLINE;
    s->safety_pending = 1;
    s->safety_sub = sub->id;
    s->safety_event = event;
    s->safety_action = sub->safety.config.fail_action;
    s->safety_clock = timer_read_time();
    sched_wake_task(&adc_stream_wake);
    acq_capture_trigger(&s->capture, 0);
    switch (sub->safety.config.fail_action) {
    case ADC_SAFETY_HOLD:
        traj_local_hold_all();
        break;
    case ADC_SAFETY_TRIGGER:
        if (sub->local_trigger)
            trigger_analog_note_error(sub->local_trigger, event);
        else
            try_shutdown("ADC safety trigger has no local consumer");
        break;
    case ADC_SAFETY_SHUTDOWN:
        try_shutdown("ADC safety policy fired");
        break;
    }
}

static uint_fast8_t
adc_stream_deadline_event(struct timer *timer)
{
    struct adc_stream_subscription *sub = container_of(
        timer, struct adc_stream_subscription, deadline_timer);
    sub->deadline_armed = 0;
    uint8_t event = adc_safety_check_deadline(
        &sub->safety, timer_read_time());
    if (event)
        adc_stream_apply_safety(sub->stream, sub, event);
    return SF_DONE;
}

static uint_fast8_t
adc_stream_start_event(struct timer *timer)
{
    struct adc_stream *s = container_of(timer, struct adc_stream, start_timer);
    if (s->state != ADC_STREAM_ARMED)
        return SF_DONE;
    // The backend advertises uncertainty for the interval between this
    // machine-clock observation and its first conversion aperture.
    s->first_clock = timer_read_time();
    board_adc_stream_start();
    s->state = ADC_STREAM_RUNNING;
    return SF_DONE;
}

void
command_config_adc_stream(uint32_t *args)
{
    struct adc_stream *s = oid_alloc(
        args[0], command_config_adc_stream, sizeof(*s));
    s->oid = args[0];
    s->state = ADC_STREAM_STOPPED;
    s->raw_output = 1;
    s->hardware_oversample = 1;
    s->start_timer.func = adc_stream_start_event;
    s->buffer = dma_pool_alloc(
        ADC_STREAM_BLOCK_COUNT * ADC_STREAM_MAX_BLOCK_VALUES
            * sizeof(*s->buffer), 32,
        DMA_POOL_BUFFER | DMA_POOL_DMA_REACHABLE | DMA_POOL_NONCACHEABLE,
        s->oid);
    if (!s->buffer)
        shutdown("ADC stream DMA pool exhausted");
}
DECL_COMMAND(command_config_adc_stream, "config_adc_stream oid=%c");

void
command_adc_stream_add_channel(uint32_t *args)
{
    struct adc_stream *s = oid_lookup(args[0], command_config_adc_stream);
    if (s->state != ADC_STREAM_STOPPED)
        shutdown("ADC stream channel change while active");
    if (s->channel_count >= ADC_STREAM_MAX_CHANNELS)
        shutdown("Too many ADC stream channels");
    s->pins[s->channel_count++] = board_adc_stream_setup_pin(args[1]);
}
DECL_COMMAND(command_adc_stream_add_channel,
             "adc_stream_add_channel oid=%c pin=%u");

void
command_adc_stream_subscribe(uint32_t *args)
{
    struct adc_stream *s = oid_lookup(args[0], command_config_adc_stream);
    uint8_t sub_id = args[1], channel = args[2], shift = args[5];
    uint16_t input_div = args[3], osr = args[4], report_div = args[6];
    uint8_t report_class = args[7];
    if (s->state != ADC_STREAM_STOPPED)
        shutdown("ADC stream subscription change while active");
    if (s->subscription_count >= ADC_STREAM_MAX_SUBSCRIPTIONS
        || channel >= s->channel_count || report_class > 2)
        shutdown("Invalid ADC stream subscription");
    for (uint8_t i = 0; i < s->subscription_count; i++)
        if (s->subscriptions[i].id == sub_id)
            shutdown("Duplicate ADC stream subscription");
    struct adc_stream_subscription *sub =
        &s->subscriptions[s->subscription_count];
    struct adc_filter_config config = {
        .input_div = input_div,
        .osr = osr,
        .report_div = report_div,
        .shift = shift,
    };
    if (adc_filter_configure(&sub->filter, &config))
        shutdown("Invalid ADC stream filter");
    sub->id = sub_id;
    sub->channel = channel;
    sub->report_class = report_class;
    sub->stream = s;
    sub->deadline_timer.func = adc_stream_deadline_event;
    s->subscription_count++;
}
DECL_COMMAND(command_adc_stream_subscribe,
             "adc_stream_subscribe oid=%c sub=%c channel=%c"
             " input_div=%hu osr=%hu shift=%c report_div=%hu"
             " report_class=%c");

void
command_adc_stream_set_subscription_options(uint32_t *args)
{
    struct adc_stream *s = oid_lookup(args[0], command_config_adc_stream);
    struct adc_stream_subscription *sub = adc_stream_find_subscription(
        s, args[1]);
    if (s->state != ADC_STREAM_STOPPED || !sub || args[2] > 1)
        shutdown("Invalid ADC subscription options");
    sub->filter.config.summary_mode = args[2];
}
DECL_COMMAND(command_adc_stream_set_subscription_options,
             "adc_stream_set_subscription_options oid=%c sub=%c"
             " summary_mode=%c");

void
command_adc_stream_set_subscription_filter(uint32_t *args)
{
    struct adc_stream *s = oid_lookup(args[0], command_config_adc_stream);
    struct adc_stream_subscription *sub = adc_stream_find_subscription(
        s, args[1]);
    if (s->state != ADC_STREAM_STOPPED || !sub
        || adc_filter_set_postprocess(&sub->filter, args[2], args[3]))
        shutdown("Invalid ADC subscription filter");
}
DECL_COMMAND(command_adc_stream_set_subscription_filter,
             "adc_stream_set_subscription_filter oid=%c sub=%c"
             " window_divisor=%hu alpha_q15=%hu");

void
command_adc_stream_set_safety(uint32_t *args)
{
    struct adc_stream *s = oid_lookup(args[0], command_config_adc_stream);
    struct adc_stream_subscription *sub = adc_stream_find_subscription(
        s, args[1]);
    if (s->state != ADC_STREAM_STOPPED || !sub)
        shutdown("Invalid ADC safety subscription");
    struct adc_safety_config config = {
        .deadline_ticks = args[2],
        .fail_action = args[3],
        .low = args[4],
        .high = args[5],
        .fault_count = args[6],
    };
    if (adc_safety_configure(&sub->safety, &config))
        shutdown("Invalid ADC safety policy");
    uint8_t trigger_oid = args[7];
    if (trigger_oid != 0xff)
        sub->local_trigger = trigger_analog_oid_lookup(trigger_oid);
    if (config.fail_action == ADC_SAFETY_TRIGGER && !sub->local_trigger)
        shutdown("ADC trigger action requires local trigger consumer");
}
DECL_COMMAND(command_adc_stream_set_safety,
             "adc_stream_set_safety oid=%c sub=%c deadline_ticks=%u"
             " fail_action=%c low=%u high=%u fault_count=%c trigger_oid=%c");

void
command_adc_stream_set_options(uint32_t *args)
{
    struct adc_stream *s = oid_lookup(args[0], command_config_adc_stream);
    if (s->state != ADC_STREAM_STOPPED || args[1] > 1)
        shutdown("Invalid ADC stream options");
    s->raw_output = args[1];
}
DECL_COMMAND(command_adc_stream_set_options,
             "adc_stream_set_options oid=%c raw_output=%c");

void
command_adc_stream_set_hardware_oversample(uint32_t *args)
{
    struct adc_stream *s = oid_lookup(args[0], command_config_adc_stream);
    uint16_t ratio = args[1];
    uint8_t shift = args[2];
    if (s->state != ADC_STREAM_STOPPED || !ratio || ratio > 256
        || ratio & (ratio - 1) || shift > 8)
        shutdown("Invalid ADC hardware oversample configuration");
    s->hardware_oversample = ratio;
    s->hardware_shift = shift;
}
DECL_COMMAND(command_adc_stream_set_hardware_oversample,
             "adc_stream_set_hardware_oversample oid=%c ratio=%hu shift=%c");

void
command_adc_stream_arm_capture(uint32_t *args)
{
    struct adc_stream *s = oid_lookup(args[0], command_config_adc_stream);
    if (acq_capture_arm(&s->capture, args[1], args[2]))
        shutdown("Invalid ADC fault capture window");
}
DECL_COMMAND(command_adc_stream_arm_capture,
             "adc_stream_arm_capture oid=%c pre_blocks=%c post_blocks=%c");

static void
adc_stream_stop(struct adc_stream *s)
{
    irqstatus_t flag = irq_save();
    uint8_t old_state = s->state;
    s->state = ADC_STREAM_STOPPED;
    if (active_stream == s)
        active_stream = NULL;
    irq_restore(flag);
    if (old_state == ADC_STREAM_ARMED)
        sched_del_timer(&s->start_timer);
    if (old_state == ADC_STREAM_RUNNING || old_state == ADC_STREAM_FAULTED)
        board_adc_stream_stop();
    for (uint8_t i = 0; i < s->subscription_count; i++) {
        struct adc_stream_subscription *sub = &s->subscriptions[i];
        if (sub->deadline_armed) {
            sched_del_timer(&sub->deadline_timer);
            sub->deadline_armed = 0;
        }
        sub->safety.pending = 0;
    }
}

void
command_adc_stream_start(uint32_t *args)
{
    struct adc_stream *s = oid_lookup(args[0], command_config_adc_stream);
    uint32_t start_clock = args[1], period_ticks = args[2];
    uint8_t block_values = args[3], traffic_class = args[4];
    if (!s->channel_count || !period_ticks || !block_values
        || block_values > ADC_STREAM_MAX_BLOCK_VALUES
        || block_values % s->channel_count || traffic_class > 2)
        shutdown("Invalid ADC stream schedule");
    uint8_t block_scans = block_values / s->channel_count;
    for (uint8_t i = 0; i < s->subscription_count; i++) {
        struct adc_filter_config *fc = &s->subscriptions[i].filter.config;
        uint64_t report_scans = (uint64_t)fc->input_div * fc->osr
                                * fc->report_div;
        // Bound task/transport work to at most one report per subscription
        // for each completed DMA block.
        if (report_scans < block_scans)
            shutdown("ADC subscription report rate exceeds block rate");
        if (!s->subscriptions[i].report_class
            && (!s->subscriptions[i].safety.config.deadline_ticks
                || !s->subscriptions[i].safety.config.fail_action))
            shutdown("Class-0 ADC subscription lacks deadline policy");
        adc_filter_reset(&s->subscriptions[i].filter, 0);
        s->subscriptions[i].sequence = 0;
        s->subscriptions[i].safety.pending = 0;
        s->subscriptions[i].safety.outside_count = 0;
        s->subscriptions[i].deadline_armed = 0;
    }
    if (active_stream && active_stream != s)
        shutdown("ADC engine already claimed");
    if (s->state != ADC_STREAM_STOPPED)
        adc_stream_stop(s);

    s->block_values = block_values;
    s->traffic_class = traffic_class;
    acq_ring_init(&s->ready_ring, ADC_STREAM_BLOCK_COUNT);
    s->sequence = 0;
    s->dropped_blocks = 0;
    s->fault_status = 0;
    s->dma_errors = s->adc_errors = s->overruns = 0;
    s->telemetry_drops = s->watchdog_events = 0;
    s->publication_count = s->publication_ticks = 0;
    s->publication_ticks_max = 0;
    s->consumer_count = s->consumer_ticks = s->consumer_ticks_max = 0;
    s->raw_scan_count = 0;
    s->epoch++;
    for (uint8_t i = 0; i < ADC_STREAM_BLOCK_COUNT; i++) {
        acq_block_init(&s->blocks[i], &s->buffer[i * block_values]);
        acq_block_dma_take(&s->blocks[i]);
    }
    struct adc_stream_backend_config cfg = {
        .pins = s->pins,
        .buffer = s->buffer,
        .requested_period_ticks = period_ticks,
        .hardware_oversample = s->hardware_oversample,
        .channel_count = s->channel_count,
        .block_values = block_values,
        .hardware_shift = s->hardware_shift,
        .owner = s->oid,
    };
    s->info = (struct adc_stream_backend_info) { };
    board_adc_stream_setup(&cfg, &s->info);
    if (!s->info.period_denominator || !s->info.period_numerator)
        shutdown("ADC stream backend rejected schedule");
    for (uint8_t i = 0; i < s->subscription_count; i++) {
        struct adc_stream_subscription *sub = &s->subscriptions[i];
        if (sub->report_class)
            continue;
        struct adc_filter_config *fc = &sub->filter.config;
        uint64_t report_ticks = (uint64_t)fc->input_div * fc->osr
            * fc->report_div * s->info.period_numerator
            / s->info.period_denominator;
        if (!report_ticks
            || sub->safety.config.deadline_ticks >= report_ticks)
            shutdown("ADC Class-0 deadline overlaps next report");
    }

    active_stream = s;
    s->state = ADC_STREAM_ARMED;
    s->start_timer.waketime = start_clock;
    sched_add_timer(&s->start_timer);
}
DECL_COMMAND(command_adc_stream_start,
             "adc_stream_start oid=%c clock=%u period_ticks=%u"
             " block_values=%c traffic_class=%c");

void
command_adc_stream_stop(uint32_t *args)
{
    struct adc_stream *s = oid_lookup(args[0], command_config_adc_stream);
    adc_stream_stop(s);
}
DECL_COMMAND_FLAGS(command_adc_stream_stop, HF_IN_SHUTDOWN,
                   "adc_stream_stop oid=%c");

int
adc_stream_block_complete(uint8_t block_index, uint32_t status)
{
    struct adc_stream *s = active_stream;
    if (!s || s->state != ADC_STREAM_RUNNING
        || block_index >= ADC_STREAM_BLOCK_COUNT)
        return -1;
#if CONFIG_ADC_PROFILE
    uint32_t profile_start = timer_read_time();
    s->publication_count++;
#define ADC_PUBLICATION_DONE() do {                                  \
        uint32_t elapsed = timer_read_time() - profile_start;         \
        s->publication_ticks += elapsed;                              \
        if (elapsed > s->publication_ticks_max)                       \
            s->publication_ticks_max = elapsed;                       \
    } while (0)
#else
#define ADC_PUBLICATION_DONE() do { } while (0)
#endif
    struct acq_block *b = &s->blocks[block_index];
    if (status & ACQ_STATUS_DMA_ERROR)
        s->dma_errors++;
    if (status & (ACQ_STATUS_PERIPHERAL_ERROR | ACQ_STATUS_SAMPLE_ERROR))
        s->adc_errors++;
    if (status & ACQ_STATUS_OVERRUN)
        s->overruns++;
    if (b->state != ACQ_BLOCK_DMA_OWNED) {
        // The acquisition ring is exhausted. Stop immediately instead of
        // silently overwriting an unconsumed block.
        s->dropped_blocks++;
        s->overruns++;
        s->fault_status |= ACQ_STATUS_OVERRUN | ACQ_STATUS_DISCONTINUITY;
        s->state = ADC_STREAM_FAULTED;
        board_adc_stream_stop_from_isr();
        sched_wake_task(&adc_stream_wake);
        if (!s->traffic_class)
            try_shutdown("Critical ADC stream overrun");
        ADC_PUBLICATION_DONE();
        return -1;
    }
    uint32_t sequence = s->sequence++;
    uint64_t offset = (uint64_t)sequence
                      * (s->block_values / s->channel_count)
                      * s->info.period_numerator;
    uint32_t first_clock = s->first_clock
                           + offset / s->info.period_denominator;
    if (acq_block_publish(b, sequence, s->epoch, s->block_values,
                          first_clock, s->info.period_numerator,
                          s->info.period_denominator,
                          s->info.uncertainty_ticks,
                          s->info.status | status)) {
        s->fault_status |= ACQ_STATUS_PERIPHERAL_ERROR;
        s->state = ADC_STREAM_FAULTED;
        board_adc_stream_stop_from_isr();
    } else {
        if (acq_ring_push(&s->ready_ring, block_index)) {
            s->dropped_blocks++;
            s->overruns++;
            s->fault_status |= ACQ_STATUS_OVERRUN
                               | ACQ_STATUS_DISCONTINUITY;
            s->state = ADC_STREAM_FAULTED;
            board_adc_stream_stop_from_isr();
        }
    }
    // Every backend uses the two blocks in strict ping-pong order. Before the
    // just-completed DMA can wrap, the other block must have made the full
    // READY -> CONSUMER_OWNED -> FREE -> DMA_OWNED trip. A backend may have
    // already received its hardware chain trigger, so stop it synchronously
    // if ownership says that trigger could not safely start.
    struct acq_block *next = &s->blocks[block_index ^ 1];
    if (s->state != ADC_STREAM_FAULTED
        && next->state != ACQ_BLOCK_DMA_OWNED) {
        s->dropped_blocks++;
        s->overruns++;
        s->fault_status |= ACQ_STATUS_OVERRUN | ACQ_STATUS_DISCONTINUITY;
        s->state = ADC_STREAM_FAULTED;
        board_adc_stream_stop_from_isr();
    }
    sched_wake_task(&adc_stream_wake);
    if (!s->traffic_class
        && (status & (ACQ_STATUS_DMA_ERROR | ACQ_STATUS_PERIPHERAL_ERROR
                      | ACQ_STATUS_SAMPLE_ERROR | ACQ_STATUS_OVERRUN)))
        try_shutdown("Critical ADC acquisition fault");
    if (!s->traffic_class && s->state == ADC_STREAM_FAULTED)
        try_shutdown("Critical ADC stream overrun");
    ADC_PUBLICATION_DONE();
#undef ADC_PUBLICATION_DONE
    return s->state == ADC_STREAM_FAULTED ? -1 : 0;
}

void
adc_stream_backend_fault(uint32_t status)
{
    struct adc_stream *s = active_stream;
    if (!s || (s->state != ADC_STREAM_RUNNING
               && s->state != ADC_STREAM_ARMED))
        return;
    s->fault_status |= status | ACQ_STATUS_DISCONTINUITY;
    if (status & ACQ_STATUS_DMA_ERROR)
        s->dma_errors++;
    if (status & (ACQ_STATUS_PERIPHERAL_ERROR | ACQ_STATUS_SAMPLE_ERROR))
        s->adc_errors++;
    if (status & ACQ_STATUS_OVERRUN)
        s->overruns++;
    s->dropped_blocks++;
    s->state = ADC_STREAM_FAULTED;
    board_adc_stream_stop_from_isr();
    sched_wake_task(&adc_stream_wake);
    if (!s->traffic_class)
        try_shutdown("Critical ADC acquisition fault");
}

static void
adc_stream_send_summary(struct adc_stream *s,
                        struct adc_stream_subscription *sub,
                        const struct adc_filter_summary *summary,
                        uint32_t block_status)
{
    uint64_t first_offset = summary->first_scan * s->info.period_numerator;
    uint64_t last_offset = summary->last_scan * s->info.period_numerator;
    uint32_t first_clock = s->first_clock
        + first_offset / s->info.period_denominator;
    uint32_t last_clock = s->first_clock
        + last_offset / s->info.period_denominator;
    uint32_t status = block_status;
    if (summary->flags & ADC_FILTER_FLAG_DISCONTINUITY)
        status |= ACQ_STATUS_DISCONTINUITY;
    uint32_t sum_lo = summary->sum;
    uint32_t sum_hi = summary->sum >> 32;
    uint32_t sequence = sub->sequence++;
    if (sub->report_class == 0) {
        uint32_t deadline = 0;
        uint8_t event = adc_safety_begin_report(
            &sub->safety, sequence, last_clock, &deadline);
        if (event) {
            adc_stream_apply_safety(s, sub, event);
            return;
        }
        sub->deadline_timer.waketime = deadline;
        sub->deadline_armed = 1;
        sched_add_timer(&sub->deadline_timer);
        sendf("adc_stream_scheduled oid=%c sub=%c sequence=%u epoch=%u"
              " first_clock=%u last_clock=%u uncertainty=%u status=%u"
              " count=%hu min=%u max=%u sum_lo=%u sum_hi=%u shift=%c"
              " deadline=%u",
              s->oid, sub->id, sequence, s->epoch,
              first_clock, last_clock, s->info.uncertainty_ticks, status,
              summary->count, summary->minimum, summary->maximum,
              sum_lo, sum_hi, sub->filter.config.shift, deadline);
    } else if (sub->report_class == 1)
        sendf("adc_stream_prompt oid=%c sub=%c sequence=%u epoch=%u"
              " first_clock=%u last_clock=%u uncertainty=%u status=%u"
              " count=%hu min=%u max=%u sum_lo=%u sum_hi=%u shift=%c",
              s->oid, sub->id, sequence, s->epoch,
              first_clock, last_clock, s->info.uncertainty_ticks, status,
              summary->count, summary->minimum, summary->maximum,
              sum_lo, sum_hi, sub->filter.config.shift);
    else
        sendf("adc_stream_telemetry oid=%c sub=%c sequence=%u epoch=%u"
              " first_clock=%u last_clock=%u uncertainty=%u status=%u"
              " count=%hu min=%u max=%u sum_lo=%u sum_hi=%u shift=%c",
              s->oid, sub->id, sequence, s->epoch,
              first_clock, last_clock, s->info.uncertainty_ticks, status,
              summary->count, summary->minimum, summary->maximum,
              sum_lo, sum_hi, sub->filter.config.shift);
}

static void
adc_stream_send_raw(struct adc_stream *s, uint8_t prompt,
                    uint32_t sequence, uint32_t epoch, uint32_t status,
                    const uint8_t *data, uint8_t size)
{
    uint64_t scan_offset = (uint64_t)sequence
        * (s->block_values / s->channel_count);
    uint32_t first_clock = s->first_clock
        + scan_offset * s->info.period_numerator
          / s->info.period_denominator;
    // Keep the variable payload bounded well below a 64-byte protocol frame.
    // Static class ID plus sequence and byte offset make every chunk
    // independently retryable and unambiguously reassemblable.
    for (uint8_t offset = 0; offset < size; offset += 16) {
        uint8_t chunk = size - offset;
        if (chunk > 16)
            chunk = 16;
        if (prompt)
            sendf("adc_stream_data_prompt oid=%c sequence=%u epoch=%u"
                  " first_clock=%u period_num=%u period_den=%u uncertainty=%u"
                  " channels=%c status=%u offset=%c total=%c values=%*s",
                  s->oid, sequence, epoch, first_clock,
                  s->info.period_numerator, s->info.period_denominator,
                  s->info.uncertainty_ticks, s->channel_count, status,
                  offset, size, chunk, &data[offset]);
        else
            sendf("adc_stream_data_telemetry oid=%c sequence=%u epoch=%u"
                  " first_clock=%u period_num=%u period_den=%u uncertainty=%u"
                  " channels=%c status=%u offset=%c total=%c values=%*s",
                  s->oid, sequence, epoch, first_clock,
                  s->info.period_numerator, s->info.period_denominator,
                  s->info.uncertainty_ticks, s->channel_count, status,
                  offset, size, chunk, &data[offset]);
    }
}

static void
adc_stream_send_block(struct adc_stream *s, uint8_t block_index)
{
    struct acq_block *b = &s->blocks[block_index];
    uint16_t generation;
    irqstatus_t flag = irq_save();
    int ret = acq_block_consume(b, &generation);
    irq_restore(flag);
    if (ret)
        return;

    acq_capture_push(&s->capture, b->sequence, b->epoch, b->status,
                     b->data, b->item_count * sizeof(uint16_t));

    if (b->status & ACQ_STATUS_DISCONTINUITY)
        for (uint8_t i = 0; i < s->subscription_count; i++)
            adc_filter_reset(&s->subscriptions[i].filter, 1);
    uint8_t scans = b->item_count / s->channel_count;
    uint16_t *samples = b->data;
    for (uint8_t scan = 0; scan < scans; scan++) {
        uint64_t scan_index = s->raw_scan_count + scan;
        for (uint8_t i = 0; i < s->subscription_count; i++) {
            struct adc_stream_subscription *sub = &s->subscriptions[i];
            struct adc_filter_summary summary;
            uint32_t filtered_value;
            uint8_t filtered_ready;
            uint16_t sample = samples[scan * s->channel_count + sub->channel];
            int summary_ready = adc_filter_push_ex(
                &sub->filter, sample, scan_index, &summary,
                &filtered_value, &filtered_ready);
            if (filtered_ready) {
                if (sub->local_callback)
                    sub->local_callback(sub->local_context, filtered_value,
                                        b->first_machine_clock
                                        + scan * b->period_numerator
                                          / b->period_denominator);
                if (sub->local_trigger)
                    trigger_analog_update(sub->local_trigger,
                                          filtered_value);
                uint8_t event = adc_safety_check_value(
                    &sub->safety, filtered_value);
                if (event)
                    adc_stream_apply_safety(s, sub, event);
            }
            if (summary_ready)
                adc_stream_send_summary(s, sub, &summary, b->status);
        }
    }
    s->raw_scan_count += scans;

    if (s->raw_output)
        adc_stream_send_raw(s, 0, b->sequence, b->epoch, b->status,
                            (const uint8_t *)b->data,
                            b->item_count * sizeof(uint16_t));

    flag = irq_save();
    if (!acq_block_release(b, generation)
        && !acq_block_dma_take(b))
        board_adc_stream_block_released(block_index);
    irq_restore(flag);
}

void
adc_stream_task(void)
{
    if (!sched_check_wake(&adc_stream_wake))
        return;
    struct adc_stream *s = active_stream;
    if (!s)
        return;
    uint8_t block_index;
    for (;;) {
        irqstatus_t flag = irq_save();
        int ret = acq_ring_pop(&s->ready_ring, &block_index);
        irq_restore(flag);
        if (ret)
            break;
#if CONFIG_ADC_PROFILE
        uint32_t profile_start = timer_read_time();
#endif
        adc_stream_send_block(s, block_index);
#if CONFIG_ADC_PROFILE
        uint32_t elapsed = timer_read_time() - profile_start;
        s->consumer_count++;
        s->consumer_ticks += elapsed;
        if (elapsed > s->consumer_ticks_max)
            s->consumer_ticks_max = elapsed;
#endif
    }
    if (s->state == ADC_STREAM_FAULTED) {
        acq_capture_trigger(&s->capture, 1);
        sendf("adc_stream_fault oid=%c status=%u dropped=%u sequence=%u",
              s->oid, s->fault_status, s->dropped_blocks, s->sequence);
    }
    if (s->safety_pending) {
        s->safety_pending = 0;
        sendf("adc_stream_safety oid=%c sub=%c event=%c action=%c"
              " clock=%u status=%u count=%u",
              s->oid, s->safety_sub, s->safety_event, s->safety_action,
              s->safety_clock, s->fault_status, s->watchdog_events);
    }
    struct acq_capture_record record;
    while (!acq_capture_pop(&s->capture, &record))
        adc_stream_send_raw(s, 1, record.sequence, record.epoch,
                            record.status, record.data, record.size);
}
DECL_TASK(adc_stream_task);

void
command_adc_stream_ack(uint32_t *args)
{
    struct adc_stream *s = oid_lookup(args[0], command_config_adc_stream);
    struct adc_stream_subscription *sub = adc_stream_find_subscription(
        s, args[1]);
    if (!sub || sub->report_class)
        return;
    irqstatus_t flag = irq_save();
    if (adc_safety_ack(&sub->safety, args[2])) {
        irq_restore(flag);
        return;
    }
    if (sub->deadline_armed) {
        sched_del_timer(&sub->deadline_timer);
        sub->deadline_armed = 0;
    }
    irq_restore(flag);
}
DECL_COMMAND(command_adc_stream_ack,
             "adc_stream_ack oid=%c sub=%c sequence=%u");

void
command_adc_stream_get_status(uint32_t *args)
{
    struct adc_stream *s = oid_lookup(args[0], command_config_adc_stream);
    sendf("adc_stream_status oid=%c state=%c class=%c channels=%c"
          " block_values=%c epoch=%u sequence=%u dropped=%u status=%u"
          " ready_highwater=%c dma_errors=%u adc_errors=%u overruns=%u"
          " telemetry_drops=%u watchdog_events=%u publications=%u"
          " publication_ticks=%u publication_ticks_max=%u consumers=%u"
          " consumer_ticks=%u consumer_ticks_max=%u",
          s->oid, s->state, s->traffic_class, s->channel_count,
          s->block_values, s->epoch,
          s->sequence, s->dropped_blocks, s->fault_status,
          s->ready_ring.highwater, s->dma_errors, s->adc_errors,
          s->overruns, s->telemetry_drops, s->watchdog_events,
          s->publication_count, s->publication_ticks,
          s->publication_ticks_max, s->consumer_count, s->consumer_ticks,
          s->consumer_ticks_max);
}
DECL_COMMAND_FLAGS(command_adc_stream_get_status, HF_IN_SHUTDOWN,
                   "adc_stream_get_status oid=%c");

void
command_adc_stream_get_capabilities(uint32_t *args)
{
    struct adc_stream *s = oid_lookup(args[0], command_config_adc_stream);
    struct dma_pool_status pool;
    dma_pool_get_status(&pool);
    sendf("adc_stream_capabilities oid=%c version=%c max_channels=%c"
          " max_subscriptions=%c max_osr=%hu caps=%u dma_pool=%hu"
          " dma_used=%hu dma_claims=%c backend_caps=%u max_rate=%u"
          " max_hw_osr=%hu resolution=%c adc_count=%c watchdogs=%c"
          " timing_quality=%c",
          s->oid, 1, ADC_STREAM_MAX_CHANNELS, ADC_STREAM_MAX_SUBSCRIPTIONS,
          ADC_FILTER_MAX_OSR,
          ADC_STREAM_CAP_RAW_BLOCKS | ADC_STREAM_CAP_SW_BOXCAR
          | ADC_STREAM_CAP_INPUT_DECIMATION | ADC_STREAM_CAP_SUMMARIES
          | ADC_STREAM_CAP_PROMPT_REPORT | ADC_STREAM_CAP_SCHEDULED_REPORT
          | ADC_STREAM_CAP_LOCAL_SAFETY | ADC_STREAM_CAP_FAULT_CAPTURE
          | ADC_STREAM_TARGET_CAPS,
          pool.size, pool.used, pool.claims, s->info.capabilities,
          s->info.max_conversion_rate, s->info.max_hardware_oversample,
          s->info.resolution_bits, s->info.adc_count,
          s->info.watchdog_count, s->info.timing_quality);
}
DECL_COMMAND_FLAGS(command_adc_stream_get_capabilities, HF_IN_SHUTDOWN,
                   "adc_stream_get_capabilities oid=%c");

void
adc_stream_shutdown(void)
{
    struct adc_stream *s = active_stream;
    if (s)
        adc_stream_stop(s);
}
DECL_SHUTDOWN(adc_stream_shutdown);

DECL_CONSTANT("ADC_STREAM_MAX_CHANNELS", ADC_STREAM_MAX_CHANNELS);
DECL_CONSTANT("ADC_STREAM_MAX_BLOCK_VALUES", ADC_STREAM_MAX_BLOCK_VALUES);
DECL_CONSTANT("ADC_STREAM_V1", 1);
DECL_CONSTANT("ADC_STREAM_MAX_SUBSCRIPTIONS", ADC_STREAM_MAX_SUBSCRIPTIONS);
DECL_CONSTANT("ADC_STREAM_MAX_OSR", ADC_FILTER_MAX_OSR);
DECL_CONSTANT("ADC_STREAM_CAPS",
              ADC_STREAM_CAP_RAW_BLOCKS | ADC_STREAM_CAP_SW_BOXCAR
              | ADC_STREAM_CAP_INPUT_DECIMATION | ADC_STREAM_CAP_SUMMARIES
              | ADC_STREAM_CAP_PROMPT_REPORT | ADC_STREAM_CAP_SCHEDULED_REPORT
              | ADC_STREAM_CAP_LOCAL_SAFETY
              | ADC_STREAM_CAP_FAULT_CAPTURE | ADC_STREAM_TARGET_CAPS);
