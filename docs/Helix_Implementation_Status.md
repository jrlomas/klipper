# HELIX 0.9 Implementation Status

Last workstation and V0 hardware audit: 2026-07-14.

This page is the boundary between code that exists, code that has actually
run in workstation verification, target integration that remains, and tests
that require boards or a printer. It supersedes the earlier blanket
"software complete" label; that label was not supported by the tree.

## Checkpoint

The reviewed workstation state is committed and published, not just present
in a local working tree:

| Repository | Published checkpoint | Notes |
| --- | --- | --- |
| `jrlomas/klipper` | current tip of `claude/software-redesign-impl-finn0j` | Contains the reviewed transport/security and V0 hardware work plus the Atlas LLM audit remediation recorded below. Use the published branch tip as the immutable hash; this document does not self-reference its own commit. |
| `jrlomas/mainsail` | `28807856` on `claude/software-redesign-impl-finn0j` | Atlas/OpenAMS panels merged with `mainsail-crew/develop` at `e9e33c11`; Atlas is centered, bounded to ten visible events, and responsive; unit tests, lint, formatting, and production build pass. |
| `OpenAMSOrg/mainboard-firmware` | `6ff33f0` on `claude/software-redesign-impl-finn0j` | OAMS protocol-library sync, regenerated identify blob, and updater staging; updater limitations are recorded below. |
| `OpenAMSOrg/klipper_openams` | `b350ecc` on `master` | Audited with no Atlas/intentproto drift requiring a code change. |

These checkpoints do not convert any unchecked target or hardware item into a
pass.

## Verified on this workstation

* The standalone `intentproto` C/C++ suite, C ABI, CFFI API, extension
  binding, secure-session binding, datagram carrier, segment fitter,
  Ed25519 cross-check, and boot-core tests pass.
* Phase 0 of the acceptance plan is green in a single 2026-07-14 pass.
  Direct segfit sampling covers a straight trapezoid, 48-chord quarter arc,
  and finite-junction-speed corner within the 32,768-sub-unit tolerance; a
  4,000-segment higher-order chain remains bit-exact. Bring-up exposed and
  fixed truncation when a flush horizon fell between sampling ticks: the
  fitter now includes that exact endpoint and retains the completed move at
  an exact trapq boundary. Linuxprocess, STM32F407, and STM32G0B1 all link
  after the fix, and the prior homing/pulse/wrap regressions remain green.
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
* Workstation remediation for the EBB36 disconnected-island failure restores
  a fresh trapq-derived extrusion anchor at every pressure-advance/retraction
  island. Secondary local-time streams now carry an immutable local rebase
  boundary alongside their shared machine-time intent, preventing later
  discipline changes from moving a boundary into queued work. The focused
  host regressions and Pico/EBB36 target builds pass. A full 99-layer replay
  with regenerated dictionaries produced 422 E rebases and 1,135,901 HELIX
  pulses versus 1,134,514 V1 pulses, with no interval at or below 64 ticks.
  Exact `75f03262` images were then signed, archived, and flashed to both V0
  boards. Klippy identified that version and ABI `27141a58f61f9fbc` on each;
  all five onboard tests passed on both boards and the EBB36 discipline
  reconverged within 2.5 us. Its first supervised hot print exposed one
  remaining mixed-clock defect: the local-time E stream ended with legacy
  `traj_hold`, whose duration firmware interpreted as machine ticks. The host
  therefore undercounted the EBB36 horizon before the next local rebase.
  `traj_hold_local` now preserves the timer domain for fitted zero spans and
  terminal holds, while secondary setup rejects firmware lacking that ABI.
  The offline sliced-G-code replay now advances both hold domains and rejects
  a rebase before the wrap-safe local horizon; all 99 layers pass with 423
  local E holds and no E interval at or below 64 ticks. Exact Pico and EBB36
  target builds pass.
  The rejected print-long E-stream workaround physically over-extruded and
  `75f03262` is not considered physically qualified; the new hold fix required
  fresh flashing, onboard tests, and a supervised hot print. Exact clean
  commit `8ca65c37` images were subsequently signed, archived, and flashed to
  both boards. Each identified the expected ABI, passed all five onboard
  tests, and returned Klipper to `ready` after EBB36 discipline reconverged.
  The remaining gate is a supervised physical print; qualification did not
  issue a heater target.
* The signed flasher/boot simulator tests include chunked 64-byte signatures,
  unsigned-image refusal, and bad-signature rejection.
