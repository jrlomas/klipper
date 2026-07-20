// Timer-triggered ADC scan acquisition for STM32F072 and STM32G0B1.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "adc_stream.h"
#include "board/armcm_boot.h" // armcm_enable_irq
#include "command.h" // shutdown
#include "generic/acq_block.h" // ACQ_STATUS_*
#include "generic/dma_resource.h"
#include "internal.h" // peripheral registers
#include "sched.h" // sched_shutdown

// F0/G0 scan hardware emits enabled channels in numerical channel order.
// Advertise the pin-to-rank mapping for the host's merged legacy adapter.
DECL_ENUMERATION_RANGE("adc_stream_channel", "PA0", 0, 8);
DECL_ENUMERATION_RANGE("adc_stream_channel", "PB0", 8, 2);
#if CONFIG_MACH_STM32F0
DECL_ENUMERATION_RANGE("adc_stream_channel", "PC0", 10, 6);
DECL_ENUMERATION("adc_stream_channel", "ADC_TEMPERATURE", 16);
#else
DECL_ENUMERATION("adc_stream_channel", "PB2", 10);
DECL_ENUMERATION("adc_stream_channel", "PB10", 11);
DECL_ENUMERATION("adc_stream_channel", "ADC_TEMPERATURE", 12);
DECL_ENUMERATION_RANGE("adc_stream_channel", "PB11", 15, 2);
DECL_ENUMERATION_RANGE("adc_stream_channel", "PC4", 17, 2);
#endif

#define ADC_DMA_CCR (DMA_CCR_MINC | DMA_CCR_PSIZE_0 | DMA_CCR_MSIZE_0 \
                     | DMA_CCR_CIRC | DMA_CCR_HTIE | DMA_CCR_TCIE \
                     | DMA_CCR_TEIE)
#define ADC_DMA_FLAGS (DMA_IFCR_CGIF1 | DMA_IFCR_CTCIF1 \
                       | DMA_IFCR_CHTIF1 | DMA_IFCR_CTEIF1)

static uint8_t owns_tim3;

void
DMA1_Channel1_IRQHandler(void)
{
    uint32_t isr = DMA1->ISR;
    uint32_t status = 0;
    DMA1->IFCR = ADC_DMA_FLAGS;
    if (isr & DMA_ISR_TEIF1)
        status |= ACQ_STATUS_DMA_ERROR;
    if (ADC1->ISR & ADC_ISR_OVR) {
        ADC1->ISR = ADC_ISR_OVR;
        status |= ACQ_STATUS_OVERRUN;
    }
    if (status) {
        DMA1_Channel1->CCR = 0;
        adc_stream_backend_fault(status);
        return;
    }
    // In circular mode HT owns the first half and TC owns the second.  The
    // DMA immediately proceeds into the peer half; generic ownership stops
    // the stream before wrap if the consumer did not return it in time.
    if ((isr & DMA_ISR_HTIF1) && adc_stream_block_complete(0, 0))
        return;
    if (isr & DMA_ISR_TCIF1)
        adc_stream_block_complete(1, 0);
}

void
board_adc_stream_setup(const struct adc_stream_backend_config *cfg,
                       struct adc_stream_backend_info *info)
{
#if CONFIG_MACH_STM32F0
    if (cfg->hardware_oversample != 1 || cfg->hardware_shift)
        shutdown("STM32F0 ADC lacks hardware oversampling");
#endif
    if (dma_claim(DMA_RESOURCE_ADC1, 0, cfg->owner)
        || dma_claim(DMA_RESOURCE_TIM3, 0, cfg->owner)
        || dma_claim(DMA_RESOURCE_DMA1_CHANNEL1, 0, cfg->owner)
#if CONFIG_MACH_STM32G0
        || dma_claim(DMA_RESOURCE_DMAMUX1_REQUEST0, 5, cfg->owner)
#endif
        )
        shutdown("STM32 ADC stream resource conflict");
    uint32_t channel_mask = 0, previous = 0;
    for (uint8_t i = 0; i < cfg->channel_count; i++) {
        uint32_t channel = cfg->pins[i].chan;
        if (!channel || (channel & (channel - 1))
            || (i && channel <= previous))
            shutdown("STM32 ADC stream channels must ascend");
        channel_mask |= channel;
        previous = channel;
    }
    if (ADC1->CR & ADC_CR_ADSTART)
        shutdown("STM32 ADC is busy");
    if (!owns_tim3 && is_enabled_pclock(TIM3_BASE))
        shutdown("TIM3 already claimed");

