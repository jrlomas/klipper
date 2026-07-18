#ifndef __GENERIC_DMA_RESOURCE_H
#define __GENERIC_DMA_RESOURCE_H

#include <stdint.h>

// Fixed configuration-time DMA storage capabilities.  These describe the
// allocation's role and required memory properties; they never expose an
// arbitrary host-controlled DMA address.
enum dma_pool_capability {
    DMA_POOL_BUFFER = 1u << 0,
    DMA_POOL_DESCRIPTOR = 1u << 1,
    DMA_POOL_DMA_REACHABLE = 1u << 2,
    DMA_POOL_NONCACHEABLE = 1u << 3,
};

// Globally unique compiled resource identifiers.  Backends claim every
// peripheral, timer, DMA channel/stream, and request line that they consume.
enum dma_resource_endpoint {
    DMA_RESOURCE_ADC1 = 1,
    DMA_RESOURCE_TIM3 = 2,
    DMA_RESOURCE_DMA1_CHANNEL1 = 3,
    DMA_RESOURCE_DMA1_STREAM0 = 4,
    DMA_RESOURCE_DMAMUX1_REQUEST0 = 5,
    DMA_RESOURCE_RP2040_ADC = 16,
    DMA_RESOURCE_RP2040_DMA10 = 17,
    DMA_RESOURCE_RP2040_DMA11 = 18,
    DMA_RESOURCE_ESP32_ADC1 = 32,
    DMA_RESOURCE_ESP32_I2S0 = 33,
    DMA_RESOURCE_ESP32_ADC_POOL = 34,
    DMA_RESOURCE_ETH_MAC = 48,
    DMA_RESOURCE_ETH_DMA = 49,
};

struct dma_pool_status {
    uint16_t size;
    uint16_t used;
    uint16_t highwater;
    uint8_t allocations;
    uint8_t claims;
};

void *dma_pool_alloc(uint16_t size, uint16_t alignment,
                     uint8_t capabilities, uint8_t owner);
int dma_claim(uint16_t endpoint, uint16_t request, uint8_t owner);
int dma_release(uint16_t endpoint, uint16_t request, uint8_t owner);
void dma_pool_get_status(struct dma_pool_status *status);

// Test-only reset.  Production firmware allocates during configuration and
// retains resources for the life of that configuration.
void dma_resource_reset_for_test(void);

#endif // generic/dma_resource.h
