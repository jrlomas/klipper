#ifndef __ADC_STREAM_H
#define __ADC_STREAM_H

#include <stdint.h>
#include "board/gpio.h" // struct gpio_adc

#define ADC_STREAM_MAX_CHANNELS 4
#define ADC_STREAM_MAX_BLOCK_VALUES 16
#define ADC_STREAM_BLOCK_COUNT 2

struct adc_stream_backend_config {
    struct gpio_adc *pins;
    uint16_t *buffer;
    // Period between complete channel scans. Values in each block are
    // interleaved in channel order and block_values is an exact multiple of
    // channel_count.
    uint32_t requested_period_ticks;
    uint8_t channel_count;
    uint8_t block_values;
};

struct adc_stream_backend_info {
    // Actual period between complete channel scans as a rational number of
    // machine-clock ticks.
    uint32_t period_numerator;
    uint32_t period_denominator;
    uint32_t uncertainty_ticks;
    uint32_t status;
};

// Called by a board DMA ISR after it has made a completed half-buffer visible
// to the CPU (including any required cache maintenance).
int adc_stream_block_complete(uint8_t block_index, uint32_t status);

// Called by an asynchronous backend when it cannot associate a failure with a
// completed block (for example, an ESP-IDF DMA pool overflow reported on the
// other CPU). The call must run in the klipper scheduler/IRQ domain.
void adc_stream_backend_fault(uint32_t status);

#endif // adc_stream.h
