// Cache-coherent timer-triggered ADC1 acquisition for STM32H723.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "adc_stream.h"
#include "board/armcm_boot.h" // armcm_enable_irq
#include "command.h" // shutdown
#include "generic/acq_block.h" // ACQ_STATUS_*
#include "internal.h" // peripheral registers
#include "sched.h" // sched_shutdown

#define ADC_DMA_CR (DMA_SxCR_MINC | DMA_SxCR_PSIZE_0 | DMA_SxCR_MSIZE_0 \
                    | DMA_SxCR_TCIE | DMA_SxCR_TEIE | DMA_SxCR_DMEIE)
#define ADC_DMA_CLEAR (DMA_LIFCR_CFEIF0 | DMA_LIFCR_CDMEIF0 \
                       | DMA_LIFCR_CTEIF0 | DMA_LIFCR_CHTIF0 \
                       | DMA_LIFCR_CTCIF0)
#define ADC_DMA_ERRORS (DMA_LISR_FEIF0 | DMA_LISR_DMEIF0 | DMA_LISR_TEIF0)

static struct adc_stream_backend_config stream_cfg;
static uint8_t dma_block;
static uint8_t owns_tim3;

static void
adc_dma_disable(void)
{
    DMA1_Stream0->CR &= ~DMA_SxCR_EN;
    while (DMA1_Stream0->CR & DMA_SxCR_EN)
        ;
}

static void
adc_dma_arm(uint8_t block)
{
    adc_dma_disable();
    DMA1->LIFCR = ADC_DMA_CLEAR;
    uint16_t *destination = &stream_cfg.buffer[
        block * ADC_STREAM_MAX_BLOCK_VALUES];
    // Each block occupies exactly one aligned M7 cache line. No CPU writes are
    // permitted while DMA owns it, so invalidating cannot discard user data.
    SCB_InvalidateDCache_by_Addr(destination,
                                ADC_STREAM_MAX_BLOCK_VALUES * sizeof(uint16_t));
    DMA1_Stream0->PAR = (uint32_t)&ADC1->DR;
    DMA1_Stream0->M0AR = (uint32_t)destination;
    DMA1_Stream0->NDTR = stream_cfg.block_values;
    DMA1_Stream0->CR = ADC_DMA_CR | DMA_SxCR_EN;
}

void
DMA1_Stream0_IRQHandler(void)
{
    uint32_t lisr = DMA1->LISR;
    uint32_t status = lisr & ADC_DMA_ERRORS ? ACQ_STATUS_DMA_ERROR : 0;
    adc_dma_disable();
    DMA1->LIFCR = ADC_DMA_CLEAR;
    uint8_t completed = dma_block;
    uint16_t *completed_data = &stream_cfg.buffer[
        completed * ADC_STREAM_MAX_BLOCK_VALUES];
    SCB_InvalidateDCache_by_Addr(completed_data,
                                ADC_STREAM_MAX_BLOCK_VALUES * sizeof(uint16_t));
    if (ADC1->ISR & ADC_ISR_OVR) {
        ADC1->ISR = ADC_ISR_OVR;
        status |= ACQ_STATUS_OVERRUN;
    }
    if (adc_stream_block_complete(completed, status))
        return;
    dma_block = completed ^ 1;
    adc_dma_arm(dma_block);
}

void
board_adc_stream_setup(const struct adc_stream_backend_config *cfg,
                       struct adc_stream_backend_info *info)
{
    uint32_t sequence = cfg->channel_count - 1;
    static const uint8_t positions[] = {
        ADC_SQR1_SQ1_Pos, ADC_SQR1_SQ2_Pos,
        ADC_SQR1_SQ3_Pos, ADC_SQR1_SQ4_Pos,
    };
    for (uint8_t i = 0; i < cfg->channel_count; i++) {
        if (cfg->pins[i].adc != ADC1 || cfg->pins[i].chan > 19)
            shutdown("STM32H7 stream requires ADC1 channels");
        sequence |= cfg->pins[i].chan << positions[i];
    }
    if (ADC1->CR & ADC_CR_ADSTART)
        shutdown("STM32H7 ADC1 is busy");
    if (!owns_tim3 && is_enabled_pclock(TIM3_BASE))
        shutdown("TIM3 already claimed");
    if (cfg->requested_period_ticks < CONFIG_CLOCK_FREQ / 10000u)
        shutdown("STM32H7 ADC stream period below 100us");

    uint32_t pclk = get_pclock_frequency(TIM3_BASE);
    uint32_t ticks_per_count = CONFIG_CLOCK_FREQ / pclk;
    if (ticks_per_count > 1)
        ticks_per_count /= 2; // APB timers receive twice the peripheral clock
    uint32_t timer_ticks = (cfg->requested_period_ticks
                            + ticks_per_count / 2) / ticks_per_count;
    uint32_t divisor = (timer_ticks + 65535u) / 65536u;
    if (!divisor || divisor > 65536u)
        shutdown("STM32H7 ADC stream period out of range");
    uint32_t counts = (timer_ticks + divisor / 2) / divisor;
    if (!counts || counts > 65536u)
        shutdown("STM32H7 ADC stream timer out of range");
    uint32_t actual_period = ticks_per_count * divisor * counts;

    RCC->AHB1ENR |= RCC_AHB1ENR_DMA1EN;
    RCC->AHB1ENR;
    DMAMUX1_Channel0->CCR = 9; // RM0468: DMA request ADC1
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

    stream_cfg = *cfg;
    dma_block = 0;
    adc_dma_arm(0);
    armcm_enable_irq(DMA1_Stream0_IRQHandler, DMA1_Stream0_IRQn, 1);

    ADC1->SQR1 = sequence;
    ADC1->ISR = ADC_ISR_OVR | ADC_ISR_EOC | ADC_ISR_EOS;
    ADC1->CFGR = (ADC1->CFGR
                  & ~(ADC_CFGR_DMNGT | ADC_CFGR_EXTSEL | ADC_CFGR_EXTEN))
                 | ADC_CFGR_DMNGT_0 | ADC_CFGR_DMNGT_1
                 | ADC_CFGR_EXTSEL_2 | ADC_CFGR_EXTEN_0; // TIM3_TRGO rising

    info->period_numerator = actual_period;
    info->period_denominator = 1;
    info->uncertainty_ticks = CONFIG_CLOCK_FREQ / 1000000u * 15u;
    info->status = ACQ_STATUS_INFERRED_TIME;
}

void
board_adc_stream_start(void)
{
    TIM3->CNT = 0;
    TIM3->SR = 0;
    ADC1->CR |= ADC_CR_ADSTART;
    // Keep the first hardware aperture aligned with the machine-clock
    // timestamp captured immediately before this backend start call.
    TIM3->EGR = TIM_EGR_UG;
    TIM3->SR = 0;
    TIM3->CR1 = TIM_CR1_CEN;
}

void
board_adc_stream_stop_from_isr(void)
{
    TIM3->CR1 = 0;
    adc_dma_disable();
    if (ADC1->CR & ADC_CR_ADSTART)
        ADC1->CR = (ADC1->CR & ~ADC_CR_ADSTART) | ADC_CR_ADSTP;
}

void
board_adc_stream_stop(void)
{
    board_adc_stream_stop_from_isr();
    ADC1->CFGR &= ~(ADC_CFGR_DMNGT | ADC_CFGR_EXTEN);
}

void
board_adc_stream_block_released(uint8_t block_index)
{
    // The completion ISR arms the sole DMA stream for the next block.
}
