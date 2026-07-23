#ifndef __ESP32_I2S_SHIFT_H
#define __ESP32_I2S_SHIFT_H

#include <stdint.h>

// Rodent V1.x exposes a pair of serially chained 74HC595-compatible output
// registers instead of direct STEP/DIR/ENABLE GPIOs.  Keep their pin numbers
// outside the native ESP32 GPIO range so the normal Klipper pin parser can
// expose them as I2SO0..I2SO15.
#define ESP32_I2S_OUT_BASE 40
#define ESP32_I2S_OUT_COUNT 16

void i2s_shift_init(void);
void i2s_shift_write(uint8_t bit, uint8_t value);
void i2s_shift_toggle(uint8_t bit);
uint8_t i2s_shift_read(uint8_t bit);

#endif // i2s_shift.h
