# HELIX Test &amp; Bring-up Plan (0.9 → 1.0)

This is the acceptance checklist that takes HELIX from **0.9** (the
software is complete but unproven on hardware) to **1.0** (the initial
production release, after every feature has been exercised on real
boards and the problems found have been fixed). See
[Releases](Releases.md) for what that milestone means.

It is **sequential and cumulative**: each phase assumes the phases
before it passed. A later phase should never be used to "work around" a
red box in an earlier one — go back and fix the cause. Every item is a
checkbox so a bring-up owner can literally tick through it, top to
bottom, and know exactly what remains.

## How to use this document

- Work **top to bottom**. Do not skip a phase because the hardware for
  it isn't wired yet — reorder your wiring instead, or mark the whole
  phase *N/A for this machine* and record why.
- Each item has a **Do**, an **Expect**, and a **Pass** line. Tick the
  box only when *Pass* is literally true, not when it "looks about
  right." Where a number is given, write the number you measured next to
  the box.
- When an item fails, open an issue, link it next to the box, fix it,
  and re-run **the whole phase** (features interact). A phase is green
  only when every box in it is green in a single pass.
- **Safety first.** Every motion phase assumes you can hit a physical
  e-stop or cut power in under a second and that the machine is clamped
  or free of anything it can crash into. The first time a new capability
  drives real current, keep a hand on the kill switch.

**Legend** — `[ ]` open · `[x]` pass · `[~]` pass with a noted caveat
(link it) · `[-]` N/A for this machine (record why).

### Test rigs referenced below

| Rig | What it is | Needed from |
| --- | --- | --- |
| **Bench host** | A dev box that can run `klippy` and the C/C++ unit suites (no printer). | Phase 0 |
| **Single-MCU rig** | One STM32 mainboard on the bench, USB, one stepper + one endstop + one heater/thermistor it can drive safely with the toolhead off the machine. | Phase 2+ |
| **CAN toolhead rig** | The single-MCU rig plus one CAN toolhead board. | Phase 9 |
| **ESP32 rig** | An ESP32 devkit reachable over WiFi/Ethernet. | Phase 9, 11 |
| **OAMS rig** | An OpenAMS mainboard (STM32F072, 16 KB RAM) + its FPS/HDC1080 hardware. | Phase 12 |
| **Full printer** | A complete, homeable, printable machine. | Phase 13+ |

---

## Phase 0 — Software gates (no hardware)

Everything here runs on the **bench host** and must be green before any
board is powered. These are the guarantees the firmware bring-up leans
on; if the protocol library or the host emitter is wrong on a
workstation, no amount of hardware poking will save you.

- [x] **0.1 — intentproto unit suite.**
  Do: `cd lib/intentproto && make clean && make test && ./build/run` (or
  run each `build/test_*`).
  Expect: every test binary in `lib/intentproto/tests/` builds and
  passes — `test_proto`, `test_bch`, `test_hmac`, `test_sha512`,
  `test_datagram`, `test_session_sec`, `test_ed25519`, `test_can_transport`,
  `test_negotiate`, `test_extdesc`, `test_bootcore`, `test_host`,
  `test_wide`, `test_capi`.
  Pass: 0 failures, 0 skips. On 2026-07-14 the clean standalone build passed
  all C++ tests plus extension binding, UDP-FEC, Python/C Ed25519, C ABI,
  cffi, packaged-extension, and secure-session round trips. The CFFI checks
  ran under `/home/jrlomas/klippy-env`, not the dependency-poor system Python.

- [x] **0.2 — CRC-16 wire vector.**
  Do: confirm `crc16_ccitt` over the ASCII string `123456789` yields
  **0x6F91** (reflected CRC-16/MCRF4XX), not 0x29B1 (`test_proto`
  asserts this).
  Expect/Pass: the check constant is 0x6F91. This is the single most
  common interop trap — a board that answers with the wrong CRC will
  look "dead" on the wire. The clean `test_proto` run returned 0x6F91.

- [x] **0.3 — Dictionary generation & counts.**
  Do: regenerate the compile-time data dictionary and diff the
  command/response counts against the last known-good build.
  Expect: counts unchanged from the recorded baseline; the new HELIX
  commands (`config_traj_stepper`, `queue_traj_segment[_cubic|_quintic]`,
  `config_traj_pwm`, `config_trigger_gpio`, `trigger_source_arm`,
  `config_heater_hold`, `config_execlog`, `query_board_syscalls`) are all
  present.
  Pass: dictionary builds; every expected message id is in it. Two clean
  RP2040 HELIX builds at `70a791ba` produced byte-identical dictionaries:
  157 commands and 39 responses, with all ten messages above present. These
  counts are now the recorded baseline for that feature configuration.

- [x] **0.4 — C-API / cffi host binding.**
  Do: build and run `test_capi`; import the cffi binding from `klippy`.
  Expect: the host can encode/decode a segment and verify an HMAC through
  the same library the MCU uses.
  Pass: round-trip host→lib→host is byte-identical. The C loopback and all
  three Python binding tests passed, including authenticated secure-session
  traffic through the compiled C++ core.

- [x] **0.5 — Host extras unit tests.**
  Do: run the Python bench tests in `test/`:
  `asyncio_bridge_test.py`, `helix_status_test.py`,
  `failure_recovery_resume_test.py`, `traj_higher_order_test.py`,
  `traj_pwm_map_test.py`, `endstop_hw_trigger_test.py`, and
  `pause_resume_recovery_test.py`.
  Expect: all pass against the mocked MCU.
  Pass: 0 failures. The original six tests passed together on 2026-07-14;
  the macro-free recovery pause/resume regression passed on 2026-07-15. A
  Python 3.12 live-thread run then exposed and fixed a lost first-wakeup race
  in the asyncio bridge; its immediate reactor→asyncio→reactor handoff and
  the complete eight-test focused set now pass in the Klipper virtualenv.

- [x] **0.6 — Segment fitter fidelity.**
  Do: feed the host segment emitter (`chelper/segfit.c` +
  `trajectory_queuing.py`) a set of reference moves (straight, arc,
  jerk-limited corner) and compare the fitted polynomial path against the
  ideal within `motion_tolerance`.
  Expect: max deviation ≤ configured tolerance; a chain of ≥1000 segments
  ends **exactly** on target (drift-free fixed-point integration).
  Pass: no accumulated drift over the long chain. Direct quantized-wire
  checks recorded worst deviations of 32,245.91 / 32,763.97 / 29,315.09
  sub-units for the straight / 48-chord quarter-arc / finite-junction corner
  against a 32,768-sub-unit tolerance. A 4,000-segment mixed cubic/quintic
  chain was bit-exact at every boundary. This test initially exposed a real
  sub-sample endpoint truncation; `segfit_generate()` now samples the exact
  flush horizon, and all prior homing/v1-pulse/wrap regressions remain green.

- [x] **0.7 — Regression: legacy targets still build.**
  Do: build `linuxprocess`, an STM32 target (e.g. `stm32f407`), and a
  small target (`stm32g0b1` or the OAMS `stm32f072`).
  Expect: all link.
  Pass: no build breaks introduced by the fork. The final single-pass builds
  linked linuxprocess (text/data/bss 126,971/5,764/83,040), STM32F407
  (60,952/64/1,620), and STM32G0B1 (64,612/52/1,516) with GCC 13.2.1.

- [x] **0.8 — nano_udp stack unit test.**
  Do: run `test/nano_udp/run.sh`.
  Expect: the minimal UDP/IP stack (`src/generic/nano_udp.c`, the RMII
  path) parses/builds datagrams correctly. **Note:** this test compiles
  *only* `nano_udp.c` — it does **not** exercise intentproto framing-v2 or
  datagram auth (those are covered by `test_datagram` / `test_negotiate` /
  `test_host` in 0.1).
  Pass: clean run. Both `nano_udp` packet and state tests passed in the final
  Phase 0 sweep on 2026-07-14.

