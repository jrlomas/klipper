# HELIX Developer Guide

This guide is the map for anyone building, extending, or porting HELIX.
It assumes familiarity with Klipper's architecture (host `klippy` in
Python, firmware in C, a data-dictionary protocol between them) and
points you at where HELIX diverges and why. The authoritative design
rationale is [the FD-0001 canon](founding/0001-motion-intentions/00-Vision.md);
this page is the orientation you read first.

## The shape of the system

```
        klippy (host, Python)                 firmware (C / C++)
  ┌───────────────────────────┐        ┌──────────────────────────────┐
  │ kinematics, G-code         │        │ command dispatch (generated  │
  │ trajectory_queuing  ───────┼──seg───┤ or intentproto-served)       │
  │ failure_recovery           │        │ trajq + backends (stepper,   │
  │ timesync (beacon relay)    │◄─log───┤ pwm), execlog                │
  │ chelper/segfit (fitter)    │        │ trigger_source, heater_hold, │
  │ mcu.py (transport)         │◄─sync──┤ timesync, board syscalls     │
  └───────────────────────────┘        └──────────────────────────────┘
        intentproto over UDP / CAN / USB / UART, authenticated
```

The two big structural additions to Klipper are the **trajectory
subsystem** (host fitter + MCU segment core + backends) and the
**intentproto protocol library** (`lib/intentproto`, a freestanding C++
implementation of the wire protocol shared by the firmware and the
OpenAMS peripheral).

## Where things live

### Firmware (`src/`)
* `trajq.c` / `trajq.h` — the actuator-independent segment core: the
  intention queue, drift-free chained position accounting, underrun ramp
  synthesis, hold/rebase state. Cubic/quintic terms are gated by
  `WANT_TRAJECTORY_HIGHER_ORDER`.
* `traj_stepper.c` — the classic-stepper backend (a Newton root-find
  turns segments into step timing). `traj_pwm.c` — the sampled PWM/DAC
  backend for servo/heater-style actuators.
* `execlog.c` — the uplink execution log (the "flight recorder").
* `trigger_source.c` + `stm32/{gpio_exti,comp,adc_watchdog,timer_capture}.c`
  — hardware-event detection feeding trsync.
* `heater_hold.c` — the autonomous heater failsafe hold.
* `timesync.c` — machine-time discipline on the MCU side.
* `generic/board_syscall.c` — the unified cross-family syscall table.
* `boot_app/` + `lib/intentproto/boot/bootcore.cpp` — the first-class
  bootloader, with optional Ed25519 image signing.

### Host (`klippy/`)
* `extras/trajectory_queuing.py` — owns trajectory actuators, runs the
  fitter each flush window, emits normal G0/G1 motion as coordinated
  per-joint quintics, and keeps the host intention
  twin. `chelper/segfit.c` — the C segment fitter (host side of the
  drift-free integration; **must** stay bit-identical to `trajq.c`).
* `extras/trajectory_pwm.py` — configures sampled PWM/DAC actuators and
  preflights scalar value functions into bounded, drift-corrected segment
  batches with an intentional terminal hold.
* `extras/failure_recovery.py` — pause-and-hold orchestration and resume
  reconciliation.
* `extras/timesync.py` — beacon relay and discipline reporting.
* `extras/helix_status.py` — the `HELIX_STATUS` capability report.
* `mcu.py` — transport, link-loss survival, and the interrupt-driven
  homing path in `MCU_endstop`.

### Protocol library (`lib/intentproto/`)
Freestanding C++ (no heap, exceptions, RTTI, virtual dispatch, or STL):
the CRC16/VLQ framing, framing v2 (BCH FEC), the datagram/HMAC transport,
the DTLS-class session, the CAN carrier, SHA-512/Ed25519 for signing, the
host retransmit session, the served dictionary builder, and the
annotation layer. Build and test it standalone with `make test` in that
directory.

## Two decisions worth internalizing

**1. Annotation, not code generation.** A command is a macro next to its
handler:

