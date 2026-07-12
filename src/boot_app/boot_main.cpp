// First-class bootloader application main (RFC 0001 doc 11).
//
// The bootloader speaks the same MIT protocol library as the
// application (proto.hpp): same framing, same dictionary mechanism,
// same Class-1 command semantics. A host that can talk to the
// application can reflash it with the identical code path — no
// separate flashing tool. The update state machine is the portable
// bootcore; this file supplies the FlashOps table (from the
// per-target driver), the boot decision, and the transport seam.
//
// Freestanding C++: no heap, no exceptions, no RTTI. Built into the
// combined "one build, one flash" image described in README.md.

#include "../../lib/intentproto/boot/bootcore.hpp"
#include <intentproto/method.hpp>
#include <intentproto/proto.hpp>

// Signed firmware images (RFC 0001 doc 11, "Signed images"). Entirely
// compiled out unless the bootloader is built with signing enabled; an
// unsigned build behaves exactly as before (CRC-only validity). The
// embedded public key is generated from keys/helix_dev_signing.pub —
// see keys/README.md. The private key never lives on the device.
#ifdef CONFIG_WANT_SIGNED_IMAGES
#include "../../keys/helix_pubkey.h"
#endif

extern "C" {
#include "boot_flash.h"
// Per-target driver (exactly one boot_flash_stm32*.c is linked).
const struct boot_flash_geom *boot_active_geom(void);
int boot_flash_erase(uint32_t addr);
int boot_flash_write(uint32_t addr, const uint8_t *data, size_t len);
const uint8_t *boot_flash_read(uint32_t addr);
int boot_flash_erase_info(const struct boot_flash_geom *g);
int boot_flash_write_info(const struct boot_flash_geom *g, uint32_t size,
                          uint32_t crc, uint32_t flags, const uint8_t *sig);

// Transport seam. The port supplies these (USB CDC / UART / CAN);
// weak defaults let the object set link and compile-check standalone.
// boot_link_read returns bytes read (0 if none); boot_link_write
// transmits a whole frame.
int __attribute__((weak)) boot_link_read(uint8_t *buf, int cap)
{
    (void)buf;
    (void)cap;
    return 0;
}
int __attribute__((weak)) boot_link_write(const uint8_t *buf, int len)
{
    (void)buf;
    return len;
}

// No-init request word, placed by the linker script at the same
// physical RAM address as the application's INTENTPROTO_BOOT_REQ_ADDR
// (src/generic/bootentry.h). Survives the software reset.
extern volatile uint64_t _boot_reqword;
}

using namespace intentproto;

// The request magic the application stamps (mirror of
// INTENTPROTO_BOOT_REQUEST in bootentry.h).
static const uint64_t BOOT_REQUEST_MAGIC = 0x49504200544f4f42ULL;

// ---- FlashOps table over the per-target driver ----
// ops.user carries the geometry pointer; the trampolines apply the
// portable bounds / erase-boundary logic (boot_flash.h) and defer the
// register work to the driver.
static BootCore g_bc;

static int
ops_erase(uint32_t addr, void *user)
{
    const boot_flash_geom *g = (const boot_flash_geom *)user;
    if (!boot_flash_is_erase_start(g, addr))
        return 0; // mid-page write: the page was erased at its start
    return boot_flash_erase(addr);
}

static int
ops_write(uint32_t addr, const uint8_t *data, size_t len, void *user)
{
    const boot_flash_geom *g = (const boot_flash_geom *)user;
    if (!boot_flash_in_app(g, addr, len))
        return -1;
    return boot_flash_write(addr, data, len);
}

static const uint8_t *
ops_read(uint32_t addr, void *user)
{
    (void)user;
    return boot_flash_read(addr);
}

static int
ops_set_valid(int valid, void *user)
{
    const boot_flash_geom *g = (const boot_flash_geom *)user;
    if (!valid)
        return boot_flash_erase_info(g);
    // Persist the signed flag and signature (bootcore set them only
    // after the signature verified); an unsigned image has flags==0.
    const uint8_t *sig = (g_bc.flags & BOOTCORE_FLAG_SIGNED)
                             ? g_bc.signature : nullptr;
    return boot_flash_write_info(g, g_bc.image_size, g_bc.image_crc,
                                 g_bc.flags, sig);
}

static FlashOps g_ops;

static void
build_ops(void)
{
    const boot_flash_geom *g = boot_active_geom();
    g_ops.app_base = g->app_base;
    g_ops.app_size = g->app_size;
    g_ops.erase_sector = ops_erase;
    g_ops.write = ops_write;
    g_ops.read = ops_read;
    g_ops.set_app_valid = ops_set_valid;
    g_ops.user = (void *)g;
}

