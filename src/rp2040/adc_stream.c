// RP2040 ADC FIFO to SRAM acquisition using chained DMA channels.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "adc_stream.h"
#include "board/armcm_boot.h" // armcm_enable_irq
#include "command.h" // shutdown
#include "generic/acq_block.h" // ACQ_STATUS_*
#include "generic/dma_resource.h"
#include "hardware/regs/adc.h"
#include "hardware/regs/dma.h"
#include "hardware/regs/dreq.h"
#include "hardware/structs/adc.h"
#include "hardware/structs/dma.h"
#include "hardware/structs/resets.h"
#include "internal.h" // enable_pclock
#include "sched.h" // sched_shutdown

#define ADC_DMA_CH0 10
#define ADC_DMA_CH1 11
#define ADC_DMA_MASK ((1u << ADC_DMA_CH0) | (1u << ADC_DMA_CH1))

static struct adc_stream_backend_config stream_cfg;
static uint8_t stream_active;
static uint32_t dma_ctrl[2];

static void
adc_dma_rearm(uint8_t block)
{
    uint8_t channel = block ? ADC_DMA_CH1 : ADC_DMA_CH0;
    dma_channel_hw_t *dma = &dma_hw->ch[channel];
    dma->read_addr = (uint32_t)&adc_hw->fifo;
    dma->write_addr = (uint32_t)&stream_cfg.buffer[
        block * stream_cfg.block_values];
    dma->transfer_count = stream_cfg.block_values;
    // AL1_CTRL is a non-triggering alias. Keep the channel armed until the
    // peer's CHAIN_TO event starts it.
    dma->al1_ctrl = dma_ctrl[block];
}

void
DMA_IRQ_1_Handler(void)
{
    uint32_t pending = dma_hw->ints1 & ADC_DMA_MASK;
    dma_hw->ints1 = pending;
    if (pending & (1u << ADC_DMA_CH0)) {
        uint32_t status = dma_hw->ch[ADC_DMA_CH0].ctrl_trig
                          & DMA_CH0_CTRL_TRIG_AHB_ERROR_BITS
                          ? ACQ_STATUS_DMA_ERROR : 0;
        if (adc_hw->fcs & ADC_FCS_OVER_BITS)
            status |= ACQ_STATUS_OVERRUN;
        adc_stream_block_complete(0, status);
    }
    if (pending & (1u << ADC_DMA_CH1)) {
        uint32_t status = dma_hw->ch[ADC_DMA_CH1].ctrl_trig
                          & DMA_CH0_CTRL_TRIG_AHB_ERROR_BITS
                          ? ACQ_STATUS_DMA_ERROR : 0;
        if (adc_hw->fcs & ADC_FCS_OVER_BITS)
            status |= ACQ_STATUS_OVERRUN;
        adc_stream_block_complete(1, status);
    }
}

void
board_adc_stream_setup(const struct adc_stream_backend_config *cfg,
                       struct adc_stream_backend_info *info)
{
    if (cfg->hardware_oversample != 1 || cfg->hardware_shift)
        shutdown("RP2040 ADC lacks hardware oversampling");
    if (dma_claim(DMA_RESOURCE_RP2040_ADC, 0, cfg->owner)
        || dma_claim(DMA_RESOURCE_RP2040_DMA10, DREQ_ADC, cfg->owner)
        || dma_claim(DMA_RESOURCE_RP2040_DMA11, DREQ_ADC, cfg->owner))
        shutdown("RP2040 ADC stream resource conflict");
    if (cfg->channel_count > 4)
        shutdown("RP2040 ADC stream channel limit");
    uint32_t mask = 0;
    uint8_t first = cfg->pins[0].chan;
    for (uint8_t i = 0; i < cfg->channel_count; i++) {
        uint8_t chan = cfg->pins[i].chan;
        if (chan > 4 || (i && chan <= cfg->pins[i - 1].chan))
            shutdown("RP2040 ADC stream channels must ascend");
        mask |= 1u << chan;
    }

    // clk_adc is 48MHz and the machine timer is 12MHz. ADC_DIV represents
    // (period_adc_clocks - 1) in unsigned 16.8 fixed point.
    uint64_t scan_period_fp = (uint64_t)cfg->requested_period_ticks * 4 * 256;
    uint64_t adc_period_fp = scan_period_fp / cfg->channel_count;
    if (adc_period_fp < 96u * 256u || adc_period_fp > 0x1000000u)
        shutdown("RP2040 ADC stream period out of range");
    uint32_t divider = adc_period_fp - 256;

