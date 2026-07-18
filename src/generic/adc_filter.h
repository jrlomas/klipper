#ifndef __GENERIC_ADC_FILTER_H
#define __GENERIC_ADC_FILTER_H

#include <stdint.h>

#define ADC_FILTER_MAX_OSR 256
#define ADC_FILTER_MAX_REPORT_DIV 4096

enum adc_filter_flags {
    ADC_FILTER_FLAG_DISCONTINUITY = 1u << 0,
};

struct adc_filter_config {
    // Take one sample every input_div physical scans, accumulate osr accepted
    // samples, round and right-shift the accumulator, then summarize
    // report_div filtered results.
    uint16_t input_div;
    uint16_t osr;
    uint16_t report_div;
    uint8_t shift;
};

struct adc_filter_summary {
    uint64_t sum;
    uint64_t first_scan;
    uint64_t last_scan;
    uint32_t minimum;
    uint32_t maximum;
    uint16_t count;
    uint8_t flags;
};

struct adc_filter {
    struct adc_filter_config config;
    struct adc_filter_summary summary;
    uint64_t accumulator;
    uint64_t raw_index;
    uint16_t osr_count;
    uint8_t pending_flags;
};

int adc_filter_configure(struct adc_filter *filter,
                         const struct adc_filter_config *config);
void adc_filter_reset(struct adc_filter *filter, uint8_t discontinuity);
// Return one when a complete summary was copied to result, zero otherwise.
int adc_filter_push(struct adc_filter *filter, uint16_t sample,
                    uint64_t scan_index, struct adc_filter_summary *result);
// Extended form also reports each completed filtered value before report
// decimation, allowing bounded local consumers and threshold checks.
int adc_filter_push_ex(struct adc_filter *filter, uint16_t sample,
                       uint64_t scan_index, struct adc_filter_summary *result,
                       uint32_t *filtered_value, uint8_t *filtered_ready);

#endif // generic/adc_filter.h
