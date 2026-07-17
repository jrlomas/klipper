#ifndef __ESP32_ADC_STREAM_H
#define __ESP32_ADC_STREAM_H

#include <stdint.h>
#include "../adc_stream.h"

struct gpio_adc board_adc_stream_setup_pin(uint32_t pin);
void board_adc_stream_setup(const struct adc_stream_backend_config *cfg,
                            struct adc_stream_backend_info *info);
void board_adc_stream_start(void);
void board_adc_stream_stop(void);
void board_adc_stream_stop_from_isr(void);
void board_adc_stream_block_released(uint8_t block_index);

// Cross-core service hooks. The IDF continuous driver runs on core 0 while
// generic stream ownership remains in the klipper scheduler domain on core 1.
void esp32_adc_stream_init(void);
void esp32_adc_stream_poll(void);
uint8_t esp32_adc_stream_is_claimed(void);

#endif // esp32/adc_stream.h
