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
        || config->shift > 31)
        return -1;
    memset(f, 0, sizeof(*f));
    f->config = *config;
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
adc_filter_push(struct adc_filter *f, uint16_t sample, uint64_t scan_index,
                struct adc_filter_summary *result)
{
    // input_div is phase-locked to the epoch, not to block boundaries.
    uint64_t raw_index = f->raw_index++;
    if (raw_index % f->config.input_div)
        return 0;
    f->accumulator += sample;
    if (++f->osr_count < f->config.osr)
        return 0;

    uint64_t value = f->accumulator;
    if (f->config.shift)
        value = (value + ((uint64_t)1 << (f->config.shift - 1)))
                >> f->config.shift;
    f->accumulator = 0;
    f->osr_count = 0;

    struct adc_filter_summary *s = &f->summary;
    uint32_t output = value > UINT32_MAX ? UINT32_MAX : value;
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
