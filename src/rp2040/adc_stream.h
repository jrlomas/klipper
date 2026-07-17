#ifndef __RP2040_ADC_STREAM_H
#define __RP2040_ADC_STREAM_H

#include <stdint.h>
#include "../adc_stream.h"

static inline struct gpio_adc
board_adc_stream_setup_pin(uint32_t pin)
{
    return gpio_adc_setup(pin);
}

void board_adc_stream_setup(const struct adc_stream_backend_config *cfg,
                            struct adc_stream_backend_info *info);
void board_adc_stream_start(void);
void board_adc_stream_stop(void);
void board_adc_stream_stop_from_isr(void);
void board_adc_stream_block_released(uint8_t block_index);

#endif // rp2040/adc_stream.h
