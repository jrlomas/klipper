// Manual APP-CPU (core 1) bringup and the bare-metal core-1 runtime
// for the "IDF as modem" architecture (RFC 0001 doc 12 stage 3).
//
// The IDF app is built unicore (CONFIG_FREERTOS_UNICORE=y): IDF and
// FreeRTOS never learn that core 1 exists.  esp32_appcpu_start()
// (called from app_main on core 0, after WiFi bringup so the PHY
// calibration NVS write - the last flash write - has happened)
// replays the exact register sequence IDF's SMP startup uses to
// release the second core, but points its boot address at
// appcpu_entry (appcpu_vectors.S), which sets up a private stack and
// vector table and enters appcpu_main() below: klipper's sched_main()
// with register-level peripherals and not a single IDF or FreeRTOS
// symbol in reach.
//
// Register-sequence provenance (ESP-IDF v5.3.2, verified against the
// on-disk clone; keep these references with the code):
//  * components/esp_system/port/cpu_start.c:355-372
//    do_multicore_settings(): APP-CPU cache/MMU init when the app was
//    built unicore - Cache_Read_Disable(1), Cache_Flush(1), MMU
//    invalid-access clear via DPORT_APP_CACHE_CTRL1_REG, mmu_init(1),
//    then restore_app_mmu_from_pro_mmu() copying the 2048-entry
//    PRO flash-MMU table (0x3ff10000) to the APP table (0x3ff12000).
//  * components/hal/esp32/include/hal/cpu_utility_ll.h:51-66
//    cpu_utility_ll_unstall_cpu(1): clear RTC_CNTL_SW_STALL_APPCPU_C0
//    in RTC_CNTL_OPTIONS0_REG and RTC_CNTL_SW_STALL_APPCPU_C1 in
//    RTC_CNTL_SW_CPU_STALL_REG (the split 0x86 stall magic).
//  * components/esp_system/port/cpu_start.c:263-294
//    start_other_core(): Cache_Flush(1); Cache_Read_Enable(1);
//    unstall; then DPORT_APPCPU_CTRL_B_REG CLKGATE_EN set,
//    DPORT_APPCPU_CTRL_C_REG RUNSTALL clear, DPORT_APPCPU_CTRL_A_REG
//    RESETTING pulse; finally ets_set_appcpu_boot_addr(entry).
//  * components/esp_system/port/cpu_start.c:163-173
//    core_intr_matrix_clear(): route all ETS_MAX_INTR_SOURCE sources
//    to ETS_INVALID_INUM (6).  IDF runs this *on* core 1; here core 0
//    writes the DPORT_APP_*_INTR_MAP registers before unstalling.
//
// The core-1 runtime below is polled, like the klipper linux mcu:
// no interrupt is ever enabled on core 1 (INTENABLE=0, set by
// appcpu_entry), timers dispatch from irq_poll()/irq_wait() in
// sched_main's loop, and the console rings are checked in the same
// place.  On a dedicated 240MHz core whose only job is klipper, the
// poll granularity is the longest non-yielding stretch of task code,
// the same contract the linux mcu ships with.  A level-1 timer ISR
// (through the appcpu_vectors.S table) is the flagged follow-up once
// hardware bringup allows measuring both variants.
//
// RUNTIME UNPROVEN: this file host-compiles and every register write
// is source-verified against IDF, but it has never executed on
// silicon (no xtensa toolchain/hardware in the build environment).
// See the bring-up checklist in docs/ESP32.md.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "soc/soc.h" // REG_GET_BIT, ETS_INVALID_INUM
#include "soc/dport_reg.h" // DPORT_APPCPU_CTRL_*_REG
#include "soc/efuse_reg.h" // EFUSE_RD_DISABLE_APP_CPU
#include "soc/rtc_cntl_reg.h" // RTC_CNTL_SW_STALL_APPCPU_*
#include "soc/timer_group_struct.h" // TIMERG0
#include "esp32/rom/cache.h" // Cache_Flush, mmu_init
#include "esp32/rom/ets_sys.h" // ets_set_appcpu_boot_addr
#include "esp_log.h" // ESP_LOGE (core-0 half only)
#include "esp_private/periph_ctrl.h" // periph_module_enable
#include "freertos/FreeRTOS.h" // vTaskDelay (core-0 half only)
#include "freertos/task.h"
#include "autoconf.h" // CONFIG_CLOCK_FREQ
#include "board/irq.h" // irq_disable
#include "board/misc.h" // timer_read_time
#include "board/timer_irq.h" // timer_dispatch_many
#include "internal.h" // esp32_appcpu_start
#include "sched.h" // sched_main
#include "shmem_ring.h" // esp32_shmem

static const char *TAG = "klipper_appcpu";

// Fault record written by the appcpu_vectors.S park handlers:
// { flag, exccause (or 0x100+level), epc1, excvaddr }
uint32_t esp32_core1_fault[4];

// Entry point in appcpu_vectors.S (xtensa only; the host-gcc
// harness link-checks this file with the stand-in below)
void appcpu_entry(void);
#ifndef __XTENSA__
void appcpu_entry(void) { }
#endif


