#ifndef __GENERIC_ADC_SAFETY_H
#define __GENERIC_ADC_SAFETY_H

#include <stdint.h>

enum adc_safety_action {
    ADC_SAFETY_NONE = 0,
    ADC_SAFETY_HOLD = 1,
    ADC_SAFETY_TRIGGER = 2,
    ADC_SAFETY_SHUTDOWN = 3,
};

enum adc_safety_event {
    ADC_SAFETY_EVENT_NONE = 0,
    ADC_SAFETY_EVENT_THRESHOLD = 1,
    ADC_SAFETY_EVENT_UNACKED = 2,
    ADC_SAFETY_EVENT_REPLACED = 3,
};

struct adc_safety_config {
    uint32_t deadline_ticks;
    uint32_t low;
    uint32_t high;
    uint8_t fault_count;
    uint8_t fail_action;
};

struct adc_safety {
    struct adc_safety_config config;
    uint32_t pending_sequence;
    uint32_t pending_deadline;
    uint8_t outside_count;
    uint8_t pending;
};

int adc_safety_configure(struct adc_safety *s,
                         const struct adc_safety_config *config);
uint8_t adc_safety_check_value(struct adc_safety *s, uint32_t value);
uint8_t adc_safety_begin_report(struct adc_safety *s, uint32_t sequence,
                                uint32_t sample_clock, uint32_t *deadline);
int adc_safety_ack(struct adc_safety *s, uint32_t sequence);
uint8_t adc_safety_check_deadline(struct adc_safety *s, uint32_t now);

#endif // generic/adc_safety.h
