// Analog to digital conversion (ADC1) on ESP32 via the IDF oneshot
// driver
//
// klipper samples ADCs from timer (ISR) context, but
// adc_oneshot_read() takes locks and is not ISR-callable.  The
// conversion is therefore deferred to a low-priority FreeRTOS task
// on core 0: gpio_adc_sample() (timer context) starts a request and
// polls a small state machine, returning retry delays until the
// task has stored the result.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "freertos/FreeRTOS.h" // vTaskDelay
#include "freertos/task.h" // xTaskCreatePinnedToCore
#include "esp_adc/adc_oneshot.h" // adc_oneshot_unit_handle_t
#include "board/gpio.h" // gpio_adc_setup
#include "board/irq.h" // irq_save
#include "board/misc.h" // timer_from_us
#include "command.h" // shutdown
#include "internal.h" // ESP32_GPIO_COUNT
#include "sched.h" // sched_shutdown

DECL_CONSTANT("ADC_MAX", 4095);

// ADC1 channel for each gpio (-1 = not an ADC pin).  Classic ESP32:
// GPIO36..39 = ch0..3, GPIO32..35 = ch4..7.
static const int8_t adc_pin_to_chan[ESP32_GPIO_COUNT] = {
    [0 ... 31] = -1,
    [36] = 0, [37] = 1, [38] = 2, [39] = 3,
    [32] = 4, [33] = 5, [34] = 6, [35] = 7,
};

enum { ADC_IDLE, ADC_CONVERTING, ADC_DONE, ADC_CANCELLED };

static adc_oneshot_unit_handle_t adc_unit;
static volatile uint8_t adc_state = ADC_IDLE;
static volatile uint8_t adc_chan;
static volatile uint16_t adc_result;

// Deferred conversion worker (core 0, low priority)
static void
adc_task(void *arg)
{
    for (;;) {
        if (adc_state == ADC_CONVERTING) {
            int raw = 0;
            adc_oneshot_read(adc_unit, adc_chan, &raw);
            irqstatus_t flag = irq_save();
            if (adc_state == ADC_CONVERTING) {
                adc_result = raw;
                adc_state = ADC_DONE;
            } else if (adc_state == ADC_CANCELLED) {
                adc_state = ADC_IDLE;
            }
            irq_restore(flag);
        }
        vTaskDelay(1);
    }
}

struct gpio_adc
gpio_adc_setup(uint32_t pin)
{
    if (pin >= ESP32_GPIO_COUNT || adc_pin_to_chan[pin] < 0)
        shutdown("Not a valid ADC pin");
    if (!adc_unit) {
        adc_oneshot_unit_init_cfg_t ucfg = {
            .unit_id = ADC_UNIT_1,
            .ulp_mode = ADC_ULP_MODE_DISABLE,
        };
        if (adc_oneshot_new_unit(&ucfg, &adc_unit))
            shutdown("ADC init failed");
        xTaskCreatePinnedToCore(adc_task, "klipper_adc", 3072, NULL
                                , tskIDLE_PRIORITY + 1, NULL, 0);
    }
    adc_oneshot_chan_cfg_t ccfg = {
        .atten = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_12,
    };
    if (adc_oneshot_config_channel(adc_unit, adc_pin_to_chan[pin], &ccfg))
        shutdown("ADC channel config failed");
    return (struct gpio_adc){ .chan = adc_pin_to_chan[pin] };
}

// Try to sample a value. Returns zero if sample ready, otherwise
// returns the number of clock ticks the caller should wait before
// retrying this function.
uint32_t
gpio_adc_sample(struct gpio_adc g)
{
    irqstatus_t flag = irq_save();
    uint8_t state = adc_state;
    if (state == ADC_DONE && adc_chan == g.chan) {
        irq_restore(flag);
        return 0;
    }
    if (state == ADC_IDLE) {
        adc_chan = g.chan;
        adc_state = ADC_CONVERTING;
    }
    irq_restore(flag);
    // The worker polls at the FreeRTOS tick rate (1ms)
    return timer_from_us(1500);
}

// Read a value; use only after gpio_adc_sample() returns zero
uint16_t
gpio_adc_read(struct gpio_adc g)
{
    uint16_t v = adc_result;
    adc_state = ADC_IDLE;
    return v;
}

// Cancel a sample that may have been started with gpio_adc_sample()
void
gpio_adc_cancel_sample(struct gpio_adc g)
{
    irqstatus_t flag = irq_save();
    if (adc_chan == g.chan) {
        if (adc_state == ADC_CONVERTING)
            adc_state = ADC_CANCELLED;
        else if (adc_state == ADC_DONE)
            adc_state = ADC_IDLE;
    }
    irq_restore(flag);
}
