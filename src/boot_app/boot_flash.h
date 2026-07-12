#ifndef BOOT_APP_BOOT_FLASH_H
#define BOOT_APP_BOOT_FLASH_H
// Portable per-target flash geometry for the first-class bootloader
// (RFC 0001 doc 11).
//
// The geometry and the erase-boundary / bounds / validity-record
// predicates here are pure logic — no register access — so they are
// host-testable (tests/test_flashops.cpp) against a RAM-backed fake
// flash. The register-level erase/program lives in the per-target
// arm drivers (boot_flash_stm32*.c); those drivers build their
// intentproto::FlashOps table from this same geometry, so the logic
// exercised on the desktop is the logic that runs on-chip.

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// A target's flash layout.
//
//   [ flash_base .. app_base )        bootloader — never erased in-band
//   [ app_base   .. app_base+app_size ) application image (CRC'd)
//   [ info_addr  .. info_addr+info_size ) validity record page
//
// page_size != 0 => uniform erase pages (F0/G0). page_size == 0 =>
// variable sectors (F4) enumerated by sect_bounds (absolute addresses
// of every erase-unit start in the app region, ascending). The
// bootloader region is excluded from sect_bounds so it can never be
// selected for erase.
struct boot_flash_geom {
    uint32_t flash_base;
    uint32_t app_base;
    uint32_t app_size;              // image region, excludes info page
    uint32_t info_addr;             // validity record location
    uint32_t info_size;             // one erase unit
    uint32_t page_size;             // 0 for variable-sector parts (F4)
    const uint32_t *sect_bounds;    // NULL unless page_size == 0
    uint32_t sect_bounds_len;
    uint8_t dual_bank;              // 1 if the part has two flash banks
    uint32_t bank_size;             // bytes per bank (dual_bank only)
};

// Validity record written by set_app_valid(1) into the info page and
// read back at boot. A whole-image CRC check gates the jump, so an
// interrupted update (info page still erased, or CRC mismatch) simply
// keeps the board in the bootloader — a retry, never a paperweight.
//
// Signed-image layout (RFC 0001 doc 11, "Signed images"): the spare
// word is a flags word. BOOT_INFO_FLAG_SIGNED means the 64-byte
// Ed25519 signature over the application image is stored in the SAME
// info erase-unit immediately after this 16-byte record, at
// info_addr + BOOT_INFO_SIG_OFFSET. The signature covers exactly the
// application bytes the CRC covers ([app_base, app_base+size)). An
// unsigned image leaves the flag clear and validates by CRC alone;
// signature enforcement is the bootloader's compile-time policy
// (CONFIG_WANT_SIGNED_IMAGES), so this stays backward compatible.
#define BOOT_INFO_MAGIC 0x50414F42UL   // "BOAP"
#define BOOT_INFO_FLAG_SIGNED 0x00000001UL
#define BOOT_INFO_SIG_OFFSET 16        // signature follows the record
#define BOOT_INFO_SIG_SIZE 64          // Ed25519 signature length
struct boot_info_record {
    uint32_t magic;
    uint32_t size;
    uint32_t crc;
    uint32_t flags;                 // BOOT_INFO_FLAG_* (was reserved)
};

// Per-target geometry constructors (portable; see boot_flash.c).
struct boot_flash_geom boot_geom_stm32f072(void);
struct boot_flash_geom boot_geom_stm32g0b1(void);
struct boot_flash_geom boot_geom_stm32f4(void);

// Is addr the first byte of an erase unit inside the app image region?
// bootcore calls erase_sector(addr) on every write; a driver erases
// only when this returns true (a fresh page/sector begins at addr).
static inline int
boot_flash_is_erase_start(const struct boot_flash_geom *g, uint32_t addr)
{
    if (addr < g->app_base || addr >= g->app_base + g->app_size)
        return 0;
    if (g->page_size)
        return ((addr - g->flash_base) % g->page_size) == 0;
    for (uint32_t i = 0; i < g->sect_bounds_len; i++)
        if (g->sect_bounds[i] == addr)
            return 1;
    return 0;
}

// Is [addr, addr+len) fully inside the app image region?
static inline int
boot_flash_in_app(const struct boot_flash_geom *g, uint32_t addr, size_t len)
{
    return addr >= g->app_base
        && addr + len >= addr                       // no wrap
        && addr + len <= g->app_base + g->app_size;
}

#ifdef __cplusplus
}
#endif

#endif // boot_flash.h
