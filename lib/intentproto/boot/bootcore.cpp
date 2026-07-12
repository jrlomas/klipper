// intentproto bootloader core: portable in-band update state machine.
// See bootcore.hpp and RFC 0001 doc 11.

#include "bootcore.hpp"
#include <intentproto/ed25519.hpp>
#include <string.h>

namespace intentproto {

uint32_t
crc32(uint32_t seed, const uint8_t* data, size_t len)
{
    uint32_t crc = ~seed;
    while (len--) {
        crc ^= *data++;
        for (int i = 0; i < 8; i++)
            crc = (crc >> 1) ^ (0xedb88320 & (0 - (crc & 1)));
    }
    return ~crc;
}

void
bootcore_init(BootCore* bc, const FlashOps* ops)
{
    memset(bc, 0, sizeof(*bc));
    bc->ops = ops;
    bc->state = BootState::Idle;
}

static int
fail(BootCore* bc, int err)
{
    bc->state = BootState::Failed;
    bc->last_error = (uint32_t)err;
    return err;
}

int
bootcore_begin(BootCore* bc, uint32_t size, uint32_t crc)
{
    const FlashOps* ops = bc->ops;
    if (!size || size > ops->app_size)
        return fail(bc, BOOT_ERR_RANGE);
    // The app is invalid the moment an update starts; interrupted
    // updates therefore always land back in the bootloader.
    if (ops->set_app_valid(0, ops->user))
        return fail(bc, BOOT_ERR_FLASH);
    bc->image_size = size;
    bc->image_crc = crc;
    bc->received = 0;
    bc->state = BootState::Receiving;
    return BOOT_OK;
}

int
bootcore_data(BootCore* bc, uint32_t offset, const uint8_t* data,
              size_t len)
{
    const FlashOps* ops = bc->ops;
    if (bc->state != BootState::Receiving)
        return fail(bc, BOOT_ERR_STATE);
    if (offset != bc->received)
        return fail(bc, BOOT_ERR_ORDER);
    if (offset + len > bc->image_size)
        return fail(bc, BOOT_ERR_RANGE);
    uint32_t addr = ops->app_base + offset;
    if (ops->erase_sector && ops->erase_sector(addr, ops->user))
        return fail(bc, BOOT_ERR_FLASH);
    if (ops->write(addr, data, len, ops->user))
        return fail(bc, BOOT_ERR_FLASH);
    bc->received = offset + (uint32_t)len;
    return BOOT_OK;
}

int
bootcore_verify(BootCore* bc)
{
    const FlashOps* ops = bc->ops;
    if (bc->state != BootState::Receiving
        || bc->received != bc->image_size)
        return fail(bc, BOOT_ERR_STATE);
    const uint8_t* img = ops->read(ops->app_base, ops->user);
    if (crc32(0, img, bc->image_size) != bc->image_crc)
        return fail(bc, BOOT_ERR_CRC);
    bc->state = BootState::Verified;
    return BOOT_OK;
}

int
bootcore_boot(BootCore* bc)
{
    const FlashOps* ops = bc->ops;
    if (bc->state != BootState::Verified)
        return fail(bc, BOOT_ERR_STATE);
    if (ops->set_app_valid(1, ops->user))
        return fail(bc, BOOT_ERR_FLASH);
    bc->state = BootState::Idle;
    return BOOT_OK;
}

int
bootcore_app_crc_ok(const FlashOps* ops, uint32_t size, uint32_t crc)
{
    if (!size || size > ops->app_size)
        return 0;
    const uint8_t* img = ops->read(ops->app_base, ops->user);
    return crc32(0, img, size) == crc;
}

// ---- Optional Ed25519 signed images ----

int
bootcore_set_signature(BootCore* bc, const uint8_t sig[BOOT_SIG_SIZE])
{
    // Accept the signature while a transfer is in progress or verified;
    // it is checked against the image by bootcore_verify_signature.
    if (bc->state != BootState::Receiving
        && bc->state != BootState::Verified)
        return fail(bc, BOOT_ERR_STATE);
    memcpy(bc->signature, sig, BOOT_SIG_SIZE);
    return BOOT_OK;
}

int
bootcore_app_sig_ok(const FlashOps* ops, uint32_t size,
                    const uint8_t sig[BOOT_SIG_SIZE],
                    const uint8_t pub_key[32])
{
    if (!size || size > ops->app_size)
        return 0;
    // Flash is memory-mapped (ops->read returns a pointer), so the
    // image is hashed in place — no separate streaming buffer needed.
    const uint8_t* img = ops->read(ops->app_base, ops->user);
    return ed25519_verify(sig, img, size, pub_key) ? 1 : 0;
}

int
bootcore_verify_signature(BootCore* bc, const uint8_t pub_key[32])
{
    const FlashOps* ops = bc->ops;
    // CRC must have passed first: the signature covers exactly the
    // bytes the CRC covered.
    if (bc->state != BootState::Verified)
        return fail(bc, BOOT_ERR_STATE);
    if (!bootcore_app_sig_ok(ops, bc->image_size, bc->signature, pub_key))
        return fail(bc, BOOT_ERR_SIG);
    // Record the image as signed; set_app_valid persists the flag and
    // the signature so the boot-time gate can re-verify.
    bc->flags |= BOOTCORE_FLAG_SIGNED;
    return BOOT_OK;
}

} // namespace intentproto
