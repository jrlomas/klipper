# HELIX 0.9 Implementation Status

Last workstation audit: 2026-07-13.

This page is the boundary between code that exists, code that has actually
run in workstation verification, target integration that remains, and tests
that require boards or a printer. It supersedes the earlier blanket
"software complete" label; that label was not supported by the tree.

## Checkpoint

The reviewed workstation state is committed and published, not just present
in a local working tree:

| Repository | Published checkpoint | Notes |
| --- | --- | --- |
| `jrlomas/klipper` | `claude/software-redesign-impl-finn0j` (ESP32 implementation checkpoint `6b251da9`) | Includes the Helix transport/security review plus real ESP-IDF component, RMT, and modem build fixes; this document is committed on top as the status checkpoint. |
| `jrlomas/mainsail` | `fe5d30a9` on `claude/software-redesign-impl-finn0j` | Atlas/OpenAMS panels merged with `mainsail-crew/develop` at `e9e33c11`; unit tests, lint, formatting, and production build pass. |
| `OpenAMSOrg/mainboard-firmware` | `6ff33f0` on `claude/software-redesign-impl-finn0j` | OAMS protocol-library sync, regenerated identify blob, and updater staging; updater limitations are recorded below. |
| `OpenAMSOrg/klipper_openams` | `b350ecc` on `master` | Audited with no Atlas/intentproto drift requiring a code change. |

These hashes identify the software checkpoint before the next Atlas model
work. They do not convert any unchecked target or hardware item into a pass.

## Verified on this workstation

* The standalone `intentproto` C/C++ suite, C ABI, CFFI API, extension
  binding, secure-session binding, datagram carrier, segment fitter,
  Ed25519 cross-check, and boot-core tests pass.
* A real `linuxprocess` firmware proves legacy/v2 dual acceptance, the v2
  latch, and three-bit BCH correction against the host codec.
* A real `linuxprocess` UDP responder proves the three-message session,
  mandatory expected board identity, authenticated command/reply traffic,
  tamper rejection, live-session preservation, legitimate re-handshake,
  ClientHello PSK proof, and protection of the authenticated reply peer from
  an untrusted ClientHello.
* The PWM/DAC value path passes both the shared C segment-fitter test and the
  bounded scalar-function preflight/terminal-hold test.
* The signed flasher/boot simulator tests include chunked 64-byte signatures,
  unsigned-image refusal, and bad-signature rejection.
* Static datagram FEC uses bounded pair blocks: tests drop either the first or
  second packet, reconstruct it from authenticated parity, and prove recovered
  frames reach the consumer in original order. Unsupported block sizes are
  rejected instead of silently weakening that guarantee.
* Pinned ESP-IDF v5.3.2 and `xtensa-esp-elf` 13.2.0 build the ESP32 component,
  component-RMT, and unicore modem images. The modem link map confirms the
  private vectors and selected motion-hot objects land in IRAM with 33,450B
  remaining in the 128KiB region. These are compiler/linker results, not a
  claim that any image has run on a board.
* The full deterministic Atlas workstation suite passes. The Mainsail Atlas
  and OpenAMS panels pass 46 unit tests across 7 test files, lint, formatting,
  and a production build after merging the current upstream `develop` branch.
* The downstream OAMS protocol port regenerates an identical checked-in
  identify blob and its host protocol/introspection test passes with stable
  OAMS message IDs plus the library meta messages.

The dedicated Helix linuxprocess configurations and live tests are now part
of `scripts/ci-build.sh`. `HELIX_REQUIRE_LIVE=1` turns a missing feature build
into a failure, so these tests can no longer silently skip while CI reports
success.

## Remaining software and target integration

These are not hardware measurements; they are code/integration work still
visible in the repositories:

* **Native STM32 RMII:** the nano-UDP layer is host-tested, but MAC/PHY reset,
  pin map, PHY address/negotiation, and IP configuration remain board seams.
* **ESP32:** all maintained variants now have real Xtensa compiler/linker
  evidence. Board runtime remains unvalidated; the ESP32 guide lists the
  devkit procedure plus keepalive/reconnect, ISR bring-up, FEC measurement,
  reset/watchdog, RMII, and RMT/FOC follow-ups.
* **OAMS updater:** the canonical boot core and chunked `flash_sign` handler
  are vendored downstream, but the in-band update commands are deliberately
  unregistered and the product signing key is not provisioned. The shipped
  OAMS bootloader therefore remains on its existing Katapult/CRC-only path.
* **Heterogeneous fleet enforcement:** the timing substrate exists, but the
  new FD-0001 doc 14 correctly labels host enforcement as future work.

## Hardware and printer qualification

All remaining board, Pi, Hailo, signal-integrity, timing, thermal, fault-
injection, soak, and real-print evidence stays unchecked in the
[HELIX Test and Bring-up Plan](Helix_Test_Plan.md). Host emulation is useful
evidence, but it does not establish flash/RAM fit on every target, ISR jitter,
PWM waveform quality, network behavior on a real radio/PHY, or safe recovery
on a moving and heated printer.

HELIX should not be called 1.0 or production-ready until the remaining
software seams are closed and the applicable bring-up-plan evidence is
recorded.
