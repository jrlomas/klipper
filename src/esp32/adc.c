// ESP32 ADC1 one-shot compatibility backend.
//
// ESP-IDF ADC APIs are confined to a low-priority task on core 0. The
// klipper scheduler on core 1 communicates with it through a lock-free
// sequence/acknowledgement mailbox. This path is deliberately exclusive with
// the continuous DMA stream: IDF's one-shot and continuous drivers cannot own
// classic ESP32 ADC1 at the same time.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "freertos/FreeRTOS.h" // vTaskDelay
#include "freertos/task.h" // xTaskCreatePinnedToCore
#include "esp_adc/adc_oneshot.h" // adc_oneshot_unit_handle_t
#include "board/adc_stream.h" // esp32_adc_stream_is_claimed
#include "board/gpio.h" // gpio_adc_setup
#include "board/misc.h" // timer_from_us
#include "command.h" // shutdown
#include "internal.h" // ESP32_GPIO_COUNT, KLIPPER_ARCH_MODEM
#include "sched.h" // sched_shutdown

DECL_CONSTANT("ADC_MAX", 4095);

// ADC1 channel for each GPIO (-1 = not an ADC1 pin). Classic ESP32:
// GPIO36..39 = ch0..3, GPIO32..35 = ch4..7.
static const int8_t adc_pin_to_chan[ESP32_GPIO_COUNT] = {
    [0 ... 31] = -1,
    [36] = 0, [37] = 1, [38] = 2, [39] = 3,
    [32] = 4, [33] = 5, [34] = 6, [35] = 7,
};

static adc_oneshot_unit_handle_t adc_unit;
static uint32_t adc_req_seq, adc_done_seq;
static uint32_t adc_req_chan, adc_result_word;
static uint32_t adc_my_seq;
static uint8_t adc_pending, adc_pending_chan;
static volatile uint8_t adc_legacy_active;

static int
adc_ensure_unit(uint8_t channel)
{
    if (!adc_unit) {
        adc_oneshot_unit_init_cfg_t ucfg = {
            .unit_id = ADC_UNIT_1,
            .ulp_mode = ADC_ULP_MODE_DISABLE,
        };
        if (adc_oneshot_new_unit(&ucfg, &adc_unit))
            return -1;
    }
    adc_oneshot_chan_cfg_t ccfg = {
        .atten = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_12,
    };
    return adc_oneshot_config_channel(adc_unit, channel, &ccfg) ? -1 : 0;
}

static void
adc_task(void *arg)
{
    uint32_t done = 0;
    for (;;) {
        uint32_t req = __atomic_load_n(&adc_req_seq, __ATOMIC_ACQUIRE);
        if (req != done) {
            uint8_t channel = __atomic_load_n(
                &adc_req_chan, __ATOMIC_RELAXED);
            int raw = 0;
            if (adc_ensure_unit(channel)
                || adc_oneshot_read(adc_unit, channel, &raw))
                raw = 0;
            __atomic_store_n(&adc_result_word, raw, __ATOMIC_RELAXED);
            done = req;
            __atomic_store_n(&adc_done_seq, done, __ATOMIC_RELEASE);
#if !KLIPPER_ARCH_MODEM
            board_wake_main();
#endif
        }
        vTaskDelay(1);
    }
}

void
esp32_adc_init(void)
{
    xTaskCreatePinnedToCore(adc_task, "klipper_adc", 3072, NULL,
                            tskIDLE_PRIORITY + 1, NULL, 0);
}

uint8_t
esp32_adc_legacy_is_active(void)
{
    return __atomic_load_n(&adc_legacy_active, __ATOMIC_ACQUIRE);
}

struct gpio_adc
gpio_adc_setup(uint32_t pin)
{
    if (pin >= ESP32_GPIO_COUNT || adc_pin_to_chan[pin] < 0)
        shutdown("Not a valid ADC pin");
    if (esp32_adc_stream_is_claimed())
        shutdown("ESP32 ADC1 claimed by continuous stream");
    __atomic_store_n(&adc_legacy_active, 1, __ATOMIC_RELEASE);
    return (struct gpio_adc){ .chan = adc_pin_to_chan[pin] };
}

uint32_t
gpio_adc_sample(struct gpio_adc g)
{
    if (adc_pending) {
        if (__atomic_load_n(&adc_done_seq, __ATOMIC_ACQUIRE) == adc_my_seq) {
            if (adc_pending_chan == g.chan)
                return 0;
            adc_pending = 0;
        } else {
            return timer_from_us(1500);
        }
    }
    adc_pending = 1;
    adc_pending_chan = g.chan;
    __atomic_store_n(&adc_req_chan, g.chan, __ATOMIC_RELAXED);
    __atomic_store_n(&adc_req_seq, ++adc_my_seq, __ATOMIC_RELEASE);
    return timer_from_us(1500);
}

uint16_t
gpio_adc_read(struct gpio_adc g)
{
    adc_pending = 0;
    return __atomic_load_n(&adc_result_word, __ATOMIC_RELAXED);
}

void
gpio_adc_cancel_sample(struct gpio_adc g)
{
    if (adc_pending && adc_pending_chan == g.chan)
        adc_pending = 0;
}
