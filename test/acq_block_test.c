#include <assert.h>
#include <stdint.h>
#include <stdio.h>

#include "src/generic/acq_block.h"

int
main(void)
{
    uint16_t samples[8] = { 0 };
    struct acq_block b;
    acq_block_init(&b, samples);
    assert(b.state == ACQ_BLOCK_FREE && b.data == samples);

    assert(!acq_block_dma_take(&b));
    assert(acq_block_dma_take(&b) < 0);
    samples[0] = 1234;
    assert(!acq_block_publish(&b, 7, 2, 8, 1000, 48, 1, 3,
                              ACQ_STATUS_INFERRED_TIME));
    assert(b.state == ACQ_BLOCK_READY && b.sequence == 7);

    uint16_t generation;
    assert(!acq_block_consume(&b, &generation));
    assert(samples[0] == 1234 && b.item_count == 8);
    assert(acq_block_release(&b, generation + 1) < 0);
    assert(!acq_block_release(&b, generation));
    assert(b.state == ACQ_BLOCK_FREE && b.generation == 1);

    // Invalid transitions and a zero period denominator fail closed.
    assert(acq_block_consume(&b, &generation) < 0);
    assert(!acq_block_dma_take(&b));
    assert(acq_block_publish(&b, 8, 2, 8, 2000, 48, 0, 0, 0) < 0);

    puts("PASS: acquisition block ownership and stale release detection");
    return 0;
}