* Static datagram FEC uses bounded pair blocks: tests drop either the first or
  second packet, reconstruct it from authenticated parity, and prove recovered
  frames reach the consumer in original order. Unsupported block sizes are
  rejected instead of silently weakening that guarantee.  A real Lolin32
  component image also loaded its dictionary and emitted stats through a UDP
  proxy that deliberately dropped the first packet of a protected pair.
* Pinned ESP-IDF v5.3.2 and `xtensa-esp-elf` 13.2.0 build the ESP32 component,
  component-RMT, and unicore modem images. The modem link map confirms the
  private vectors and selected motion-hot objects land in IRAM. These are
  compiler/linker results; the separate board evidence below defines what has
  actually run. The component task and bare modem
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
* The same Lolin32 has run the unicore modem architecture: core 0 brought up
  WiFi while core 1 booted bare Klipper with its private vectors, APP flash
  cache mapping, polled timer, and shared-memory console ring.  Static-HMAC
  and rotating-key session bridges each loaded all 112 commands and delivered
  repeated five-second MCU stats; a stopped/restarted host bridge established
  a fresh authenticated session and repeated the result.  Bring-up found and
  fixed the missing APP cache-bus enable, canonical window-stack bootstrap,
  and syscall-0 window spill required by ROM `setjmp`.  This is boot/console
  evidence, not motion, peripheral, ISR-jitter, or thermal qualification.
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
* A real V0 rig now runs this branch with a USB SKR Pico (RP2040, 12 MHz
  Klipper scheduler clock and 200 MHz core) and USB EBB36 v1.2 (STM32G0B1,
  64 MHz). The Pico dictionary now distinguishes
  `MCU_CORE_FREQ=200000000` from `CLOCK_FREQ=12000000`, and Mainsail displays
  the former while Klipper continues to schedule against the latter. The signed
  provisioning executor verified Ed25519 signatures, rebuilt from archived
  Kconfig, required exact artifact equality, flashed both normal bootloader
  paths, and recorded the successful jobs. Both boards now run `d2a9d7a5`,
  advertise ABI `27141a58f61f9fbc`, and `HELIX_STATUS` reports fleet lockstep.
* All five live self-tests passed on both V0 boards after a standard
  `FIRMWARE_RESTART`; on `d2a9d7a5` Pico RTT was 0.24 ms and EBB36 RTT was
  0.27 ms. Both MCU
  reset implementations re-enumerated, reconfigured, and returned Klipper to
  ready without manual intervention.
* The STM32G0B1 crossing solver now has an explicit practical EBB36 envelope.
  The on-silicon gate covers zero-velocity startup and time-compresses a
  captured quintic through approximately 20,000 extruder steps/s while
  enforcing a 1/8-step spatial bound and 25% timer reserve. A 40,000-step/s
  probe exceeded that reserve and remains unclaimed. Independent V1/HELIX
  comparisons match full edge counts and directions for homing, reverse,
  phase-wrap, short, and 19.2k-step/s profiles. Both freshly flashed boards
  passed all five self-tests (`traj_kernel=PASS`): EBB36 RTT 0.26 ms and Pico
  RTT 0.18 ms. The rationale, graphs, negative result, and reproduction steps
  are in [STM32G0B1 HELIX motion qualification](STM32G0B1_Helix_Qualification.md).
* A real sliced-G-code V1/HELIX differential now exercises the complete Klippy
  planner and retains the production MCU solver state across every crossing.
  It exposed a defect hidden by the earlier mathematical endpoint replay: on
  the failed benchmark session the solver fell behind by up to 571 X steps and
  later generated compressed catch-up pulses. Boundary predictions are now
  spatially validated, the cheap recurrence has a bounded exact fallback with
  nearest-tick selection, and errors beyond 1/4 step fail closed. Full
  captured-session replay has zero
  endpoint mismatches or <=64-tick bursts; a two-layer offline run through
  solid infill has continuous edge streams on X/Y/Z/E. Workstation tests and
  both target builds pass. A subsequent 100% physical retry exposed two more
  edge cases before completion: the host direction check rejected an all-zero
  CoreXY cancellation segment, and a cold solve after a direction reversal
  could fall through to a one-tick interval. Zero polynomials now have no
  manufactured direction, and invalid cold higher-order solves use the same
  bounded nearest-tick sign bracket. Exact cube X/Y/E reversal regressions are
  committed. The corrected 100% two-layer replay expands 317,607 X, 323,300
  Y, 1,280 Z, and 63,842 E edges with respective minimum intervals of 260,
  256, 1,353, and 4,755 ticks and zero intervals at or below 64 ticks. Pico,
  EBB36, and Linux builds plus Linuxprocess live self-tests pass. Flashing the
  new images and a supervised physical print remain open, so benchmark item
  14.2 is not yet a hardware pass.
