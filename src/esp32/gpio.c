// GPIO functions on ESP32
//
// Pad configuration goes through the IDF gpio driver (task context
// only); the speed-critical set/clear/read paths used from timer
// dispatch are direct register writes through the cached
// GPIO.out_w1ts/out_w1tc (out1_* bank for pins 32..39) addresses -
// single-instruction and safe from ISRs.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "driver/gpio.h" // gpio_config
#include "soc/gpio_struct.h" // GPIO
#include "board/gpio.h" // gpio_out_setup
#include "board/irq.h" // irq_save
#include "command.h" // shutdown
#include "internal.h" // ESP32_GPIO_COUNT
#include "sched.h" // sched_shutdown

DECL_ENUMERATION_RANGE("pin", "GPIO0", 0, ESP32_GPIO_COUNT);

// GPIO34..39 are input only on the ESP32
#define GPIO_FIRST_INPUT_ONLY 34


/****************************************************************
 * Output pins
 ****************************************************************/

struct gpio_out
gpio_out_setup(uint32_t pin, uint8_t val)
{
    if (pin >= GPIO_FIRST_INPUT_ONLY)
        goto fail;
    struct gpio_out g;
    g.pin = pin;
    if (pin < 32) {
        g.bit = 1u << pin;
        g.w1ts = (volatile uint32_t *)&GPIO.out_w1ts;
        g.w1tc = (volatile uint32_t *)&GPIO.out_w1tc;
        g.out = (volatile uint32_t *)&GPIO.out;
    } else {
        g.bit = 1u << (pin - 32);
        g.w1ts = (volatile uint32_t *)&GPIO.out1_w1ts;
        g.w1tc = (volatile uint32_t *)&GPIO.out1_w1tc;
        g.out = (volatile uint32_t *)&GPIO.out1;
    }
    gpio_out_reset(g, val);
    return g;
fail:
    shutdown("Not an output pin");
}

void
gpio_out_reset(struct gpio_out g, uint8_t val)
{
    gpio_config_t config = {
        .pin_bit_mask = 1ULL << g.pin,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    irqstatus_t flag = irq_save();
    gpio_out_write(g, val);
    int ret = gpio_config(&config);
    irq_restore(flag);
    if (ret)
        shutdown("gpio_config failed");
}

void
gpio_out_write(struct gpio_out g, uint8_t val)
{
    if (val)
        *g.w1ts = g.bit;
    else
        *g.w1tc = g.bit;
}

void
gpio_out_toggle_noirq(struct gpio_out g)
{
    if (*g.out & g.bit)
        *g.w1tc = g.bit;
    else
        *g.w1ts = g.bit;
}

void
gpio_out_toggle(struct gpio_out g)
{
    irqstatus_t flag = irq_save();
    gpio_out_toggle_noirq(g);
    irq_restore(flag);
}


/****************************************************************
 * Input pins
 ****************************************************************/

struct gpio_in
gpio_in_setup(uint32_t pin, int8_t pull_up)
{
    if (pin >= ESP32_GPIO_COUNT)
        goto fail;
    struct gpio_in g;
    g.pin = pin;
    if (pin < 32) {
        g.bit = 1u << pin;
        g.in = (volatile uint32_t *)&GPIO.in;
    } else {
        g.bit = 1u << (pin - 32);
        g.in = (volatile uint32_t *)&GPIO.in1;
    }
    gpio_in_reset(g, pull_up);
    return g;
fail:
    shutdown("Not a valid input pin");
}

void
gpio_in_reset(struct gpio_in g, int8_t pull_up)
{
    // Note: pins 34..39 have no internal pull resistors
    gpio_config_t config = {
        .pin_bit_mask = 1ULL << g.pin,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = pull_up > 0 ? GPIO_PULLUP_ENABLE : GPIO_PULLUP_DISABLE,
        .pull_down_en = (pull_up < 0
                         ? GPIO_PULLDOWN_ENABLE : GPIO_PULLDOWN_DISABLE),
        .intr_type = GPIO_INTR_DISABLE,
    };
    int ret = gpio_config(&config);
    if (ret)
        shutdown("gpio_config failed");
}

uint8_t
gpio_in_read(struct gpio_in g)
{
    return !!(*g.in & g.bit);
}