---

## Phase 1 — Firmware build &amp; flash matrix

For every board you intend to certify, build with the HELIX Kconfig
flags on and get it running its **stock legacy behaviour first** — prove
the toolchain and flashing before touching new features.

Repeat this whole phase per target: **STM32 mainboard**, **CAN
toolhead**, **ESP32**, **OAMS mainboard (F072)**.

- [~] **1.1 — Configure.** `make menuconfig` selects the target and the
  HELIX capability flags appropriate to it (`WANT_TRAJECTORY`,
  `WANT_TRAJECTORY_HIGHER_ORDER`, `WANT_TRAJECTORY_PWM`,
  `WANT_TRIGGER_SOURCE`, `WANT_HEATER_HOLD`, `WANT_SYSCALL_API`,
  `WANT_SIGNED_IMAGES`). On the F072, confirm `HAVE_LIMITED_CODE_SIZE`
  drops the features that don't fit — and that this is *by design*, not a
  build error.
  Pass: `.config` reflects the intended feature set. The RP2040 Pico,
  STM32G0B1 EBB36, and computation-only STM32H723 configurations have built
  and run with their intended capability sets. The CAN, ESP32, and OAMS/F072
  certification configurations remain.

- [~] **1.2 — Build.** `make` completes.
  Expect: image links; flash/RAM usage is reported.
  Pass: on the F072, the image fits 128 KB flash / 16 KB RAM with margin.
  Record the numbers. Pico, EBB36, and H723 images link; the workstation
  regression also links linuxprocess, STM32F407, and STM32G0B1. The complete
  certification matrix—especially the OAMS F072 size result—remains.

- [~] **1.3 — Flash.** Flash by the board's normal path (DFU / SD /
  CAN-flash / serial).
  Pass: board boots, LED/heartbeat as expected. Signed HELIX images were
  flashed to the Pico and EBB36 and exercised through homing and complete
  prints; the H723 was flashed through ROM DFU and served its dictionary and
  self-tests. CAN, ESP32, and OAMS targets remain.

- [~] **1.4 — Capability advertisement.** Connect klippy; run
  **`HELIX_STATUS`**.
  Expect: the board reports exactly the flags built in 1.1, plus
  `BOARD_SYSCALL_ABI` / `CAPS` if `WANT_SYSCALL_API` is set.
  Pass: advertised set == intended set. **This is the ground truth every
  later phase reads.** On 2026-07-15 the live Pico and EBB36 again advertised
  ABI `27141a58f61f9fbc`, fleet lockstep, trajectory/quintic/PWM/heater-hold/
  execlog/syscall capabilities, with hardware trigger sources present on both
  boards after the Pico was flashed with the clean `915760f5` trigger-enabled
  build; both passed all five onboard tests. Remaining targets must repeat
  this comparison.

---

## Phase 2 — Link &amp; protocol bring-up (single-MCU rig)

Prove the wire before you trust it to carry motion.

- [~] **2.1 — Identify.** Host connects; MCU serves its dictionary.
  Pass: klippy starts, no version/CRC complaints. On 2026-07-14 the SKR Pico
  and EBB36 v1.2 served their 198/204-command dictionaries over USB from
  `e1ec0b9e`/`fdad253f` and configured cleanly. This qualifies these two USB
  targets, not the remaining board matrix.