* Repeat supervised runs on `5f652c6e` separated two later failure modes. A
  disabled-trace comparison bug first generated 257 trace records/s on the
  EBB36 and destabilized time discipline; 2,048 live disabled probes now
  produce zero records. With that fix holding, an uninterrupted print stopped
  at an E rebase whose local deadline was 19,931 ticks (311.4 us) in the past.
  The recorder proved timesync was converged, both USB links had zero invalid
  bytes, and the MCU had not overrun its solver. The host scanner had returned
  a historical pressure-advance pre-active start after generation was already
  inside that window. It now clips the activity boundary to the generation
  cursor, matching stock itersolve's `last_flush_time` rule. The focused host
  suite and the same cube's 100% two-layer replay pass (63,846 E edges,
  4,896-tick minimum, no <=64-tick interval). Klipper is ready with converged
  Class-0 time after restart; a supervised repeat remains required, so this is
  not yet a repeatable full-print qualification.
* Hot ABS extrusion at 260 C completed through the EBB36: +10 mm at 2 mm/s,
  +5 mm at 10 mm/s, and a bounded -2/+2 mm retract cycle, staged at
  X=60/Y=60/Z=100. The focused +5 mm audit reconciled 3,529 intended and
  3,529 executed pulses with identical 8,762-tick minimum intervals and zero
  errors. Mixed-clock telemetry now retains both 12 MHz machine-time and
  64 MHz EBB execution-time fields; the audit can anchor legacy captures from
  the execution rebase. Heaters were returned to target zero after the test.
* The 64 MHz EBB36 disciplined to the 12 MHz Pico's machine time for ten
  minutes without losing lock, including 32-bit local-clock wraps. Final
  error was 36 EBB ticks (0.56 us). It reconverged after restart; the
  remaining physical coordinated-pin/scope test and CAN repetition are still
  open.
* Structured trace is live-qualified on both boards. Registered diagnostic
  records rendered and merged in cross-board machine-time order. Under a
  256-record burst, each 64-record ring reported 192 overwrites, the host saw
  exactly 192 sequence gaps, and paced draining left zero unaccounted gaps or
  write errors. This validates the trace carrier and accounting, not the
  motion-path `step_underrun` call-site or trace-off step-timing cost.
* Trajectory homing now has live V0 evidence. Independent `G28 X` and `G28 Y`
  completed. The Z override then exposed and fixed three distinct defects:
  signed trigger readback, an unsealed lift-to-home rebase boundary, and the
  former ±32768-microstep accumulator range. Signed, byte-reproducible
  `fdad253f` images were flashed to both boards; `G28 Z` completed its lift,
  two trigger approaches, retract, and move to Z=30 while Klipper remained
  ready. Its narrow flight-recorder audit replayed 126 planned segments and
  73,995 executed pulses, matched 124 completed boundaries and two trigger
  stops, found five explicit holds, and reported zero errors. The recorded
  unwrapped path crossed -2³¹ sub-units while the compact wire phase wrapped
  exactly as designed.
* Normal G0/G1 is now the production quintic path for trajectory steppers.
  Klippy retains Cartesian lookahead, kinematics, and the authoritative
  toolhead position; the fitter sends synchronized per-joint quintic
  intentions and the boards synthesize their own pulses. Quadratic is an
  explicit compatibility setting, and migrated steppers never configure
  `queue_step`. On the live V0, a fresh full `G28` completed on `d2a9d7a5`
  without TMC UART or underrun errors. A coordinated X/Y/Z move from
  `[110,110,30]` to `[60,60,40]` updated the toolhead object automatically,
  then a normal G1 returned Z to 30 without `SET_KINEMATIC_POSITION`. The two
  narrow deterministic audits replayed 32,000 physical pulses, matched 77 MCU
  boundaries, and reported zero errors; all emitted motion records used
  quintic order flags and all three post-move TMC `GSTAT` reads were zero.
