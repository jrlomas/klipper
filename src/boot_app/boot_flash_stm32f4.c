// STM32F4 (Octopus-class) flash driver for the first-class bootloader
// (FD-0001 doc 11). 512 KB flash, variable sectors: 16K x4, 64K,
// 128K x3. The app starts at sector 2 (0x08008000), a sector
// boundary, leaving sectors 0-1 (32 KB) for the bootloader.
//
// Byte programming (PSIZE=0) is used so no Vpp/voltage assumption is
// needed. Register-level, arm-only; see README.md.

#include "stm32f407xx.h" // FLASH
#include "boot_flash.h"

#define FLASH_KEY1_VAL 0x45670123UL
#define FLASH_KEY2_VAL 0xCDEF89ABUL

static struct boot_flash_geom geom;

const struct boot_flash_geom *
boot_active_geom(void)
{
    if (!geom.flash_base)
        geom = boot_geom_stm32f4();
    return &geom;
}

// Absolute sector start addresses (F407VG, 512 KB) and their numbers.
static const uint32_t f4_sector_base[] = {
    0x08000000UL, 0x08004000UL, 0x08008000UL, 0x0800C000UL, // 0-3, 16K
    0x08010000UL,                                           // 4, 64K
    0x08020000UL, 0x08040000UL, 0x08060000UL,               // 5-7, 128K
};

static int
addr_to_sector(uint32_t addr)
{
    int n = sizeof(f4_sector_base) / sizeof(f4_sector_base[0]);
    int sect = -1;
    for (int i = 0; i < n; i++)
        if (addr >= f4_sector_base[i])
            sect = i;
    return sect;
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
    while (FLASH->SR & FLASH_SR_BSY)
        ;
    if (FLASH->SR & FLASH_SR_WRPERR) {
        FLASH->SR |= FLASH_SR_WRPERR;
        return -1;
    }
    return 0;
}

int
boot_flash_erase(uint32_t addr)
{
    int sect = addr_to_sector(addr);
    if (sect < 0)
        return -1;
    flash_unlock();
    uint32_t cr = FLASH->CR & ~(FLASH_CR_SNB_Msk | FLASH_CR_PSIZE_Msk);
    cr |= FLASH_CR_SER | ((uint32_t)sect << FLASH_CR_SNB_Pos);
    FLASH->CR = cr;
    FLASH->CR |= FLASH_CR_STRT;
    int rc = flash_wait();
    FLASH->CR &= ~(FLASH_CR_SER | FLASH_CR_SNB_Msk);
    flash_lock();
    return rc;
}

int
boot_flash_write(uint32_t addr, const uint8_t *data, size_t len)
{
    flash_unlock();
    uint32_t cr = FLASH->CR & ~FLASH_CR_PSIZE_Msk; // PSIZE=0 => byte
    FLASH->CR = cr | FLASH_CR_PG;
    int rc = 0;
    for (size_t i = 0; i < len; i++) {
        *(volatile uint8_t *)(uintptr_t)(addr + i) = data[i];
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

// Write the validity record, and — for a signed image — the 64-byte
// Ed25519 signature immediately after it in the same erased info sector
// (FD-0001 doc 11). sig may be NULL / flags may lack BOOT_INFO_FLAG_SIGNED
// for an unsigned image.
int
boot_flash_write_info(const struct boot_flash_geom *g, uint32_t size,
                      uint32_t crc, uint32_t flags, const uint8_t *sig)
{
    struct boot_info_record rec = {BOOT_INFO_MAGIC, size, crc, flags};
    boot_flash_erase(g->info_addr);
    int rc = boot_flash_write(g->info_addr, (const uint8_t *)&rec,
                              sizeof(rec));
    if (rc || !(flags & BOOT_INFO_FLAG_SIGNED) || !sig)
        return rc;
    return boot_flash_write(g->info_addr + BOOT_INFO_SIG_OFFSET, sig,
                            BOOT_INFO_SIG_SIZE);
}
