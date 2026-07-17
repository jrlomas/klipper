#ifndef __GENERIC_ACQ_BLOCK_H
#define __GENERIC_ACQ_BLOCK_H

#include <stdint.h>

// Shared ownership and metadata for DMA-backed acquisition blocks.  The
// peripheral backend owns DMA_OWNED -> READY; the consumer owns READY ->
// CONSUMER_OWNED -> FREE.  Callers must provide the IRQ exclusion required by
// their producer/consumer relationship.
enum acq_block_state {
    ACQ_BLOCK_FREE = 0,
    ACQ_BLOCK_DMA_OWNED,
    ACQ_BLOCK_READY,
    ACQ_BLOCK_CONSUMER_OWNED,
};

enum acq_block_status {
    ACQ_STATUS_DISCONTINUITY = 1u << 0,
    ACQ_STATUS_DMA_ERROR = 1u << 1,
    ACQ_STATUS_PERIPHERAL_ERROR = 1u << 2,
    ACQ_STATUS_SAMPLE_ERROR = 1u << 3,
    ACQ_STATUS_OVERRUN = 1u << 4,
    ACQ_STATUS_INFERRED_TIME = 1u << 5,
};

struct acq_block {
    void *data;
    uint32_t sequence;
    uint32_t epoch;
    uint32_t item_count;
    uint32_t first_machine_clock;
    uint32_t period_numerator;
    uint32_t period_denominator;
    uint32_t uncertainty_ticks;
    uint32_t status;
    uint16_t generation;
    volatile uint8_t state;
};

void acq_block_init(struct acq_block *block, void *data);
int acq_block_dma_take(struct acq_block *block);
int acq_block_publish(struct acq_block *block, uint32_t sequence,
                      uint32_t epoch, uint32_t item_count,
                      uint32_t first_machine_clock,
                      uint32_t period_numerator,
                      uint32_t period_denominator,
                      uint32_t uncertainty_ticks, uint32_t status);
int acq_block_consume(struct acq_block *block, uint16_t *generation);
int acq_block_release(struct acq_block *block, uint16_t generation);

#endif // generic/acq_block.h