* RP2040 homing now uses IO_BANK0 edge interrupts instead of periodic endstop
  polling. Fresh X, Y, and Z homes completed on `e1ec0b9e`; each flight-recorder
  window contained a distinct hardware-source record before its actuator stop
  record. The 261–300 tick gap (21.8–25.0 us at 12 MHz) matches the configured
  20 us qualification plus dispatch. The RP2040 timestamp is read at ISR entry,
  not by timer input capture. The final full X/Y/Z homing and self-test run
  accumulated zero invalid bytes and no retransmissions while Klipper remained
  ready. Repeatability, forced-polled comparison, and scoped physical edge-to-
  stop latency remain open measurements.
* The deterministic Atlas decoder diagnosed a genuine earlier host/MCU
  `sync_beacon` format fault from the live V0 log and captured the unmatched
  case for the knowledge base. Its real-machine GitHub-issue bundle passed
  manual review under the numeric-only policy with no hostname, key, USB
  serial, or filesystem path exposed.
* The full deterministic Atlas workstation suite passes. The Mainsail Atlas
  and OpenAMS panels pass 50 unit tests across 8 test files, lint, formatting,
  and a production build after merging the current upstream `develop` branch.
  The served build matches the production artifact byte-for-byte. Atlas now
  shares the center dashboard column with Temperatures, shows the newest ten
  matching events, and uses a wrapping, bounded, full-width table so later
  columns remain visible.
* The Atlas LLM audit remediation replaces whole-config model output with
  deterministic targeted edits, fences untrusted prompt data, grounds
  read-only questions in bounded config context, uses scored BM25 retrieval,
  confirms unknown/executable config semantics by default, verifies structured
  event references, verifies same-UID IPC peers, and exposes lock-free bounded
  queue/latency/token/load/error/proposal status. Corpus v2 contains 50 cases
  with deterministic and model metrics reported separately. Its contract suite
  passes. The pinned model then passed all six per-kind metrics on both CUDA
  and ROCm on 2026-07-14; Hailo validation remains open.
* The downstream OAMS protocol port regenerates an identical checked-in
  identify blob and its host protocol/introspection test passes with stable
  OAMS message IDs plus the library meta messages.

The dedicated Helix linuxprocess configurations and live tests are now part
of `scripts/ci-build.sh`. `HELIX_REQUIRE_LIVE=1` turns a missing feature build
into a failure, so these tests can no longer silently skip while CI reports
success.

## Deferred integration requiring external inputs

The remaining items require boards, measurements, a product
security decision, or belong to an explicitly optional later architecture:

* **ESP32:** the Lolin32 component and bare-core modem consoles now have real
  board evidence, and controlled-loss pair FEC has recovered traffic on its
  WiFi link. Timer/ISR jitter, FEC cost/benefit under natural loss, RMII,
  RMT/PCNT/FOC, and actual motion/peripheral paths remain unvalidated. The
  ESP32 guide lists the required next measurements.
* **OAMS updater:** the canonical boot core and chunked `flash_sign` handler
  are vendored downstream, but the in-band update commands are deliberately
  unregistered because the product signing key and coexistence policy have
  not been provisioned. The shipped OAMS bootloader therefore remains on its
  existing Katapult/CRC-only path instead of exposing an unsigned updater.
* **Atlas deploy validation:** workstation corpus v2 is green on CUDA and
  ROCm. The Hailo backend remains unavailable until Qwen3-4B is compiled and
  evaluated on the Pi 5 + Hailo-10H target.
* **Optional architecture work:** a native klippy UDP endpoint, bare-core
  ESP32 timer/RMT ISR, and richer packet FEC are optimizations or
  hardware-informed follow-ups, not missing correctness paths in the
  workstation checkpoint.

## Hardware and printer qualification

The V0 USB rig now establishes real Pico/EBB36 identification, signed
build/flash, feature/ABI advertisement, built-in self-tests, legacy telemetry,
mixed-frequency machine-time discipline, structured trace/drop accounting,
and firmware-reset recovery. The Lolin32 evidence above separately establishes
the authenticated WiFi component/modem console and controlled-loss pair FEC.

The unchecked items in the [HELIX Test and Bring-up Plan](Helix_Test_Plan.md)
remain material: trajectory drift/underrun/stress tests, trigger repeatability
and forced-polled latency comparison, trace-off step timing, scoped cross-MCU
action, PWM waveform quality, heater hold, fault injection, soak, real printing, V2.4 CAN,
constrained F072 silicon, native RMII/W5500 PHYs, product-key provisioning,
and Pi/Hailo deployment.
USB success on the V0 is not implicit CAN sign-off for the V2.4.

HELIX should not be called 1.0 or production-ready until the applicable
bring-up-plan evidence and product-key provisioning are recorded.
