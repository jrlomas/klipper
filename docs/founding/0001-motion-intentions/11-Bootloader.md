# FD-0001: First-Class Bootloader

Status: Canonical Klipper bootloader/update path implemented and
workstation-tested in HELIX 0.9; per-target hardware proof remains, and the
downstream OAMS port is staged but not yet registered in its CAN dispatcher.

In the current ecosystem the bootloader is an afterthought: Katapult
is a separate project, separately built, separately flashed, with its
own configuration step — and boards routinely ship without it,
leaving users juggling DFU pins and SD cards. For a fleet of boards
on a network ([07-Link_Transport.md](07-Link_Transport.md)) that is
untenable: firmware update is part of the *protocol*, and the
bootloader is part of the *firmware image*, from the beginning.

## Principles

* **One build, one flash.** Building a target produces a single image
  containing bootloader + application. First installation is the only
  time a programmer/DFU is ever required; everything after is
  in-band.
* **The bootloader speaks the protocol.** It links the MIT protocol
  library ([10-Protocol_Library.md](10-Protocol_Library.md)) — same
  framing, same dictionary mechanism, same Class-1 command semantics,
  same HMAC on network transports. A host that can talk to the
  application can talk to the bootloader with the same code path. No
  separate flashing tool, no second protocol.

  > **Note (envelope architecture).** The bootloader is the **one
  > in-repo, on-device image that links intentproto's full v1 core**
  > (`proto.cpp` + `dict.cpp`) rather than stock `command.c` — it is a
  > freestanding image and cannot link the application's command layer,
  > so it is the sanctioned exception to the quarantine rule in
  > [Upstream_Tracking](../../Upstream_Tracking.md). The application
  > firmware, by contrast, keeps stock Klipper's v1 path; the bootloader
  > and application therefore reach the *same wire behaviour* by two
  > different implementations that are held in agreement by the shared
  > wire specification and the library's conformance tests — not by
  > sharing code with the application.
* **Unbrickable by construction.** The bootloader region is never
  erased in-band. The application is CRC-verified at boot; a failed
  or interrupted update simply leaves the board in the bootloader,
  reachable over the same link, ready to retry. Power loss mid-update
  is a retry, not a paperweight.
* **MIT-licensed, like the library** — a vendor shipping a closed
  board needs a bootloader they can ship with it.

## Update flow

```
host: enter_bootloader              (Class 1, application)
  → app stamps request, resets
  → bootloader runs, announces itself (identify dictionary marks mode)
host: flash_begin size=... crc=...  (Class 1)
      flash_data offset=... <block> (Class 1, windowed)
      flash_verify → flash_boot
  → bootloader verifies whole-image CRC, marks app valid, jumps
```

* Entry paths: protocol command (normal case), boot-pin/double-reset
  (recovery), and absent/invalid application (automatic).
* The data path is ordinary Class-1 traffic — flow-controlled by the
  existing ack window, HMAC-authenticated on network links. An
  unauthenticated network peer cannot reflash a board, which is the
  entire security argument of
  [07-Link_Transport.md](07-Link_Transport.md) made concrete.
* Update of a live printer: the host drains/holds the board's queues
  first (pause-and-hold, [08-Failure_Recovery.md](08-Failure_Recovery.md));
  a board mid-print refuses `enter_bootloader` unless forced.

## Flash budgets (fleet targets)

| Target | Flash | Bootloader budget | Notes |
| --- | --- | --- | --- |
| STM32F072 (OpenAMS) | 128 KB | ≤ 16 KB | tightest; library embedded profile sized for this |
| STM32G0B1 (EBB36, pressure sensor) | 128–512 KB | ≤ 16 KB | dual-bank parts allow background update |
| STM32F4 (Octopus v1.x) | 512 KB–1 MB | ≤ 32 KB | large sectors dictate layout; app starts at a sector boundary |
| ESP32 (PoC) | ≥ 4 MB ext. | n/a | maps onto ESP-IDF's OTA machinery: same protocol commands, IDF A/B partitions underneath |

Where the silicon offers dual-bank flash (G0B1, some F4/H7), the
update writes the inactive bank and swaps atomically — zero downtime
worth having, but the single-bank flow above is the baseline every
target supports.

For ESP32, "A/B partitions underneath" is a packaging invariant, not merely
an optimization. The maintained IDF defaults select `TWO_OTA` (factory plus
two 1 MiB OTA slots on the qualified 4 MiB boards). A single-app partition
table leaves `esp_ota_get_next_update_partition()` without an inactive target:
the command dictionary still advertises the in-band flash ABI, but
`flash_begin` fails closed with `ERR_FLASH`. Existing single-app boards need
one final ROM-serial migration of bootloader, partition table, blank OTA-data,
and factory app; preserve NVS so network credentials and the PSK survive.
Every application update after that migration uses the normal authenticated
in-band A/B flow in the architecture. Hardware qualification remains open:
on 2026-07-23 a Rodent V1.1 booted a complete, hash-verified ROM-serial
factory/two-OTA install, but an authenticated application-side
`flash_begin` remained inside `esp_ota_begin()` beyond the 90-second host
bound while the WiFi/core-0 side remained pingable. Until the likely
core-affinity/flash-IPC deadlock is corrected and interrupted-update recovery
is exercised, USB-UART ROM flashing is the verified ESP32 recovery path and
the in-band path must not be the only field-update mechanism.

## Signed images