```cpp
KLIPPER_RESPONSE(oams_status, (uint8_t, code), (uint32_t, value));

KLIPPER_METHOD(oams_load, (uint8_t, spool)) {
    start_load(spool);
    intentproto::reply(oams_status{OK, spool});
}
```

The descriptor registers itself before `main()`; `init()` freezes the
registry and assigns wire ids. The data dictionary is a *serialization
of that registry*, not a scrape of your source. There is no generator,
no build step that parses code, and parameter **types** are deduced from
the function signature so they can never drift.
See [doc 10](founding/0001-motion-intentions/10-Protocol_Library.md).

**2. Drift-free integration is a contract, not an implementation
detail.** The MCU segment evaluator (`trajq.c`), the host fitter
(`chelper/segfit.c`), and the Python reference (`trajectory_queuing.py`)
compute end-of-segment position with the *same* integer truncation. They
must stay in lockstep; a change to one is a change to all three, and the
higher-order tests exist to prove they still agree bit-for-bit.
See [doc 02](founding/0001-motion-intentions/02-Intention_Protocol.md).

## Building and testing

* **Firmware:** the usual Klipper flow — `make menuconfig` then `make`.
  HELIX capabilities are Kconfig flags (`WANT_TRAJECTORY`,
  `WANT_TRAJECTORY_HIGHER_ORDER`, `WANT_TRIGGER_SOURCE`,
  `WANT_HEATER_HOLD`, `WANT_SYSCALL_API`, `WANT_SIGNED_IMAGES`, …),
  most defaulting on where code size allows and off on
  `HAVE_LIMITED_CODE_SIZE` boards.
* **Protocol library:** `cd lib/intentproto && make test` (host g++),
  `make capi` (the C ABI + cffi binding), `make embedded` (a Cortex-M0
  size report).
* **Host units:** the standalone tests in `test/` (e.g.
  `traj_higher_order_test.py`, `failure_recovery_resume_test.py`,
  `pause_resume_recovery_test.py`, `endstop_hw_trigger_test.py`,
  `helix_status_test.py`) run with plain `python3` and stub what they need.

## The constrained-board policy

Small MCUs (the STM32F042 is the working tripwire) cannot fit every
feature. HELIX's rule is explicit: a feature that doesn't fit is simply
**not built** on that target — no contortions, no shrinking the feature
to squeeze in. Kconfig gates express this (`default y if
!HAVE_LIMITED_CODE_SIZE`), and where even the bootloader won't fit with
a feature (e.g. Ed25519 signing on a 16 KB boot budget), that target
builds without it. When you add a feature, add its gate and confirm the
floor targets still build.

## Porting to a new MCU family

Because HELIX is written against the same `board/*.h` surface Klipper
uses — and now against the versioned
[board syscall table](founding/0001-motion-intentions/13-Syscall_API.md) —
a new family that implements the board primitives gets the
hardware-agnostic modules (`trajq`, `execlog`, `trigger_source`,
`heater_hold`, `timesync`) for free. The ESP32 port is the worked
example of doing this for a network-native, dual-core target; its
architecture stance is [doc 12](founding/0001-motion-intentions/12-ESP32_Architecture.md).

## Contributing

HELIX develops in the open and keeps Klipper's attribution and GPLv3
licensing intact. New protocol features belong in `lib/intentproto` with
tests; new firmware capabilities behind a Kconfig gate with the floor
targets still building; new host behavior in `klippy/extras` with a
standalone test in `test/`. Design changes of any weight get a founding-document entry
in `docs/founding/` before the code, so the *why* is recorded alongside the
*what* — the same discipline that produced the
[FD-0001 canon](founding/0001-motion-intentions/00-Vision.md).

Notice the through-line in that paragraph: **new behavior goes in new
files, not on top of upstream's.** Before writing any change, read
[How to change Helix without fighting upstream](CONTRIBUTING.md#how-to-change-helix-without-fighting-upstream) —
the decision rule for *where* a change should live so it stays merge-clean
— and [Upstream_Tracking.md](Upstream_Tracking.md), the enforced contract
behind it.
