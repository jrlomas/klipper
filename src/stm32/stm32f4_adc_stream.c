// Timer-triggered ADC1 acquisition for STM32F4/F7 using DMA2 double buffer.
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

#define ADC_DMA_CR ((0u << DMA_SxCR_CHSEL_Pos) | DMA_SxCR_MINC \
                    | DMA_SxCR_PSIZE_0 | DMA_SxCR_MSIZE_0 \
                    | DMA_SxCR_CIRC | DMA_SxCR_DBM \
                    | DMA_SxCR_TCIE | DMA_SxCR_TEIE | DMA_SxCR_DMEIE)
#define ADC_DMA_CLEAR (DMA_LIFCR_CFEIF0 | DMA_LIFCR_CDMEIF0 \
                       | DMA_LIFCR_CTEIF0 | DMA_LIFCR_CHTIF0 \
                       | DMA_LIFCR_CTCIF0)
#define ADC_DMA_ERRORS (DMA_LISR_FEIF0 | DMA_LISR_DMEIF0 | DMA_LISR_TEIF0)

static void
adc_dma_disable(void)
{
    DMA2_Stream0->CR &= ~DMA_SxCR_EN;
    while (DMA2_Stream0->CR & DMA_SxCR_EN)
        ;
}

void
DMA2_Stream0_IRQHandler(void)
{
    uint32_t lisr = DMA2->LISR;
    DMA2->LIFCR = ADC_DMA_CLEAR;
    uint32_t status = lisr & ADC_DMA_ERRORS ? ACQ_STATUS_DMA_ERROR : 0;
    if (ADC1->SR & ADC_SR_OVR) {
        ADC1->SR = ~ADC_SR_OVR;
        status |= ACQ_STATUS_OVERRUN;
    }
    if (!(lisr & DMA_LISR_TCIF0)) {
        if (status) {
            adc_dma_disable();
            adc_stream_backend_fault(status);
        }
        return;
    }
    // CT names the buffer now being filled; the opposite buffer completed.
    uint8_t completed = DMA2_Stream0->CR & DMA_SxCR_CT ? 0 : 1;
    adc_stream_block_complete(completed, status);
}

void
board_adc_stream_setup(const struct adc_stream_backend_config *cfg,
                       struct adc_stream_backend_info *info)
{
    if (dma_claim(DMA_RESOURCE_ADC1, 0, cfg->owner)
        || dma_claim(DMA_RESOURCE_TIM3, 0, cfg->owner)
        || dma_claim(DMA_RESOURCE_DMA2_STREAM0, 0, cfg->owner))
        shutdown("STM32F4/F7 ADC stream resource conflict");
    if (ADC1->SR & ADC_SR_STRT)
        shutdown("STM32F4/F7 ADC1 is busy");
    if (DMA2_Stream0->CR & DMA_SxCR_EN)
        shutdown("STM32F4/F7 DMA2 Stream0 is busy");
    if (is_enabled_pclock(TIM3_BASE))
        shutdown("TIM3 already claimed");

    uint32_t sequence = (cfg->channel_count - 1) << ADC_SQR1_L_Pos;
    uint32_t sqr3 = 0;
    static const uint8_t positions[] = {
        ADC_SQR3_SQ1_Pos, ADC_SQR3_SQ2_Pos,
        ADC_SQR3_SQ3_Pos, ADC_SQR3_SQ4_Pos,
    };
    for (uint8_t i = 0; i < cfg->channel_count; i++) {
        if (cfg->pins[i].adc != ADC1 || cfg->pins[i].chan > 18)
            shutdown("STM32F4/F7 stream requires ADC1 channels");
        sqr3 |= cfg->pins[i].chan << positions[i];
    }

    uint32_t pclk = get_pclock_frequency(TIM3_BASE);
    uint32_t timer_hz = pclk * 2u;
    uint32_t ticks_per_count = CONFIG_CLOCK_FREQ / timer_hz;
    if (!ticks_per_count)
        ticks_per_count = 1;
    uint32_t timer_ticks = (cfg->requested_period_ticks
                            + ticks_per_count / 2) / ticks_per_count;
    uint32_t divisor = (timer_ticks + 65535u) / 65536u;
    if (!divisor || divisor > 65536u)
        shutdown("STM32F4/F7 ADC stream period out of range");
    uint32_t counts = (timer_ticks + divisor / 2) / divisor;
    if (!counts || counts > 65536u)
        shutdown("STM32F4/F7 ADC timer out of range");
    uint32_t actual_period = ticks_per_count * divisor * counts;

    enable_pclock(DMA2_BASE);
    enable_pclock(TIM3_BASE);
    TIM3->CR1 = 0;
    TIM3->PSC = divisor - 1;
    TIM3->ARR = counts - 1;
    TIM3->CR2 = TIM_CR2_MMS_1; // update event is TRGO
    TIM3->EGR = TIM_EGR_UG;
    TIM3->SR = 0;

    adc_dma_disable();
    DMA2->LIFCR = ADC_DMA_CLEAR;
    DMA2_Stream0->PAR = (uint32_t)&ADC1->DR;
    DMA2_Stream0->M0AR = (uint32_t)&cfg->buffer[0];
    DMA2_Stream0->M1AR = (uint32_t)&cfg->buffer[
        ADC_STREAM_MAX_BLOCK_VALUES];
    DMA2_Stream0->NDTR = cfg->block_values;
    DMA2_Stream0->FCR = 0;
    DMA2_Stream0->CR = ADC_DMA_CR;
    armcm_enable_irq(DMA2_Stream0_IRQHandler, DMA2_Stream0_IRQn, 1);

    ADC1->SQR1 = sequence;
    ADC1->SQR2 = 0;
    ADC1->SQR3 = sqr3;
    ADC1->SR = 0;
    ADC1->CR1 = cfg->channel_count > 1 ? ADC_CR1_SCAN : 0;
    // EXTSEL=8 is TIM3_TRGO on the F4/F7 regular conversion table.
    ADC1->CR2 = ADC_CR2_ADON | ADC_CR2_DMA | ADC_CR2_DDS
                | (8u << ADC_CR2_EXTSEL_Pos) | ADC_CR2_EXTEN_0;

    info->period_numerator = actual_period;
    info->period_denominator = 1;
    info->uncertainty_ticks = 0;
    info->status = 0;
}

void
board_adc_stream_start(void)
{
    DMA2_Stream0->CR |= DMA_SxCR_EN;
    TIM3->CNT = 0;
    TIM3->SR = 0;
    TIM3->EGR = TIM_EGR_UG;
    TIM3->SR = 0;
    TIM3->CR1 = TIM_CR1_CEN;
}

void
board_adc_stream_stop_from_isr(void)
{
    TIM3->CR1 = 0;
    adc_dma_disable();
}

void
board_adc_stream_stop(void)
{
    board_adc_stream_stop_from_isr();
    ADC1->CR2 &= ~(ADC_CR2_DMA | ADC_CR2_DDS | ADC_CR2_EXTEN);
}

void
board_adc_stream_block_released(uint8_t block_index)
{
    // Native double-buffer mode keeps both fixed addresses programmed. The
    // generic ownership layer stops the stream before a non-free half wraps.
}
