#ifndef __GENERIC_ADC_FILTER_H
#define __GENERIC_ADC_FILTER_H

#include <stdint.h>

#define ADC_FILTER_MAX_OSR 256
#define ADC_FILTER_MAX_REPORT_DIV 4096
#define ADC_FILTER_ALPHA_ONE 32768

enum adc_filter_flags {
    ADC_FILTER_FLAG_DISCONTINUITY = 1u << 0,
};

enum adc_filter_summary_mode {
    ADC_FILTER_SUMMARY_AGGREGATE = 0,
    ADC_FILTER_SUMMARY_LATEST = 1,
};

struct adc_filter_config {
    // Take one sample every input_div physical scans, accumulate osr accepted
    // samples, normalize by window_divisor or right-shift the accumulator,
    // apply an alpha_q15 EWMA, then summarize report_div filtered results.
    uint16_t input_div;
    uint16_t osr;
    uint16_t report_div;
    uint16_t window_divisor;
    uint16_t alpha_q15;
    uint8_t shift;
    uint8_t summary_mode;
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
    int64_t ewma_q15;
    uint16_t osr_count;
    uint16_t report_count;
    uint8_t pending_flags;
    uint8_t ewma_valid;
};

int adc_filter_configure(struct adc_filter *filter,
                         const struct adc_filter_config *config);
int adc_filter_set_postprocess(struct adc_filter *filter,
                               uint16_t window_divisor,
                               uint16_t alpha_q15);
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
