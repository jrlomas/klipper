// bootcore tests: in-band update flow against a RAM-backed flash.

#include "../boot/bootcore.hpp"

#include <stdio.h>
#include <string.h>

static int g_failures = 0;
#define CHECK(cond)                                                     \
    do {                                                                \
        if (!(cond)) {                                                  \
            printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);      \
            g_failures++;                                               \
        }                                                               \
    } while (0)

using namespace intentproto;

// Fake flash: 4KB app region at 0x1000, 256-byte sectors
static uint8_t g_flash[0x2000];
static int g_app_valid;
static int g_fail_writes;

static int f_erase(uint32_t addr, void*) {
    if (addr % 256 == 0)
        memset(g_flash + addr, 0xff, 256);
    return 0;
}
static int f_write(uint32_t addr, const uint8_t* d, size_t n, void*) {
    if (g_fail_writes)
        return -1;
    memcpy(g_flash + addr, d, n);
    return 0;
}
static const uint8_t* f_read(uint32_t addr, void*) { return g_flash + addr; }
static int f_valid(int v, void*) { g_app_valid = v; return 0; }

static const FlashOps OPS = {0x1000, 0x1000, f_erase, f_write, f_read,
                             f_valid, nullptr};

int main() {
    // crc32 known vector: "123456789" -> 0xcbf43926
    CHECK(crc32(0, (const uint8_t*)"123456789", 9) == 0xcbf43926);

    uint8_t image[1000];
    for (size_t i = 0; i < sizeof(image); i++)
        image[i] = (uint8_t)(i * 13 + 5);
    uint32_t icrc = crc32(0, image, sizeof(image));

    BootCore bc;
    bootcore_init(&bc, &OPS);
    g_app_valid = 1;

    // Happy path: begin invalidates, contiguous blocks, verify, boot
    CHECK(bootcore_begin(&bc, sizeof(image), icrc) == BOOT_OK);
    CHECK(g_app_valid == 0);
    for (size_t off = 0; off < sizeof(image); off += 200)
        CHECK(bootcore_data(&bc, off, image + off, 200) == BOOT_OK);
    CHECK(bootcore_verify(&bc) == BOOT_OK);
    CHECK(bootcore_boot(&bc) == BOOT_OK);
    CHECK(g_app_valid == 1);
    CHECK(!memcmp(g_flash + 0x1000, image, sizeof(image)));
    CHECK(bootcore_app_crc_ok(&OPS, sizeof(image), icrc));

    // Interrupted update = retry, never a paperweight: power loss is
    // modeled by a fresh core after a partial transfer.
    CHECK(bootcore_begin(&bc, sizeof(image), icrc) == BOOT_OK);
    CHECK(g_app_valid == 0);
    CHECK(bootcore_data(&bc, 0, image, 200) == BOOT_OK);
    BootCore bc2;
    bootcore_init(&bc2, &OPS);         // "reboot"
    CHECK(g_app_valid == 0);           // app stays invalid -> bootloader
    CHECK(bootcore_begin(&bc2, sizeof(image), icrc) == BOOT_OK);
    for (size_t off = 0; off < sizeof(image); off += 200)
        CHECK(bootcore_data(&bc2, off, image + off, 200) == BOOT_OK);
    CHECK(bootcore_verify(&bc2) == BOOT_OK);
    CHECK(bootcore_boot(&bc2) == BOOT_OK);

    // Error paths
    CHECK(bootcore_begin(&bc2, 0x1001, icrc) == BOOT_ERR_RANGE);
    bootcore_init(&bc2, &OPS);
    CHECK(bootcore_begin(&bc2, sizeof(image), icrc) == BOOT_OK);
    CHECK(bootcore_data(&bc2, 200, image, 200) == BOOT_ERR_ORDER);
    CHECK(bc2.state == BootState::Failed);
    CHECK(bootcore_verify(&bc2) == BOOT_ERR_STATE);
    CHECK(bootcore_boot(&bc2) == BOOT_ERR_STATE);

    // Corrupt image fails verify and never marks valid
    bootcore_init(&bc2, &OPS);
    CHECK(bootcore_begin(&bc2, sizeof(image), icrc ^ 1) == BOOT_OK);
    for (size_t off = 0; off < sizeof(image); off += 200)
        CHECK(bootcore_data(&bc2, off, image + off, 200) == BOOT_OK);
    CHECK(bootcore_verify(&bc2) == BOOT_ERR_CRC);
    CHECK(g_app_valid == 0);

    // Flash write failure surfaces as BOOT_ERR_FLASH
    bootcore_init(&bc2, &OPS);
    CHECK(bootcore_begin(&bc2, sizeof(image), icrc) == BOOT_OK);
    g_fail_writes = 1;
    CHECK(bootcore_data(&bc2, 0, image, 200) == BOOT_ERR_FLASH);
    g_fail_writes = 0;

    // ---- Ed25519 signed-image gate ----
    // A signature over the exact 1000-byte test image, generated with a
    // fixed Ed25519 key (seed 0xa1*32) by the cryptography library.
    static const uint8_t sig_pub[32] = {
        0xbc,0x7c,0xbc,0xb5,0x63,0x63,0x75,0xfa,0x1d,0x82,0x43,0x4d,
        0x46,0x67,0x24,0xd9,0x23,0x77,0xf5,0x3b,0x98,0x06,0x95,0xdd,
        0x49,0xd2,0x6d,0x0c,0xe1,0x22,0x05,0xa5};
    static const uint8_t sig_good[64] = {
        0x1e,0x72,0x68,0x39,0x0f,0x26,0x35,0x2f,0x1e,0xf4,0xed,0x77,
        0x29,0x96,0xa9,0xce,0xec,0xcb,0x6c,0x91,0x1d,0xa6,0xe3,0x32,
        0xed,0xad,0xa5,0x1a,0x9f,0x33,0x51,0xab,0x17,0x3b,0x93,0x2d,
        0x40,0xb9,0x75,0x29,0x29,0xbd,0xd2,0xd6,0xc2,0xb9,0x3b,0x91,
        0x84,0xd1,0x06,0x95,0xa9,0x7c,0x3f,0xa3,0xcb,0x2c,0x1c,0x3c,
        0x61,0x91,0x56,0x08};

    // Happy path: flash the image, CRC verify, then signature verify
    // sets BOOTCORE_FLAG_SIGNED and permits boot.
    bootcore_init(&bc2, &OPS);
    CHECK(bootcore_begin(&bc2, sizeof(image), icrc) == BOOT_OK);
    for (size_t off = 0; off < sizeof(image); off += 200)
        CHECK(bootcore_data(&bc2, off, image + off, 200) == BOOT_OK);
    CHECK(bootcore_set_signature(&bc2, sig_good) == BOOT_OK);
    CHECK(bootcore_verify(&bc2) == BOOT_OK);
    CHECK(bootcore_verify_signature(&bc2, sig_pub) == BOOT_OK);
    CHECK((bc2.flags & BOOTCORE_FLAG_SIGNED) != 0);
    CHECK(bootcore_boot(&bc2) == BOOT_OK);
    // Boot-time signature gate accepts the stored image + signature.
    CHECK(bootcore_app_sig_ok(&OPS, sizeof(image), sig_good, sig_pub) == 1);
    CHECK(bootcore_app_crc_ok(&OPS, sizeof(image), icrc) == 1);

    // Signature verification must happen only after CRC: called before
    // bootcore_verify (state Idle/Receiving) it refuses with STATE.
    bootcore_init(&bc2, &OPS);
    CHECK(bootcore_verify_signature(&bc2, sig_pub) == BOOT_ERR_STATE);
    CHECK(bc2.state == BootState::Failed);

    // A tampered signature fails the gate and never marks valid.
    bootcore_init(&bc2, &OPS);
    CHECK(bootcore_begin(&bc2, sizeof(image), icrc) == BOOT_OK);
    for (size_t off = 0; off < sizeof(image); off += 200)
        CHECK(bootcore_data(&bc2, off, image + off, 200) == BOOT_OK);
    uint8_t sig_bad[64];
    memcpy(sig_bad, sig_good, 64);
    sig_bad[10] ^= 0x01;
    CHECK(bootcore_set_signature(&bc2, sig_bad) == BOOT_OK);
    CHECK(bootcore_verify(&bc2) == BOOT_OK);
    CHECK(bootcore_verify_signature(&bc2, sig_pub) == BOOT_ERR_SIG);
    CHECK(bc2.state == BootState::Failed);
    CHECK(bootcore_boot(&bc2) == BOOT_ERR_STATE); // cannot boot on bad sig

    // Boot-time gate rejects a good signature against the wrong key.
    uint8_t wrong_pub[32];
    memcpy(wrong_pub, sig_pub, 32);
    wrong_pub[0] ^= 0x01;
    CHECK(bootcore_app_sig_ok(&OPS, sizeof(image), sig_good, wrong_pub) == 0);

    if (g_failures) {
        printf("%d FAILURE(S)\n", g_failures);
        return 1;
    }
    printf("all tests passed\n");
    return 0;
}
