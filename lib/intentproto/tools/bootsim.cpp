// bootsim: a desktop bootloader simulator for end-to-end flasher tests.
//
// Links the REAL protocol core (proto.cpp/dict.cpp) and the REAL update
// state machine (boot/bootcore.cpp) against a RAM-backed fake flash, and
// speaks the wire protocol over stdin/stdout. scripts/helix_flash.py can
// therefore be tested end-to-end — identify, flash_begin/data windowing,
// verify, boot — with no hardware, exercising the exact code the on-board
// bootloader runs.
//
//   bootsim <dumpfile> [pubkey-hex-file]
//
// On a successful flash_boot the app region [0, image_size) is written to
// <dumpfile> and the process exits 0 (the "reset" of a real board).
//
// With a public-key file (64 hex chars, e.g. keys/helix_dev_signing.pub)
// the simulator behaves like a signing-enabled bootloader: flash_verify
// additionally requires a valid Ed25519 signature supplied via flash_sign
// (mirroring boot_main.cpp's CONFIG_WANT_SIGNED_IMAGES gate).
//
// Desktop-only tool: uses system zlib to build the identify blob.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <zlib.h>

#include <intentproto/method.hpp>
#include <intentproto/proto.hpp>
#include "../boot/bootcore.hpp"

using namespace intentproto;

// ---- RAM fake flash: 2 KB pages, byte-addressed at app_base 0x1000 ----
static constexpr uint32_t PAGE = 2048;
static constexpr uint32_t APP_BASE = 0x1000;
static constexpr uint32_t APP_SIZE = 64 * 1024;
static uint8_t g_flash[APP_BASE + APP_SIZE];
static int g_app_valid_calls, g_app_valid_last = -1;

static int ops_erase(uint32_t addr, void *) {
    // Contract of the real boot_flash drivers: called for EVERY data
    // address; erase only when the address starts an erase unit
    // (boot_flash_is_erase_start), otherwise a no-op.
    if (addr % PAGE)
        return 0;
    if (addr < APP_BASE || addr + PAGE > sizeof(g_flash))
        return -1;
    memset(g_flash + addr, 0xff, PAGE);
    return 0;
}
static int ops_write(uint32_t addr, const uint8_t *d, size_t n, void *) {
    if (addr < APP_BASE || addr + n > sizeof(g_flash))
        return -1;
    memcpy(g_flash + addr, d, n);
    return 0;
}
static const uint8_t *ops_read(uint32_t addr, void *) {
    return addr < sizeof(g_flash) ? g_flash + addr : nullptr;
}
static int ops_set_valid(int valid, void *) {
    g_app_valid_calls++;
    g_app_valid_last = valid;
    return 0;
}

static BootCore g_bc;
static const char *g_dumpfile;

// ---- the five boot commands, exactly boot_main.cpp's surface ----
KLIPPER_RESPONSE(flash_result, (uint8_t, op), (uint8_t, code),
                 (uint32_t, arg));

enum { OP_BEGIN = 0, OP_DATA, OP_VERIFY, OP_BOOT, OP_ENTER, OP_SIGN };

KLIPPER_METHOD(flash_begin, (uint32_t, size), (uint32_t, crc32))
{
    int rc = bootcore_begin(&g_bc, size, crc32);
    reply(flash_result{OP_BEGIN, (uint8_t)rc, size});
}

KLIPPER_METHOD(flash_data, (uint32_t, offset), (buf, data))
{
    int rc = bootcore_data(&g_bc, offset, data.data, data.len);
    reply(flash_result{OP_DATA, (uint8_t)rc, g_bc.received});
}

// Signing gate (mirrors boot_main.cpp's CONFIG_WANT_SIGNED_IMAGES):
// enabled when a public key file is given on the command line.
static bool g_signing;
static uint8_t g_pubkey[32];

// flash_sign offset=%u data=%.*s - chunked exactly like flash_data (a
// whole 64-byte signature cannot fit one frame's payload). Always
// registered here (the simulator plays both bootloader variants);
// without a pubkey the signature is stored but never required.
KLIPPER_METHOD(flash_sign, (uint32_t, offset), (buf, data))
{
    int rc = bootcore_sign_data(&g_bc, offset, data.data, data.len);
    reply(flash_result{OP_SIGN, (uint8_t)rc, g_bc.sig_received});
}

