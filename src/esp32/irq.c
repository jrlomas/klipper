// Interrupt enable/disable and idle-wait for the ESP32 port
//
// klipper's irq_disable()/irq_save() contract is implemented with a
// FreeRTOS critical section (a spinlock plus masking of this core's
// interrupts).  portENTER/EXIT_CRITICAL_SAFE picks the ISR-safe
// variant automatically, so the same functions are usable from
// klipper timer dispatch running inside the hardware-timer ISR.  The
// spinlock also serializes against the WiFi core touching shared
// klipper state (the udp rx path), which plain interrupt masking
// would not.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "freertos/FreeRTOS.h" // portENTER_CRITICAL_SAFE
#include "freertos/task.h" // ulTaskNotifyTake
#include "board/irq.h" // irq_disable
#include "internal.h" // board_wake_main

portMUX_TYPE klipper_mux = portMUX_INITIALIZER_UNLOCKED;
void *klipper_main_task;

void
board_set_main_task(void *task_handle)
{
    klipper_main_task = task_handle;
}

// Wake the klipper main task (from another task, e.g. the udp rx
// task on the WiFi core)
void
board_wake_main(void)
{
    if (klipper_main_task)
        xTaskNotifyGive((TaskHandle_t)klipper_main_task);
}

// Wake the klipper main task from an ISR
void IRAM_ATTR
board_wake_main_from_isr(void)
{
    if (!klipper_main_task)
        return;
    BaseType_t hpw = pdFALSE;
    vTaskNotifyGiveFromISR((TaskHandle_t)klipper_main_task, &hpw);
    if (hpw == pdTRUE) {
        portYIELD_FROM_ISR();
    }
}

void
irq_disable(void)
{
    portENTER_CRITICAL_SAFE(&klipper_mux);
}

void
irq_enable(void)
{
    portEXIT_CRITICAL_SAFE(&klipper_mux);
}

irqstatus_t
irq_save(void)
{
    irq_disable();
    return 0;
}

void
irq_restore(irqstatus_t flag)
{
    irq_enable();
}

// Sleep the main task until an event - called by sched_main with
// "irqs disabled"; must wait with them enabled and return with them
// disabled (same contract as armcm's cpsie/wfi/cpsid sequence).  The
// one-tick timeout is a backstop; the timer ISR and the udp rx task
// both send task notifications.
void
irq_wait(void)
{
    irq_enable();
    ulTaskNotifyTake(pdTRUE, 1);
    irq_disable();
}

void
irq_poll(void)
{
    esp32_adc_stream_poll();
}