// ---- protocol surface: the five Class-1 boot commands ----
// The data path is ordinary Class-1 traffic; the ack window provides
// flow control (doc 11).

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

#ifdef CONFIG_WANT_SIGNED_IMAGES
// flash_sign data=<64-byte Ed25519 signature> — the host supplies the
// signature over the exact application image (the CRC'd bytes) before
// flash_verify. Only present in a signing-enabled bootloader.
KLIPPER_METHOD(flash_sign, (buf, data))
{
    int rc = (data.len == BOOT_SIG_SIZE)
                 ? bootcore_set_signature(&g_bc, data.data)
                 : BOOT_ERR_RANGE;
    reply(flash_result{OP_SIGN, (uint8_t)rc, (uint32_t)data.len});
}
#endif

KLIPPER_METHOD0(flash_verify)
{
    int rc = bootcore_verify(&g_bc);
#ifdef CONFIG_WANT_SIGNED_IMAGES
    // Signature is a second, mandatory gate: the app is not marked
    // valid unless BOTH the CRC and the Ed25519 signature pass.
    if (rc == BOOT_OK)
        rc = bootcore_verify_signature(&g_bc, helix_pubkey);
#endif
    reply(flash_result{OP_VERIFY, (uint8_t)rc, g_bc.image_crc});
}

// Mark valid, then reset so the freshly written app runs from a clean
// vector table (the jump is performed by boot_main at the next boot,
// which re-checks the CRC — unbrickable by construction).
extern "C" void boot_system_reset(void); // port hook (NVIC_SystemReset)
void __attribute__((weak)) boot_system_reset(void) {}

KLIPPER_METHOD0(flash_boot)
{
    int rc = bootcore_boot(&g_bc);
    reply(flash_result{OP_BOOT, (uint8_t)rc, 0});
    if (rc == BOOT_OK)
        boot_system_reset();
}

// enter_bootloader is answered by the application; inside the
// bootloader it simply acknowledges that we are already here.
KLIPPER_METHOD(enter_bootloader, (uint8_t, force))
{
    (void)force;
    reply(flash_result{OP_ENTER, BOOT_OK, 0});
}

// ---- boot decision ----

// Read the validity record and CRC-check the stored image. Returns 1
// when the application may be jumped to.
static int
app_is_valid(void)
{
    const boot_flash_geom *g = boot_active_geom();
    const boot_info_record *rec =
        (const boot_info_record *)boot_flash_read(g->info_addr);
    if (rec->magic != BOOT_INFO_MAGIC)
        return 0;
    if (!rec->size || rec->size > g->app_size)
        return 0;
    if (!bootcore_app_crc_ok(&g_ops, rec->size, rec->crc))
        return 0;
#ifdef CONFIG_WANT_SIGNED_IMAGES
    // Enforced signing: the stored image must be marked signed and its
    // signature (stored right after the record) must verify against the
    // embedded key. A CRC-only image is refused here — the same
    // re-verification that makes an interrupted update a retry now also
    // makes an unsigned or tampered image a non-boot.
    if (!(rec->flags & BOOT_INFO_FLAG_SIGNED))
        return 0;
    const uint8_t *sig = boot_flash_read(g->info_addr + BOOT_INFO_SIG_OFFSET);
    if (!bootcore_app_sig_ok(&g_ops, rec->size, sig, helix_pubkey))
        return 0;
#endif
    return 1;
}

// Standard Cortex-M application handoff: load the app's stack pointer
// and reset vector and branch. Defined weak so the host build (which
// has no vector table) links; the arm port provides the real jump.
extern "C" void boot_jump_to_app(uint32_t app_base);
void __attribute__((weak)) boot_jump_to_app(uint32_t app_base)
{
    (void)app_base;
}

// ---- link glue ----

static int
link_write(const uint8_t *data, size_t len, void *user)
{
    (void)user;
    return boot_link_write(data, (int)len);
}

// Entry point invoked by the port after low-level init. Stays in the
// update loop when the application is absent/invalid or an entry was
// requested; otherwise hands off to the application.
extern "C" void
boot_main(void)
{
    build_ops();
    bootcore_init(&g_bc, &g_ops);

    int requested = (_boot_reqword == BOOT_REQUEST_MAGIC);
    _boot_reqword = 0; // consume the request

    if (!requested && app_is_valid()) {
        boot_jump_to_app(boot_active_geom()->app_base);
        return;
    }

    Config cfg;
    cfg.write = link_write;
    cfg.version = "intentproto-boot";
    cfg.build_version = "boot";
    init(cfg);

    for (;;) {
        uint8_t rxbuf[64];
        int n = boot_link_read(rxbuf, sizeof(rxbuf));
        if (n > 0)
            rx(rxbuf, (size_t)n);
    }
}
