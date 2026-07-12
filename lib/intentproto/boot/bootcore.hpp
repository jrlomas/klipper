#ifndef INTENTPROTO_BOOTCORE_HPP
#define INTENTPROTO_BOOTCORE_HPP
// First-class bootloader core (RFC 0001 doc 11).
//
// The portable half of the in-band update flow: flash-agnostic state
// machine for enter/flash_begin/flash_data/flash_verify/flash_boot,
// whole-image CRC32 validation, and the unbrickable-by-construction
// rules (the bootloader region is never erased in-band; an
// incomplete update leaves the board in the bootloader, ready to
// retry). Ports supply the FlashOps table; transport authentication
// is the datagram layer's HMAC on network links. MIT, like the rest
// of the library — a vendor shipping a closed board needs a
// bootloader they can ship with it.
//
// Freestanding profile: no heap, no exceptions, no RTTI.

#include <stddef.h>
#include <stdint.h>

namespace intentproto {

// CRC-32 (IEEE 802.3, reflected) — the image validity check.
uint32_t crc32(uint32_t seed, const uint8_t* data, size_t len);

struct FlashOps {
    // Application region (absolute addresses; never overlaps the
    // bootloader region by construction of the port's link script).
    uint32_t app_base;
    uint32_t app_size;
    // Erase the sector containing addr; returns 0 on success.
    int (*erase_sector)(uint32_t addr, void* user);
    // Program len bytes (port-aligned; core sends aligned blocks).
    int (*write)(uint32_t addr, const uint8_t* data, size_t len,
                 void* user);
    // Read is memory-mapped.
    const uint8_t* (*read)(uint32_t addr, void* user);
    // Mark the application image valid/invalid (e.g. a flag word or
    // the vector-table write done last).
    int (*set_app_valid)(int valid, void* user);
    void* user;
};

enum class BootState : uint8_t {
    Idle,        // announcing; no update in progress
    Receiving,   // flash_begin accepted, taking flash_data blocks
    Verified,    // whole-image CRC matched; ready to boot
    Failed,      // update failed; safe to retry with flash_begin
};

struct BootCore {
    const FlashOps* ops;
    BootState state;
    uint32_t image_size;
    uint32_t image_crc;
    uint32_t received;    // contiguous high-water mark
    uint32_t last_error;
};

enum {
    BOOT_OK = 0,
    BOOT_ERR_STATE = 1,      // command not valid in this state
    BOOT_ERR_RANGE = 2,      // offset/size outside the app region
    BOOT_ERR_ORDER = 3,      // non-contiguous data block
    BOOT_ERR_FLASH = 4,      // port erase/write failure
    BOOT_ERR_CRC = 5,        // whole-image CRC mismatch
};

void bootcore_init(BootCore* bc, const FlashOps* ops);

// flash_begin size=%u crc32=%u — invalidates the app, erases as
// needed lazily, starts a transfer. Power loss after this point
// leaves an invalid app and a reachable bootloader: a retry, not a
// paperweight.
int bootcore_begin(BootCore* bc, uint32_t size, uint32_t crc);

// flash_data offset=%u data=%*s — blocks must be contiguous
// (offset == high-water mark); the ack window provides flow control.
int bootcore_data(BootCore* bc, uint32_t offset, const uint8_t* data,
                  size_t len);

// flash_verify — recompute CRC32 over the written image.
int bootcore_verify(BootCore* bc);

// flash_boot — mark valid; the port then jumps to the application.
int bootcore_boot(BootCore* bc);

// Boot-time check the port runs before jumping: is there a valid
// application? (valid flag is the port's; this checks the CRC the
// port stored alongside the image, if it chooses to.)
int bootcore_app_crc_ok(const FlashOps* ops, uint32_t size, uint32_t crc);

} // namespace intentproto

#endif // INTENTPROTO_BOOTCORE_HPP
