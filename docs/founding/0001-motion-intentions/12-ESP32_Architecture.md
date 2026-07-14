# FD-0001: ESP32 Architecture Stance

Status: Partial in HELIX 0.9; ESP-IDF v5.3.2 Xtensa builds pass for the
component, component-RMT, and modem variants with watchdog/reset contracts.
The component UDP/session console has now run on a Lolin32 over a real LAN;
the modem and listed motion/stepper follow-ups remain unvalidated.

The ESP32 is this fork's network-native target
([07-Link_Transport.md](07-Link_Transport.md)). Mainline Klipper has
stated it will never support the chip because a WiFi ESP32 cannot be
*self-contained* — and that judgment deserves a precise treatment
rather than a shrug, because it is two objections fused together with
very different answers.

## The two halves of "self-contained"

**The radio: genuinely closed.** The WiFi/BT MAC and PHY are driven
by closed-source binary blobs (`libpp`, `libnet80211`, `libphy`) that
Espressif does not document at register level and that hard-require
FreeRTOS primitives and the ESP-IDF driver framework. There is no
register-level path to the radio. Any WiFi ESP32 firmware contains
IDF, FreeRTOS, and blobs somewhere on the die. This half of the
objection is unanswerable, and this fork does not pretend otherwise.

**Everything else: as open as an STM32.** The non-radio peripherals —
GPIO, timer groups, UART, SPI, I2C, ADC, LEDC/MCPWM, RMT, PCNT — are
fully documented in the Technical Reference Manual with plain register
access, and the `soc/*.h` register headers are Apache-2.0. Note what
Klipper's "self-contained" ports actually are: the STM32 port vendors
CMSIS register headers into `lib/` and writes register drivers against
them. The identical pattern applies here: vendor the ESP32 soc
register headers into `lib/esp32/` and write `src/esp32/` drivers at
register level. (The port's GPIO binding already works this way —
`GPIO.out_w1ts/w1tc` direct writes — because the IDF driver API is not
ISR-safe anyway.)

## Target architecture: IDF as modem

The strongest position available, staged as the P7 follow-up to the
initial IDF-component build:

* **Core 0 (PRO CPU)**: unicore-configured IDF + FreeRTOS + the radio
  blobs. Nothing of Klipper runs here. Its entire job is moving
  HMAC-authenticated datagrams between the air and a lock-free ring
  buffer in shared RAM.
* **Core 1 (APP CPU)**: booted manually (a documented register
  sequence), running **bare-metal Klipper** — no RTOS, no IDF calls,
  IRAM-resident hot code, register-level peripherals, Klipper's own
  scheduler exactly as on STM32. Keeping the hot path in IRAM also
  removes the real determinism hazard on this chip: flash-cache
  stalls, which halt *both* cores when XIP code misses.
* The shared-memory ring is, from Klipper's perspective, just another
  serial port — the same role a UART DMA buffer plays today.

Core 0 thereby becomes an on-die network coprocessor: no different in
kind from the closed firmware inside a W5500 Ethernet controller or a
CAN transceiver that mainline boards already use without controversy.
The motion half of the chip *is* self-contained Klipper; the blob is
quarantined behind a byte pipe it cannot reach across.

## Staging

1. **Now (P7 initial)**: the IDF-component build — Klipper's sources
   compiled as an IDF component, sched pinned to core 1, UDP console
   over the target-independent datagram glue. This validates the port
   surface, the timer contract, and the network path end-to-end; the
   fork's design center (deep intention queues absorbing link and
   scheduling jitter, [02](02-Intention_Protocol.md)) makes it
   functionally sound as-is.
2. **Cheap purity wins, adopted regardless of politics**: vendored
   register headers, register-level drivers, IRAM discipline for
   every hot path. These improve determinism on their own merits.
3. **IDF-as-modem**: re-plumb the console glue's socket ops to the
   shared-memory ring and boot core 1 bare. Mostly a re-plumbing, not
   a rewrite, precisely because the datagram glue is
   target-independent.

**Stage-3 status**: implemented as the selectable `modem`
architecture (Kconfig; the stage-1 `component` build remains the
fallback). `lib/esp32/` vendors the Apache-2.0 register headers,
`src/esp32/appcpu_boot.c` + `appcpu_vectors.S` boot core 1 bare with
a private vector table and a polled scheduler, `shmem_console.c` /
`modem.c` split the console over a lock-free shared-RAM ring — with
HMAC verification kept on the Klipper core, so the blob core shuttles
only sealed bytes it cannot forge — and the hot path is pinned to
IRAM. Every register sequence is source-verified against ESP-IDF
v5.3.2 and both architectures host-compile/link, but the modem
architecture has **not yet run on silicon** — see the bring-up
checklist in [docs/ESP32.md](../../ESP32.md).

Both stages have a rebooting liveness contract. Component mode subscribes
the Klipper task to IDF's task watchdog and enables panic-on-timeout. Modem
mode feeds a register-level Timer Group 1 MWDT from the bare scheduler; its
`reset` command asks core 0 to execute `esp_restart()` and relies on that MWDT
if the modem core cannot respond. Cross-core ring publication uses explicit
acquire/release atomics.

## Command parity with STM32

The generic command modules (`gpiocmds`, `endstop`, `trsync`,
`buttons`, `neopixel`, `tmcuart`, `spicmds`, `i2ccmds`, `pwmcmds`,
plus this fork's `trajq`/`execlog`/`trigger_source`/`heater_hold`/
`timesync`) are hardware-agnostic: they compile for any port that
implements the board API. Bindings beyond the initial set (timer,
GPIO, ADC, UDP console): **SPI**, **I2C**, **hard PWM (LEDC)**, and
the step path — where the **RMT peripheral** is the flagged escape
hatch ([07](07-Link_Transport.md)) for hardware-timed step pulses,
immune to the WiFi-stack timer-IRQ jitter that makes tick-precise
stepping on this chip hard. That escape hatch now **exists**: with
`CONFIG_KLIPPER_RMT_STEP` the port compiles an esp32-specific stepper
backend (`src/esp32/rmt_stepper.c`) *in place of* the portable
`src/stepper.c` — same `config_stepper`/`queue_step` command surface,
but the `(interval, count, add)` triples drive the RMT channels
(`rmt_step.c`) rather than GPIO toggles in a timer ISR. It solves the
integration's real problems concretely: direction changes fence the
pulse stream (dir GPIO flipped only between drained trains), the first
pulse of every train is clock-anchored from a sched timer, and a
wrap-mode refill underrun is watermarked (transmitter read cursor vs.
write cursor) into a controlled stop wired to shutdown rather than
silent bad motion; homing/trsync stop ceases pulses immediately and
freezes an exact clock-derived position. The backend host-compiles,
links and passes off-hardware pulse-planning unit tests, but has
**not yet run on silicon** (component architecture only, since the RMT
refill interrupt needs `esp_intr_alloc`; the bare-core modem arch
revisits it with a register-level ISR) — see the bring-up checklist
in [docs/ESP32.md](../../ESP32.md). The FOC/sampled backend (its own
timer, tolerant of µs-level jitter) remains this chip's first-class
citizen; the RMT path is the production classic-stepping route once
validated.

## Honesty clause

Mainline will not take this port under any structuring — the blob is
on-die and that is disqualifying for upstream no matter how it is
quarantined. Per the fork stance ([06-Migration.md](06-Migration.md))
the goal is not acceptance; it is being *unobjectionable in
architecture*: the same port layout as every other target, the same
vendored-header pattern, IDF confined to one directory and eventually
one core.
