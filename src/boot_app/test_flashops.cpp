// Flash-ops unit tests (FD-0001 doc 11, part 4).
//
// The register-level programming can't run on a desktop, but the
// portable FlashOps *table logic* — app-region bounds, erase-unit
// boundaries, and the validity-record (set_app_valid) semantics — is
// pure and host-testable. This drives the real per-target geometry
// (boot_flash.c) through bootcore against a RAM-backed fake flash,
// one fake built exactly the way boot_main.cpp builds the on-chip
// FlashOps, so the logic exercised here is the logic that ships.

#include "../../lib/intentproto/boot/bootcore.hpp"
#include "boot_flash.h"

#include <stdio.h>
#include <stdlib.h>
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

// ---- RAM-backed fake flash bound to one target's geometry ----
struct FakeFlash {
    boot_flash_geom g;
    uint8_t *mem;     // indexed by (addr - flash_base)
    size_t memlen;
    BootCore *bc;     // for set_app_valid to read image size/crc
};

static uint32_t
erase_unit_end(const boot_flash_geom *g, uint32_t addr)
{
    if (g->page_size)
        return addr + g->page_size;
    uint32_t end = g->info_addr;
    for (uint32_t i = 0; i < g->sect_bounds_len; i++)
        if (g->sect_bounds[i] > addr && g->sect_bounds[i] < end)
            end = g->sect_bounds[i];
    return end;
}

static int
fake_erase(uint32_t addr, void *user)
{
    FakeFlash *f = (FakeFlash *)user;
    if (!boot_flash_is_erase_start(&f->g, addr))
        return 0; // mid-unit write: unit already erased at its start
    uint32_t end = erase_unit_end(&f->g, addr);
    memset(f->mem + (addr - f->g.flash_base), 0xff, end - addr);
    return 0;
}
static int
fake_write(uint32_t addr, const uint8_t *data, size_t len, void *user)
{
    FakeFlash *f = (FakeFlash *)user;
    if (!boot_flash_in_app(&f->g, addr, len))
        return -1;
    memcpy(f->mem + (addr - f->g.flash_base), data, len);
    return 0;
}
static const uint8_t *
fake_read(uint32_t addr, void *user)
{
    FakeFlash *f = (FakeFlash *)user;
    return f->mem + (addr - f->g.flash_base);
}
// Validity record: valid=0 erases the info page; valid=1 writes the
// {magic,size,crc} record read back at boot. Mirrors boot_main.cpp.
static int
fake_set_valid(int valid, void *user)
{
    FakeFlash *f = (FakeFlash *)user;
    uint8_t *info = f->mem + (f->g.info_addr - f->g.flash_base);
    if (!valid) {
        memset(info, 0xff, f->g.info_size);
        return 0;
    }
    boot_info_record rec = {BOOT_INFO_MAGIC, f->bc->image_size,
                            f->bc->image_crc, 0};
    memset(info, 0xff, f->g.info_size);
    memcpy(info, &rec, sizeof(rec));
    return 0;
}

// Boot-time validity check, same predicate as boot_main::app_is_valid.
static int
fake_app_valid(FakeFlash *f, const FlashOps *ops)
{
    const boot_info_record *rec =
        (const boot_info_record *)fake_read(f->g.info_addr, f);
    if (rec->magic != BOOT_INFO_MAGIC)
        return 0;
    if (!rec->size || rec->size > f->g.app_size)
        return 0;
    return bootcore_app_crc_ok(ops, rec->size, rec->crc);
}

static void
make_fake(FakeFlash *f, boot_flash_geom g, BootCore *bc)
{
    f->g = g;
    f->bc = bc;
    f->memlen = (g.info_addr + g.info_size) - g.flash_base;
    f->mem = (uint8_t *)malloc(f->memlen);
    memset(f->mem, 0x00, f->memlen); // pre-fill non-erased so erase is visible
}

