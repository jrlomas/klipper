#ifndef __ADC_STREAM_H
#define __ADC_STREAM_H

#include <stdint.h>
#include "board/gpio.h" // struct gpio_adc

#define ADC_STREAM_MAX_CHANNELS 4
#define ADC_STREAM_MAX_BLOCK_VALUES 64
#define ADC_STREAM_BLOCK_COUNT 2
#define ADC_STREAM_MAX_SUBSCRIPTIONS 8

enum adc_stream_capability {
    ADC_STREAM_CAP_RAW_BLOCKS = 1u << 0,
    ADC_STREAM_CAP_SW_BOXCAR = 1u << 1,
    ADC_STREAM_CAP_INPUT_DECIMATION = 1u << 2,
    ADC_STREAM_CAP_SUMMARIES = 1u << 3,
    ADC_STREAM_CAP_PROMPT_REPORT = 1u << 4,
    ADC_STREAM_CAP_SCHEDULED_REPORT = 1u << 5,
    ADC_STREAM_CAP_LOCAL_SAFETY = 1u << 6,
    ADC_STREAM_CAP_FAULT_CAPTURE = 1u << 7,
    ADC_STREAM_CAP_HW_OVERSAMPLE = 1u << 8,
};

enum adc_stream_backend_capability {
    ADC_BACKEND_CAP_HARDWARE_PACED = 1u << 0,
    ADC_BACKEND_CAP_INFERRED_START = 1u << 1,
    ADC_BACKEND_CAP_NATIVE_DBM = 1u << 2,
    ADC_BACKEND_CAP_HW_OVERSAMPLE = 1u << 3,
    ADC_BACKEND_CAP_SAMPLE_TAGS = 1u << 4,
    ADC_BACKEND_CAP_CALIBRATION = 1u << 5,
    ADC_BACKEND_CAP_WATCHDOG_WITH_DMA = 1u << 6,
};

struct adc_stream_backend_config {
    struct gpio_adc *pins;
    uint16_t *buffer;
    // Period between complete channel scans. Values in each block are
    // interleaved in channel order and block_values is an exact multiple of
    // channel_count.
    uint32_t requested_period_ticks;
    uint16_t hardware_oversample;
    uint8_t channel_count;
    uint8_t block_values;
    uint8_t hardware_shift;
    uint8_t owner;
};

struct adc_stream_backend_info {
    // Actual period between complete channel scans as a rational number of
    // machine-clock ticks.
    uint32_t period_numerator;
    uint32_t period_denominator;
    uint32_t uncertainty_ticks;
    uint32_t status;
    uint32_t max_conversion_rate;
    uint32_t capabilities;
    uint16_t max_hardware_oversample;
    uint8_t resolution_bits;
    uint8_t adc_count;
    uint8_t watchdog_count;
    // 0=inferred period/phase, 1=hardware-paced period with inferred start,
    // 2=hardware-captured first aperture.
    uint8_t timing_quality;
};

typedef void (*adc_stream_local_callback)(void *context, uint32_t value,
                                          uint32_t clock);

// Bind one firmware-local consumer to a filtered subscription.  The callback
// runs from adc_stream_task(), never from the DMA ISR.  It receives the exact
// boxcar result before any telemetry-only EWMA is applied.
int adc_stream_bind_local(uint8_t stream_oid, uint8_t subscription,
                          adc_stream_local_callback callback, void *context);

// Called by a board DMA ISR after it has made a completed half-buffer visible
// to the CPU (including any required cache maintenance).
int adc_stream_block_complete(uint8_t block_index, uint32_t status);

// Called by an asynchronous backend when it cannot associate a failure with a
// completed block (for example, an ESP-IDF DMA pool overflow reported on the
// other CPU). The call must run in the klipper scheduler/IRQ domain.
void adc_stream_backend_fault(uint32_t status);

#endif // adc_stream.h
