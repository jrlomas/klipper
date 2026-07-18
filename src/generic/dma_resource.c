// Fixed-lifetime DMA memory and resource ownership.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <string.h> // memset
#ifndef CONFIG_DMA_POOL_SIZE
#include "autoconf.h" // CONFIG_DMA_POOL_SIZE
#endif
#include "compiler.h" // __aligned, __section
#include "generic/dma_resource.h"
#ifdef CONFIG_MACH_ESP32
#include "esp_attr.h" // DMA_ATTR
#include "esp_memory_utils.h" // esp_ptr_dma_capable
#endif

#define DMA_MAX_ALLOCATIONS 12
#define DMA_MAX_CLAIMS 16

struct dma_allocation {
    uint16_t offset;
    uint16_t size;
    uint8_t alignment;
    uint8_t capabilities;
    uint8_t owner;
};

struct dma_claim_record {
    uint16_t endpoint;
    uint16_t request;
    uint8_t owner;
};

// The linker gives this section a power-of-two-aligned, fixed extent.  M7
// targets map it non-cacheable with one MPU region before enabling D-cache.
#ifdef CONFIG_MACH_ESP32
// ESP-IDF's default orphan-section placement maps an otherwise unknown
// read/write section into flash DROM.  Use the IDF's DMA attribute so this
// arena is guaranteed to reside in byte-addressable internal DRAM (and never
// PSRAM or flash) before handing any pointer to a peripheral driver.
static uint8_t dma_pool[CONFIG_DMA_POOL_SIZE]
    DMA_ATTR __aligned(CONFIG_DMA_POOL_SIZE);
#else
static uint8_t dma_pool[CONFIG_DMA_POOL_SIZE]
    __section(".dma_buffer") __aligned(CONFIG_DMA_POOL_SIZE);
#endif
static struct dma_allocation allocations[DMA_MAX_ALLOCATIONS];
static struct dma_claim_record claims[DMA_MAX_CLAIMS];
static uint16_t pool_used, pool_highwater;
static uint8_t allocation_count, claim_count;

static int
is_power_of_two(uint16_t value)
{
    return value && !(value & (value - 1));
}

void *
dma_pool_alloc(uint16_t size, uint16_t alignment,
               uint8_t capabilities, uint8_t owner)
{
    if (!size || !is_power_of_two(alignment)
        || alignment > CONFIG_DMA_POOL_SIZE
        || allocation_count >= DMA_MAX_ALLOCATIONS)
        return NULL;
    uint16_t offset = (pool_used + alignment - 1) & ~(alignment - 1);
    if ((uint32_t)offset + size > CONFIG_DMA_POOL_SIZE)
        return NULL;
    void *buffer = &dma_pool[offset];
#ifdef CONFIG_MACH_ESP32
    // Keep a runtime assertion at the allocation boundary as well as the
    // linker attribute.  This makes a future IDF linker-layout change fail
    // closed during configuration instead of giving a peripheral a DROM or
    // PSRAM pointer.
    if (!esp_ptr_dma_capable(buffer))
        return NULL;
#endif
    struct dma_allocation *allocation = &allocations[allocation_count++];
    allocation->offset = offset;
    allocation->size = size;
    allocation->alignment = alignment;
    allocation->capabilities = capabilities;
    allocation->owner = owner;
    pool_used = offset + size;
    if (pool_used > pool_highwater)
        pool_highwater = pool_used;
    memset(buffer, 0, size);
    return buffer;
}

int
dma_claim(uint16_t endpoint, uint16_t request, uint8_t owner)
{
    if (!endpoint)
        return -1;
    for (uint8_t i = 0; i < claim_count; i++) {
        struct dma_claim_record *claim = &claims[i];
        if (claim->endpoint != endpoint)
            continue;
        if (claim->request == request && claim->owner == owner)
            return 0;
        return -1;
    }
    if (claim_count >= DMA_MAX_CLAIMS)
        return -1;
    claims[claim_count++] = (struct dma_claim_record) {
        .endpoint = endpoint, .request = request, .owner = owner,
    };
    return 0;
}

int
dma_release(uint16_t endpoint, uint16_t request, uint8_t owner)
{
    for (uint8_t i = 0; i < claim_count; i++) {
        struct dma_claim_record *claim = &claims[i];
        if (claim->endpoint != endpoint || claim->request != request
            || claim->owner != owner)
            continue;
        claims[i] = claims[--claim_count];
        return 0;
    }
    return -1;
}

void
dma_pool_get_status(struct dma_pool_status *status)
{
    *status = (struct dma_pool_status) {
        .size = CONFIG_DMA_POOL_SIZE,
        .used = pool_used,
        .highwater = pool_highwater,
        .allocations = allocation_count,
        .claims = claim_count,
    };
}

void
dma_resource_reset_for_test(void)
{
    memset(dma_pool, 0, sizeof(dma_pool));
    memset(allocations, 0, sizeof(allocations));
    memset(claims, 0, sizeof(claims));
    pool_used = pool_highwater = 0;
    allocation_count = claim_count = 0;
}