// Run the full in-band flow for a target and assert the geometry,
// bounds, erase, and validity-record semantics.
static void
run_target(const char *name, boot_flash_geom g)
{
    printf("  target %s: app_base=%08x app_size=%u info=%08x\n", name,
           g.app_base, g.app_size, g.info_addr);

    BootCore bc;
    FakeFlash f;
    make_fake(&f, g, &bc);

    FlashOps ops = {g.app_base, g.app_size, fake_erase, fake_write,
                    fake_read, fake_set_valid, &f};
    bootcore_init(&bc, &ops);

    // Build an image spanning several erase units.
    uint32_t img_len = 20000;
    if (img_len > g.app_size)
        img_len = g.app_size / 2;
    uint8_t *img = (uint8_t *)malloc(img_len);
    for (uint32_t i = 0; i < img_len; i++)
        img[i] = (uint8_t)(i * 7 + 3);
    uint32_t icrc = crc32(0, img, img_len);

    // Happy path in 256-byte blocks (block starts hit every page /
    // sector boundary, so erase fires at each unit start).
    CHECK(bootcore_begin(&bc, img_len, icrc) == BOOT_OK);
    for (uint32_t off = 0; off < img_len; off += 256) {
        uint32_t n = img_len - off < 256 ? img_len - off : 256;
        CHECK(bootcore_data(&bc, off, img + off, n) == BOOT_OK);
    }
    CHECK(bootcore_verify(&bc) == BOOT_OK);
    CHECK(bootcore_boot(&bc) == BOOT_OK);
    // Image landed at app_base and the erase turned pre-set 0x00 into
    // the image bytes (proves erase+program ran over each unit).
    CHECK(!memcmp(f.mem + (g.app_base - g.flash_base), img, img_len));
    // Validity record committed and the boot-time CRC gate passes.
    CHECK(fake_app_valid(&f, &ops) == 1);

    // Bounds: an image larger than the app region is refused.
    bootcore_init(&bc, &ops);
    CHECK(bootcore_begin(&bc, g.app_size + 1, icrc) == BOOT_ERR_RANGE);

    // Bounds: a data block running past the image end is refused and
    // never touches the info page.
    bootcore_init(&bc, &ops);
    CHECK(bootcore_begin(&bc, 256, icrc) == BOOT_OK);
    CHECK(bootcore_data(&bc, 0, img, 512) == BOOT_ERR_RANGE);

    // Interrupted update leaves the app invalid: begin invalidates the
    // record (set_app_valid(0)) and a reboot before boot() keeps it so.
    bootcore_init(&bc, &ops);
    CHECK(bootcore_begin(&bc, img_len, icrc) == BOOT_OK);
    CHECK(bootcore_data(&bc, 0, img, 256) == BOOT_OK);
    CHECK(fake_app_valid(&f, &ops) == 0); // record erased at begin

    // Corrupt image fails verify and never marks valid.
    bootcore_init(&bc, &ops);
    CHECK(bootcore_begin(&bc, img_len, icrc ^ 1) == BOOT_OK);
    for (uint32_t off = 0; off < img_len; off += 256) {
        uint32_t n = img_len - off < 256 ? img_len - off : 256;
        CHECK(bootcore_data(&bc, off, img + off, n) == BOOT_OK);
    }
    CHECK(bootcore_verify(&bc) == BOOT_ERR_CRC);
    CHECK(fake_app_valid(&f, &ops) == 0);

    free(img);
    free(f.mem);
}

// Direct checks of the portable erase-boundary / bounds predicates.
static void
check_predicates()
{
    boot_flash_geom f072 = boot_geom_stm32f072();
    // Page boundaries are erase starts; mid-page and out-of-range not.
    CHECK(boot_flash_is_erase_start(&f072, f072.app_base));
    CHECK(boot_flash_is_erase_start(&f072, f072.app_base + 0x800));
    CHECK(!boot_flash_is_erase_start(&f072, f072.app_base + 0x400));
    CHECK(!boot_flash_is_erase_start(&f072, f072.flash_base)); // bootloader
    CHECK(!boot_flash_is_erase_start(&f072, f072.info_addr));  // info page
    CHECK(boot_flash_in_app(&f072, f072.app_base, 16));
    CHECK(!boot_flash_in_app(&f072, f072.app_base + f072.app_size, 1));

    boot_flash_geom f4 = boot_geom_stm32f4();
    // Only the enumerated sector starts are erase points.
    CHECK(boot_flash_is_erase_start(&f4, 0x08008000));
    CHECK(boot_flash_is_erase_start(&f4, 0x08010000));
    CHECK(!boot_flash_is_erase_start(&f4, 0x08009000)); // mid-sector
    CHECK(!boot_flash_is_erase_start(&f4, 0x08060000)); // info sector
    CHECK(!boot_flash_is_erase_start(&f4, 0x08000000)); // bootloader

    boot_flash_geom g0 = boot_geom_stm32g0b1();
    CHECK(g0.dual_bank == 1);
    CHECK(g0.bank_size == 0x40000);
    CHECK(boot_flash_is_erase_start(&g0, g0.app_base));
}

int
main()
{
    printf("flashops geometry predicates\n");
    check_predicates();
    printf("per-target FlashOps flow\n");
    run_target("stm32f072", boot_geom_stm32f072());
    run_target("stm32g0b1", boot_geom_stm32g0b1());
    run_target("stm32f4", boot_geom_stm32f4());

    if (g_failures) {
        printf("%d FAILURE(S)\n", g_failures);
        return 1;
    }
    printf("all tests passed\n");
    return 0;
}