    if (!is_enabled_pclock(RESETS_RESET_DMA_BITS))
        enable_pclock(RESETS_RESET_DMA_BITS);
    if ((dma_hw->ch[ADC_DMA_CH0].ctrl_trig
         | dma_hw->ch[ADC_DMA_CH1].ctrl_trig)
        & DMA_CH0_CTRL_TRIG_BUSY_BITS)
        shutdown("RP2040 ADC DMA channels busy");
    dma_hw->abort = ADC_DMA_MASK;
    dma_hw->inte1 &= ~ADC_DMA_MASK;
    dma_hw->ints1 = ADC_DMA_MASK;

    stream_cfg = *cfg;
    stream_active = 1;
    for (uint8_t block = 0; block < 2; block++) {
        uint8_t channel = block ? ADC_DMA_CH1 : ADC_DMA_CH0;
        uint8_t chain = block ? ADC_DMA_CH0 : ADC_DMA_CH1;
        dma_ctrl[block] = DMA_CH0_CTRL_TRIG_EN_BITS
            | (DREQ_ADC << DMA_CH0_CTRL_TRIG_TREQ_SEL_LSB)
            | (chain << DMA_CH0_CTRL_TRIG_CHAIN_TO_LSB)
            | DMA_CH0_CTRL_TRIG_INCR_WRITE_BITS
            | (DMA_CH0_CTRL_TRIG_DATA_SIZE_VALUE_SIZE_HALFWORD
               << DMA_CH0_CTRL_TRIG_DATA_SIZE_LSB);
        dma_hw->ch[channel].ctrl_trig = 0;
        adc_dma_rearm(block);
    }
    dma_hw->inte1 |= ADC_DMA_MASK;
    armcm_enable_irq(DMA_IRQ_1_Handler, DMA_IRQ_1_IRQn, 1);

    // Flush stale FIFO entries and configure per-sample error tagging. The
    // channel identity remains the documented ascending round-robin pattern.
    adc_hw->cs &= ~ADC_CS_START_MANY_BITS;
    adc_hw->fcs = 0;
    while (!(adc_hw->fcs & ADC_FCS_EMPTY_BITS))
        (void)adc_hw->fifo;
    adc_hw->div = divider;
    adc_hw->cs = (adc_hw->cs & ADC_CS_TS_EN_BITS) | ADC_CS_EN_BITS
                 | (first << ADC_CS_AINSEL_LSB)
                 | (cfg->channel_count > 1 ? mask << ADC_CS_RROBIN_LSB : 0);
    adc_hw->fcs = (1u << ADC_FCS_THRESH_LSB) | ADC_FCS_DREQ_EN_BITS
                  | ADC_FCS_ERR_BITS | ADC_FCS_EN_BITS;

    info->period_numerator = adc_period_fp * cfg->channel_count;
    info->period_denominator = 1024; // 256 fractional steps * 4 ADC clocks/tick
    info->uncertainty_ticks = 24; // conversion aperture/start inferred, <=2us
    info->status = ACQ_STATUS_INFERRED_TIME;
    info->max_conversion_rate = 500000;
    info->capabilities = ADC_BACKEND_CAP_HARDWARE_PACED
                         | ADC_BACKEND_CAP_INFERRED_START;
    info->max_hardware_oversample = 1;
    info->resolution_bits = 12;
    info->adc_count = 1;
    info->watchdog_count = 0;
    info->timing_quality = 0;
}

void
board_adc_stream_start(void)
{
    dma_hw->multi_channel_trigger = 1u << ADC_DMA_CH0;
    adc_hw->cs |= ADC_CS_START_MANY_BITS;
}

void
board_adc_stream_stop_from_isr(void)
{
    adc_hw->cs &= ~ADC_CS_START_MANY_BITS;
    dma_hw->abort = ADC_DMA_MASK;
    dma_hw->inte1 &= ~ADC_DMA_MASK;
}

void
board_adc_stream_stop(void)
{
    board_adc_stream_stop_from_isr();
    stream_active = 0;
}

void
board_adc_stream_block_released(uint8_t block_index)
{
    // Re-arm only after the consumer has released the matching generation.
    // If the peer DMA completes first, its CHAIN_TO event finds this channel
    // disabled and the generic layer reports ring exhaustion instead of
    // allowing a silent overwrite.
    if (stream_active)
        adc_dma_rearm(block_index);
}
