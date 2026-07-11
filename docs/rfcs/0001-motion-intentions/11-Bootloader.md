# RFC 0001: First-Class Bootloader

Status: Draft / Discussion

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
* **The bootloader speaks the protocol.** It links the same MIT
  protocol library ([10-Protocol_Library.md](10-Protocol_Library.md))
  as the application — same framing, same dictionary mechanism, same
  Class-1 command semantics, same HMAC on network transports. A host
  that can talk to the application can talk to the bootloader with
  the same code path. No separate flashing tool, no second protocol.
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
* Signed firmware images (beyond transport HMAC): worthwhile once
  boards accept updates over networks — defer to the same phase as
  key provisioning ([07-Link_Transport.md](07-Link_Transport.md)).
* A/B application slots on large-flash single-bank parts (F4): worth
  the flash, or is bootloader-retry sufficient? (Proposed: retry is
  sufficient; A/B only where dual-bank hardware makes it free.)
