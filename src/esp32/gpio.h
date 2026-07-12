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

struct gpio_pwm {
    uint8_t chan;   // LEDC high-speed channel
    uint8_t shift;  // 15 - duty_resolution (see hard_pwm.c)
};
struct gpio_pwm gpio_pwm_setup(uint32_t pin, uint32_t cycle_time
                               , uint16_t val);
void gpio_pwm_write(struct gpio_pwm g, uint16_t val);

struct spi_config {
    void *spi;       // spi_dev_t* (SPI2 or SPI3)
    uint32_t clock;  // precomputed SPI_CLOCK_REG value
    uint8_t mode;
};
struct spi_config spi_setup(uint32_t bus, uint8_t mode, uint32_t rate);
void spi_prepare(struct spi_config config);
void spi_transfer(struct spi_config config, uint8_t receive_data
                  , uint8_t len, uint8_t *data);

struct i2c_config {
    uint8_t addr;    // 7-bit address, pre-shifted left by one
};
struct i2c_config i2c_setup(uint32_t bus, uint32_t rate, uint8_t addr);
int i2c_write(struct i2c_config config, uint8_t write_len, uint8_t *write);
int i2c_read(struct i2c_config config, uint8_t reg_len, uint8_t *reg
             , uint8_t read_len, uint8_t *read);

#endif // gpio.h