/****************************************************************
 * Core 0: releasing the APP CPU
 ****************************************************************/

int
esp32_appcpu_start(void)
{
    // Single-core die variants (ESP32-S0WD/U4WDH) fuse the APP CPU
    // off; mirrors chip_info.cores check in start_other_core()
    if (REG_GET_BIT(EFUSE_BLK0_RDATA3_REG, EFUSE_RD_DISABLE_APP_CPU)) {
        ESP_LOGE(TAG, "APP CPU disabled by efuse - single-core chip; "
                 "use the 'component' architecture");
        return -1;
    }

    // Clock the peripherals the bare core drives at register level.
    // Done here - through IDF's spinlocked periph_ctrl, while core 1
    // is still stalled - because DPORT_PERIP_CLK_EN_REG is a shared
    // read-modify-write register that core 1 must never touch
    // concurrently with core 0 (ESP32 DPORT access hazard; see
    // soc/dport_access.h).
    periph_module_enable(PERIPH_TIMG0_MODULE); // klipper timer (T0)
    periph_module_enable(PERIPH_HSPI_MODULE);
    periph_module_enable(PERIPH_VSPI_MODULE);
    periph_module_enable(PERIPH_I2C0_MODULE);
    periph_module_enable(PERIPH_LEDC_MODULE);

    // Route every interrupt source of the APP core's matrix to the
    // invalid CPU interrupt (cpu_start.c core_intr_matrix_clear();
    // core 1 additionally boots with INTENABLE=0)
    for (int i = 0; i < ETS_MAX_INTR_SOURCE; i++)
        DPORT_REG_WRITE(DPORT_APP_MAC_INTR_MAP_REG + 4 * i
                        , ETS_INVALID_INUM);

    // APP-CPU flash cache + MMU: the unicore IDF boot skipped all of
    // this (cpu_start.c do_multicore_settings(), lines 355-372)
    Cache_Read_Disable(1);
    Cache_Flush(1);
    DPORT_REG_SET_BIT(DPORT_APP_CACHE_CTRL1_REG, DPORT_APP_CACHE_MMU_IA_CLR);
    mmu_init(1);
    DPORT_REG_CLR_BIT(DPORT_APP_CACHE_CTRL1_REG, DPORT_APP_CACHE_MMU_IA_CLR);
    // restore_app_mmu_from_pro_mmu(): both cores must see the same
    // flash mapping (2048 words, cpu_start.c lines 340-350)
    for (int i = 0; i < 2048; i++)
        DPORT_REG_WRITE(DR_REG_FLASH_MMU_TABLE_APP + 4 * i
                        , DPORT_REG_READ(DR_REG_FLASH_MMU_TABLE_PRO + 4 * i));

    // start_other_core(), cpu_start.c lines 279-294
    Cache_Flush(1);
    Cache_Read_Enable(1);

    // esp_cpu_unstall(1) -> cpu_utility_ll_unstall_cpu(1),
    // hal/esp32 cpu_utility_ll.h lines 51-66
    CLEAR_PERI_REG_MASK(RTC_CNTL_OPTIONS0_REG, RTC_CNTL_SW_STALL_APPCPU_C0_M);
    CLEAR_PERI_REG_MASK(RTC_CNTL_SW_CPU_STALL_REG
                        , RTC_CNTL_SW_STALL_APPCPU_C1_M);

    // Clock-gate + reset pulse (skip if a debugger already released
    // the core, preserving its breakpoints - same check as IDF)
    if (!DPORT_GET_PERI_REG_MASK(DPORT_APPCPU_CTRL_B_REG
                                 , DPORT_APPCPU_CLKGATE_EN)) {
        DPORT_SET_PERI_REG_MASK(DPORT_APPCPU_CTRL_B_REG
                                , DPORT_APPCPU_CLKGATE_EN);
        DPORT_CLEAR_PERI_REG_MASK(DPORT_APPCPU_CTRL_C_REG
                                  , DPORT_APPCPU_RUNSTALL);
        DPORT_SET_PERI_REG_MASK(DPORT_APPCPU_CTRL_A_REG
                                , DPORT_APPCPU_RESETTING);
        DPORT_CLEAR_PERI_REG_MASK(DPORT_APPCPU_CTRL_A_REG
                                  , DPORT_APPCPU_RESETTING);
    }

    // Hand the APP-CPU ROM its jump target; it polls for a nonzero
    // boot address and calls it (cpu_start.c line 328)
    ets_set_appcpu_boot_addr((uint32_t)(uintptr_t)appcpu_entry);

    // Wait for sched_main to come alive on core 1
    for (int ms = 0; ms < 1000; ms += 10) {
        if (__atomic_load_n(&esp32_shmem.core1_alive, __ATOMIC_ACQUIRE)) {
            ESP_LOGI(TAG, "core 1 running bare klipper (after ~%dms)", ms);
            return 0;
        }
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    ESP_LOGE(TAG, "core 1 did not start (fault=%u cause=0x%x epc=0x%x)"
             , (unsigned)esp32_core1_fault[0]
             , (unsigned)esp32_core1_fault[1]
             , (unsigned)esp32_core1_fault[2]);
    return -1;
}


/****************************************************************
 * Core 1: bare timer (TIMG0 timer 0, polled - no ISR)
 ****************************************************************/

// TIMG0 T0 counts up at 20MHz (80MHz APB / 4; see autoconf.h).  Only
// core 1 touches the T0 register block; core 0's esp_timer uses the
// separate LACT block and the task watchdog the WDT block of the
// same group - disjoint registers, no cross-core RMW.  Register
// programming follows hal/esp32/include/hal/timer_ll.h (v5.3.2).
#define BARE_TIMER_DIVIDER (APB_CLK_FREQ / CONFIG_CLOCK_FREQ) // = 4

static uint32_t timer_next_wake;

// Latch and read the running counter (timer_ll_trigger_soft_capture
// + timer_ll_get_counter_value; klipper time is the low 32 bits)
uint32_t DECL_IRAM
timer_read_time(void)
{
    TIMERG0.hw_timer[0].update.tx_update = 1;
    return TIMERG0.hw_timer[0].lo.tx_lo;
}

// Activate timer dispatch as soon as possible
void
timer_kick(void)
{
    timer_next_wake = timer_read_time() + timer_from_us(2);
}

static void
bare_timer_init(void)
{
    // Stop, program divider/direction, zero, restart (clock already
    // enabled by esp32_appcpu_start on core 0)
    TIMERG0.hw_timer[0].config.tx_en = 0;
    TIMERG0.hw_timer[0].config.tx_divider = BARE_TIMER_DIVIDER;
    TIMERG0.hw_timer[0].config.tx_increase = 1;
    TIMERG0.hw_timer[0].config.tx_autoreload = 0;
    TIMERG0.hw_timer[0].config.tx_alarm_en = 0;
    TIMERG0.hw_timer[0].config.tx_level_int_en = 0;
    TIMERG0.hw_timer[0].config.tx_edge_int_en = 0;
    TIMERG0.hw_timer[0].loadlo.tx_load_lo = 0;
    TIMERG0.hw_timer[0].loadhi.tx_load_hi = 0;
    TIMERG0.hw_timer[0].load.tx_load = 1; // any write reloads
    TIMERG0.hw_timer[0].config.tx_en = 1;
    timer_kick();
}


/****************************************************************
 * Core 1: irq_* contract (bare, polled)
 ****************************************************************/

// No interrupt is ever enabled on the bare core (INTENABLE=0), so
// PS.INTLEVEL manipulation is formal today - it is kept real (RSIL)
// so a future level-1 timer ISR drops in without revisiting every
// critical section.  Nothing here synchronizes with core 0: the only
// cross-core state is the lock-free console ring (shmem_ring.h) and
// the ADC handshake (adc.c), which carry their own acquire/release
// ordering.

irqstatus_t DECL_IRAM
irq_save(void)
{
#ifdef __XTENSA__
    uint32_t ps;
    __asm__ __volatile__("rsil %0, 15" : "=a"(ps) :: "memory");
    return ps;
#else
    __asm__ __volatile__("" ::: "memory"); // host stand-in
    return 0;
#endif
}

void DECL_IRAM
irq_restore(irqstatus_t flag)
{
#ifdef __XTENSA__
    __asm__ __volatile__("wsr.ps %0 ; rsync" :: "a"(flag) : "memory");
#else
    (void)flag;
    __asm__ __volatile__("" ::: "memory");
#endif
}

void DECL_IRAM
irq_disable(void)
{
    irq_save();
}

void DECL_IRAM
irq_enable(void)
{
#ifdef __XTENSA__
    uint32_t ps;
    __asm__ __volatile__("rsil %0, 0" : "=a"(ps) :: "memory");
    (void)ps;
#else
    __asm__ __volatile__("" ::: "memory");
#endif
}

// Poll for work: run due klipper timers, surface pending console
// datagrams.  Called from sched_main's run_tasks() loop and (via
// irq_wait) from its idle loop - this is the dispatch point of the
// whole bare core, the moral equivalent of the timer ISR.
void DECL_IRAM
irq_poll(void)
{
    shmem_console_poll();
    if ((int32_t)(timer_read_time() - timer_next_wake) >= 0) {
        irq_disable();
        timer_next_wake = timer_dispatch_many();
        irq_enable();
    }
}

// Sleep until an event: entered with "irqs disabled", must wait with
// them enabled and return with them disabled.  The bare core busy
// polls - it has no other purpose, and the WiFi core's wakeup
// (a new ring record) is visible within one poll iteration.
void DECL_IRAM
irq_wait(void)
{
    irq_enable();
    irq_poll();
    irq_disable();
}


/****************************************************************
 * Core 1: main
 ****************************************************************/

// Called (never to return) from appcpu_entry in appcpu_vectors.S.
// From here on core 1 must not reach any IDF/FreeRTOS symbol.
void
appcpu_main(void)
{
    bare_timer_init();
    shmem_console_init();
    __atomic_store_n(&esp32_shmem.core1_alive, 1, __ATOMIC_RELEASE);
    sched_main();
}
