// ESP32 watchdog and chip-reset command
//
// Component architecture: subscribe the klipper FreeRTOS task to IDF's task
// watchdog. sdkconfig makes a timeout panic/reboot instead of only logging.
//
// Modem architecture: core 1 may not call IDF. It owns Timer Group 1's MWDT
// directly (Timer Group 0 remains owned by the IDF core and klipper timer),
// and asks the core-0 modem to perform an orderly esp_restart() for the reset
// command. If core 0 is wedged too, the independent MWDT resets the system.

// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "sdkconfig.h" // CONFIG_KLIPPER_ARCH_MODEM
#include "command.h" // DECL_COMMAND_FLAGS
#include "sched.h" // DECL_INIT

#if CONFIG_KLIPPER_ARCH_MODEM

#include "hal/mwdt_ll.h" // mwdt_ll_feed
#include "hal/wdt_types.h" // WDT_STAGE_ACTION_RESET_SYSTEM
#include "soc/timer_group_struct.h" // TIMERG1
#include "shmem_ring.h" // esp32_shmem

// The ESP32 MWDT uses one 500us tick with the IDF default prescaler.
// Match the roughly 500ms watchdog contract used by the STM32 ports.
#define KLIPPER_WDT_TICKS 1000

void
watchdog_init(void)
{
    mwdt_ll_write_protect_disable(&TIMERG1);
    mwdt_ll_disable(&TIMERG1);
    mwdt_ll_disable_stage(&TIMERG1, WDT_STAGE1);
    mwdt_ll_disable_stage(&TIMERG1, WDT_STAGE2);
    mwdt_ll_disable_stage(&TIMERG1, WDT_STAGE3);
    mwdt_ll_set_edge_intr(&TIMERG1, false);
    mwdt_ll_set_level_intr(&TIMERG1, false);
    mwdt_ll_set_intr_enable(&TIMERG1, false);
    mwdt_ll_set_cpu_reset_length(&TIMERG1, WDT_RESET_SIG_LENGTH_3_2us);
    mwdt_ll_set_sys_reset_length(&TIMERG1, WDT_RESET_SIG_LENGTH_3_2us);
    mwdt_ll_set_prescaler(&TIMERG1, MWDT_LL_DEFAULT_CLK_PRESCALER);
    mwdt_ll_config_stage(&TIMERG1, WDT_STAGE0, KLIPPER_WDT_TICKS,
                         WDT_STAGE_ACTION_RESET_SYSTEM);
    mwdt_ll_feed(&TIMERG1);
    mwdt_ll_enable(&TIMERG1);
    mwdt_ll_write_protect_enable(&TIMERG1);
}

void
watchdog_reset(void)
{
    mwdt_ll_write_protect_disable(&TIMERG1);
    mwdt_ll_feed(&TIMERG1);
    mwdt_ll_write_protect_enable(&TIMERG1);
}

void
command_reset(uint32_t *args)
{
    (void)args;
    __atomic_store_n(&esp32_shmem.reset_request, 1, __ATOMIC_RELEASE);
    // Do not execute another command while core 0 performs esp_restart().
    // The MWDT is the bounded fallback if that core cannot service it.
    for (;;)
        ;
}

#else

#include "esp_system.h" // esp_restart
#include "esp_task_wdt.h" // esp_task_wdt_add

void
watchdog_init(void)
{
    ESP_ERROR_CHECK(esp_task_wdt_add(NULL));
}

void
watchdog_reset(void)
{
    esp_task_wdt_reset();
}

void
command_reset(uint32_t *args)
{
    (void)args;
    esp_restart();
}

#endif

DECL_INIT(watchdog_init);
DECL_TASK(watchdog_reset);
DECL_COMMAND_FLAGS(command_reset, HF_IN_SHUTDOWN, "reset");
