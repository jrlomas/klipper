// STM32G0B1 flash driver for the first-class bootloader (RFC 0001
// doc 11). Up to 512 KB in two 256 KB banks, uniform 2 KB pages,
// 64-bit (double-word) programming.
//
// Dual-bank note: a G0B1 update could write the inactive bank and
// swap FLASH_CR.BKER atomically for background update (doc 11). This
// baseline driver implements the single-bank flow that every G0
// supports; the bank of a page is derived from the address so the
// same code programs either bank in place.
//
// Register-level programming, arm-only; see README.md.

#include "stm32g0b1xx.h" // FLASH
#include "boot_flash.h"

#define FLASH_KEY1_VAL 0x45670123UL
#define FLASH_KEY2_VAL 0xCDEF89ABUL

static struct boot_flash_geom geom;

const struct boot_flash_geom *
boot_active_geom(void)
{
    if (!geom.flash_base)
        geom = boot_geom_stm32g0b1();
    return &geom;
}

static void
flash_unlock(void)
{
    if (FLASH->CR & FLASH_CR_LOCK) {
        FLASH->KEYR = FLASH_KEY1_VAL;
        FLASH->KEYR = FLASH_KEY2_VAL;
    }
}

static void
flash_lock(void)
{
    FLASH->CR |= FLASH_CR_LOCK;
}

static int
flash_wait(void)
{
    while (FLASH->SR & FLASH_SR_BSY1)
        ;
    if (FLASH->SR & FLASH_SR_WRPERR) {
        FLASH->SR |= FLASH_SR_WRPERR | FLASH_SR_EOP;
        return -1;
    }
    FLASH->SR |= FLASH_SR_EOP;
    return 0;
}

// Erase the 2 KB page containing addr, selecting its bank.
int
boot_flash_erase(uint32_t addr)
{
    const struct boot_flash_geom *g = boot_active_geom();
    uint32_t off = addr - g->flash_base;
    uint32_t bank = (g->dual_bank && off >= g->bank_size) ? 1 : 0;
    uint32_t page = (bank ? (off - g->bank_size) : off) / g->page_size;

    flash_unlock();
    uint32_t cr = FLASH->CR & ~(FLASH_CR_PNB_Msk | FLASH_CR_BKER_Msk);
    cr |= FLASH_CR_PER | (page << FLASH_CR_PNB_Pos);
    if (bank)
        cr |= FLASH_CR_BKER;
    FLASH->CR = cr;
    FLASH->CR |= FLASH_CR_STRT;
    int rc = flash_wait();
    FLASH->CR &= ~(FLASH_CR_PER | FLASH_CR_PNB_Msk | FLASH_CR_BKER_Msk);
    flash_lock();
    return rc;
}

// Program len bytes at addr as 64-bit double words (padded with 0xff).
int
boot_flash_write(uint32_t addr, const uint8_t *data, size_t len)
{
    flash_unlock();
    FLASH->CR |= FLASH_CR_PG;
    int rc = 0;
    for (size_t i = 0; i < len; i += 8) {
        uint32_t w0 = 0xffffffffUL, w1 = 0xffffffffUL;
        for (int b = 0; b < 4 && i + b < len; b++)
            w0 = (w0 & ~(0xffUL << (8 * b))) | ((uint32_t)data[i + b] << (8 * b));
        for (int b = 0; b < 4 && i + 4 + b < len; b++)
            w1 = (w1 & ~(0xffUL << (8 * b)))
                 | ((uint32_t)data[i + 4 + b] << (8 * b));
        *(volatile uint32_t *)(uintptr_t)(addr + i) = w0;
        *(volatile uint32_t *)(uintptr_t)(addr + i + 4) = w1;
        rc = flash_wait();
        if (rc)
            break;
    }
    FLASH->CR &= ~FLASH_CR_PG;
    flash_lock();
    return rc;
}

const uint8_t *
boot_flash_read(uint32_t addr)
{
    return (const uint8_t *)(uintptr_t)addr;
}

int
boot_flash_erase_info(const struct boot_flash_geom *g)
{
    return boot_flash_erase(g->info_addr);
}

int
boot_flash_write_info(const struct boot_flash_geom *g, uint32_t size,
                      uint32_t crc)
{
    struct boot_info_record rec = {BOOT_INFO_MAGIC, size, crc, 0};
    boot_flash_erase(g->info_addr);
    return boot_flash_write(g->info_addr, (const uint8_t *)&rec, sizeof(rec));
}
