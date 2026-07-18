// Deterministic local threshold and Class-0 acknowledgement policy.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <string.h> // memset
#include "generic/adc_safety.h"

int
adc_safety_configure(struct adc_safety *s,
                     const struct adc_safety_config *config)
{
    if (config->fail_action > ADC_SAFETY_SHUTDOWN
        || (config->fault_count && config->low > config->high)
        || (config->deadline_ticks && !config->fail_action)
        || (config->fault_count && !config->fail_action)
        || (config->fail_action && !config->deadline_ticks
            && !config->fault_count))
        return -1;
    memset(s, 0, sizeof(*s));
    s->config = *config;
    return 0;
}

uint8_t
adc_safety_check_value(struct adc_safety *s, uint32_t value)
{
    if (!s->config.fault_count)
        return ADC_SAFETY_EVENT_NONE;
    if (value >= s->config.low && value <= s->config.high) {
        s->outside_count = 0;
        return ADC_SAFETY_EVENT_NONE;
    }
    if (s->outside_count < UINT8_MAX)
        s->outside_count++;
    if (s->outside_count < s->config.fault_count)
        return ADC_SAFETY_EVENT_NONE;
    s->outside_count = 0;
    return ADC_SAFETY_EVENT_THRESHOLD;
}

uint8_t
adc_safety_begin_report(struct adc_safety *s, uint32_t sequence,
                        uint32_t sample_clock, uint32_t *deadline)
{
    if (!s->config.deadline_ticks)
        return ADC_SAFETY_EVENT_NONE;
    if (s->pending)
        return ADC_SAFETY_EVENT_REPLACED;
    s->pending_sequence = sequence;
    s->pending_deadline = sample_clock + s->config.deadline_ticks;
    s->pending = 1;
    *deadline = s->pending_deadline;
    return ADC_SAFETY_EVENT_NONE;
}

int
adc_safety_ack(struct adc_safety *s, uint32_t sequence)
{
    if (!s->pending || s->pending_sequence != sequence)
        return -1;
    s->pending = 0;
    return 0;
}

uint8_t
adc_safety_check_deadline(struct adc_safety *s, uint32_t now)
{
    if (!s->pending || (int32_t)(now - s->pending_deadline) < 0)
        return ADC_SAFETY_EVENT_NONE;
    s->pending = 0;
    return ADC_SAFETY_EVENT_UNACKED;
}
