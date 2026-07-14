// Hardware timer support for the ESP32 port
//
// klipper's timer is a GPTimer counting at CONFIG_CLOCK_FREQ (20MHz,
// an integer division of the 80MHz APB clock - see autoconf.h).  The
// alarm ISR runs the generic timer_dispatch_many() loop and then
// programs the next alarm.  esp32_timer_setup() must be called from
// the klipper main task *after* it has been pinned to core 1: the
// esp_intr_alloc() performed by gptimer_register_event_callbacks()
// attaches the interrupt to the calling core, which is what pins
// klipper timer dispatch away from the WiFi core (FD-0001 doc 07
// core-pinning).
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "freertos/FreeRTOS.h" // portENTER_CRITICAL_ISR
#include "driver/gptimer.h" // gptimer_new_timer
#include "autoconf.h" // CONFIG_CLOCK_FREQ
#include "board/irq.h" // irq_disable
#include "board/misc.h" // timer_read_time
#include "board/timer_irq.h" // timer_dispatch_many
#include "internal.h" // esp32_timer_setup
#include "sched.h" // DECL_SHUTDOWN

extern portMUX_TYPE klipper_mux; // irq.c

static gptimer_handle_t klipper_timer;
static volatile uint8_t klipper_timer_ready;

uint8_t
esp32_timer_ready(void)
{
    return klipper_timer_ready;
}

// Return the current time (in clock ticks); klipper time is the low
// 32 bits of the 64-bit hardware count - wraparound arithmetic is
// handled by timer_is_before() as on every other 32-bit target
uint32_t
timer_read_time(void)
{
    uint64_t count = 0;
    gptimer_get_raw_count(klipper_timer, &count);
    return (uint32_t)count;
}

// Program the hardware alarm for a 32-bit klipper time
static void
timer_set(uint32_t next)
{
    uint64_t count = 0;
    gptimer_get_raw_count(klipper_timer, &count);
    // Translate the 32-bit target onto the 64-bit counter timeline
    uint64_t target = count + (uint64_t)(int64_t)(int32_t)(next
                                                           - (uint32_t)count);
    gptimer_alarm_config_t alarm = {
        .alarm_count = target,
        .flags.auto_reload_on_alarm = false,
    };
    gptimer_set_alarm_action(klipper_timer, &alarm);
}

// Activate timer dispatch as soon as possible
void
timer_kick(void)
{
    irqstatus_t flag = irq_save();
    timer_set(timer_read_time() + timer_from_us(2));
    irq_restore(flag);
}

// Hardware timer alarm - dispatch klipper timers.  Runs on core 1
// (see esp32_timer_setup) with this core's interrupts already masked
// by the interrupt dispatch; the explicit critical section makes the
// irq_enable()/irq_disable() pair inside timer_dispatch_many()'s
// busy-wait path balance correctly.
static bool IRAM_ATTR
timer_alarm_cb(gptimer_handle_t timer, const gptimer_alarm_event_data_t *edata
               , void *user_ctx)
{
    portENTER_CRITICAL_ISR(&klipper_mux);
    uint32_t next = timer_dispatch_many();
    timer_set(next);
    portEXIT_CRITICAL_ISR(&klipper_mux);
    // Wake the main task in case timer handlers woke klipper tasks
    board_wake_main_from_isr();
    return false;
}

void
esp32_timer_setup(void)
{
    gptimer_config_t config = {
        .clk_src = GPTIMER_CLK_SRC_DEFAULT,
        .direction = GPTIMER_COUNT_UP,
        .resolution_hz = CONFIG_CLOCK_FREQ,
    };
    ESP_ERROR_CHECK(gptimer_new_timer(&config, &klipper_timer));
    gptimer_event_callbacks_t cbs = { .on_alarm = timer_alarm_cb };
    ESP_ERROR_CHECK(gptimer_register_event_callbacks(klipper_timer, &cbs
                                                     , NULL));
    ESP_ERROR_CHECK(gptimer_enable(klipper_timer));
    ESP_ERROR_CHECK(gptimer_start(klipper_timer));
    // app_main() waits for this before it initializes the UDP console.
    // Session initialization samples timer_read_time() for a per-boot nonce,
    // so it must not run while klipper_timer is still NULL.
    klipper_timer_ready = 1;
    timer_kick();
}
