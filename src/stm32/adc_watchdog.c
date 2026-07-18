// ADC analog watchdog trigger source (FD-0001 doc 09 section 2).
//
// Board half of src/trigger_source.c for the adc_watchdog kind. The
// ADC free-runs (continuous conversion) on one channel and the analog
// watchdog (AWD) compares every sample against high/low thresholds in
// hardware; a crossing raises the ADC interrupt with no CPU polling.
// This is the "event-not-poll" analog path on families that lack a
// COMP peripheral -- per doc 09's portability table the ADC watchdog
// is the fallback used where COMP is absent, so it is wired here on
// the classic-ADC families (F2/F4/F7) that have no COMP. On STM32G0,
// which *does* have COMP, the preferred analog trigger is the window
// comparator (src/stm32/comp.c) and it owns the shared ADC1_COMP
// interrupt vector; the AWD is left unwired there and this config
// command reports the pin as unavailable.
//
// When the DMA scan engine owns the ADC, the watchdog observes that regular
// sequence in hardware and arm/disarm changes only AWDIE.  Without a stream,
// it retains the legacy single-channel free-running mode.
//
// Timestamping: the AWD has no capture unit, so the trigger clock is
// the ADC ISR-entry read (a threshold crossing, unlike a GPIO edge,
// has no single hardware-latchable instant anyway).
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "autoconf.h" // CONFIG_MACH_STM32F4
#include "board/armcm_boot.h" // armcm_enable_irq
#include "board/irq.h" // irq_disable
#include "board/misc.h" // timer_read_time
#include "command.h" // shutdown
#include "compiler.h" // ARRAY_SIZE
#include "gpio.h" // gpio_adc_setup
#include "internal.h" // ADC1
#include "sched.h" // sched_shutdown
#include "trigger_source.h" // board_adc_watchdog_setup
#include "stm32/adc_watchdog.h"

#if CONFIG_WANT_ADC \
    && (CONFIG_MACH_STM32F2 || CONFIG_MACH_STM32F4 || CONFIG_MACH_STM32F7)

struct awd_state {
    struct trigger_source *tsrc;
    ADC_TypeDef *adc;
    uint8_t chan;
    uint8_t armed;
};
static struct awd_state awd_list[4];
static uint8_t awd_count;
static uint8_t awd_irq_ready;
static ADC_TypeDef *stream_adc;

// Combined ADC1/2/3 vector: scan every armed watchdog for a crossing.
void
ADC_IRQHandler(void)
{
    uint32_t clock = timer_read_time();
    for (int i = 0; i < awd_count; i++) {
        struct awd_state *a = &awd_list[i];
        ADC_TypeDef *adc = a->adc;
        if (!(adc->SR & ADC_SR_AWD))
            continue;
        adc->SR = ~ADC_SR_AWD; // clear the watchdog flag
        trigger_source_notify(a->tsrc, clock);
    }
}

int
board_adc_watchdog_setup(struct trigger_source *tsrc)
{
    if (awd_count >= ARRAY_SIZE(awd_list))
        return -1;
    // Reuse the standard ADC pin routing + block enable/calibrate.
    struct gpio_adc g = gpio_adc_setup(tsrc->pin);
    ADC_TypeDef *adc = g.adc;
    uint8_t chan = g.chan;
    for (int i = 0; i < awd_count; i++)
        if (awd_list[i].adc == adc)
            return -1; // classic ADC IP has one regular watchdog comparator

    // One conversion of the watched channel (continuous mode repeats).
    adc->SQR1 = 0;
    adc->SQR3 = chan;
    // 12-bit thresholds (host supplies ADC counts, 0-4095).
    adc->HTR = tsrc->hw[0] & ADC_HTR_HT_Msk;
    adc->LTR = tsrc->hw[1] & ADC_LTR_LT_Msk;
    // Single-channel analog watchdog on that channel; AWDIE stays off
    // until the source is armed.
    adc->CR1 = (ADC_CR1_AWDEN | ADC_CR1_AWDSGL
                | ((uint32_t)chan << ADC_CR1_AWDCH_Pos));

    awd_list[awd_count].tsrc = tsrc;
    awd_list[awd_count].adc = adc;
    awd_list[awd_count].chan = chan;
    awd_count++;

    if (!awd_irq_ready) {
        armcm_enable_irq(ADC_IRQHandler, ADC_IRQn, 0);
        awd_irq_ready = 1;
    }
    return 0;
}

static struct awd_state *
awd_lookup(struct trigger_source *tsrc)
{
    for (int i = 0; i < awd_count; i++)
        if (awd_list[i].tsrc == tsrc)
            return &awd_list[i];
    return NULL;
}

void
board_adc_watchdog_arm(struct trigger_source *tsrc, int enable)
{
    struct awd_state *a = awd_lookup(tsrc);
    if (!a)
        return;
    ADC_TypeDef *adc = a->adc;
    a->armed = enable;
    if (enable) {
        adc->SR = ~ADC_SR_AWD; // discard any stale crossing
        adc->CR1 |= ADC_CR1_AWDIE;
        if (stream_adc == adc)
            return;
        // Free-run the ADC on the watched channel.
        adc->CR2 = ADC_CR2_ADON | ADC_CR2_CONT;
        adc->CR2 = ADC_CR2_ADON | ADC_CR2_CONT | ADC_CR2_SWSTART;
    } else {
        adc->CR1 &= ~ADC_CR1_AWDIE;
        adc->CR2 &= ~ADC_CR2_CONT;
    }
}

uint32_t
stm32_adc_watchdog_stream_configure(ADC_TypeDef *adc, uint32_t channel_mask)
{
    uint32_t bits = 0;
    for (int i = 0; i < awd_count; i++) {
        struct awd_state *a = &awd_list[i];
        if (a->adc != adc)
            continue;
        if (!(channel_mask & (1u << a->chan)))
            shutdown("ADC watchdog channel absent from DMA scan");
        bits = ADC_CR1_AWDEN | ADC_CR1_AWDSGL
            | ((uint32_t)a->chan << ADC_CR1_AWDCH_Pos);
        if (a->armed)
            bits |= ADC_CR1_AWDIE;
    }
    stream_adc = adc;
    return bits;
}

void
stm32_adc_watchdog_stream_stopped(ADC_TypeDef *adc)
{
    if (stream_adc != adc)
        return;
    stream_adc = NULL;
    for (int i = 0; i < awd_count; i++) {
        struct awd_state *a = &awd_list[i];
        if (a->adc == adc && a->armed) {
            adc->CR2 = ADC_CR2_ADON | ADC_CR2_CONT;
            adc->CR2 = ADC_CR2_ADON | ADC_CR2_CONT | ADC_CR2_SWSTART;
            return;
        }
    }
}

#else

// Families with no wired ADC watchdog trigger (F0/F1, or COMP-bearing
// G0/G4 where the comparator path is preferred and owns the shared
// ADC interrupt vector). The config command shuts down gracefully.
int
board_adc_watchdog_setup(struct trigger_source *tsrc)
{
    (void)tsrc;
    return -1;
}

void
board_adc_watchdog_arm(struct trigger_source *tsrc, int enable)
{
    (void)tsrc;
    (void)enable;
}

#endif
