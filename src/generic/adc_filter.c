// Deterministic integer ADC boxcar and report decimator.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "adc_filter.h"
#include <string.h> // memset

int
adc_filter_configure(struct adc_filter *f,
                     const struct adc_filter_config *config)
{
    if (!config->input_div || !config->osr || !config->report_div
        || config->osr > ADC_FILTER_MAX_OSR
        || config->report_div > ADC_FILTER_MAX_REPORT_DIV
        || config->window_divisor > config->osr
        || config->alpha_q15 > ADC_FILTER_ALPHA_ONE
        || config->shift > 31
        || config->summary_mode > ADC_FILTER_SUMMARY_LATEST)
        return -1;
    memset(f, 0, sizeof(*f));
    f->config = *config;
    if (!f->config.alpha_q15)
        f->config.alpha_q15 = ADC_FILTER_ALPHA_ONE;
    return 0;
}

int
adc_filter_set_postprocess(struct adc_filter *f, uint16_t window_divisor,
                           uint16_t alpha_q15)
{
    if (window_divisor > f->config.osr || !alpha_q15
        || alpha_q15 > ADC_FILTER_ALPHA_ONE)
        return -1;
    f->config.window_divisor = window_divisor;
    f->config.alpha_q15 = alpha_q15;
    f->ewma_q15 = 0;
    f->ewma_valid = 0;
    return 0;
}

void
adc_filter_reset(struct adc_filter *f, uint8_t discontinuity)
{
    struct adc_filter_config config = f->config;
    memset(f, 0, sizeof(*f));
    f->config = config;
    if (discontinuity)
        f->pending_flags = ADC_FILTER_FLAG_DISCONTINUITY;
}

int
adc_filter_push_ex(struct adc_filter *f, uint16_t sample, uint64_t scan_index,
                   struct adc_filter_summary *result,
                   uint32_t *filtered_value, uint8_t *filtered_ready)
{
    *filtered_ready = 0;
    // input_div is phase-locked to the epoch, not to block boundaries.
    uint64_t raw_index = f->raw_index++;
    if (raw_index % f->config.input_div)
        return 0;
    f->accumulator += sample;
    if (++f->osr_count < f->config.osr)
        return 0;

    uint64_t value = f->accumulator;
    if (f->config.window_divisor)
        value = (value + f->config.window_divisor / 2)
                / f->config.window_divisor;
    else if (f->config.shift)
        value = (value + ((uint64_t)1 << (f->config.shift - 1)))
                >> f->config.shift;
    f->accumulator = 0;
    f->osr_count = 0;

    struct adc_filter_summary *s = &f->summary;
    uint32_t window_value = value > UINT32_MAX ? UINT32_MAX : value;
    // Local safety and trigger consumers see the finite boxcar result before
    // the deliberately lagging EWMA, so smoothing cannot hide a new limit
    // violation. Reports use the EWMA value.
    *filtered_value = window_value;
    *filtered_ready = 1;
    int64_t target_q15 = (int64_t)window_value << 15;
    if (!f->ewma_valid) {
        f->ewma_q15 = target_q15;
        f->ewma_valid = 1;
    } else {
        int64_t delta = target_q15 - f->ewma_q15;
        int64_t scaled = delta * f->config.alpha_q15;
        int64_t adjustment = scaled >= 0
            ? (scaled + (ADC_FILTER_ALPHA_ONE / 2)) / ADC_FILTER_ALPHA_ONE
            : -((-scaled + (ADC_FILTER_ALPHA_ONE / 2))
                / ADC_FILTER_ALPHA_ONE);
        f->ewma_q15 += adjustment;
    }
    uint32_t output = (f->ewma_q15 + (ADC_FILTER_ALPHA_ONE / 2)) >> 15;
    if (f->config.summary_mode == ADC_FILTER_SUMMARY_LATEST) {
        if (++f->report_count < f->config.report_div)
            return 0;
        f->report_count = 0;
        *result = (struct adc_filter_summary) {
            .sum = output,
            .first_scan = scan_index,
            .last_scan = scan_index,
            .minimum = output,
            .maximum = output,
            .count = 1,
            .flags = f->pending_flags,
        };
        f->pending_flags = 0;
        return 1;
    }
    if (!s->count) {
        s->first_scan = scan_index;
        s->minimum = s->maximum = output;
        s->flags = f->pending_flags;
        f->pending_flags = 0;
    } else {
        if (output < s->minimum)
            s->minimum = output;
        if (output > s->maximum)
            s->maximum = output;
    }
    s->last_scan = scan_index;
    s->sum += output;
    if (++s->count < f->config.report_div)
        return 0;

    *result = *s;
    memset(s, 0, sizeof(*s));
    return 1;
}

int
adc_filter_push(struct adc_filter *f, uint16_t sample, uint64_t scan_index,
                struct adc_filter_summary *result)
{
    uint32_t filtered_value;
    uint8_t filtered_ready;
    return adc_filter_push_ex(f, sample, scan_index, result,
                              &filtered_value, &filtered_ready);
}
