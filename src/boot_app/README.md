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

## Build status (honest)

This is delivered as a **staged, compile-checked object set**, *not*
a wired-in combined-image build. `make check` (this directory) cross-
compiles every translation unit for cortex-m0 / m0plus / m4 against
the vendored CMSIS headers and reports per-target object sizes. The
register drivers are compile-checked, not hardware-tested here.

Reasons it is staged rather than a full `out/klipper+boot.bin` this
pass: a bootable ELF additionally needs the port startup
(`src/generic/armcm_boot.c`, whose `ResetHandler` would call
`boot_main`) and the transport glue (a USB-CDC / UART / CAN
implementation of the weak `boot_link_read` / `boot_link_write`
seam), plus a `boot_system_reset` / `boot_jump_to_app` port hook. Those
are per-board and out of scope for this pass; the seams are marked
`__attribute__((weak))` so the object set links and compile-checks
standalone.

### How the combined "one build, one flash" image is assembled

1. Build the bootloader ELF: the object set here + `armcm_boot.c` +
   the board transport, linked with `boot_stm32<target>.ld`; objcopy
   to `boot.bin` at `0x08000000`.
2. Build the application as usual with
   `CONFIG_FLASH_APPLICATION_ADDRESS` set to the app base above
   (existing `STM32_FLASH_START_*` choice), producing `klipper.bin`.
3. Concatenate: `boot.bin` padded to the app base, then `klipper.bin`.
   The result is a single image flashed once by DFU/programmer; every
   later update is in-band. `_boot_reqword` in the link script is
   pinned to the application's `INTENTPROTO_BOOT_REQ_ADDR`
   (`src/generic/bootentry.h`) so the entry request survives the reset.

Wiring this into the top-level Makefile (a `WANT_BOOTLOADER`-gated
combined-image target) is the natural next step and is intentionally
deferred so the entry command (part 1) could land and be size-verified
independently.
