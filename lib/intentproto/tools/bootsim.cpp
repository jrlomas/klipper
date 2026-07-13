// bootsim: a desktop bootloader simulator for end-to-end flasher tests.
//
// Links the REAL protocol core (proto.cpp/dict.cpp) and the REAL update
// state machine (boot/bootcore.cpp) against a RAM-backed fake flash, and
// speaks the wire protocol over stdin/stdout. scripts/helix_flash.py can
// therefore be tested end-to-end — identify, flash_begin/data windowing,
// verify, boot — with no hardware, exercising the exact code the on-board
// bootloader runs.
//
//   bootsim <dumpfile>
//
// On a successful flash_boot the app region [0, image_size) is written to
// <dumpfile> and the process exits 0 (the "reset" of a real board).
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

KLIPPER_METHOD0(flash_verify)
{
    int rc = bootcore_verify(&g_bc);
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

int main(int argc, char **argv)
{
    if (argc < 2) {
        fprintf(stderr, "usage: bootsim <dumpfile>\n");
        return 2;
    }
    g_dumpfile = argv[1];
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