KLIPPER_METHOD0(flash_verify)
{
    int rc = bootcore_verify(&g_bc);
    if (rc == BOOT_OK && g_signing)
        // Signature is a second, mandatory gate, exactly as on-device.
        rc = bootcore_verify_signature(&g_bc, g_pubkey);
    reply(flash_result{OP_VERIFY, (uint8_t)rc, g_bc.image_crc});
}

KLIPPER_METHOD0(flash_boot)
{
    int rc = bootcore_boot(&g_bc);
    reply(flash_result{OP_BOOT, (uint8_t)rc, 0});
    if (rc == BOOT_OK) {
        // "Reset": dump the flashed app region and exit like a real board
        // rebooting into the new application.
        FILE *f = fopen(g_dumpfile, "wb");
        if (f) {
            fwrite(g_flash + APP_BASE, 1, g_bc.image_size, f);
            fclose(f);
        }
        fflush(nullptr);
        _exit(0);
    }
}

KLIPPER_METHOD(enter_bootloader, (uint8_t, force))
{
    (void)force;
    reply(flash_result{OP_ENTER, BOOT_OK, 0});
}

// ---- transport: raw stdio ----
static int wire_write(const uint8_t *data, size_t len, void *)
{
    size_t off = 0;
    while (off < len) {
        ssize_t n = write(1, data + off, len - off);
        if (n < 0) {
            if (errno == EINTR)
                continue;
            return -1;
        }
        off += (size_t)n;
    }
    return 0;
}

static int load_pubkey_hex(const char *path)
{
    FILE *f = fopen(path, "r");
    if (!f)
        return -1;
    char hex[65] = {0};
    size_t n = fread(hex, 1, 64, f);
    fclose(f);
    if (n != 64)
        return -1;
    for (int i = 0; i < 32; i++) {
        unsigned v;
        if (sscanf(&hex[2 * i], "%2x", &v) != 1)
            return -1;
        g_pubkey[i] = (uint8_t)v;
    }
    return 0;
}

int main(int argc, char **argv)
{
    if (argc < 2) {
        fprintf(stderr, "usage: bootsim <dumpfile> [pubkey-hex-file]\n");
        return 2;
    }
    g_dumpfile = argv[1];
    if (argc > 2) {
        if (load_pubkey_hex(argv[2])) {
            fprintf(stderr, "bootsim: bad pubkey file %s\n", argv[2]);
            return 2;
        }
        g_signing = true;
    }
    memset(g_flash, 0xff, sizeof(g_flash));

    static FlashOps ops;
    ops.app_base = APP_BASE;
    ops.app_size = APP_SIZE;
    ops.erase_sector = ops_erase;
    ops.write = ops_write;
    ops.read = ops_read;
    ops.set_app_valid = ops_set_valid;
    bootcore_init(&g_bc, &ops);

    // Build the identify blob: serialize the live registry, zlib-compress.
    Config cfg;
    cfg.write = wire_write;
    cfg.version = "bootsim";
    cfg.build_version = "desktop";
    init(cfg);  // freeze registry first so build_dictionary sees the ids
    static char dict_json[16384];
    size_t jlen = build_dictionary(dict_json, sizeof(dict_json));
    static uint8_t blob[16384];
    uLongf blen = sizeof(blob);
    if (!jlen || compress2(blob, &blen, (const Bytef *)dict_json, jlen,
                           9) != Z_OK) {
        fprintf(stderr, "bootsim: dictionary build failed\n");
        return 2;
    }
    cfg.identify_blob = blob;
    cfg.identify_blob_len = (size_t)blen;
    init(cfg);  // re-init with the blob (registry ids are stable)

    // Pump stdin into the protocol core forever (flash_boot exits).
    uint8_t rxbuf[256];
    for (;;) {
        ssize_t n = read(0, rxbuf, sizeof(rxbuf));
        if (n <= 0) {
            if (n < 0 && errno == EINTR)
                continue;
            return 1;  // peer closed without flash_boot
        }
        rx(rxbuf, (size_t)n);
    }
}
