// Non-cacheable MPU arena for DMA descriptors and payload buffers on M7.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "autoconf.h" // CONFIG_DMA_POOL_SIZE
#include "internal.h" // CMSIS core definitions
#include "stm32/dma_mpu.h"

#if CONFIG_DMA_POOL_SIZE < 32 \
    || (CONFIG_DMA_POOL_SIZE & (CONFIG_DMA_POOL_SIZE - 1))
#error "DMA_POOL_SIZE must be a power of two of at least 32 bytes"
#endif

extern uint8_t _dma_buffer_start[], _dma_buffer_end[];

void
stm32_dma_mpu_init(void)
{
    uint32_t base = (uint32_t)_dma_buffer_start;
    uint32_t size = (uint32_t)(_dma_buffer_end - _dma_buffer_start);
    if (size > CONFIG_DMA_POOL_SIZE || base & (CONFIG_DMA_POOL_SIZE - 1))
        for (;;)
            ;

    // Region 7: privileged/unprivileged read-write, execute-never, shareable
    // normal non-cacheable memory (TEX=1, C=0, B=0).  Keep the default map
    // enabled for all memory outside this narrowly scoped DMA arena.
    __DMB();
    MPU->CTRL = 0;
    MPU->RNR = 7;
    MPU->RBAR = base;
    uint32_t log2_size = 31u - __builtin_clz(CONFIG_DMA_POOL_SIZE);
    MPU->RASR = MPU_RASR_XN_Msk
        | (3u << MPU_RASR_AP_Pos)
        | (1u << MPU_RASR_TEX_Pos)
        | MPU_RASR_S_Msk
        | ((log2_size - 1u) << MPU_RASR_SIZE_Pos)
        | MPU_RASR_ENABLE_Msk;
    MPU->CTRL = MPU_CTRL_PRIVDEFENA_Msk | MPU_CTRL_ENABLE_Msk;
    __DSB();
    __ISB();
}
