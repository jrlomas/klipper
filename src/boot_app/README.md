# First-class bootloader application (RFC 0001 doc 11)

The flashable bootloader that ships *inside* the firmware image. It
links the same MIT protocol library (`lib/intentproto/`) as the
application — same framing, same dictionary mechanism, same Class-1
command semantics — so a host that can talk to the application can
reflash it with the identical code path. The portable update state
machine is `lib/intentproto/boot/bootcore.cpp` (`BootCore`); this
directory supplies the parts a real board needs around it:

| File | Role |
| --- | --- |
| `boot_main.cpp` | boot decision + the five Class-1 commands wired to `bootcore` |
| `boot_flash.h` / `boot_flash.c` | portable per-target geometry (host-testable) |
| `boot_flash_stm32f072.c` | F072 register driver (128 KB, 2 KB pages, 16-bit program) |
| `boot_flash_stm32g0b1.c` | G0B1 register driver (dual-bank aware, 2 KB pages, 64-bit) |
| `boot_flash_stm32f4.c` | F4 register driver (variable sectors, byte program) |
| `boot_stm32*.ld` | per-target link scripts; app region + `_boot_reqword` slot |

## Update flow

```
host: enter_bootloader force=0     (Class 1, application firmware)
  -> app stamps INTENTPROTO_BOOT_REQUEST in no-init RAM, resets
  -> boot_main() sees the request word, stays in the update loop
host: flash_begin size=.. crc32=.. (Class 1)
      flash_data offset=.. data=..  (Class 1, ack-windowed)
      flash_verify -> flash_boot
  -> bootcore CRC-checks the whole image, writes the validity record,
     resets; the next boot re-verifies the CRC and jumps.
```

Entry paths: the protocol command (normal), the boot request word
surviving a reset (as above), and an absent/invalid application
(automatic — `app_is_valid()` fails, so the loop is entered). The
bootloader region is never erased in-band, and the application is
CRC-verified at boot, so a failed or interrupted update simply leaves
the board in the bootloader, reachable over the same link — a retry,
not a paperweight.

## Per-target geometry (doc 11 fleet budgets)

| Target | Flash | Bootloader | App base | Erase unit | Validity page |
| --- | --- | --- | --- | --- | --- |
| STM32F072 | 128 KB | 16 KB | 0x08004000 | 2 KB page | last 2 KB page |
| STM32G0B1 | 512 KB | 16 KB | 0x08004000 | 2 KB page (bank-aware) | last 2 KB page |
| STM32F4 (F407) | 512 KB | 32 KB (sectors 0-1) | 0x08008000 (sector 2) | variable sector | sector 7 (128 KB) |

The portable predicates in `boot_flash.h`
(`boot_flash_is_erase_start`, `boot_flash_in_app`) and the
validity-record semantics are exercised on the desktop against a
RAM-backed fake flash by `lib/intentproto/tests/test_flashops.cpp` —
the same geometry the on-chip drivers use.

## Build status (combined-image build implemented)

The combined "one build, one flash" image is now a real, flashable
build. A bootable bootloader ELF is linked from the object set here +
a minimal startup + a polled UART transport + the intentproto protocol
library, and `scripts/build_combined.py` merges it with the
application at the app offset into a single artifact.

### The three build steps

**1. The bootloader image** — `make bootloader` (this directory):

```
$ make bootloader
== bootloader f072 ==
   boot-f072.bin = 11052 bytes (budget 16384)
== bootloader f4 ==
   boot-f4.bin = 10892 bytes (budget 32768)
```

It links: the object set here, `boot_startup.c` (a self-contained
Cortex-M reset vector + C-runtime + `.init_array` runner that calls
`boot_main`; it does *not* need the firmware's `armcm_boot.c` /
`compile_time_request` machinery), the polled UART transport
(`boot_uart_stm32f0.c` for F072/G0B1, `boot_uart_stm32f4.c` for F4),
and the intentproto library (`proto` + `dict` + `datagram` + `bch` +
`hmac`), with `boot_stm32<target>.ld`. `boot_startup.c` also supplies
the real `boot_system_reset` (NVIC reset) and `boot_jump_to_app`
(VTOR + SP/PC handoff) that `boot_main.cpp` declares weak.

