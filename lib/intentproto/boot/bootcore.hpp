#ifndef INTENTPROTO_BOOTCORE_HPP
#define INTENTPROTO_BOOTCORE_HPP
// First-class bootloader core (FD-0001 doc 11).
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

// Signature size for the optional Ed25519 signed-image feature
// (see bootcore_verify_signature and FD-0001 doc 11 "Signed images").
constexpr size_t BOOT_SIG_SIZE = 64;

// flags bit persisted in the validity record's spare word: the image
// carries a valid Ed25519 signature (set only after the signature
// verifies).
enum { BOOTCORE_FLAG_SIGNED = 0x00000001u };

struct BootCore {
    const FlashOps* ops;
    BootState state;
    uint32_t image_size;
    uint32_t image_crc;
    uint32_t received;    // contiguous high-water mark
    uint32_t last_error;
    // Optional signed-image state (FD-0001 doc 11). flags carries
    // BOOTCORE_FLAG_SIGNED once the signature verifies; signature holds
    // the host-supplied Ed25519 signature that set_app_valid persists
    // alongside the validity record. Zero on an unsigned update.
    uint32_t flags;
    uint8_t signature[BOOT_SIG_SIZE];
};

enum {
    BOOT_OK = 0,
    BOOT_ERR_STATE = 1,      // command not valid in this state
    BOOT_ERR_RANGE = 2,      // offset/size outside the app region
    BOOT_ERR_ORDER = 3,      // non-contiguous data block
    BOOT_ERR_FLASH = 4,      // port erase/write failure
    BOOT_ERR_CRC = 5,        // whole-image CRC mismatch
    BOOT_ERR_SIG = 6,        // Ed25519 signature verification failed
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

// ---- Optional Ed25519 signed images (FD-0001 doc 11) ----
// A second gate beyond the CRC: the bootloader verifies an Ed25519
// signature (RFC 8032) over the exact application image bytes — the
// same bytes the CRC covers — before it will mark the image valid or
// boot it. Signing is off-device; the device only verifies against an
// embedded public key. All of this is inert unless the port wires it
// up (see src/boot_app/boot_main.cpp, gated by CONFIG_WANT_SIGNED_IMAGES),
// so an unsigned build behaves exactly as before.

// Stash the host-supplied 64-byte signature for this transfer. The
// signature is verified by bootcore_verify_signature and persisted by
// the port's set_app_valid alongside the validity record.
int bootcore_set_signature(BootCore* bc, const uint8_t sig[BOOT_SIG_SIZE]);

// After bootcore_verify (CRC ok, state Verified): verify the stashed
// signature over the flashed image against pub_key. On success sets
// BOOTCORE_FLAG_SIGNED (so set_app_valid records the image as signed)
// and leaves the state Verified; on failure -> Failed, BOOT_ERR_SIG.
int bootcore_verify_signature(BootCore* bc,
                              const uint8_t pub_key[32]);

// Boot-time signature check the port runs before jumping (mirror of
// bootcore_app_crc_ok): verify sig over the stored image [app_base,
// app_base+size) against pub_key. Returns 1 on a good signature.
int bootcore_app_sig_ok(const FlashOps* ops, uint32_t size,
                        const uint8_t sig[BOOT_SIG_SIZE],
                        const uint8_t pub_key[32]);

} // namespace intentproto

#endif // INTENTPROTO_BOOTCORE_HPP
