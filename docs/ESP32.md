# ESP32 micro-controller (UDP/WiFi network transport)

This document describes the experimental ESP32 target and the UDP
datagram console transport it uses (RFC 0001
[doc 07](rfcs/0001-motion-intentions/07-Link_Transport.md), phase P7).
The same transport is testable on a desktop with no hardware via the
linux micro-controller's UDP option - see
[the test recipe below](#desktop-testing-the-linux-mcu-over-udp),
which is the recommended way to exercise the network stack.

## Architecture

The port offers two selectable architectures (Kconfig, "Klipper
firmware" -> Architecture; RFC 0001
[doc 12](rfcs/0001-motion-intentions/12-ESP32_Architecture.md)):

* **component** (stage 1, default): klipper compiled as an IDF
  component, running as a FreeRTOS task pinned to core 1; IDF is
  present on both cores.  This is the original, validated build.
* **modem** (stage 3, "IDF as modem"): core 1 runs **bare-metal
  klipper** — no RTOS, no IDF calls, register-level peripherals,
  IRAM-resident hot path — and core 0 is reduced to a network
  coprocessor shuttling sealed console datagrams through a lock-free
  shared-memory ring.  See
  [the modem architecture](#the-modem-architecture-idf-as-modem)
  below, including its **runtime-unvalidated** status banner.

Both share the identical console stack:

```
klippy (serial protocol, unchanged)
   |          pty
lib/intentproto/tools/udp_bridge.py     (host)
   |          UDP datagrams: [u16 seq][u8 flags][frames][8B HMAC-SHA256]
src/generic/udp_console.c               (mcu, transport independent)
src/generic/udp_datagram.cpp            (C shim over lib/intentproto)
   |          struct udp_console_ops {recv, send, rx_accepted}
src/esp32/udp_port.c (component)  - or -  src/esp32/shmem_console.c +
src/esp32/modem.c (modem)         - or -  src/linux/udp.c
```

* The console glue (`src/generic/udp_console.c`) authenticates and
  unwraps received datagrams through the intentproto datagram layer
  and feeds the contained klipper frames to the normal frame
  dispatcher; outgoing frames are batched for ~2ms and sealed into
  one datagram (matching the bridge's host-side batching).
* Authentication (truncated HMAC-SHA256 with a pre-shared key) is
  mandatory; running without it requires the explicit
  "trust network" confession on both ends.  Responses are only ever
  sent to the source address of the last *authenticated* datagram.
* The socket itself sits behind a three-function ops struct, so the
  identical glue serves ESP32 WiFi, ESP32 Ethernet (RMII - replace
  the WiFi bringup with `esp_eth`, the binding is unchanged), and the
  linux mcu.
* Datagram-level erasure FEC (XOR parity) exists in lib/intentproto
  but is not yet enabled by the glue: single-loss recovery delivers
  the recovered datagram out of order, which the in-order frame
  dispatcher would nak anyway.  Loss recovery is currently the frame
  layer's ARQ; enabling parity needs a small in-order reassembly
  buffer (future work).

## Core pinning (RFC 0001 doc 07) - component architecture

The ESP32 is dual core; the component architecture splits it:

* **Core 0**: WiFi and lwIP tasks (pinned via `sdkconfig.defaults`),
  `app_main` (NVS init, PSK load, WiFi bringup), the UDP receive
  task, and the deferred ADC conversion task.
* **Core 1**: the klipper scheduler task
  (`xTaskCreatePinnedToCore(..., 1)`) and the klipper hardware timer
  interrupt - the GPTimer callback is registered from the core-1
  task, which allocates its interrupt on core 1.  Motion dispatch
  never contends with the radio stack's interrupts.

The klipper timer is a GPTimer at 20MHz (`CONFIG_CLOCK_FREQ`
20000000 in the hand-written `src/esp32/autoconf.h`): the highest
integer division of the 80MHz APB clock that keeps a long 32-bit
wrap period (~214s), giving 50ns scheduling granularity.

**Caution (from RFC 0001 doc 07):** the WiFi stack's interrupt and
flash-cache behavior make *tick-precise* step generation on this
silicon genuinely hard.  Core pinning removes most contention, but
occasional microsecond-level jitter remains (e.g. during flash
writes, when interrupts are briefly deferred).  The classic stepper
backend compiles and runs on this port but should be treated as
**experimental**; the RMT/PCNT pulse peripherals are the likely
escape hatch for production step generation, and the FOC backend
(own timer, tolerant of µs-level ISR jitter) is a better first
citizen of this chip.  This target is FOC-first.

## The modem architecture (IDF as modem)

> **RUNTIME UNVALIDATED — NEEDS HARDWARE.**  Everything in this
> section compiles and links (host-gcc harness, both architectures),
> the SPSC ring is unit-tested on the desktop under ThreadSanitizer/
> AddressSanitizer, and every register write and boot step is
> source-verified line-by-line against ESP-IDF v5.3.2 — but none of
> it has executed on silicon: the development environment has no
> xtensa toolchain and no devkit.  The APP-CPU bringup, the vector
> table, the polled timer and the IRAM placement are exactly the
> kind of code that only a serial console and a scope can finish.
> Treat the `component` architecture as the working build until the
> [bring-up checklist](#devkit-bring-up-checklist) has been run.

The stage-3 architecture of doc 12: IDF, FreeRTOS and the closed
radio blobs are confined to core 0, which becomes an on-die network
coprocessor - no different in kind from the closed firmware inside a
W5500.  Core 1 runs klipper the way an STM32 does: bare metal,
register-level drivers against the vendored `lib/esp32` headers, its
own stack and vector table, scheduler in a tight loop.

```
core 0 (PRO)  unicore IDF + WiFi/lwIP + blobs     src/esp32/modem.c
                 |  sealed datagrams (HMAC intact) - opaque bytes
              lock-free SPSC rings in shared DRAM  src/esp32/shmem_ring.h
                 |  "just another serial port"
core 1 (APP)  bare-metal klipper sched_main()      src/esp32/appcpu_boot.c
              udp_console.c + HMAC verify          src/esp32/shmem_console.c
```

Files (all under `src/esp32/`): `appcpu_boot.c` (core-1 release
sequence + the bare runtime), `appcpu_vectors.S` (entry stub +
private vector table), `shmem_ring.h` (the ring), `shmem_console.c`
(core-1 console ops), `modem.c` (core-0 socket shuttle).  The IDF
project is built **unicore** (`CONFIG_FREERTOS_UNICORE=y`), so IDF
never learns core 1 exists.

### Security property

HMAC verification runs on the **klipper core**, not the radio core:
the modem moves sealed datagrams it cannot forge or unwrap, and it
only ever transmits to a peer address that arrived attached to a
datagram core 1 authenticated (the address blob travels with each rx
ring record and is republished by core 1 through a seqlock on
acceptance).  The blob is quarantined behind a byte pipe it cannot
reach across - doc 12's stance made literal.

### APP-CPU bringup sequence

`esp32_appcpu_start()` (core 0, called from `app_main` after WiFi is
up) replays IDF's own SMP release sequence, source-verified against
the v5.3.2 tree (each step carries the file/line citation in the
code):

1. Refuse on single-core die variants (`EFUSE_RD_DISABLE_APP_CPU`).
2. Enable the peripheral clocks core 1 will use at register level
   (TIMG0, HSPI, VSPI, I2C0, LEDC) - done on core 0 because
   `DPORT_PERIP_CLK_EN_REG` is a shared RMW register and DPORT must
   never be touched cross-core (the ESP32 DPORT hazard).
3. Clear core 1's interrupt-matrix routing
   (`cpu_start.c core_intr_matrix_clear()`).
4. APP flash cache + MMU init: `Cache_Read_Disable(1)`,
   `Cache_Flush(1)`, MMU invalid-access clear, `mmu_init(1)`, copy
   the 2048-entry PRO flash-MMU table to the APP table
   (`cpu_start.c do_multicore_settings()`, needed because the
   unicore boot skipped it), then `Cache_Flush(1)`,
   `Cache_Read_Enable(1)` (`start_other_core()`).
5. Unstall: clear the split 0x86 stall magic in
   `RTC_CNTL_OPTIONS0_REG` / `RTC_CNTL_SW_CPU_STALL_REG`
   (`hal/esp32 cpu_utility_ll.h`).
6. Clock-gate + reset pulse via `DPORT_APPCPU_CTRL_B/C/A_REG`
   (`start_other_core()`), then hand the ROM the entry address with
   `ets_set_appcpu_boot_addr(appcpu_entry)`.
7. Wait (<=1s) for core 1 to set the `core1_alive` flag.

`appcpu_entry` (assembly) resets the register-window state, sets
`INTENABLE=0`, installs the private vector table, sets
`PS=WOE|UM|INTLEVEL 0`, switches to a 16KiB DRAM stack and calls
`appcpu_main()`, which initializes the bare timer + console and
enters `sched_main()` - after which core 1 must never reach an
IDF/FreeRTOS symbol (nothing links it there: no interrupt is routed,
no callback registered).

The vector table carries the canonical Xtensa window
overflow/underflow and alloca handlers (byte-for-byte the sequences
from IDF's `xtensa_vectors.S`, mechanically diffed); every other
vector parks the core after recording `EXCCAUSE/EPC1/EXCVADDR` into
`esp32_core1_fault[]`, which the modem task reports over the core-0
log - the bare core's only diagnostics channel.

**Flash-write discipline**: a flash write disables the cache both
cores execute from.  WiFi bringup (whose first run writes PHY
calibration to NVS) therefore completes *before* core 1 boots, and
nothing writes flash afterwards.  Any future feature that writes
flash at runtime must stall core 1 first.

### Ring protocol

`shmem_ring.h` implements a single-producer/single-consumer byte
ring (8KiB per direction) in internal DRAM, which both cores address
uncached.  Records are `[u16 len][payload]`; rx records carry a
16-byte opaque source-address blob (a `sockaddr_in` in the modem's
encoding - core 1 copies it, never parses it) ahead of the sealed
datagram.  Indices are free-running `uint32` moved with
acquire/release atomics, which gcc lowers to plain `l32i`/`s32i`
fenced with `memw` on the LX6 - and which lets the desktop unit test
(`src/esp32/shmem_ring_test.c`: two threads, 2M records, content +
order + torn-index invariants) run under ThreadSanitizer with the
protocol fully visible to it.  A full ring drops the datagram -
identical recovery contract to a wired port's rx/tx overflow (frame
layer ARQ / host retransmit).

Wakeups are polled, not signalled: core 1 checks the rx ring in its
`irq_poll()`; the modem task alternates a 1ms-timeout `recvfrom`
with a tx-ring drain.  Board->host latency is bounded by ~1ms + the
console's own 2ms batching; the intention-queue design center makes
both irrelevant to motion.

### The polled bare runtime

Core 1 enables no interrupts at all (like the klipper linux mcu,
which dispatches timers from `irq_poll()` in the sched loop and
serves production printers that way).  The klipper timer is TIMG0
timer 0 at register level, 80MHz APB / 4 = 20MHz (same
`CONFIG_CLOCK_FREQ` as the component arch); `irq_poll()`/`irq_wait()`
run `timer_dispatch_many()` when the next deadline is due and
surface pending ring records.  Dispatch latency therefore equals the
longest non-preemptible stretch of task code - flash-cache misses in
task-level code included - which is the same microsecond-order
jitter class the component arch already tolerates and doc 07 already
flags for classic stepping (this target stays FOC-first).  A level-1
timer ISR through the private vector table is the natural upgrade
once hardware allows measuring both variants; `irq_save()` is
already a real `rsil` so the critical sections survive that change.

Peripheral notes specific to the modem arch:

* **ADC**: the oneshot unit, all eight ADC1 channel configs and the
  conversion worker are set up from core 0 before core 1 boots;
  the sample handshake is a lock-free seq/ack word pair (core 1
  requests, the core-0 worker converts - the SAR ADC is entangled
  with WiFi calibration, so conversions stay on the modem core).
* **I2C**: bus-error recovery reprograms the controller but cannot
  pulse the DPORT module reset from core 1; a truly wedged FSM
  escalates to `I2C_BUS_TIMEOUT` -> shutdown.
* **rmt_step.c** is compiled only in the component arch (it needs
  `esp_intr_alloc`); it returns with the bare-core ISR work.
* GPIO pad config, GPIO-matrix routing, pulls (including the RTC-pad
  pull table), SPI, and LEDC config are all register-level in this
  arch - no IDF call remains anywhere core 1 can reach.

### IRAM discipline and map

A flash-cache miss stalls the requesting core for microseconds while
the line refills over SPI, and the fill path is shared - so the
motion core's dispatch path must never fault.  Two mechanisms:

* `DECL_IRAM` (src/esp32/internal.h) - a `.iram1.klipper.*` section
  attribute on the board files' hot functions (`timer_read_time`,
  `irq_poll`/`irq_wait`, the ring poll), mapped by IDF's built-in
  `*(.iram1 .iram1.*)` rule in both architectures.
* `src/esp32/main/linker.lf` - an ldgen fragment mapping whole hot
  objects `noflash` in the modem arch.

IRAM-resident set (modem arch) and estimated code sizes (host-gcc
x86-64 text as proxy; confirm with `idf.py size` + map file on the
first real build):

| object | why | ~text |
| ------ | --- | ----- |
| sched.c | timer list + task loop | 2.0KiB |
| generic/timer_irq.c | `timer_dispatch_many` | 0.5KiB |
| stepper.c | step event handlers | 2.5KiB |
| trajq.c + traj_stepper.c | trajectory execute path | 5.4KiB |
| esp32/gpio.c | `out_w1ts/w1tc` hot writes | 2.2KiB |
| esp32/appcpu_boot.c | poll loop, bare timer | 1.6KiB |
| esp32/shmem_console.c | ring ops on dispatch path | 0.9KiB |
| appcpu_vectors.S | vector table + entry | 1.2KiB |

Total ≈ 16KiB code (+ a few KiB rodata moved by `noflash`) against
the 128KiB IRAM, most of which WiFi/IDF claims; the budget fits with
tens of KiB to spare.  Task-level code (command parsing, config,
sensors) deliberately stays in flash: IRAM is the scarce resource,
and a miss there costs dispatch latency of the jitter class already
accepted on this chip.  Note the ldgen mapping of the klipper OBJECT
library into `libmain.a` is one of the things the first hardware
build must confirm (see checklist).

### Building the modem architecture

```
cd /path/to/klipper/src/esp32
idf.py -DSDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.modem" \
    set-target esp32
idf.py menuconfig    # WiFi credentials etc., as below
idf.py build flash monitor
```

`sdkconfig.defaults.modem` sets `CONFIG_FREERTOS_UNICORE=y` and
`CONFIG_KLIPPER_ARCH_MODEM=y`; everything else (PSK provisioning,
bridge, klippy) is identical to the component arch below.

### Devkit bring-up checklist

What a devkit owner runs, in order, to convert "source-verified" to
"validated" (serial monitor + scope; classic ESP32-WROOM/WROVER,
dual-core silicon):

1. **Component-arch smoke test first**: build/flash the default
   architecture, join WiFi, run the udp_bridge + `scripts/console.py`
   identify handshake.  Proves toolchain, board, credentials, PSK.
2. **Modem build boots core 0**: flash the modem build; expect
   `klipper_modem: modem shuttling datagrams on udp port ...` then
   `klipper_appcpu: core 1 running bare klipper (after ~Xms)` on the
   monitor.  If instead `core 1 did not start`, the logged
   fault/cause/epc triple (from `esp32_core1_fault[]`) localizes it:
   cause=0 means the core never reached the entry stub (bringup
   sequence), 0x1nn means a spurious interrupt-level vector fired,
   anything else is a real exception at EPC.
3. **Console end-to-end**: identify handshake + dictionary download
   through the ring path; confirm auth-failure rejection (wrong PSK)
   still drops silently; confirm responses only go to the
   authenticated peer (send from a second source).
4. **IRAM placement**: `idf.py size-components`, then check the map
   file that sched/stepper/trajq/timer_irq landed in `iram0` (the
   OBJECT-library-in-libmain.a ldgen question) and that IRAM didn't
   overflow.
5. **Timer sanity**: `timesync` round trips; scope a
   `queue_step`-driven pin for rate correctness; measure dispatch
   jitter (poll-loop worst case) idle vs. under command load and
   WiFi soak - compare against the component arch numbers.
6. **Flash-cache discipline**: confirm no runtime NVS writes occur
   (none are expected after WiFi-up); optionally force one to verify
   the failure mode is understood.
7. **Peripheral re-verification on the register-level config paths**
   (they differ from the component arch): gpio in/out + pulls on an
   RTC pad (e.g. GPIO12) and a non-RTC pad, SPI loopback, I2C device
   probe + NACK/timeout statuses, LEDC duty on a scope, ADC
   readings.
8. **Fault path**: deliberately crash core 1 (e.g. a test null
   store) and confirm the modem logs the parked fault.

## Peripheral bindings

Beyond the initial timer/GPIO/ADC set, the port implements the RFC
0001 [doc 12](rfcs/0001-motion-intentions/12-ESP32_Architecture.md)
"command parity" bindings.  Per that document's stance, everything
documented in the TRM is driven at register level against the
Apache-2.0 `soc/*.h` headers (vendored in `lib/esp32/`); in the
component architecture the IDF driver additionally appears where the
call is task-context-only configuration - in the modem architecture
those configuration paths are register-level too (see above):

| Binding | Commands | Implementation |
| ------- | -------- | -------------- |
| GPIO in/out | `gpiocmds`, `endstop`, `trsync`, `buttons` | IDF pad config; register (`out_w1ts/w1tc`) hot path |
| ADC (ADC1) | `adccmds` | IDF oneshot, deferred to a core-0 task |
| SPI (`spi2`/HSPI, `spi3`/VSPI) | `spicmds` + software SPI | register level (polled, W0..W15 buffer) |
| I2C (`i2c0`) | `i2ccmds` + software I2C | register level (command-list engine) |
| Hard PWM (LEDC high-speed) | `pwmcmds` | IDF `ledc` config; register duty updates |
| UART bit-bang | `tmcuart` | generic (gpio + timers) |
| Neopixel | `neopixel` | generic bit-bang - timing is subject to the jitter caution above; verify on hardware |
| RMT step module | `config_stepper`/`queue_step`/... (when `CONFIG_KLIPPER_RMT_STEP`) | register level, see below |

Details and constraints:

* **SPI** (`src/esp32/spi.c`): synchronous polled transfers, ISR
  tolerant (klipper's `spidev_shutdown` can run from the shutdown
  path, where the IDF driver's mutexes are unusable).  Bus `spi2` is
  MISO/MOSI/CLK = GPIO12/13/14, `spi3` is GPIO19/23/18, routed
  through the GPIO matrix; modes 0-3; the divider realizes
  80MHz/(2*pre) without exceeding the requested rate, capped at
  20MHz for matrix-routed MISO timing.  CS is a plain klipper gpio.
* **I2C** (`src/esp32/i2c.c`): bus `i2c0` on SCL=GPIO22, SDA=GPIO21
  (open drain, internal pullups - external pullups still
  recommended).  The address byte travels in its own hardware
  command segment so klippy sees distinct `START_NACK` /
  `START_READ_NACK` / `NACK` / `TIMEOUT` statuses (stm32 semantics);
  transfers longer than the 32-byte FIFO use the TRM's END-command
  continuation.  Errors reset the controller (the ESP32 I2C FSM is
  not reliably recoverable in place).
* **Hard PWM** (`src/esp32/hard_pwm.c`): 8 LEDC high-speed channels
  (one per pin), 4 shared timers (channels with equal cycle_time
  share).  `cycle_time` (20MHz klipper ticks) maps to an LEDC
  frequency of `20MHz/cycle_ticks` with duty resolution chosen as
  the largest realizable `res <= 15` bits (`~log2(80MHz*cycle/20MHz)`);
  higher PWM frequency costs resolution (20kHz -> 12 bits, 1MHz -> 6
  bits).  Duty writes from timer dispatch are two register writes
  (duty + duty_start latch); `PWM_MAX` is 32768.

## RMT step generation (implemented - unvalidated on silicon)

> **IMPLEMENTED, NOT YET RUN ON HARDWARE.**  The backend below
> host-compiles and links (the esp32 hostcheck harness builds a third
> `component-rmt` variant with `CONFIG_KLIPPER_RMT_STEP`), its
> pulse-planning logic is unit-tested off-hardware (item translation,
> clock-anchor math, wrap-underrun watermark), and every RMT register
> and field is source-verified against ESP-IDF v5.3.2 - but it has
> **not executed on a devkit**.  Edge timing, the dir fence, the
> anchor latency and the underrun watermark are exactly the kind of
> code only a scope can finish; see the
> [bring-up checklist](#rmt-step-bring-up-checklist).  The default
> build leaves `CONFIG_KLIPPER_RMT_STEP` off and uses the classic
> timer-IRQ `stepper.c`.

`src/esp32/rmt_step.c` is the register-level pulse-train emitter for
the "RMT escape hatch" flagged in docs 07/12: each RMT channel turns
klipper-style `(interval, count, add)` move triples into
hardware-timed step pulses at 20MHz resolution using the channel's
64-item RAM as a wrap-mode ring buffer (threshold interrupt refills
one half while the other transmits; long intervals become low-level
filler items).  Once started, edge timing is immune to WiFi and
flash-cache jitter.

`src/esp32/rmt_stepper.c` is the **stepper backend** that drives it.
When `CONFIG_KLIPPER_RMT_STEP` is set (component architecture only -
the RMT refill interrupt needs `esp_intr_alloc`) the esp32 CMake
compiles it **in place of** the portable `src/stepper.c`, so it
registers the identical `config_stepper` / `queue_step` /
`set_next_step_dir` / `reset_step_clock` / `stepper_get_position` /
`stepper_stop_on_trigger` command surface (klippy is unchanged - same
dictionary, same 76/27 command/response counts) but feeds the RMT
channels instead of scheduling GPIO toggles.  One RMT channel is
consumed per configured stepper (8 total).  `src/stepper.c` itself is
untouched and remains the backend for every other target.

**Integration choice: option A (the `queue_step` move stream).**  Of
the two candidate integration points, feeding `rmt_step` from the
legacy `queue_step` path was chosen over emitting RMT items from the
trajectory backend (`traj_stepper.c`): `rmt_step` already speaks
`(interval, count, add)`, so the translation is direct, and the
substitution stays a self-contained esp32 file that *owns* the pulse
stream rather than a patch of the portable, per-event `stepper.c`
(which, as doc 12 noted, cannot be retargeted at a queued pulse
stream).

The three open problems are now solved concretely:

* **Direction-change fencing.**  An RMT channel emits step edges only,
  so a dir change cannot occur mid-train.  `rmt_stepper.c` groups
  consecutive same-direction moves into a **train** and closes the
  train at each direction change.  The next train's start clock is the
  current train's exact end clock (chained with
  `rmt_step_move_ticks()`).  The dir GPIO is flipped in the anchor
  timer callback, which first **fences on `rmt_step_is_busy()`** - so
  the flip never lands under live pulses - and bails to `shutdown`
  ("RMT dir fence timeout") if a channel is still busy past its
  computed end clock.
* **Clock anchoring.**  Each train's first pulse is armed from a sched
  timer firing at the train's absolute start clock, correlating the
  `tx_start` register write to `timer_read_time()`.  Chaining the
  per-train start clocks keeps the whole motion anchored, and every
  direction change / `reset_step_clock` re-synchronizes exactly.
  *Residual error:* (1) the interval between the timer deadline and
  the `tx_start` write - a few timer-dispatch microseconds - offsets
  the whole train by a constant; (2) the RMT gen's per-step phase puts
  the first edge of each move at the move's start clock rather than one
  interval later (the klippy `queue_step` convention), a bounded
  offset of ≤ one step interval that is **non-cumulative** - every
  move boundary, and therefore every train boundary, re-aligns to the
  clock because each move's emitted span equals its
  `rmt_step_move_ticks()` span.  Step *counts* are exact; only
  sub-interval edge phase drifts, so reported position is exact.  The
  dir-setup time before the first step equals the gap between the dir
  GPIO write and `tx_start` in the anchor callback.
* **Wrap-mode underrun watermarking.**  In wrap mode the hardware
  re-reads stale ring items if a refill is late - duplicated steps,
  not a clean stop.  The refill ISR now samples the transmitter read
  cursor (`RMT.status_ch[ch] & 0x3FF`, normalized per
  `hal/esp32/.../rmt_ll.h`) against the write cursor at every
  threshold event; if the writer's lead has collapsed below
  `RMT_WRAP_MARGIN` it **latches an underrun and blanks the ring**, so
  the transmitter hits an end marker (controlled stop) instead of
  emitting garbage.  `rmt_stepper_task()` polls the latch
  (`rmt_step_take_underrun`) and escalates to `shutdown`
  ("RMT step underrun") - lost steps mean a desynchronized axis, a
  hard fault.  (The exact margin wants scope calibration on first
  silicon.)  A benign move-queue drain still ends a train cleanly via
  the existing end-marker path and is *not* flagged as an underrun.

**Homing / trsync stop.**  `stepper_stop_on_trigger` registers a
trsync signal that, on trigger, calls `rmt_step_abort()` (pulses cease
within one RMT item), cancels any pending train, and **freezes an
exact stopped position from the clock** - `rmt_step_move_emitted()`
counts how many edges of the in-flight train have physically been
emitted by the trigger instant, so `stepper_get_position` reports the
true position with no pulse counter.  (A PCNT cross-check remains
future work.)

The FOC/sampled backend (own timer, tolerant of microsecond jitter)
remains this chip's first-class motion citizen; the RMT backend is the
production path for classic step/dir steppers once validated on
silicon.

### RMT step bring-up checklist

What a devkit owner runs to convert "source-verified" to "validated"
(component architecture, `CONFIG_KLIPPER_RMT_STEP=y`; scope on a
step/dir pin):

1. **Build/flash** the component arch with the RMT step option; run
   the identify handshake so klippy downloads the dictionary and
   configures a stepper (each consumes one RMT channel).
2. **Rate correctness:** drive a constant-velocity `queue_step` stream
   and scope the step pin; confirm the pulse rate and the pulse width
   (`step_pulse_ticks`) match, idle and under WiFi soak.  Compare
   edge jitter against the classic backend on the same pin - the RMT
   edges should be jitter-free where the classic ISR path is not.
3. **Clock anchor:** with `timesync` established, command a move at a
   known future clock and confirm the first edge lands at that clock
   within the documented `tx_start` dispatch residual.
4. **Direction fence:** alternate `set_next_step_dir` + `queue_step`
   so the stream reverses; confirm the dir pin flips only in the gap
   between trains (no dir edge under live step pulses) and that step
   counts are exact across the reversal.
5. **Underrun watermark:** starve the refill (e.g. stall the host feed
   mid-move) and confirm the channel stops cleanly and the board
   reports `shutdown: RMT step underrun` rather than emitting extra
   steps.  Tune `RMT_WRAP_MARGIN` from the observed read/write cursor
   lead.
6. **Homing:** run a homing move into an endstop and confirm pulses
   cease immediately on trigger and `stepper_get_position` matches the
   physically stepped count (cross-check with a PCNT channel if
   available).

## Building

The ESP32 target builds with ESP-IDF (v5.3.x) rather than klipper's
Kconfig/Makefile flow:

```
git clone --depth 1 -b v5.3.2 --recurse-submodules \
    https://github.com/espressif/esp-idf.git
cd esp-idf && ./install.sh esp32 && . export.sh
cd /path/to/klipper/src/esp32
idf.py set-target esp32
idf.py menuconfig       # "Klipper firmware": architecture, WiFi SSID/
                        # password, UDP port, optional build-time PSK,
                        # TRUST_NETWORK
idf.py build flash monitor
```

This builds the (default) component architecture; for the modem
architecture see
[Building the modem architecture](#building-the-modem-architecture)
above.  The port's register drivers compile against the vendored
Apache-2.0 soc headers in `lib/esp32/` (see `lib/esp32/README`), not
against the installed IDF's copies - the same pattern as
`lib/stm32*`'s CMSIS.

The IDF build replicates klipper's `compile_time_request` flow in
CMake (`src/esp32/main/CMakeLists.txt`): the klipper sources are
compiled as a CMake object library, their `.compile_time_request`
sections are extracted with objcopy and fed to
`scripts/buildcommands.py` (`src/esp32/gen_compile_time_request.py`),
which produces both the generated command tables and
`build/main/klipper.dict` - the data dictionary is embedded in the
image and served over the link exactly as on every other target.

## PSK provisioning

The pre-shared key is read, in order of preference, from:

1. **NVS**: namespace `klipper`, key `udp_psk` (blob or string).
   Provision it with IDF's NVS partition tools, e.g. a CSV
   `klipper,namespace,,` / `udp_psk,data,string,<key>` processed by
   `nvs_partition_gen.py` and flashed to the `nvs` partition - the
   key then survives reflashes of the app image.
2. **Build-time Kconfig**: `CONFIG_KLIPPER_PSK` (menuconfig).  A key
   baked into the image is readable by anyone holding the image;
   prefer NVS.

Without a key the firmware refuses to start unless
`CONFIG_KLIPPER_TRUST_NETWORK` is set (the deliberate confession for
isolated lab segments; mirrors the bridge's `--trust-network`).

Generate a key and give the same bytes to the host:

```
python3 -c "import secrets; print(secrets.token_hex(32))" > ~/printer_psk
```

## Connecting klippy

klippy speaks its normal serial protocol; the bridge turns a pty
into authenticated datagrams:

```
python3 lib/intentproto/tools/udp_bridge.py \
    --board <board-ip>:41414 --psk-file ~/printer_psk \
    --pty /tmp/klipper_esp32
```

printer.cfg:

```
[mcu]
serial: /tmp/klipper_esp32
```

## Desktop testing (the linux mcu over UDP)

The full network stack - bridge, HMAC, datagram sequencing, console
glue, frame dispatch - runs on a desktop with zero hardware, using
the same `src/generic/udp_console.c` glue the ESP32 uses:

```
make menuconfig          # select "Linux process"
make
python3 -c "import secrets; print(secrets.token_hex(32))" > /tmp/psk

# terminal 1: the mcu, listening on UDP instead of a pty
./out/klipper.elf -u 45988 -k /tmp/psk

# terminal 2: the host bridge
python3 lib/intentproto/tools/udp_bridge.py \
    --board 127.0.0.1:45988 --psk-file /tmp/psk \
    --pty /tmp/klipper_udp --listen-port 45989

# then point klippy (or scripts/console.py) at /tmp/klipper_udp
```

`-t` instead of `-k` selects the unauthenticated trust-network mode
(the bridge then needs `--trust-network`).  The identify handshake,
dictionary download and normal command traffic all flow through the
authenticated datagram path.

## Status / what remains

Working (verified on the desktop linux-mcu path, which shares all
transport code): authenticated datagram console end-to-end, identify
/ dictionary download, command dispatch, tx batching, auth-failure
rejection, trust-network mode.

Compiled and dictionary-verified (including `spicmds`, `i2ccmds`,
`pwmcmds`, `buttons`, `tmcuart`, `neopixel` and the software SPI/I2C
fallbacks) but, like the rest of the board code, not yet run on
silicon: the SPI, I2C and LEDC bindings.  The **RMT step backend**
(`CONFIG_KLIPPER_RMT_STEP`, `src/esp32/rmt_stepper.c`) is likewise
implemented and host-validated (its pulse-planning logic is
unit-tested off-hardware and the `component-rmt` build variant
compiles/links with the identical command surface) but
unvalidated on silicon - see
[RMT step generation](#rmt-step-generation-implemented---unvalidated-on-silicon)
and its bring-up checklist.

The ESP32 board code compiles and links in **both architectures**
(validated against stub IDF headers + the vendored `lib/esp32`
register headers with the dictionary flow executed for real - 79
commands / 28 responses in each; API names, register fields and the
APP-CPU boot sequence checked against ESP-IDF v5.3.2 sources; the
SPSC ring unit-tested under TSan/ASan), but has **not yet been built
with the xtensa toolchain or run on hardware** - the development
environment could not download the toolchain.  Remaining work, in
rough order:

* First `idf.py build` of both architectures + on-hardware bring-up:
  the component arch first, then the modem arch's
  [devkit checklist](#devkit-bring-up-checklist) (APP-CPU boot,
  vector table, IRAM map, polled-dispatch jitter measurements).
* A level-1 bare-core timer ISR through `appcpu_vectors.S` (replacing
  polled dispatch) once hardware allows comparing the two; then
  reinstating `rmt_step.c` on the bare core.
* Keepalive datagrams during idle (NAT/AP state) and lwIP socket
  reconnect handling.
* Enable datagram erasure FEC once the glue grows in-order block
  reassembly.
* On-silicon bring-up of the RMT step backend (see the
  [RMT step bring-up checklist](#rmt-step-bring-up-checklist)); PCNT
  step verification as a homing-position cross-check; FOC backend
  integration.
* Ethernet (RMII) bringup variant of `wifi.c`.
* Chip reset command, watchdog (component arch; on the bare core a
  register-level TIMG watchdog).
* A native klippy UDP transport (RFC 0001 doc 05) replacing the pty
  bridge.
