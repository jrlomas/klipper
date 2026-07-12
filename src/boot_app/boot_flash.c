// Portable per-target flash geometry constructors (RFC 0001 doc 11).
//
// No register access — pure layout data, compiled for both the arm
// bootloader and the host FlashOps tests. Budgets follow doc 11's
// fleet table: F072 <= 16 KB bootloader, G0B1 <= 16 KB (dual-bank),
// F4 <= 32 KB with the app on a sector boundary.

#include <string.h>
#include "boot_flash.h"

// ---- STM32F072: 128 KB flash, uniform 2 KB pages ----
// 16 KB bootloader; last page reserved for the validity record.
struct boot_flash_geom
boot_geom_stm32f072(void)
{
    struct boot_flash_geom g;
    memset(&g, 0, sizeof(g));
    g.flash_base = 0x08000000UL;
    g.page_size = 0x800;                 // 2 KB
    g.app_base = g.flash_base + 0x4000;  // 16 KB bootloader
    uint32_t flash_size = 0x20000;       // 128 KB
    g.info_size = g.page_size;
    g.info_addr = g.flash_base + flash_size - g.info_size; // last page
    g.app_size = g.info_addr - g.app_base;
    return g;
}

// ---- STM32G0B1: up to 512 KB, uniform 2 KB pages, dual-bank ----
// The 512 KB part is two 256 KB banks. The single-bank flow below is
// the baseline every G0 supports; a dual-bank build can instead write
// the inactive bank and swap FLASH_CR.BKER atomically (see driver).
struct boot_flash_geom
boot_geom_stm32g0b1(void)
{
    struct boot_flash_geom g;
    memset(&g, 0, sizeof(g));
    g.flash_base = 0x08000000UL;
    g.page_size = 0x800;                 // 2 KB
    g.app_base = g.flash_base + 0x4000;  // 16 KB bootloader
    uint32_t flash_size = 0x80000;       // 512 KB
    g.dual_bank = 1;
    g.bank_size = flash_size / 2;        // 256 KB per bank
    g.info_size = g.page_size;
    g.info_addr = g.flash_base + flash_size - g.info_size;
    g.app_size = g.info_addr - g.app_base;
    return g;
}

// ---- STM32F4 (Octopus-class): 512 KB, variable sectors ----
// Sectors: 16K x4, 64K, 128K x3 (F407VG, 512 KB). A 32 KB bootloader
// occupies sectors 0-1, so the app starts at sector 2 (0x08008000),
// a sector boundary as doc 11 requires. The last 128 KB sector holds
// the validity record.
static const uint32_t f4_sect_bounds[] = {
    0x08008000UL, // sector 2  (16 KB)
    0x0800C000UL, // sector 3  (16 KB)
    0x08010000UL, // sector 4  (64 KB)
    0x08020000UL, // sector 5  (128 KB)
    0x08040000UL, // sector 6  (128 KB)
    // sector 7 (0x08060000, 128 KB) is the info page — excluded here
    // so bootcore never erases it as part of the image.
};

struct boot_flash_geom
boot_geom_stm32f4(void)
{
    struct boot_flash_geom g;
    memset(&g, 0, sizeof(g));
    g.flash_base = 0x08000000UL;
    g.page_size = 0;                     // variable sectors
    g.sect_bounds = f4_sect_bounds;
    g.sect_bounds_len = sizeof(f4_sect_bounds) / sizeof(f4_sect_bounds[0]);
    g.app_base = 0x08008000UL;           // sector 2 boundary
    g.info_size = 0x20000;               // sector 7 (128 KB)
    g.info_addr = 0x08060000UL;          // sector 7 start
    g.app_size = g.info_addr - g.app_base;
    return g;
}
