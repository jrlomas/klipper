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
| `jrlomas/klipper` | `claude/software-redesign-impl-finn0j` (current checkpoint) | Includes the Helix transport/security review, host Class-0 preflight, ESP resilience work, real ARM W5500/RMII plus ESP-IDF builds, and the Lolin32 secure-session hardware validation recorded below. |
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
* Machine-time state is refreshed after priming and checked for host-side
  freewheel freshness. Both stepper and PWM/DAC trajectory paths fail before
  advancing their fitted-intention twins when a secondary is not converged;
  firmware also refuses an unsynchronized rebase or segment.
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
  claim that any image has run on a board. The component task and bare modem
  core now have rebooting watchdog contracts, `reset` works in both
  architectures, WiFi reconnect no longer blocks the IDF event task, and the
  component UDP ring publishes slots with acquire/release ordering.
* A classic dual-core Lolin32 has run the component architecture on the real
  wired-host/WiFi-board LAN path.  Its 4MB flash geometry was verified, its
  authenticated rotating-key session presented the configured board identity,
  the 112-command dictionary loaded through Klippy, and periodic MCU `stats`
  keep-alives remained continuous during a non-motion soak.  Startup testing
  also found and fixed session nonce initialization before the GPTimer was
  ready.  This is component-console evidence only, not motion or peripheral
  qualification.
* `arm-none-eabi-gcc` 13.2.1 builds the native-RMII console as an
  authenticated STM32F407 image and as an authenticated, pair-FEC STM32F765
  image. The path includes configurable pins and reset, bounded MDIO,
  standards-based link negotiation/reconnect, DMA ownership barriers, the
  actual MCU console hooks, fail-closed PSK setup, and a stateful regression
  proving a dropped packet cannot replace the authenticated candidate peer.
  This is compiler/linker and host-test evidence, not PHY runtime evidence.
* The authenticated W5500 console has a persistent STM32F407 CI configuration.
  Its SPI command waits and counter reads are bounded, malformed receive
  lengths are rejected, an authenticated peer is cleared across hardware
  reinitialization, and a failed/reset chip is health-checked and reopened.
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

## Deferred integration requiring external inputs

No unblocked, workstation-only HELIX implementation seam remains in this
repository at this checkpoint. The following work requires boards,
measurements, a product security decision, or belongs to an explicitly
optional later architecture:

* **ESP32:** the Lolin32 component console now has real board evidence, while
  the modem image, timer/ISR jitter, FEC behavior, RMII, RMT/PCNT/FOC, and
  actual motion/peripheral paths remain unvalidated.  The ESP32 guide lists
  the required next measurements.
* **OAMS updater:** the canonical boot core and chunked `flash_sign` handler
  are vendored downstream, but the in-band update commands are deliberately
  unregistered because the product signing key and coexistence policy have
  not been provisioned. The shipped OAMS bootloader therefore remains on its
  existing Katapult/CRC-only path instead of exposing an unsigned updater.
* **Optional architecture work:** a native klippy UDP endpoint, bare-core
  ESP32 timer/RMT ISR, and richer packet FEC are optimizations or
  hardware-informed follow-ups, not missing correctness paths in the
  workstation checkpoint.

## Hardware and printer qualification

All remaining board, Pi, Hailo, signal-integrity, timing, thermal, fault-
injection, soak, and real-print evidence stays unchecked in the
[HELIX Test and Bring-up Plan](Helix_Test_Plan.md). Host emulation is useful
evidence, but it does not establish flash/RAM fit on every target, ISR jitter,
PWM waveform quality, native-RMII behavior on a real PHY, network behavior on
a real radio, or safe recovery on a moving and heated printer.

HELIX should not be called 1.0 or production-ready until the applicable
bring-up-plan evidence and product-key provisioning are recorded.
