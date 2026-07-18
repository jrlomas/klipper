#include <assert.h>
#include <stdint.h>
#include <stdio.h>

#include "src/generic/dma_resource.h"

int
main(void)
{
    dma_resource_reset_for_test();
    void *first = dma_pool_alloc(31, 4, DMA_POOL_BUFFER, 1);
    void *second = dma_pool_alloc(64, 32,
                                  DMA_POOL_DESCRIPTOR
                                  | DMA_POOL_DMA_REACHABLE, 2);
    assert(first && second);
    assert(!((uintptr_t)first & 3));
    assert(!((uintptr_t)second & 31));
    assert(second > first);

    struct dma_pool_status status;
    dma_pool_get_status(&status);
    assert(status.allocations == 2);
    assert(status.used == status.highwater);
    assert(status.used >= 96);

    assert(!dma_claim(DMA_RESOURCE_ADC1, 5, 7));
    assert(!dma_claim(DMA_RESOURCE_ADC1, 5, 7));
    assert(dma_claim(DMA_RESOURCE_ADC1, 5, 8));
    assert(dma_claim(DMA_RESOURCE_ADC1, 6, 7));
    assert(!dma_claim(DMA_RESOURCE_TIM3, 0, 8));
    assert(dma_release(DMA_RESOURCE_TIM3, 0, 7));
    assert(!dma_release(DMA_RESOURCE_TIM3, 0, 8));
    assert(!dma_release(DMA_RESOURCE_ADC1, 5, 7));
    assert(!dma_claim(DMA_RESOURCE_ADC1, 6, 8));

    dma_pool_get_status(&status);
    assert(status.claims == 1);
    puts("PASS: DMA pool alignment and exclusive resource claims");
    return 0;
}
