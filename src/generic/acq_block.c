// Ownership state machine for DMA-backed acquisition blocks.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "compiler.h" // barrier
#include "generic/acq_block.h"

void
acq_block_init(struct acq_block *block, void *data)
{
    block->data = data;
    block->sequence = block->epoch = block->item_count = 0;
    block->first_machine_clock = 0;
    block->period_numerator = 0;
    block->period_denominator = 1;
    block->uncertainty_ticks = block->status = 0;
    block->generation = 0;
    barrier();
    block->state = ACQ_BLOCK_FREE;
}

int
acq_block_dma_take(struct acq_block *block)
{
    if (block->state != ACQ_BLOCK_FREE)
        return -1;
    block->status = 0;
    barrier();
    block->state = ACQ_BLOCK_DMA_OWNED;
    return 0;
}

int
acq_block_publish(struct acq_block *block, uint32_t sequence,
                  uint32_t epoch, uint32_t item_count,
                  uint32_t first_machine_clock, uint32_t period_numerator,
                  uint32_t period_denominator, uint32_t uncertainty_ticks,
                  uint32_t status)
{
    if (block->state != ACQ_BLOCK_DMA_OWNED || !period_denominator)
        return -1;
    block->sequence = sequence;
    block->epoch = epoch;
    block->item_count = item_count;
    block->first_machine_clock = first_machine_clock;
    block->period_numerator = period_numerator;
    block->period_denominator = period_denominator;
    block->uncertainty_ticks = uncertainty_ticks;
    block->status = status;
    // Publish metadata and DMA-written contents before READY is visible.
    barrier();
    block->state = ACQ_BLOCK_READY;
    return 0;
}

int
acq_block_consume(struct acq_block *block, uint16_t *generation)
{
    if (block->state != ACQ_BLOCK_READY)
        return -1;
    // Acquire metadata and DMA-written contents after observing READY.
    barrier();
    block->state = ACQ_BLOCK_CONSUMER_OWNED;
    *generation = block->generation;
    return 0;
}

int
acq_block_release(struct acq_block *block, uint16_t generation)
{
    if (block->state != ACQ_BLOCK_CONSUMER_OWNED
        || block->generation != generation)
        return -1;
    block->generation++;
    barrier();
    block->state = ACQ_BLOCK_FREE;
    return 0;
}