- [~] **2.1b — Built-in self test, live.** Run **`HELIX_SELF_TEST`**
  (board built with `WANT_SELF_TEST`; `[helix_self_test]` configured —
  or `on_connect: True` to make it automatic).
  Expect: every advertised test passes ON THE BOARD — `crc_wire` returns
  0x6F91 (the 0.2 vector, live), `timer_monotonic`, `ram_pattern`, and
  `traj_kernel` (the board's fixed-point trajectory math equals the
  host's golden vectors bit-for-bit on this silicon/compiler). The
  report's link round-trip time is the wire-health fingerprint.
  Pass: all PASS; record `timer_rate` and rtt as this board's baseline.
  **This item is most of Phase 0 executed on the real hardware — a
  failure here is a silicon/toolchain porting bug, catch it before
  anything moves.** Both boards passed all five advertised tests after a
  live `FIRMWARE_RESTART`: CRC wire, timer monotonic, timer rate, RAM pattern,
  and trajectory kernel. The final post-trigger-port run on 2026-07-14 recorded
  Pico timer rate 123 and RTT 0.21 ms; EBB36 timer rate 2052 and RTT 0.30 ms.
  A computation-only FK723M1-ZGT6 / STM32H723ZGT6 image subsequently served
  its dictionary over USB at 520 MHz and passed the same five tests (timer-rate
  value 528, trajectory result 4). This qualifies the H723 CPU/USB/DFU port,
  not any board-level motor or heater pins.
- [x] **2.1c — Core-clock identity.** A port whose real CPU clock differs
  from Klipper's scheduler timebase advertises both values unambiguously.
  Pass: the live RP2040 dictionary reports `MCU_CORE_FREQ=200000000` and
  `CLOCK_FREQ=12000000`; Mainsail prefers the core constant for its Machine
  display while scheduling and timestamp conversion continue to use the
  12 MHz timer timebase.
- [~] **2.2 — Legacy framing.** Confirm ordinary command/response traffic
  (CRC-framed) works — temperature reads, pin queries.
  Pass: stable, `link_stats().crc_errors == 0` over a minute. The Pico and
  EBB36 carried continuous temperature/status/trace traffic through a
  ten-minute machine-time run with zero invalid bytes and no loss of lock;
  both remained ready. This is the stock USB/serial carrier, not datagram or
  console-v2.
- [x] **2.2a — Disabled trace is silent.** Configure every `[atlas_trace]`
  subsystem level as `off`, issue `ATLAS_TRACE_TEST MCU=<name> COUNT=1024`
  to each board, and confirm `ATLAS_TRACE_STATUS` still reports zero records
  and `next_seq=0`.
  Pass: on 2026-07-14, firmware `5f652c6e` on the Pico and EBB36 rejected all
  2,048 probe attempts, Atlas recorded zero trace events, both links retained
  zero invalid bytes, and EBB36 machine time converged normally. This is a
  regression for the former `TRACE_LVL_OFF=255` sentinel bug: comparing the
  sentinel as an ordinary numeric severity threshold accidentally enabled
  every level and flooded production prints with queue-refill telemetry. In
  the captured failure session, the supposedly disabled trace produced 5,698
  records and peaked at 257 records/s; execution logging simultaneously
  peaked at 258 records/s. The combined 515-response burst occurred in the
  same machine-time second that EBB36 discipline lost convergence.
- [ ] **2.3 — klippy speaks v2 (the envelope transform).** klippy re-frames
  its stock v1 frames to v2 via the transport bridge
  (`[intentproto_transport]`), leaving serialqueue/serialhdl/msgproto stock.
  Host loopware is already tested (`test/intentproto_transport_test.py`);
  this validates it on silicon. Two modes:
  - *Datagram (network — end-to-end today):* configure
    `[intentproto_transport] mode: datagram` to a UDP/Ethernet board;
    confirm authenticated datagrams flow, erasure-FEC recovers injected
    loss, and a forged datagram is dropped. The MCU side is `udp_console.c`.
  - *Console-BCH (UART — transform LIVE-tested in emulation):* build the
    board with `WANT_CONSOLE_FRAMING_V2=y` (advertises `FRAMING_V2`),
    configure `mode: bch`; confirm the board latches to v2 and normal
    traffic survives. The de-frame/latch/BCH-correction logic is already
    proven against linuxprocess firmware
    (`test/console_v2_live_test.py`); what this validates on silicon is
    the `serial_irq.c` IRQ-path call sites — a failure here points at
    the IRQ glue, not the transform.
  Pass: datagram mode clean and auth-enforced; console-BCH latches and runs
  (a failure implicates the silicon IRQ glue, not the proven transform).
- [ ] **2.4 — Negotiation fallback.** A host that only speaks legacy still
  works (probe limit respected).
  Pass: a legacy-only host session is clean.
- [ ] **2.5 — Extension self-description.** `list_extensions` /
  `list_constants` paginate to `extension_done`.
  Pass: the host can reconstruct the registry with no dictionary blob.

---

## Phase 3 — Machine time (single-MCU, then multi-MCU)

- [ ] **3.1 — Single-clock sanity.** With `[timesync]` loaded,
  `TIMESYNC_STATUS` on a lone MCU.
  Pass: reports converged (trivially) with ~0 error.
- [x] **3.2 — Beacon discipline (needs a 2nd MCU — revisit after Phase 9).**
  Two boards discipline to shared machine time.
  Expect: secondary converges; `TIMESYNC_STATUS` shows sync error settling
  into the converge window and a stable ppm correction.
  Pass: sustained sync error within the documented bound; no loss of lock
  over 10 min. On 2026-07-13 a 64 MHz EBB36 disciplined to the 12 MHz Pico
  for ten minutes without losing lock; the final error was 36 EBB ticks
  (0.56 us). After the final signed flash and `FIRMWARE_RESTART` it
  reconverged and reported -1.6 us. This qualifies mixed-frequency USB
  discipline; the scoped physical action in 3.3 and CAN repetition remain.
- [ ] **3.3 — "Do this at T" agreement.** Schedule a synchronized action
  (e.g. a coordinated pin toggle) on two boards; scope both pins.
  Pass: edges land within the time-model's stated tolerance.

---

## Phase 4 — Motion intentions core (single trajectory joint)

**Toolhead off the machine / joint free to move.** One stepper set to
`motion_protocol: trajectory`.

- [x] **4.1 — Anchor.** `TRAJECTORY_STATUS`.
  Expect: joint listed, commanded position readable, sub-unit resolution and
  higher-order support reported. During queued motion the joint is anchored;
  after its explicit terminal hold the host deliberately drops the anchor, so
  the clean idle state is `anchored=0 need_rebase=0`.
  Pass: the 2026-07-14 V0 run reported all three trajectory joints, higher-order
  support, and sane CoreXY wire-twin coordinates at commanded XYZ
  `[60,60,30]`: A=120.0003 mm, B=0.0000 mm, Z=30.0000 mm. After the stress
  return it reported A=120.0002 mm and B=0.0002 mm (sub-microstep residual).
- [x] **4.2 — Single move.** Command a short move via normal G-code.
  Expect: the host emits segments; the MCU synthesizes steps and arrives.
  Pass: measured end position == commanded within one step. On 2026-07-14,
  the V0 completed independent X and Y homing and a complete `G28 Z` override
  (5 mm lift, two trigger approaches, retract, and move to Z=30). Klipper
  remained ready and reported Z=30. The operator then confirmed a commanded
  10 mm move from Z40 to Z30 at 10 mm/s physically raised the V0 bed toward
  the toolhead, matching the expected kinematic direction. An independent
  endpoint measurement remains. The production path now defaults normal
  G0/G1 motion to quintic intentions while keeping Klippy's Cartesian planner
  authoritative. On `d2a9d7a5`, both boards passed their live self-tests and
  `G28` completed without a TMC UART or underrun error. A coordinated
  `G1 X60 Y60 Z40 F600` then moved CoreXY and Z together; the toolhead object
  updated automatically from `[110,110,30]` to `[60,60,40]`, proving normal
  motion no longer has the stale-position limitation of raw `BEZIER_MOVE`.
  The exact wire twins ended at A=119.9993 mm, B=0, Z=40.0001 mm. A normal
  `G1 Z30 F600` returned the displayed toolhead to Z=30 without coordinate
  repair.
- [x] **4.3 — Chained moves / no drift.** Run a long back-and-forth
  (≥1000 segments) that returns to the origin.
  Pass: returns to origin exactly (fixed-point integration; matches 0.6
  on hardware). On 2026-07-14, the Pico ran nine recorder-bounded X reversal
  chains between X=50 and X=70 at 100 mm/s, plus a slow X=60 to X=40 physical
  witness move, and returned to commanded X=60. The operator confirmed visible
  left/right toolhead travel. Both CoreXY joints completed 1,071 fitted
  segments and 579,200 replayed physical pulses; the final wire twin was
  A=120.0002 mm, B=0.0002 mm at host XY=[60,60]. The printer remained ready.
  The run also exposed software-TMC-UART sampling contention at 40 kbaud and
  20 kbaud during acceleration-heavy trajectory solving. The trajectory-aware
  9 kbaud default completed the full corpus with clean X/Y/Z GSTAT reads.
- [~] **4.4 — Underrun ramp.** Deliberately starve the segment queue
  (throttle the host) and confirm `motion_underrun_decel` ramps the joint
  to a controlled stop rather than a hard halt or overrun.
  Pass: decel observed; no lost steps on the resume.
  On 2026-07-15, Klippy was stopped for 1.5 s during a cold 5 mm/s Z move.
  The Pico completed its configured underrun ramp, retained the exact
  sub-unit endpoint, and emitted `traj_underrun`; neither MCU shut down.
  This exposed and fixed three host recovery defects: a historical trapq
  flush sent a rebase in the past (`Timer too close`), held readback was
  incorrectly applied through stale itersolve state, and macro-free resume
  recursively entered the G-Code mutex. The final run latched a machine-wide
  trajectory hold, emitted no post-underrun work, rebased all four joints at
  one future machine time, and inverse-transformed the held CoreXY/Z joints
  from the already-planned endpoint `[60,60,30]` to the actual controlled-stop
  coordinate `[60,60,87.789057]`. Unit regressions cover group freeze, silent
  flush, future-clock rebase, Cartesian restoration, and macro-free pause.
  A second physical run exposed one more boundary: the recovery rebases had
  no segment attached, so appending work after an operator delay tried to
  start from their now-historical clocks (`Trajectory anchor in past`). A
  recovery rebase is now treated as a coordinated position snapshot, and all
  stopped executors require a fresh future rebase for their first subsequent
  move. With the corrected host loaded, all four joints (including the EBB36
  extruder) reported `anchored=0 need_rebase=1`; after a deliberate delay, a
  cold Z witness completed exactly from 32.210946 to 37.210946 mm and both
  MCUs remained ready. An independent scope/encoder pulse count remains
  before the "no lost steps" half of this item is fully checked. For a
  virtual-SD print, reconstructing the unexecuted suffix of the interrupted
  G0/G1 is also still required; the current safe resume continues at the next
  command from the measured stop coordinate rather than inventing that path.
- [~] **4.5 — Velocity/accel limits honored.** Compare commanded vs
  measured motion profile.
  Pass: within limits; no audible/visible step loss.
  The 4.3 audit proved identical intended/executed pulse counts and a 637-tick
  minimum interval at 100 mm/s, with visible motion and no observed step loss.
  A scope/encoder comparison of the physical velocity and acceleration profile
  remains.
- [x] **4.6 — Deterministic wire/execution audit.** After the move, run
  `scripts/helix_motion_audit.py ~/printer_data/logs/atlas-telemetry.jsonl`
  with `--session latest` and a narrow `--start` / `--end` machine-time
  window. Older telemetry without session identifiers can be isolated with
  `--after-line`; `--before-line` closes a line-bounded evidence window. The
  audit replays
  every half-step crossing from the exact persisted wire coefficients and
  matches each MCU flight-recorder boundary. This requires
  `[failure_recovery]` with `execlog_stream_max` greater than zero; host
  intentions without MCU execution records fail the audit.
  Pass: both coupled joints end in explicit holds; zero underruns, clock or
  accumulator discontinuities, unmatched execution endpoints, or trigger
  position differences. The 2026-07-14 Z window (`309.3..318.8`) passed with
  126 planned segments, five holds, 73,995 executed pulses, 124 matched
  boundaries, two triggers, a 964-tick minimum interval, and zero errors. The
  long search crossed the signed phase boundary while its unwrapped host twin
  continued below -2³¹ sub-units. The 2026-07-14 coupled X/Y stress audit then
  passed with 1,071 segments and 579,200 pulses per joint, 2,182 matched MCU
  boundaries, identical intended/executed 637-tick minimum intervals, and zero
  underruns or errors. `atlas_trace` now assigns each Klippy process a session
  id; the auditor scopes by session, line, wire-clock interval, and wrap-safe
  recorder sequence, and streams pulse statistics so this corpus is bounded in
  memory. Recorder dumps were issued after every short batch to prevent ring
  lapping.
  The first production-G1 quintic audit used telemetry lines 62528–62820:
  every moving segment carried polynomial-order flags 128. It replayed 31 X
  segments / 16,000 pulses and 31 Z segments / 8,000 pulses, matched 67 MCU
  boundaries, and reported zero errors. The Z40→Z30 return independently
  replayed eight segments / 8,000 pulses, matched ten boundaries, and also
  reported zero errors. All three post-move TMC `GSTAT` reads were zero.
  The hot EBB36 +5 mm / 10 mm/s extrusion window (telemetry lines
  77268–77297) passed after mixed-clock audit metadata was corrected: eight
  quintic segments, one hold, 3,529 intended and 3,529 executed pulses,
  identical 8,762-local-tick minimum intervals, eight matched boundaries,
  and zero errors. New records persist both machine and local execution
  clocks; the auditor infers the local rebase anchor for legacy captures.

---

## Phase 5 — Higher-order Bézier segments

Requires `WANT_TRAJECTORY_HIGHER_ORDER`.

- [~] **5.1 — Cubic.** `BEZIER_MOVE STEPPER=<n> DURATION=<s> P0..P3`
  (idle; `enable_bezier_move: True`).
  Pass: joint follows the cubic; ends at P3; follow with
  `SET_KINEMATIC_POSITION` cleanly.
  On 2026-07-14, the V0 Z joint ran a 10 mm / 2 s cubic as eight
  fixed-point-safe wire segments. The audit matched 8,000 intended and
  executed pulses, ten boundaries, a 1,991-tick minimum interval, and zero
  errors. It ended at 40.000273 mm; exact `SET_KINEMATIC_POSITION` preserved
  CoreXY A/B and the Z wire twin. Operator visual confirmation remains.
- [~] **5.2 — Quintic (jerk &amp; snap limited).** `BEZIER_MOVE … P0..P5`.
  Pass: smooth motion, no discontinuity at segment joins; ends at P5.
  On 2026-07-14, the V0 Z joint ran the 10 mm / 2 s quintic return as 32
  wire segments. The audit matched 8,000 intended and executed pulses, 34
  boundaries, a 1,599-tick minimum interval, and zero errors. It ended at
  30.000046 mm and reconciled cleanly. An initial attempt exposed periodic
  software-TMC-UART reads colliding with higher-order stepping; bounded
  standalone moves now suspend same-MCU checks only while active and perform
  the normal checks immediately after becoming idle. The clean retry remained
  ready with EBB36 timesync converged at 0.8 us. Operator visual confirmation
  remains.
- [~] **5.3 — Long higher-order chain.** Confirm the same drift-free
  property as 4.3 with cubic/quintic segments.
  Pass: no accumulated error.
  The host/MCU integer mirror is bit-exact over a 4,000-segment mixed chain,
  and the hardware quintic audit passed a 32-segment chain. A >=1,000-segment
  hardware return-to-origin run remains.
- [x] **5.4 — EBB36 quintic compute envelope.** Prove that onboard crossing
  computation covers the practical extruder role rather than merely fitting
  curves on the host. The STM32G0B1 live self-test qualifies a captured
  quintic at 1x through 16x while holding every crossing within 1/8 step and
  reserving 25% of the following pulse interval. The 16x case is about
  20,000 steps/s (28.3 mm/s filament at the active 705.5 steps/mm gearing)
  and passes. A 32x / about 40,000-step/s probe took 1,304 ticks (20.4 us) at
  its failing crossing and was rejected against the 18.75 us solve deadline
  required to retain a 6.25 us / 25% reserve; it is not claimed. The committed
  `run_captured_quintic_probe` diagnostic reproduces the 16x and 32x results
  without weakening the automatic 16x pass gate. V1-versus-HELIX regressions
  match the complete edge count
  and direction stream for homing, reverse, phase-wrap, short, and 19.2k
  step/s quintic profiles. See
  [STM32G0B1 HELIX motion qualification](STM32G0B1_Helix_Qualification.md).
- [x] **5.5 — Hot EBB36 extrusion integration.** At X=60, Y=60, Z=100 with
  ABS at 260 C and the bed off, completed +10 mm at 2 mm/s, +5 mm at
  10 mm/s, and a bounded -2/+2 mm retract/unretract at 5 mm/s. Reported and
  live E positions ended at E=15, the printer remained ready, heaters were
  returned to target zero, and no underrun/fault records were present. The
  focused 10 mm/s flight audit is the 3,529-pulse evidence in 4.6. This proves
  the hotend/driver/EBB path at realistic flow; it does not substitute for a
  sustained sliced print.
- [x] **5.6 — H7 next-board compute headroom.** An STM32H723ZGT6 at Klipper's
  conservative 520 MHz setting ran the production recurring quintic crossing
  solver without GPIO. One axis at 640k crossings/s, two at 320k each, four at
  160k each, and eight at 80k each all passed the 1/8-step spatial gate and
  retained at least 25% deadline reserve: 640k qualified aggregate curved
  crossings/s. Deliberate 1.0M to 1.28M aggregate probes were rejected rather
  than weakening the reserve. This supports an H7-first controller design;
  it does not qualify the FK723M1 board's external I/O. Reproduction command,
  raw measurements, and graph are in
  [STM32G0B1 HELIX motion qualification](STM32G0B1_Helix_Qualification.md)
  under “STM32H723 compute-headroom comparison.”

---

## Phase 6 — Actuator backends (agnostic path)

The point of intentions: the segment says *where the joint should be*,
not which pulses to send. Prove more than one backend behind the same
queue.

- [x] **6.1 — Step/dir stepper backend.** The V0 Pico and EBB36 synthesized
  their own step/dir pulses from production G0/G1 quintic intentions through
  two complete sliced ABS prints. Both finished without a trajectory fault,
  toolhead stall, invalid link byte, or operator-observed motion defect.
- [ ] **6.2 — Sampled PWM/DAC backend.** Requires `WANT_TRAJECTORY_PWM` +
  `config_traj_pwm`. Drive a PWM/DAC actuator (or a scope on the PWM pin)
  along a trajectory.
  Expect: the sampled output tracks the commanded segment path.
  Pass: output waveform matches the trajectory within the sampler's
  resolution (cross-check `traj_pwm_map_test.py` expectations on real
  output).
- [ ] **6.3 — Backend swap without host motion changes.** Confirm the host
  motion path is identical for 6.1 and 6.2 (only the backend config
  differs).
  Pass: same G-code, same trajectory, different actuator — both land.
  *(This is the BLDC/FOC door: no BLDC hardware to test here, but the
  agnostic seam is what 6.2/6.3 prove.)*

---

## Phase 7 — Hardware triggers (interrupt-driven homing/probing)

Requires `WANT_TRIGGER_SOURCE`. This is a **capability unlock**, not just
a faster stop — test both the latency and the things polling could not do.

- [x] **7.1 — Edge-interrupt endstop.** With
  `hardware_endstop_trigger` on (default), home one axis.
  Expect: the stop begins from a GPIO edge interrupt rather than a periodic
  software sample; timestamp precision is the port's advertised capture
  method (timer input capture where wired, ISR-entry time otherwise).
  Pass: the V0 Pico running `e1ec0b9e` completed independent `G28 X`, `G28 Y`,
  and `G28 Z` runs and remained ready. The flight recorder showed distinct
  hardware-source records before the actuator-stop records: OID 19 for X,
  OID 21 for Y, and two OID 23 firings for Z. The RP2040 port timestamps at
  IO_BANK0 ISR entry; its source records preceded actuator stops by 261–300
  scheduler ticks (21.8–25.0 us), consistent with the configured 20 us
  qualification window plus dispatch. This supersedes the earlier inference
  from trajectory-stop records, which alone did not prove a GPIO interrupt.
  On 2026-07-15 the clean current `915760f5` Pico build was flashed and again
  advertised hardware-trigger support. A cold full home plus independent
  axis runs retained fresh OID 19, 21, and 23 source records; their first
  actuator stops followed by 264, 262, and 277 ticks respectively, and the
  printer remained ready. This confirms the interrupt path is present in the
  candidate now on the machine, not only in the historical image.
- [x] **7.2 — Latency vs polling.** Compare stop latency with
  `hardware_endstop_trigger: False` (forced legacy) vs on.
  Pass: hardware path stops measurably sooner; record both numbers and a
  repeatability series showing trigger-position variance no worse than the
  legacy path. On 2026-07-15 a cold V0/Pico Z-switch comparison ran four
  eight-home blocks in poll--interrupt--interrupt--poll order. Each home had a
  20 mm/s and a 3 mm/s contact, yielding 32 contacts per mode without a fault.
  OID 23 ISR-entry records preceded the OID 10 trajectory halt by 276--278
  scheduler ticks: 23.086 us mean with a 0.033 us standard deviation. The
  legacy path cannot record the edge; its 62.5/416.7 us poll periods and 45 us
  confirmation window imply 48.1--110.6 us (79.3 us mean) at 20 mm/s and
  48.1--464.8 us (256.4 us mean) at 3 mm/s, using the measured common dispatch
  cost. Whole-system equivalent trigger-position sigma was also lower with
  interrupts: 17.9 vs 40.5 um fast and 14.4 vs 28.7 um slow. The original
  config was restored byte-for-byte. Method, aggregates, caveats, and the
  distinction between RP2040 ISR-entry and physical-edge timing are recorded
  in [Interrupt-driven homing versus legacy polling](Interrupt_vs_Polling.md).
- [ ] **7.3 — Multi-MCU homing.** Endstop on one board, motor on another.
  Pass: coordinated stop within the time-model tolerance.
- [ ] **7.4 — Comparator / analog trigger.** Where wired, arm an analog
  comparator trigger source.
  Pass: fires at the threshold; falls back to polling on silicon that
  lacks it (verify the fallback path too).
- [ ] **7.5 — ADC watchdog / oversampling.** Arm the ADC-watchdog trigger
  and, where supported, DMA oversampling.
  Expect: an out-of-range sample fires without host polling; oversampled
  reads are quieter than single-shot.
  Pass: watchdog trips on a forced excursion; **record the noise
  reduction** from oversampling — this is a "now possible" item, so
  capture the before/after.
- [ ] **7.6 — Input-capture timestamps.** Confirm timer input-capture
  timestamps a trigger to the tick.
  Pass: timestamp precision matches the doc-09 claim.

---

## Phase 8 — Failure recovery (pause-and-hold)

Requires `[failure_recovery]`; per-MCU `on_comm_timeout: pause`;
heaters `failure_policy: hold`. **Do this before trusting a long print.**

- [ ] **8.1 — Heater failsafe hold, host-triggered.**
  `ENGAGE_HEATER_HOLD` / `RELEASE_HEATER_HOLD`.
  Pass: heater holds target autonomously; `FAILURE_RECOVERY_STATUS` shows
  it engaged; release returns control to host.
- [ ] **8.2 — Autonomous hold on fault.** With `WANT_HEATER_HOLD` firmware,
  sever host comms mid-heat.
  Expect: the board keeps the heater at target within
  `hold_max_temp`/`hold_max_duration` instead of shutting down.
  Pass: temperature held; ceiling and duration limits enforced; safe
  release at expiry.
- [~] **8.3 — Link loss → pause-and-hold.** Unplug a secondary MCU's link
  mid-motion (`on_comm_timeout: pause`).
  Expect: the board finishes queued motion, **holds position**, does not
  shut down; host sees it paused (`FAILURE_RECOVERY_STATUS`).
  Pass: no shutdown; heaters stay on per policy.
  On 2026-07-15 the EBB36 USB data cable was physically removed during a
  cold 50-second Pico Z trajectory. Klipper stayed ready, entered recovery
  pause without invoking the configured park/retract macro, suspended
  machine-time traffic to the missing board, and the Pico completed Z40 to
  Z30 at the exact commanded endpoint. The powered EBB36 did not reboot.
  This qualifies host pause plus primary-board queued-motion continuation;
  an active E trajectory and heater hold were not exercised, and both heater
  targets were zero, so the full item remains partial.
- [x] **8.4 — Reconnect.** `RECONNECT_MCU MCU=<name>`.
  Pass: re-handshake succeeds; link re-established (datagram auth restored
  where the transport uses it).
  A final physical EBB36 USB unplug/replug on 2026-07-15 completed in place:
  `RECONNECT_MCU MCU=ebb36` preserved the live transport sequence, rejected
  stale never-transmitted outage work, verified matching config CRC and
  continuous uptime (32,103,375,787 to 34,127,589,558 ticks), re-anchored
  clock sync, and left Klipper ready. The EBB36 reconverged at -2.4 us; no
  board or host shutdown occurred. The physical test also exposed and fixed
  the required EOF worker restart, USB endpoint staging reset, stale-query
  cancellation, and pre-loss time-sync callback containment.
- [~] **8.5 — Resume &amp; reconcile.** `RESUME_MOTION`.
  Expect: each joint reconciles from its execution log to exactly where it
  stopped; the print continues; a joint marked
  `motion_homing_volatile: True` blocks for re-homing, others do not.
  Pass: geometry after resume matches before the fault (measure a witness
  feature); volatile joints correctly demand re-homing.
  A live host-stall recovery on 2026-07-15 proved reliable execution-log
  drain, exact held-position readback, one shared future rebase across Pico
  and EBB36, CoreXY/Z inverse-kinematic restoration, and a delayed first move
  from a fresh future anchor. Klipper remained ready, reported the measured
  ramp endpoint instead of the stale planned endpoint, and completed the
  5 mm post-recovery witness at its exact reported endpoint without shutting
  down either board. The link-loss/reconnect case, volatile-axis hardware
  case, independent physical position measurement, and a printed witness
  feature remain. The current virtual-SD resume restarts at
  the next unconsumed G-Code command; replay/replanning of the interrupted
  move suffix is not yet implemented, so this is not yet print-transparent.
- [x] **8.6 — Flight recorder.** `EXECLOG_DUMP`.
  Pass: retained MCU execution logs drain to the Klipper log even while the
  MCU is shut down, live `execution` records share Atlas machine time with
  exact host `intention` coefficients, and the records explain the
  interruption.
  Live streaming, reliable repeated pulls, host/MCU reconciliation, and the
  1,071-segment coupled audit passed on 2026-07-14. On 2026-07-15, a bounded
  cold Z move was followed by a deliberate `M112`; the deferred shutdown
  handler queried the still-connected shutdown boards and persisted 42 Pico
  plus 22 EBB36 records, including the Z segment completions, before firmware
  restart. Repeated unpaced full-ring pulls then exposed receive framing loss
  (`bytes_invalid` rose on both USB links); the host now pulls at most four
  records at a time and waits on a same-queue response barrier after every
  chunk. Two physical pulls of 1,475 and 1,500 retained records completed with
  Pico and EBB36 `bytes_invalid` unchanged at zero. Both heater targets were
  zero throughout.
- [ ] **8.7 — Full replug cycle under print.** Combine 8.3–8.5 during an
  actual short print; reseat a toolhead cable.
  Pass: the part survives; no cold-bed detach; layers align across the
  interruption.

---

## Phase 9 — Transports

Certify each transport the machine uses. Re-run the multi-MCU items in
Phases 3/7/8 over each real transport.

- [~] **9.1 — USB.** Normal USB operation is stable across homing, coupled
  motion audits, hot extrusion, and two complete sliced prints with no invalid
  bytes or new retransmits. Deliberate physical EBB36 disconnect, host
  recovery pause, retained primary motion, in-place USB re-enumeration, and
  `RECONNECT_MCU` with continuous board uptime now pass. Active lost-board
  motion/heater hold and an under-print `RESUME_MOTION` witness from Phase 8
  remain before the USB recovery path is complete.
- [ ] **9.2 — CAN toolhead.** Bring up a CAN toolhead board.
  Pass: enumerates (UUID admin), data traffic on the assigned ids,
  `test_can_transport` behavior confirmed on real silicon; motion + time
  sync + triggers work over CAN.
- [ ] **9.3 — WiFi (ESP32).** Datagram transport over WiFi.
  Expect: deep queue absorbs latency/jitter; authenticated datagrams;
  XOR-erasure FEC recovers dropped frames on a lossy link.
  Pass: a print-length motion stream runs with no motion defect from wire
  jitter; `bch`/erasure counters show recovery, not failure.
- [ ] **9.4 — Ethernet (RMII).** Same as 9.3 over Ethernet.
  Pass: clean; lower jitter than WiFi (record both).
- [ ] **9.5 — Datagram loss tolerance.** Inject packet loss on 9.3/9.4.
  Pass: FEC + retransmit hide it up to the documented loss rate; beyond
  that it degrades to a clean pause, never a crash.
- [ ] **9.6 — Mixed fleet: firehose + intent time agreement.** On a machine
  with at least one **stock-Klipper (firehose) board** and one **HELIX
  intent board** driving *independent* actuators (regime 1 of
  [FD-0001 doc 14](founding/0001-motion-intentions/14-Heterogeneous_Fleets.md)),
  schedule a coordinated timed event ("at T, board A pulses and board B
  starts a move") and scope both.
  Expect: both act at the same instant — `clocksync` (firehose) and the
  machine-time beacon (intent) both map the host's print-time to the same
  physical moment.
  Pass: edges land within the time-model tolerance; the config-time
  validator rejects a coordination *group* split across paradigms (try a
  deliberately mixed rail — klippy must refuse to start with the doc-14
  error). Record whether a coordinated group was (correctly) kept
  single-paradigm.

---

## Phase 10 — Security

- [ ] **10.1 — PSK datagram auth floor.** Confirm every datagram carries a
  truncated HMAC over the static PSK; a forged/altered datagram is
  dropped.
  Pass: tampered frame rejected; counter increments.
- [ ] **10.2 — DTLS-class session.** Bring up the 3-message PSK handshake;
  confirm HKDF-derived keys, epoch rotation, and the 64-entry replay
  window (auth-only).
  Pass: handshake completes; a replayed datagram is rejected; key rotation
  survives a long session; RAM cost matches the ~264-byte budget.
- [ ] **10.3 — Per-board identity.** Two boards have distinct identities;
  one cannot impersonate the other.
  Pass: cross-identity frame rejected.
- [ ] **10.4 — Signed firmware images.** With `WANT_SIGNED_IMAGES`, the
  bootloader verifies an Ed25519 signature before running an image.
  Expect: a correctly signed image boots; an unsigned/mis-signed/altered
  image is refused.
  Pass: good image runs, bad image is rejected and the board stays in a
  safe state. **The test keys are DEV/TEST-only — confirm no production
  key is on the bench.**
- [ ] **10.5 — Combined-image build &amp; update.** Build the combined
  bootloader+app image; perform an over-the-wire update.
  Pass: update applies, verifies, boots; a failed/interrupted update
  leaves a recoverable board.

---

## Phase 11 — ESP32 "IDF-as-modem" architecture

**Runtime is explicitly unproven in 0.9 — this phase is where it gets
proven.** Do it on an ESP32 devkit.

- [ ] **11.1 — Build both architectures.** Confirm the Kconfig/CMake
  switch builds *both* the `component` (validated) fallback and the
  `modem` (bare core-1) architecture.
  Pass: both link.
- [ ] **11.2 — Component fallback runs.** Flash the `component` build.
  Pass: ESP32 behaves as a normal networked MCU (regression safety net).
- [ ] **11.3 — APP-CPU bare bringup.** Flash the `modem` build; core 0 owns
  IDF/WiFi, core 1 runs bare `sched_main()`.
  Expect: core 1 comes up, runs the scheduler, and **never calls an
  IDF/FreeRTOS symbol** after unstall.
  Pass: board boots into bare core-1; console reachable.
- [ ] **11.4 — Shared-memory ring console.** The SPSC ring backs the
  console ops.
  Pass: bidirectional traffic across the ring with no lost/duplicated
  bytes under load; memory barriers correct (no torn reads on core 1).
- [ ] **11.5 — Modem task.** Core 0 moves only authenticated datagram bytes
  between the radio and the ring.
  Pass: air ↔ ring path clean; HMAC enforced on the core-0 side.
- [ ] **11.6 — IRAM discipline.** Confirm the hot path (scheduler dispatch,
  timer ISR, gpio/step, trajq execute) is IRAM-resident so a flash-cache
  miss never stalls it.
  Expect: no timing glitch correlated with flash access; IRAM budget
  within the documented map.
  Pass: sustained step generation with zero cache-stall-induced jitter;
  record the IRAM usage.
- [ ] **11.7 — ESP32 motion soak.** Run Phases 4/7/8 over the modem build.
  Pass: parity with the STM32 path.

---

## Phase 12 — OpenAMS (OAMS) port

Two repos: `mainboard-firmware` (STM32F072, 16 KB RAM — the constrained
target) and `klipper_openams` (host extras). This board is the acid test
of the `HAVE_LIMITED_CODE_SIZE` policy.

- [ ] **12.1 — F072 image fits.** Build the OAMS mainboard firmware on the
  intentproto annotation layer.
  Pass: fits 128 KB flash / 16 KB RAM; `HELIX_STATUS` shows the reduced
  (correct-for-F072) capability set — features that don't fit are absent
  *by design*.
- [ ] **12.2 — Enumerate.** Host loads `oams.py` / `oams_manager.py`;
  board identifies.
  Pass: clean handshake; periodic stats/enums arrive.
- [ ] **12.3 — FPS &amp; HDC1080.** Filament-pressure sensor (`fps.py`) and
  the HDC1080 temp/humidity (`hdc1080.py`) read correctly.
  Pass: sane values; sensible ranges.
- [ ] **12.4 — Filament group logic.** `filament_group.py` /
  `oams_macros.cfg` drive a load/unload/switch cycle.
  Pass: the AMS sequences correctly against the sample config
  (`oams_sample.cfg`).
- [ ] **12.5 — Bootloader / update path.** Per `BOOTLOADER_UPDATE.md` /
  `UPGRADE_BOOTLOADER.md`, update the F072 over its normal path.
  Pass: updates and boots; combined image where applicable.
- [ ] **12.6 — OAMS under a real print.** Exercise a filament change
  mid-print on the full printer.
  Pass: swap completes; no motion/heater fault; print resumes.

---

## Phase 13 — G-code surface acceptance (full printer)

Type every new command once on a real machine and confirm it does what
[Helix_Commands.md](Helix_Commands.md) says. Tick each:

- [x] **13.1** `HELIX_STATUS` — reports MCUs' built features + loaded host
  subsystems. Pico and EBB36 reported their live feature sets, identical ABI
  hash, and fleet lockstep on 2026-07-13.
- [x] **13.2** `TRAJECTORY_STATUS` — per-actuator state, exact wire-twin
  position, resolution, higher-order support. Verified before and after the
  1,071-segment-per-joint V0 stress run on 2026-07-14.
- [x] **13.3** `BEZIER_MOVE …` — cubic and quintic (idle, opt-in). Both
  forms completed on the V0 Z joint on 2026-07-14, reported their automatic
  wire-segment counts and exact endpoints, and accepted the required
  `SET_KINEMATIC_POSITION` reconciliation.
- [x] **13.4** `FAILURE_RECOVERY_STATUS` — holds + paused MCUs.
  Pass: the live V0 reported both boards' per-joint recovery disposition,
  zero configured heater holds, no paused links, and the active trajectory
  recovery hold with its triggering MCU/joint/clock/position. After
  `RESUME_MOTION` it exposed the reconciled joints and cleared the active hold.
- [x] **13.5** `RECONNECT_MCU MCU=<n>` — re-handshake. A physical EBB36 USB
  unplug/replug on 2026-07-15 retained the never-rebooted firmware session,
  matched its config CRC, re-synchronized its clock, reconverged machine time,
  and returned success without restarting Klippy or either MCU.
- [x] **13.6** `RESUME_MOTION` — reconcile + resume. A cold live underrun on
  2026-07-15 reconciled four held joint accumulators at one future boundary,
  inverse-transformed the actual CoreXY/Z stop position, cleared the recovery
  pause without a park/unpark macro, and completed a delayed 5 mm witness move
  while both MCUs remained ready.
- [ ] **13.7** `ENGAGE_HEATER_HOLD` / **13.8** `RELEASE_HEATER_HOLD`.
- [x] **13.9** `EXECLOG_DUMP` — reliable flight-recorder pulls completed after
  every bounded motion batch and after the final run on 2026-07-14; a deferred
  pull also persisted both boards' retained records after deliberate `M112`
  on 2026-07-15. Four-record paced chunks with a per-chunk response barrier
  subsequently transferred 1,475 and 1,500 records over the physical USB
  links without increasing either board's `bytes_invalid` counter.
- [x] **13.10** `TIMESYNC_STATUS` — per-secondary discipline state. The
  EBB36 reported `CONVERGED` against the Pico after cold connect and after
  `FIRMWARE_RESTART`.
- [ ] **13.11** Every new config option in
  [Helix_Commands.md](Helix_Commands.md) §"Config surface" loads without
  error and has the documented effect (per-stepper `motion_*`, the new
  sections, `on_comm_timeout`, `hardware_endstop_trigger`,
  `failure_policy`).

---

## Phase 14 — Full-printer integration

Now put it together on the **full printer**.

- [ ] **14.1 — Cold start → home → mesh.** A stock workflow end to end on
  HELIX firmware.
  Pass: homes, probes, meshes; no regressions vs stock Klipper behavior.
- [x] **14.2 — Benchmark print (baseline).** Print a known-good model.
  Pass: quality ≥ the machine's stock-Klipper baseline; measure and record.
  The first supervised attempt on 2026-07-14 ran at 25% speed for 3 min 49 s
  with stable 260 C / 110 C temperatures, no print stalls, and sustained
  quintic XY/extruder execution before Klippy shut down in the host flush
  handler. The cause was not an MCU disconnect: one lookahead horizon
  contained multiple disconnected pressure-advance/input-shaper activity
  windows, but the trajectory emitter consumed only the first before trapq
  cleanup discarded the sampling context for the next. Its later anchor
  therefore produced a non-finite position. The emitter now drains every
  disconnected window before cleanup and rejects any non-finite anchor with
  an actuator-specific diagnostic. A multi-window regression plus the
  pressure-advance, fitting, execution-audit, and V1 pulse-equivalence suites
  pass.

  The supervised retry on `e6ce720b` proved that the non-finite-anchor crash
  was gone, but it was stopped after two real `stepper_z` trajectory
  underruns and matching TMC phase changes. The print began at 100% before
  being reduced to 25%; speed changed the audible roughness but was not the
  cause of these underruns. Both occurred after finite Z motion while the
  start macro was blocked in its 110 C / 260 C heater waits. Ordinary
  zero-scan-window axes were still using `itersolve_check_active()`, whose
  legacy `last_flush_time` cursor is advanced only by the step-pulse path that
  trajectory steppers intentionally bypass. A completed Z move consequently
  appeared active forever, received no explicit terminal hold, and exhausted
  its finite MCU queue during the synchronous wait. Normal zero-window motion
  now uses the fitter's explicit connected activity endpoint and queues its
  hold in the same flush; only homing/probing drip mode retains incremental
  legacy activity probing. Regressions cover an ordinary Z-like move followed
  by a long synchronous wait and preserve drip streaming. The trajectory,
  pressure-advance, fitting, motion-audit, and V1 pulse-equivalence suites all
  pass. Restart Klippy to load this correction before the next supervised
  real-print retry.

  That retry crossed the heater waits without another Z underrun or host
  exception, but the physical motion became jerky and skipped near seams. It
  was canceled around G-code byte 83,734, before the layer-two solid infill.
  The prior blackbox audit had reconstructed ideal polynomial endpoint
  crossings; it did not execute the production interval-predictor state
  machine. Exact replay of the captured intentions found the firmware solver
  falling behind by up to 571 X, 206 Y, 122 E, and 121 Z steps, followed by
  near-one-tick catch-up bursts. Boundary interval guesses are now validated
  against the new segment, residuals outside 1/8 step invoke bounded exact
  refinement and sign-bracket selection, and errors beyond the 1/4-step
  representable-tick limit fail closed.

  `scripts/helix_gcode_pulse_compare.py` now runs real sliced G-code offline
  through both stock V1 `queue_step` output and the HELIX fitter plus exact
  production MCU solver. A two-layer run including solid infill produced
  148,298 X, 145,040 Y, 960 Z, and 34,709 E HELIX edges with minimum intervals
  of 692, 694, 1,346, and 6,761 respective MCU ticks; there were no catch-up
  bursts. The corresponding V1 counts were 148,192, 144,955, 960, and 34,673.
  Captured-session replay now has zero intention endpoint mismatches on every
  actuator. Workstation tests, both target builds, and Linuxprocess live
  self-tests pass. Item 14.2 remains open until the corrected images pass the
  new STM32G0B1 sharp-retract self-test and a supervised print completes
  cleanly.

  The next 100% supervised retry reached 14.29% (G-code byte 59,970, first-
  layer solid infill) before the host rejected a stationary CoreXY actuator
  segment as exceeding the wire limits. A diagonal X/Y stroke had canceled
  exactly for one motor; rounding its non-zero anchor left a -0.08-sub-unit
  floating residual, while coefficient quantization correctly produced an
  all-zero quintic. The direction validator incorrectly assigned that hold a
  positive direction and rejected it. Zero polynomials now bypass the
  inapplicable direction check, with the exact cube diagonal committed as a
  fitter regression.

  Replaying that cube at 100% then found isolated one-tick intervals on X, Y,
  and E. They occurred on the first solve after a direction reversal, when
  clearing the stale prior interval left cold Newton iteration near zero
  velocity. Its invalid result previously fell through to `t_prev + 1`. Cold
  or spatially invalid higher-order solves now use a bounded monotonic sign
  bracket and select the nearest representable timer tick within the existing
  1/4-step fail-closed limit. Captured X, Y, and E reversal vectors are
  permanent regressions.

  The corrected 100% two-layer replay completes with 317,607 X, 323,300 Y,
  1,280 Z, and 63,842 E HELIX edges. Minimum intervals are 260, 256, 1,353,
  and 4,755 target-MCU ticks respectively, with zero intervals at or below 64
  ticks. The corresponding V1 counts are 317,247, 321,270, 1,280, and 63,758.
  Pico, EBB36, and Linux firmware builds, focused motion/fitter suites, and
  Linuxprocess live self-tests pass. Item 14.2 remains open until these new
  MCU images are flashed and the supervised physical print completes.

  After flashing `5f652c6e`, one cube produced coherent motion through a
  full-speed interval, but repeat runs exposed two independent faults. The
  first was a disabled-trace sentinel bug that flooded the EBB36 with 257
  trace and 258 execution records/s until time discipline lost convergence;
  disabled probes are now silent (2,048 live attempts, zero records). The
  next repeat failed during uninterrupted printing with EBB36 `Timer too
  close`. Its recorder showed the next E rebase clock was already 19,931
  local ticks (311.4 us) old when processed. Timesync was still converged,
  trace remained empty, and both links had zero invalid bytes, separating it
  from the earlier failure.

  The mid-print deadline came from pressure-advance lookback discovery, not a
  user pause or an MCU solver overrun. If a move was appended after generation
  had entered its pre-active interval, the HELIX scanner returned that
  interval's historical start. Stock itersolve clips the same case to its
  `last_flush_time`; `segfit_check_activity()` now clips to the supplied
  generation cursor. A dedicated 40 ms pressure-advance regression, the full
  focused host suite, and a 100% two-layer replay of the failing cube pass.
  The replay emits 63,846 E edges with a 4,896-tick minimum and no interval at
  or below 64 ticks. A supervised repeat progressed into physical extrusion,
  then found a distinct scheduling bug at another disconnected E island. Its
  forward-only rebase for local clock 3,214,869,210 reached the EBB36 at
  3,214,903,493, already 34,283 ticks (535.7 us) late. The prior horizon had
  been supplied as the command's `minclock`; serialqueue therefore withheld
  the rebase until the old horizon rather than giving it the normal advance
  delivery window. Per-joint command-queue order and both host/firmware
  overlap checks already enforce the required ordering, so rebase transmission
  now uses `minclock=0` while keeping its requested execution clock. Focused
  trajectory, extrusion, and status regressions pass. Item 14.2 remains open
  pending a clean supervised repeat.

  The next repeat confirmed that change by reaching 48.7% without an MCU
  deadline fault, then stopped at a host-side overlap check. The newly visible
  E island requested local clock 35,867,360,686 while the preceding flush had
  already committed a terminal hold through 35,867,364,943: a 4,257-tick
  (66.5 us) overlap. Since an emitted hold is immutable, a late-visible island
  overlapping by no more than the intentional 1 ms terminal hold now anchors
  and samples its pressure-advance position at the exact committed horizon.
  Larger overlaps remain fatal. The captured clock vector is a regression and
  a 55-layer 100% offline run through the failed region completes with 194 E
  rebases, 195 holds, 568,122 E edges, a 4,721-tick minimum, and no interval
  at or below 64 ticks.

  Two subsequent supervised ABS cubes completed at 100% requested speed with
  operator-confirmed coherent print quality. Run one consumed all 417,479
  G-code bytes in 778.7 s of print time and commanded 1,293.6 mm of filament;
  run two consumed all 644,990 bytes in 669.0 s and commanded 1,302.9 mm.
  Both retained zero toolhead stalls, zero invalid link bytes, the existing
  nine-byte startup retransmit baseline, and no timer, rebase, flush-handler,
  or MCU shutdown error. Each also encountered the repaired condition in
  production: the host aligned a late-visible E island by 30.4 us and 31.0 us
  to its immutable committed-hold horizon, respectively, and each print then
  continued to completion. The second file requested up to 300 mm/s and
  7,000 mm/s^2, but the active V0 limits clamped motion to 200 mm/s and
  3,000 mm/s^2; it therefore closes the repeatable baseline gate, not the
  purpose-built high-load gate in 14.3.
- [ ] **14.3 — High-speed / high-accel print.** Push into the regime where
  jerk/snap limiting and deep queues matter.
  Pass: surface finish holds; no step loss; no queue underrun stalls.
- [ ] **14.4 — Networked toolhead print.** Run a full print with the
  toolhead on CAN, then on WiFi/Ethernet.
  Pass: no wire-jitter artifacts; the queue absorbs latency as designed.
- [ ] **14.5 — Fault injection during a real print.** Mid-print: pull a
  cable (8.7), starve the queue (4.4), force a heater fault (8.2).
  Pass: pause-and-hold + resume saves the part in every case.

---

## Phase 15 — Soak, stress &amp; regression

- [ ] **15.1 — 24 h motion soak.** Continuous motion (or repeated prints).
  Pass: no drift, no leak, no clock-lock loss, no memory growth on the
  host; `link_stats` error counters flat.
- [ ] **15.2 — Thermal soak.** Long high-temp hold with `failure_policy:
  hold` armed.
  Pass: no false holds; real fault still caught.
- [ ] **15.3 — Repeated replug stress.** Automate dozens of link-loss /
  reconnect / resume cycles.
  Pass: every cycle reconciles correctly; no cumulative position error.
- [ ] **15.4 — Power-loss / brown-out.** Cut power at random points.
  Pass: boards come back to a safe state; signed-image check still passes;
  no corrupted config/bootloader.
- [ ] **15.5 — Regression sweep.** Re-run Phase 0 on the final firmware/host
  commit.
  Pass: still all green (nothing bench-level regressed during bring-up).

---

## Phase 16 — Sign-off → 1.0

- [ ] **16.1** Every phase above is green (or a box is `[-]` N/A with a
  recorded reason, or `[~]` with a linked, accepted caveat).
- [ ] **16.2** Every issue opened during bring-up is closed or explicitly
  deferred with a tracked ticket referenced here.
- [ ] **16.3** The [Releases](Releases.md) page is updated: 0.9 → **1.0**,
  with the tested-hardware matrix and the list of problems found &amp;
  fixed during bring-up.
- [ ] **16.4** Any capability that could not be validated on available
  hardware is clearly labeled *unproven* in the docs (not silently
  claimed) and carried forward as a known gap.

When 16.1–16.4 are all ticked, HELIX is **1.0 — initial production
release**.

---

## Traceability

Each phase maps back to the design canon and the implementation so a
reviewer can find the code behind a test:

| Phase | Design (FD-0001) | Where it lives |
| --- | --- | --- |
| 0 protocol/host | doc 02, 10, [Protocol v2](Protocol_v2.md) | `lib/intentproto`, `chelper/segfit.c`, `klippy/extras/trajectory_queuing.py` |
| 3 machine time | doc 01 | `klippy/extras/timesync`, MCU beacon |
| 4–6 motion/backends | doc 02, 04 | `src/trajq.c`, `src/traj_stepper.c`, `config_traj_pwm` |
| 7 triggers | doc 09 | `src/trigger_source.c`, stm32 comp/exti/adc |
| 8 recovery | doc 08 | `klippy/extras/failure_recovery.py`, `src/execlog.c`, `src/heater_hold.c` |
| 9 transports | doc 07, 12 | UDP/CAN/Ethernet bindings, `src/esp32` |
| 10 security | doc 07, 11 | session-sec, Ed25519, bootloader |
| 11 ESP32 | doc 12 | `src/esp32/*`, `lib/esp32/*` |
| 12 OAMS | — | `mainboard-firmware`, `klipper_openams` |
| 13 G-code | doc 02/08 | `HELIX_STATUS` and the extras above |

*This plan is the definition of "done" for the 1.0 milestone. It is a
living document — as bring-up finds gaps, add rows, never delete a
failure without a fix behind it.*