Transport HMAC ([07-Link_Transport.md](07-Link_Transport.md)) proves a
flash command came from a peer that holds the link key, and the
whole-image CRC proves the bytes arrived intact. Neither proves the
*image itself* is one the fleet operator authorised. The threat signed
images close is an **unsigned image swap over the update channel**: a
peer that has (or steals) the transport key, a compromised host, or a
supply-chain substitution of the combined `.bin` before first flash can
push a CRC-valid image the board will happily run. Ed25519 signatures
move trust from "whoever can talk on the wire" to "whoever holds the
release private key".

**Mechanism.** The bootloader verifies an Ed25519 (RFC 8032) signature
over the exact application image — the same bytes the CRC covers,
`[app_base, app_base+size)` — against a public key compiled into the
bootloader (`keys/helix_pubkey.h`). The device only ever *verifies*;
it never signs and never generates keys, so the MCU carries just a
verify routine (freestanding SHA-512 + Curve25519 field arithmetic,
`lib/intentproto/src/{sha512,ed25519}.cpp`). Signing happens off-device
(`scripts/sign_image.py`) with the private key held on the owner's
server.

**Layout.** The validity record's previously-reserved word is now a
`flags` word; `BOOT_INFO_FLAG_SIGNED` (bit 0) marks a signed image. The
64-byte signature is stored in the same info erase-unit immediately
after the 16-byte record (`info_addr + 16`). So the info page reads:

```
info_addr + 0 : {magic "BOAP", size, crc32, flags}   (16 bytes)
info_addr + 16: Ed25519 signature                     (64 bytes, if flags&SIGNED)
```

`scripts/build_combined.py --sign-key` (or `scripts/sign_image.py
combined`) computes the signature during/after assembly and sets the
flag; the very first boot of a freshly programmed board therefore finds
a signed, valid app with no in-band step.

**Verify flow.** In-band, the host sends the signature with
`flash_sign offset=%u data=%.*s` — chunked exactly like `flash_data`,
because a whole 64-byte signature plus command overhead cannot fit one
64-byte frame's payload (`bootcore_sign_data` enforces contiguous
chunks); `flash_verify` then gates on BOTH the CRC and the
signature (`bootcore_verify` then `bootcore_verify_signature`), and
`set_app_valid(1)` — which persists the flag + signature into the info
page — runs only if both pass. At every boot the port re-checks the
CRC *and*, when signing is enabled, re-verifies the stored signature
(`bootcore_app_sig_ok`) before jumping. Because flash is memory-mapped
the image is hashed in place; no extra buffer is needed. The
unbrickable-by-construction rule is unchanged: an unsigned, tampered, or
interrupted image simply fails the gate and leaves the board in the
bootloader, reachable over the same link — a retry, not a paperweight.

**Backward compatible.** All of this is compiled out unless the
bootloader is built with signing on (`make bootloader SIGNED=1`,
`CONFIG_WANT_SIGNED_IMAGES`). A CRC-only bootloader is byte-for-byte
unchanged and still boots unsigned images; a signing-enabled bootloader
*enforces* signatures (an unsigned image is refused).

**Key management.** The real release private key lives only on the
owner's server and is never committed. A **throwaway** dev keypair is
committed deliberately (`keys/helix_dev_signing.{key,pub}`, marked
DEV/TEST-only) so the mechanism can be built and tested end-to-end; it
**must be rotated before any real release** — see `keys/README.md`.
Rotation is: generate a new key off-repo, regenerate `helix_pubkey.h`
from its public half, ship bootloaders embedding the new key, sign
releases with the new private key.

**Size / fit tradeoff.** The verify code (Ed25519 + SHA-512) adds about
5.7 KiB of Cortex-M0 code (ed25519 ~3.7 KiB + sha512 ~2.0 KiB) and,
with the bootcore glue, ~6 KiB total. That fits the 32 KiB F4 budget
comfortably (10 960 → 16 684 bytes, +5 724) but does **not** fit the
16 KiB budget of the smallest parts: an STM32F072 signed bootloader
overflows its `rom` region by ~856 bytes. Per the standing policy, a
target that cannot fit a feature simply does not build it rather than
contorting — so **F072/G0B1 (16 KiB budget) build the CRC-only
bootloader; signed images are an F4-class (32 KiB budget) feature**.
Larger dual-bank parts, where they exist, have the room.

## Compatibility

* **Katapult coexistence:** boards already running Katapult can be
  migrated by one last Katapult-mediated flash of the combined image.
  A Katapult-compatible entry request (so existing host tooling's
  "request bootloader" path works) is cheap and proposed; full
  Katapult protocol emulation is not a goal.
* **ROM fallback forever:** the vendor ROM loaders (STM32 system DFU,
  ESP32 ROM serial) remain the documented recovery of last resort;
  nothing we do can or should remove them.

## Open questions

* Whether the bootloader dictionary is a static minimal one (proposed)
  or generated per-build like the application's.
* Signed firmware images (beyond transport HMAC): **implemented** — see
  "Signed images" above. Ed25519 verify in the bootloader, private key
  off-repo, dev key committed for the mechanism and rotated before
  release. Fits the F4-class budget; the 16 KiB parts stay CRC-only.
* A/B application slots on large-flash single-bank parts (F4): worth
  the flash, or is bootloader-retry sufficient? (Proposed: retry is
  sufficient; A/B only where dual-bank hardware makes it free.)
