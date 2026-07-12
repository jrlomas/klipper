#ifndef __ESP32_GPIO_H
#define __ESP32_GPIO_H
// GPIO and ADC interface for the ESP32 port

#include <stdint.h> // uint32_t

struct gpio_out {
    // Cached register addresses for ISR-fast single-instruction
    // set/clear (GPIO.out_w1ts / out_w1tc, or the out1_* bank for
    // pins 32+)
    volatile uint32_t *w1ts, *w1tc, *out;
    uint32_t bit;
    uint8_t pin;
};
struct gpio_out gpio_out_setup(uint32_t pin, uint8_t val);
void gpio_out_reset(struct gpio_out g, uint8_t val);
void gpio_out_toggle_noirq(struct gpio_out g);
void gpio_out_toggle(struct gpio_out g);
void gpio_out_write(struct gpio_out g, uint8_t val);

struct gpio_in {
    volatile uint32_t *in;
    uint32_t bit;
    uint8_t pin;
};
struct gpio_in gpio_in_setup(uint32_t pin, int8_t pull_up);
void gpio_in_reset(struct gpio_in g, int8_t pull_up);
uint8_t gpio_in_read(struct gpio_in g);

struct gpio_adc {
    uint8_t chan;
};
struct gpio_adc gpio_adc_setup(uint32_t pin);
uint32_t gpio_adc_sample(struct gpio_adc g);
uint16_t gpio_adc_read(struct gpio_adc g);
void gpio_adc_cancel_sample(struct gpio_adc g);

#endif // gpio.h