**Transport: a polled UART, not USB.** The F072 budget is 16 KB and the
protocol library already fills most of it; a full USB-CDC stack does
not fit alongside it there, and would add an enumeration step before
the host could request an update. A polled UART needs no interrupts
(so the bootloader vector table stays trivial), is the same physical
link Katapult's serial recovery uses, and carries the *identical*
intentproto framing — a host that speaks the protocol to the
application speaks it to the bootloader unchanged (doc 11's core
promise). USART1 on PA9/PA10. USB in the bootloader is deferred to the
larger-flash / dual-bank targets where the budget allows it.

**2. The application at the app offset** — build the firmware as usual
with the new Kconfig bootloader-offset selection:

```
$ make menuconfig     # Bootloader offset ->
                      #   "16KiB first-class bootloader (intentproto)"  (F072/G0B1)
                      #   "32KiB first-class bootloader (intentproto)"  (F4)
$ make
```

These select `CONFIG_STM32_FLASH_START_IPBOOT_16K` / `_32K`, which set
`CONFIG_FLASH_APPLICATION_ADDRESS` to `0x08004000` / `0x08008000` and
(on F0) `CONFIG_ARMCM_RAM_VECTORTABLE=y` so the app's vector table is
relocated. The application links at the app base and `out/klipper.bin`
is produced normally.

**3. The combined image** — `scripts/build_combined.py`:

```
$ make combined TARGET=stm32f072 \
      BOOT_BIN=build/boot-f072.bin APP_BIN=../../out/klipper.bin \
      OUT=build/combined-f072.bin
combined image: build/combined-f072.bin (stm32f072)
  bootloader :  11052 bytes @ 0x08000000
  application:  56880 bytes @ 0x08004000 (crc32=0x51ff1e8f)
  validity   :     16 bytes @ 0x0801f800 (magic=0x50414f42)
  total      : 129040 bytes
```

The script (`zlib.crc32`, i.e. `intentproto::crc32`) lays out one raw
image: bootloader at offset 0, 0xFF pad to the app base, the
application image, 0xFF pad to the info page, then the 16-byte
`boot_info_record` `{magic, app_size, crc32, flags}` the bootloader
reads at boot. Stamping the validity record here means the *first* boot
of a freshly programmed board finds a valid app and jumps straight to it
— no in-band update needed to bootstrap. The single `combined.bin` is
flashed once at `0x08000000` by DFU/programmer; every later update is
in-band. `_boot_reqword` in the link script is pinned to the
application's `INTENTPROTO_BOOT_REQ_ADDR` (`src/generic/bootentry.h`)
so the `enter_bootloader` request survives the reset.

### Signed images (optional)

Built with `make bootloader SIGNED=1`, the bootloader also verifies an
Ed25519 (RFC 8032) signature over the application image before it marks
it valid or boots it (RFC 0001 doc 11, "Signed images"). The signature
covers the same bytes the CRC covers and is stored in the info page
right after the 16-byte record (`info_addr + 16`); the record's `flags`
word carries `BOOT_INFO_FLAG_SIGNED`. The public key is compiled in from
`keys/helix_pubkey.h`; the private key signs off-device.

Sign during assembly, or sign an assembled image:

```
# one step:
../../scripts/build_combined.py stm32f4 build/boot-f4.bin ../../out/klipper.bin \
    -o build/combined-f4.bin --sign-key ../../keys/helix_dev_signing.key
# or after the fact:
../../scripts/sign_image.py combined stm32f4 build/combined-f4.bin \
    --key ../../keys/helix_dev_signing.key -o build/combined-f4.signed.bin
```

**Fit:** the Ed25519+SHA-512 verify adds ~6 KB, which does not fit the
16 KB bootloader budget of STM32F072/G0B1 — those stay CRC-only. Signed
images are an F4-class (32 KB budget) feature: `make bootloader SIGNED=1`
builds F4 signed and leaves F072 CRC-only. The committed signing keypair
is a **throwaway dev key** that must be rotated before release — see
[`keys/README.md`](../../keys/README.md). Verification code and vectors
live in `lib/intentproto` (`sha512`, `ed25519`, `test_sha512`,
`test_ed25519`, and the Python↔C crosscheck `tools/test_ed25519_e2e.py`).

### What remains hardware-gated

The register-level flash drivers (`boot_flash_stm32*.c`) and the UART
bring-up (`boot_uart_*.c`) are compile/link verified but not
hardware-tested — there is no board in the build environment. The
polled UART clock/baud divisor assumes the reset-default kernel clock
(documented in each `boot_uart_*.c`); a board that runs its USART from
a different clock adjusts `BOOT_UART_FCK`. On-chip validation (an
actual `enter_bootloader` -> `flash_*` -> jump cycle over the wire) is
the remaining step and needs the target silicon.
