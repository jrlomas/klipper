// STM32F072 flash driver for the first-class bootloader (RFC 0001
// doc 11). 128 KB flash, uniform 2 KB pages, 16-bit programming.
//
// Register-level programming (not exercised by the host tests, which
// cover the portable geometry in boot_flash.h). Built only for the
// arm bootloader image; see src/boot_app/README.md.

#include "stm32f072xb.h" // FLASH
#include "boot_flash.h"

#define FLASH_KEY1_VAL 0x45670123UL
#define FLASH_KEY2_VAL 0xCDEF89ABUL

static struct boot_flash_geom geom;

const struct boot_flash_geom *
boot_active_geom(void)
{
    if (!geom.flash_base)
        geom = boot_geom_stm32f072();
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
    while (FLASH->SR & FLASH_SR_BSY)
        ;
    if (FLASH->SR & (FLASH_SR_PGERR | FLASH_SR_WRPERR)) {
        FLASH->SR |= FLASH_SR_PGERR | FLASH_SR_WRPERR | FLASH_SR_EOP;
        return -1;
    }
    FLASH->SR |= FLASH_SR_EOP;
    return 0;
}

// Erase the 2 KB page containing addr.
int
boot_flash_erase(uint32_t addr)
{
    flash_unlock();
    FLASH->CR |= FLASH_CR_PER;
    FLASH->AR = addr;
    FLASH->CR |= FLASH_CR_STRT;
    int rc = flash_wait();
    FLASH->CR &= ~FLASH_CR_PER;
    flash_lock();
    return rc;
}

// Program len bytes at addr (16-bit half-word programming; bootcore
// hands aligned blocks, tail padded to a half-word by the caller).
int
boot_flash_write(uint32_t addr, const uint8_t *data, size_t len)
{
    flash_unlock();
    FLASH->CR |= FLASH_CR_PG;
    int rc = 0;
    for (size_t i = 0; i < len; i += 2) {
        uint16_t hw = data[i];
        if (i + 1 < len)
            hw |= (uint16_t)data[i + 1] << 8;
        else
            hw |= 0xff00;
        *(volatile uint16_t *)(uintptr_t)(addr + i) = hw;
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
// Ed25519 signature immediately after it in the same erased info page
// (RFC 0001 doc 11). sig may be NULL / flags may lack BOOT_INFO_FLAG_SIGNED
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