    // Keep the block boundary comfortably larger than the non-circular DMA
    // re-arm ISR. This first implementation prioritizes explicit ownership
    // over silently overwriting a circular buffer.
    if (cfg->requested_period_ticks < CONFIG_CLOCK_FREQ / 20000u)
        shutdown("STM32 ADC stream period below 50us");
    uint32_t divisor = (cfg->requested_period_ticks + 65535u) / 65536u;
    if (!divisor || divisor > 65536u)
        shutdown("STM32 ADC stream period out of range");
    uint32_t counts = (cfg->requested_period_ticks + divisor / 2) / divisor;
    if (!counts || counts > 65536u)
        shutdown("STM32 ADC stream timer out of range");
    uint32_t actual_period = divisor * counts;

#if CONFIG_MACH_STM32F0
    RCC->AHBENR |= RCC_AHBENR_DMA1EN;
    RCC->AHBENR;
#else
    RCC->AHBENR |= RCC_AHBENR_DMA1EN;
    RCC->AHBENR;
    DMAMUX1_Channel0->CCR = 5; // RM0444: ADC1 request
#endif
    if (!owns_tim3) {
        enable_pclock(TIM3_BASE);
        owns_tim3 = 1;
    }
    TIM3->CR1 = 0;
    TIM3->PSC = divisor - 1;
    TIM3->ARR = counts - 1;
    TIM3->CR2 = TIM_CR2_MMS_1; // update event is TRGO
    TIM3->EGR = TIM_EGR_UG;
    TIM3->SR = 0;

    DMA1_Channel1->CCR = 0;
    DMA1->IFCR = ADC_DMA_FLAGS;
    DMA1_Channel1->CPAR = (uint32_t)&ADC1->DR;
    DMA1_Channel1->CMAR = (uint32_t)cfg->buffer;
    DMA1_Channel1->CNDTR = 2u * cfg->block_values;
    DMA1_Channel1->CCR = ADC_DMA_CCR;
    armcm_enable_irq(DMA1_Channel1_IRQHandler, DMA1_Channel1_IRQn, 1);

#if CONFIG_MACH_STM32G0
    uint8_t osr_bits = 0;
    for (uint16_t ratio = cfg->hardware_oversample; ratio > 2; ratio >>= 1)
        osr_bits++;
    ADC1->CFGR2 = cfg->hardware_oversample > 1
        ? ADC_CFGR2_OVSE | ((uint32_t)osr_bits << ADC_CFGR2_OVSR_Pos)
          | ((uint32_t)cfg->hardware_shift << ADC_CFGR2_OVSS_Pos)
        : 0;
    ADC1->ISR = ADC_ISR_CCRDY;
    ADC1->CHSELR = channel_mask;
    while (!(ADC1->ISR & ADC_ISR_CCRDY))
        ;
#else
    ADC1->CHSELR = channel_mask;
#endif
    ADC1->ISR = ADC_ISR_OVR | ADC_ISR_EOC | ADC_ISR_EOS;
    ADC1->CFGR1 = (ADC1->CFGR1
                   & ~(ADC_CFGR1_DMAEN | ADC_CFGR1_DMACFG
                       | ADC_CFGR1_EXTSEL | ADC_CFGR1_EXTEN))
                  | ADC_CFGR1_DMAEN | ADC_CFGR1_DMACFG
                  | ADC_CFGR1_EXTSEL_1 | ADC_CFGR1_EXTSEL_0
                  | ADC_CFGR1_EXTEN_0; // rising TIM3_TRGO

    info->period_numerator = actual_period;
    info->period_denominator = 1;
    info->uncertainty_ticks = CONFIG_CLOCK_FREQ / 1000000u * 5u;
    info->status = ACQ_STATUS_INFERRED_TIME;
    info->max_conversion_rate = 80000;
    info->capabilities = ADC_BACKEND_CAP_HARDWARE_PACED
                         | ADC_BACKEND_CAP_INFERRED_START;
    info->max_hardware_oversample = 1;
#if CONFIG_MACH_STM32G0
    info->capabilities |= ADC_BACKEND_CAP_HW_OVERSAMPLE;
    info->max_hardware_oversample = 256;
#endif
    info->resolution_bits = 12;
    info->adc_count = 1;
    info->watchdog_count = 0;
    info->timing_quality = 1;
}

void
board_adc_stream_start(void)
{
    DMA1_Channel1->CCR |= DMA_CCR_EN;
    TIM3->CNT = 0;
    TIM3->SR = 0;
    ADC1->CR |= ADC_CR_ADSTART;
    // Emit the first TRGO immediately after the ADC is armed. Without this
    // update, the first aperture occurs one complete scan period after the
    // machine-clock timestamp captured by the generic start timer.
    TIM3->EGR = TIM_EGR_UG;
    TIM3->SR = 0;
    TIM3->CR1 = TIM_CR1_CEN;
}

void
board_adc_stream_stop_from_isr(void)
{
    TIM3->CR1 = 0;
    DMA1_Channel1->CCR = 0;
    if (ADC1->CR & ADC_CR_ADSTART)
        ADC1->CR |= ADC_CR_ADSTP;
}

void
board_adc_stream_stop(void)
{
    board_adc_stream_stop_from_isr();
    ADC1->CFGR1 &= ~(ADC_CFGR1_DMAEN | ADC_CFGR1_DMACFG
                     | ADC_CFGR1_EXTEN);
}

void
board_adc_stream_block_released(uint8_t block_index)
{
    (void)block_index;
    // Circular DMA retains both half-buffer addresses; generic ownership
    // checks prevent a hardware wrap from overwriting an unreturned half.
}
