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
#include "generic/adc_filter.h"
#include "generic/dma_resource.h"
#include "sched.h" // DECL_TASK

enum {
    ADC_STREAM_STOPPED = 0,
    ADC_STREAM_ARMED,
    ADC_STREAM_RUNNING,
    ADC_STREAM_FAULTED,
};

struct adc_stream_subscription {
    struct adc_filter filter;
    uint32_t sequence;
    uint8_t id;
    uint8_t channel;
    uint8_t report_class;
};

struct adc_stream {
    struct timer start_timer;
    struct gpio_adc pins[ADC_STREAM_MAX_CHANNELS];
    struct acq_block blocks[ADC_STREAM_BLOCK_COUNT];
    struct adc_stream_backend_info info;
    struct adc_stream_subscription subscriptions[ADC_STREAM_MAX_SUBSCRIPTIONS];
    uint16_t *buffer;
    uint32_t first_clock;
    uint32_t sequence;
    uint32_t epoch;
    uint32_t dropped_blocks;
    uint32_t fault_status;
    uint64_t raw_scan_count;
    uint8_t channel_count;
    uint8_t subscription_count;
    uint8_t block_values;
    uint8_t traffic_class;
    uint8_t oid;
    uint8_t raw_output;
    volatile uint8_t ready_mask;
    volatile uint8_t state;
};

// The first implementation deliberately permits one physical ADC engine.
static struct adc_stream *active_stream;
static struct task_wake adc_stream_wake;

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
        || channel >= s->channel_count || report_class < 1
        || report_class > 2)
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
    s->subscription_count++;
}
DECL_COMMAND(command_adc_stream_subscribe,
             "adc_stream_subscribe oid=%c sub=%c channel=%c"
             " input_div=%hu osr=%hu shift=%c report_div=%hu"
             " report_class=%c");

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
        adc_filter_reset(&s->subscriptions[i].filter, 0);
        s->subscriptions[i].sequence = 0;
    }
    if (active_stream && active_stream != s)
        shutdown("ADC engine already claimed");
    if (s->state != ADC_STREAM_STOPPED)
        adc_stream_stop(s);

    s->block_values = block_values;
    s->traffic_class = traffic_class;
    s->ready_mask = 0;
    s->sequence = 0;
    s->dropped_blocks = 0;
    s->fault_status = 0;
    s->raw_scan_count = 0;
    s->epoch++;
    for (uint8_t i = 0; i < ADC_STREAM_BLOCK_COUNT; i++) {
        acq_block_init(&s->blocks[i],
                       &s->buffer[i * ADC_STREAM_MAX_BLOCK_VALUES]);
        acq_block_dma_take(&s->blocks[i]);
    }
    struct adc_stream_backend_config cfg = {
        .pins = s->pins,
        .buffer = s->buffer,
        .requested_period_ticks = period_ticks,
        .channel_count = s->channel_count,
        .block_values = block_values,
        .owner = s->oid,
    };
    board_adc_stream_setup(&cfg, &s->info);
    if (!s->info.period_denominator || !s->info.period_numerator)
        shutdown("ADC stream backend rejected schedule");

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
    struct acq_block *b = &s->blocks[block_index];
    if (b->state != ACQ_BLOCK_DMA_OWNED) {
        // The acquisition ring is exhausted. Stop immediately instead of
        // silently overwriting an unconsumed block.
        s->dropped_blocks++;
        s->fault_status |= ACQ_STATUS_OVERRUN | ACQ_STATUS_DISCONTINUITY;
        s->state = ADC_STREAM_FAULTED;
        board_adc_stream_stop_from_isr();
        sched_wake_task(&adc_stream_wake);
        if (!s->traffic_class)
            try_shutdown("Critical ADC stream overrun");
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
        s->ready_mask |= 1u << block_index;
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
    if (sub->report_class == 1)
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
adc_stream_send_block(struct adc_stream *s, uint8_t block_index)
{
    struct acq_block *b = &s->blocks[block_index];
    uint16_t generation;
    irqstatus_t flag = irq_save();
    s->ready_mask &= ~(1u << block_index);
    int ret = acq_block_consume(b, &generation);
    irq_restore(flag);
    if (ret)
        return;

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
            uint16_t sample = samples[scan * s->channel_count + sub->channel];
            if (adc_filter_push(&sub->filter, sample, scan_index, &summary))
                adc_stream_send_summary(s, sub, &summary, b->status);
        }
    }
    s->raw_scan_count += scans;

    if (s->raw_output)
        sendf("adc_stream_data_telemetry oid=%c sequence=%u epoch=%u class=%c"
              " first_clock=%u period_num=%u period_den=%u uncertainty=%u"
              " channels=%c status=%u values=%*s",
              s->oid, b->sequence, b->epoch, s->traffic_class,
              b->first_machine_clock,
              b->period_numerator, b->period_denominator,
              b->uncertainty_ticks, s->channel_count, b->status,
              b->item_count * sizeof(uint16_t), b->data);

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
    // Preserve acquisition order if both halves became ready before the task
    // ran. With two blocks, sequence parity identifies the older half.
    uint8_t mask = s->ready_mask;
    if (mask == 3) {
        uint8_t first = s->blocks[0].sequence < s->blocks[1].sequence ? 0 : 1;
        adc_stream_send_block(s, first);
        adc_stream_send_block(s, first ^ 1);
    } else if (mask) {
        adc_stream_send_block(s, mask & 1 ? 0 : 1);
    }
    if (s->state == ADC_STREAM_FAULTED)
        sendf("adc_stream_fault oid=%c status=%u dropped=%u sequence=%u",
              s->oid, s->fault_status, s->dropped_blocks, s->sequence);
}
DECL_TASK(adc_stream_task);

void
command_adc_stream_get_status(uint32_t *args)
{
    struct adc_stream *s = oid_lookup(args[0], command_config_adc_stream);
    sendf("adc_stream_status oid=%c state=%c class=%c channels=%c"
          " block_values=%c epoch=%u sequence=%u dropped=%u status=%u",
          s->oid, s->state, s->traffic_class, s->channel_count,
          s->block_values, s->epoch,
          s->sequence, s->dropped_blocks, s->fault_status);
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
          " dma_used=%hu dma_claims=%c",
          s->oid, 1, ADC_STREAM_MAX_CHANNELS, ADC_STREAM_MAX_SUBSCRIPTIONS,
          ADC_FILTER_MAX_OSR,
          ADC_STREAM_CAP_RAW_BLOCKS | ADC_STREAM_CAP_SW_BOXCAR
          | ADC_STREAM_CAP_INPUT_DECIMATION | ADC_STREAM_CAP_SUMMARIES
          | ADC_STREAM_CAP_PROMPT_REPORT,
          pool.size, pool.used, pool.claims);
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
              | ADC_STREAM_CAP_PROMPT_REPORT);
