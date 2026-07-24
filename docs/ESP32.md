# ESP32 micro-controller (UDP/WiFi network transport)

This document describes the experimental ESP32 target and the UDP
datagram console transport it uses (FD-0001
[doc 07](founding/0001-motion-intentions/07-Link_Transport.md), phase P7).
The same transport is testable on a desktop with no hardware via the
linux micro-controller's UDP option - see
[the test recipe below](#desktop-testing-the-linux-mcu-over-udp),
which is the recommended way to exercise the network stack.

## Architecture

The port offers two selectable architectures (Kconfig, "Klipper
firmware" -> Architecture; FD-0001
[doc 12](founding/0001-motion-intentions/12-ESP32_Architecture.md)):

* **component** (stage 1, default): Helix compiled as an IDF
  component, running as a FreeRTOS task pinned to core 1; IDF is
  present on both cores. Its authenticated session console and continuous
  ADC stream have run on a classic dual-core Lolin32. A Rodent V1.1 has
  additionally driven its I2S-expanded STEP/DIR/ENABLE outputs and TMC2160
  SPI interface on a real V0 Z axis. A print reached ordinary motion before
  a WiFi command-link loss exposed the socket-lifecycle defect described
  below; a complete post-fix print soak remains pending.
* **modem** (stage 3, "IDF as modem"): core 1 runs **bare-metal
  Helix** — no RTOS, no IDF calls, register-level peripherals,
  IRAM-resident hot path — and core 0 is reduced to a network
  coprocessor shuttling sealed console datagrams through a lock-free
  shared-memory ring.  See
  [the modem architecture](#the-modem-architecture-idf-as-modem)
  below.  Its boot and console path are hardware-validated; motion,
  peripheral behavior, and timing remain unqualified.

Both share the identical console stack:

```
klippy (serial protocol, unchanged)
   |          pty
lib/intentproto/tools/udp_bridge.py     (host)
   |          static HMAC datagrams or rotating-key secure session
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
* Authentication (a truncated HMAC-SHA256 — hash-based message
  authentication code — with a pre-shared key, PSK) is
  mandatory; running without it requires the explicit
  "trust network" confession on both ends.  Responses are only ever
  sent to the source address of the last *authenticated* datagram.
* `CONFIG_KLIPPER_DATAGRAM_SESSION` (on by default) additionally offers a
  three-message, PSK-authenticated session with rotating traffic keys,
  replay protection, and a board identity verified by the host.  Once it is
  established, static datagrams cannot bypass it.  The legacy static HMAC
  envelope remains available only for backward-compatible bootstrap.
* The socket itself sits behind a three-function ops struct, so the
  identical glue serves ESP32 WiFi, ESP32 Ethernet (RMII - replace
  the WiFi bringup with `esp_eth`, the binding is unchanged), and the
  linux mcu.
* Datagram-level erasure FEC (forward error correction; XOR parity) is implemented as bounded
  pair blocks (`fec_k=2`): either single loss is reconstructed and a
  later survivor is deferred until it can be released in order. The
  linux MCU exposes it with `-f 2`; ESP32 exposes
  `CONFIG_KLIPPER_FEC_PAIR`.  A Lolin32 component image completed identify
  and stats traffic through a controlled proxy that dropped the first data
  packet of a protected pair, proving recovery over the real WiFi/UDP path.
  FEC remains off by default until link-profile measurements show that its
  50% packet overhead is beneficial; the default recovery path is frame ARQ.

## Core pinning (FD-0001 doc 07) - component architecture

The ESP32 is dual core; the component architecture splits it:

* **Core 0**: WiFi and lwIP tasks (pinned via `sdkconfig.defaults`),
  `app_main` (NVS init, PSK load, WiFi bringup), the UDP receive
  task, the deferred legacy ADC conversion task, and the continuous ADC DMA
  drain task.
* **Core 1**: the Helix scheduler task
  (`xTaskCreatePinnedToCore(..., 1)`) and the Helix hardware timer
  interrupt - the GPTimer callback is registered from the core-1
  task, which allocates its interrupt on core 1.  Motion dispatch
  never contends with the radio stack's interrupts.

The Helix timer is a GPTimer at 20MHz (`CONFIG_CLOCK_FREQ`
20000000 in the hand-written `src/esp32/autoconf.h`): the highest
integer division of the 80MHz APB clock that keeps a long 32-bit
wrap period (~214s), giving 50ns scheduling granularity.

**Caution (from FD-0001 doc 07):** the WiFi stack's interrupt and
flash-cache behavior make *tick-precise* step generation on this
silicon genuinely hard.  Core pinning removes most contention, but
occasional microsecond-level jitter remains (e.g. during flash
writes, when interrupts are briefly deferred).  The classic stepper
backend compiles and runs on this port but should be treated as
**experimental**; the RMT/PCNT pulse peripherals are the likely
escape hatch for production step generation, and the field-oriented
control (FOC) backend
(own timer, tolerant of µs-level ISR jitter) is a better first
citizen of this chip.  This target is FOC-first.

## WiFi command-link policy and diagnostics

The command link is configured for latency and recoverability rather than
maximum radio range:

* Modem sleep is disabled with `esp_wifi_set_ps(WIFI_PS_NONE)` (the ESP-IDF
  equivalent of Arduino's `WiFi.setSleep(false)`). Firmware applies this
  before the initial association, reads it back, and refuses bring-up if the
  effective policy is not `WIFI_PS_NONE`.
* The Rodent low-latency profile disables A-MPDU RX and TX. This follows
  [Espressif's recommendation](https://github.com/espressif/esp-faq/blob/master/docs/en/software-framework/wifi.rst#after-disabling-modem-sleep-the-ping-latency-is-still-high-how-can-it-be-further-optimized)
  when latency remains unstable after modem sleep is disabled, trading bulk
  WiFi throughput for independently completed command datagrams.
  `HELIX_WIFI_STATUS` reports the compiled RX/TX settings and the live
  power-save readback so the experiment does not rely on a setter call or
  build-file assumption.
* The Rodent profile requests a maximum transmit power of 8.5 dBm. The Kconfig
  value is in quarter-dBm units, so
  `CONFIG_KLIPPER_WIFI_MAX_TX_POWER_QDBM=34`. The classic ESP32's supported
  power table quantizes that request to an observed 8.0 dBm. This is
  appropriate for the lab board next to its access point and reduces current
  transients; other installations must qualify their own link budget.
* The UDP receive task runs at FreeRTOS priority 17, immediately below lwIP's
  priority 18, and uses a 15-record producer/consumer queue.
* `WIFI_EVENT_STA_DISCONNECTED` is a socket boundary, not merely a radio
  notification. The firmware marks the network down and closes the UDP
  socket. Only `IP_EVENT_STA_GOT_IP` permits the owner task to create and bind
  a fresh socket. Both the component and IDF-as-modem architectures implement
  this lifecycle.

That final rule closes a concrete recovery defect found while investigating
the 2026-07-23 Rodent print attempt. The ESP32 replied normally and then
stopped answering while the wired host and access point remained available.
The capture cannot prove whether the station disconnected or the board reset.
It does prove that the old firmware would call `esp_wifi_connect()` after a
disconnect while retaining the socket ESP-IDF had invalidated, so a successful
re-association could not restore Helix traffic. The revised firmware also
counts WiFi disconnect reasons, socket opens, receive-ring drops, and socket
errors so a future radio event, local queue overflow, and MCU reset are
distinguishable rather than all appearing as `Lost communication`.

With `[helix_self_test]` loaded, query a component-architecture ESP32 with:

```
HELIX_WIFI_STATUS MCU=rodent
```

The response includes association/IP state, RSSI, configured transmit power,
effective power-save mode, compiled A-MPDU RX/TX state, disconnect count and
last reason, ESP reset reason, socket reopen count, datagrams
received/transmitted, receive-ring drops, and socket errors. After a future
communication pause, re-establish the host session without first issuing an
MCU reset and capture this status; otherwise a deliberate software
reset can overwrite the reset cause that would distinguish a brownout.

### Rodent A-MPDU latency experiment (2026-07-23)

The controlled A/B used the same Rodent, access point, wired host, 8.5 dBm
requested transmit-power profile, disabled modem sleep, and active Klipper
session. Only A-MPDU RX/TX changed. The enabled baseline exhibited second-long
service freezes:

| ICMP workload | A-MPDU RX/TX | Loss | Mean RTT | Maximum RTT |
| --- | --- | ---: | ---: | ---: |
| 100 packets at 20 pps | enabled | 1% | 211.8 ms | 1202 ms |
| 100 packets at 20 pps | disabled | 0% | 4.309 ms | 29.960 ms |
| 50 packets at 5 pps | enabled | 2% | 155 ms | 1223 ms |
| 50 packets at 5 pps | disabled | 0% | 4.209 ms | 11.435 ms |
| 1000 packets at 20 pps | disabled | 0.1% | 3.742 ms | 29.001 ms |

The flashed firmware reported `power_save=none(valid=1)`, `ampdu_rx=0`,
`ampdu_tx=0`, RSSI -46 dBm, and no WiFi disconnect, UDP socket, receive-ring,
or invalid-byte errors. Rodent remained machine-time converged through the
extended run. Its host transport showed a 5 ms SRTT, 25 ms RTO, and only 49
additional retransmitted bytes during the extended measurement, rather than
the earlier multi-kilobyte growth and 1.57-second RTO.

This is strong causal evidence that aggregation was the dominant source of
the observed one-second latency tail on this AP/ESP32 combination. It does not
yet qualify print-length reliability: retain the no-A-MPDU profile and repeat
a complete supervised print while checking the same counters.

For a secondary network MCU that may recover, configure:

```
[mcu rodent]
on_comm_timeout: pause
```

Without this option the historical Klipper default is a printer shutdown.
The pause policy stops virtual-SD ingestion and enters Helix's coordinated
hold/reconciliation path; it does not promise that a disconnected actuator
can continue moving without communication.

## The modem architecture (IDF as modem)

> **BOOT AND CONSOLE VALIDATED ON HARDWARE; MOTION/PERIPHERALS PENDING.** The
> component, component-RMT, and modem images compile and link with
> pinned ESP-IDF v5.3.2 and its `xtensa-esp-elf` 13.2.0 toolchain.
> The modem map confirms the private vectors and selected motion-hot
> objects are in IRAM.  The SPSC ring is also unit-tested on the
> desktop under ThreadSanitizer/AddressSanitizer, and every register
> write and boot step is source-verified against that IDF release.
> On 2026-07-13 a dual-core Lolin32 booted the bare APP CPU, ran
> `sched_main`, loaded the 112-command dictionary through the shared ring
> using both static authentication and the rotating-key session, emitted
> repeated MCU stats, and established a fresh session after a host-bridge
> restart.  This exercises the polled timer enough to operate the console;
> it does not measure dispatch jitter or qualify GPIO, motion, heaters,
> modem-mode ADC acquisition, SPI, I2C, LEDC, watchdog fault injection, or
> RMT/PCNT/FOC behavior. Component-mode continuous ADC has a separate live
> result below.

The stage-3 architecture of doc 12: IDF, FreeRTOS and the closed
radio blobs are confined to core 0, which becomes an on-die network
coprocessor - no different in kind from the closed firmware inside a
W5500.  Core 1 runs Helix the way an STM32 does: bare metal,
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

HMAC verification runs on the **Helix core**, not the radio core:
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

The component architecture's separate UDP receive ring follows the same
rule explicitly: the producer release-stores a completed slot index, the
Helix consumer acquire-loads it, and the consumer release-stores the freed
slot. No `volatile` field is treated as a cross-core memory barrier.

Wakeups are polled, not signalled: core 1 checks the rx ring in its
`irq_poll()`; the modem task alternates a 1ms-timeout `recvfrom`
with a tx-ring drain.  Board->host latency is bounded by ~1ms + the
console's own 2ms batching; the intention-queue design center makes
both irrelevant to motion.

### The polled bare runtime

Core 1 enables no interrupts at all (like the Helix linux mcu,
which dispatches timers from `irq_poll()` in the sched loop and
serves production printers that way).  The Helix timer is TIMG0
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

* **ADC**: both IDF drivers remain on core 0. Legacy ADC1 requests use a
  lock-free seq/ack mailbox. The continuous stream uses IDF's I2S0-backed DMA
  pool, software boxcar averaging, and an ownership-checked cross-core block
  mailbox drained by core 1's `irq_poll()`. The two paths are explicitly
  exclusive because IDF cannot give both drivers ADC1 simultaneously.
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

IRAM-resident set in the ESP-IDF v5.3.2 modem image, measured from the
Xtensa link map on 2026-07-13:

| object | why | IRAM code |
| ------ | --- | ----- |
| sched.c | timer list + task loop | 861B |
| generic/timer_irq.c | `timer_dispatch_many` | 165B |
| stepper.c | step event handlers | 1,124B |
| trajq.c + traj_stepper.c | trajectory execute path | 2,886B |
| esp32/gpio.c | `out_w1ts/w1tc` hot writes | 1,253B |
| esp32/appcpu_boot.c | poll loop, bare timer | 1,036B |
| esp32/shmem_console.c | ring ops on dispatch path | 596B |
| appcpu_vectors.S | vector table + entry | 1,175B |

The selected set occupies 9,096B.  The whole image uses 97,886B of
the 128KiB IRAM region (`.iram0.vectors` + `.iram0.text`), leaving
33,186B.  The same map confirms the selected OBJECT-library members
land at IRAM addresses, resolving the earlier ldgen placement
question.  Task-level code (command parsing, config,
sensors) deliberately stays in flash: IRAM is the scarce resource,
and a miss there costs dispatch latency of the jitter class already
accepted on this chip.  The hardware console run exercised both IRAM and
flash-resident task code after the APP cache/MMU setup; motion and peripheral
qualification must still check timing and cache behavior under their actual
loads.

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
   architecture, join WiFi, run the session bridge + `klippy/console.py`
   identify handshake.  On 2026-07-13 this passed on a Lolin32 over a
   wired-host/WiFi-board LAN path: the board identity was verified, the
   112-command dictionary loaded, and periodic `stats` keep-alives remained
   continuous during a non-motion soak.  This proves the component console,
   credentials, PSK, and session path; it does not qualify motion hardware.
2. **Modem build boots core 0**: flash the modem build; expect
   `klipper_modem: modem shuttling datagrams on udp port ...` then
   `klipper_appcpu: core 1 running bare klipper (after ~Xms)` on the
   monitor.  This passed on the Lolin32 on 2026-07-13.  If startup instead
   parks, `esp32_core1_fault[]` reports the exception cause, EPC, address,
   vector offset, PS, window base/start, and exception-entry `a0`; `0x1nn`
   causes are synthetic interrupt-vector markers and architectural exception
   causes otherwise follow the Xtensa definitions.
3. **Console end-to-end**: identify handshake + dictionary download
   through the ring path; confirm auth-failure rejection (wrong PSK)
   still drops silently; confirm responses only go to the
   authenticated peer (send from a second source).  Static-HMAC and secure
   session identify, repeated stats, and a fresh session after bridge restart
   passed on the Lolin32; the adversarial second-source hardware check remains.
4. **IRAM placement**: `idf.py size-components`, then check the map
   file against the workstation baseline above and confirm runtime
   behavior does not reveal an unlisted flash dependency.
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

Beyond the initial timer/GPIO/ADC set, the port implements the founding document
0001 [doc 12](founding/0001-motion-intentions/12-ESP32_Architecture.md)
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
| ADC stream (ADC1/I2S0 DMA) | `config_adc_stream` / `adc_stream_*` | IDF continuous driver on core 0; bounded cross-core blocks; component and modem builds |
| SPI (`spi2`/HSPI, `spi3`/VSPI) | `spicmds` + software SPI | register level (polled, W0..W15 buffer) |
| I2C (`i2c0`) | `i2ccmds` + software I2C | register level (command-list engine) |
| Hard PWM (LEDC high-speed) | `pwmcmds` | IDF `ledc` config; register duty updates |
| UART bit-bang | `tmcuart` | generic (gpio + timers) |
| Neopixel | `neopixel` | generic bit-bang - timing is subject to the jitter caution above; verify on hardware |
| RMT step module | `config_stepper`/`queue_step`/... (when `CONFIG_KLIPPER_RMT_STEP`) | register level, see below |
| Rodent V1.x output chain | `I2SO0`..`I2SO15` (when `CONFIG_KLIPPER_I2S_SHIFT`) | continuous I2S0 FIFO output on the board's I2S-labelled pins; see below |

Details and constraints:

* **Continuous ADC** (`src/esp32/adc_stream.c`): ADC1 only, one physical
  stream, one to four ascending channels, and no coexistence with legacy ADC1
  inputs in this first implementation. An 8 KiB non-overwriting IDF pool
  absorbs bounded core-1 scheduling delays; pool exhaustion has its own status
  flag and stops the stream. Rates below IDF's 20 kconversion/s minimum use
  software boxcar averaging. IDF line-fitting calibration is created per
  channel when supported and publishes its scheme, reference voltage, and
  raw-to-millivolt metadata. The shared HELIX DMA arena uses `DMA_ATTR` and an
  `esp_ptr_dma_capable()` allocation guard; the current component map places it
  at internal-DRAM address `0x3ffb2800`, not flash DROM or PSRAM. Start phase
  is explicitly inferred. On
  2026-07-17 a Lolin32 component image delivered 47,072 GPIO32 scans at
  1 kscan/s in 2,942 consecutive 16-value blocks over WiFi/UDP with zero drops
  and zero status faults, then stopped cleanly. Modem mode compiles and links
  but has not yet received the equivalent live acquisition soak.

* **SPI** (`src/esp32/spi.c`): synchronous polled transfers, ISR
  tolerant (klipper's `spidev_shutdown` can run from the shutdown
  path, where the IDF driver's mutexes are unusable).  Bus `spi2` is
  MISO/MOSI/CLK = GPIO12/13/14, `spi3` is GPIO19/23/18, routed
  through the GPIO matrix; modes 0-3; the divider realizes
  80MHz/(2*pre) without exceeding the requested rate, capped at
  20MHz for matrix-routed MISO timing.  CS is a plain Helix gpio.
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
  share).  `cycle_time` (20MHz Helix ticks) maps to an LEDC
  frequency of `20MHz/cycle_ticks` with duty resolution chosen as
  the largest realizable `res <= 15` bits (`~log2(80MHz*cycle/20MHz)`);
  higher PWM frequency costs resolution (20kHz -> 12 bits, 1MHz -> 6
  bits).  Duty writes from timer dispatch are two register writes
  (duty + duty_start latch); `PWM_MAX` is 32768.

### BIGTREETECH Rodent V1.x shift-register outputs

Rodent V1.x routes all four TMC2160 STEP/DIR/ENABLE groups through two
serially chained output registers. The electrical pins are DATA=GPIO21,
BCK=GPIO22, and WS/latch=GPIO17; the TMC register bus remains ordinary
SPI3 on GPIO19/23/18. A Rodent build enables `KLIPPER_I2S_SHIFT` and exposes
the schematic names `I2SO0` through `I2SO15`.

The first Helix implementation wrote a sparse output snapshot by stopping,
resetting, draining, and restarting I2S0. That worked at low manual rates but
made a 20 mm/s, 1,000-microstep/mm Z retract perform 40,000 complete peripheral
resets per second, starving trajectory refill. The replacement uses the same
hardware-serializer principle as FluidNC: I2S0 continuously emits one complete
output word every 2 us, and a level-three FIFO interrupt fills eight future
words at a time. The trajectory solver advances against each future sample
clock, so WiFi and RTOS interrupt latency cannot move an edge already committed
to the peripheral FIFO. SPI3 remains available to the TMC chain.

FluidNC's implementation history matters to this design. Its older
`I2SOut.cpp` used five circular DMA buffers of about 2 ms each. That insulated
the serializer from software latency, but also placed roughly 10 ms of already
committed motion between an endstop and the physical output. FluidNC replaced
that engine with the current no-jitter FIFO implementation in commit
`e6e00db1` (October 2024). The current ISR does not plan motion: a foreground
segment pipeline has already reduced motion to `n_step`, `isrPeriod`, and
Bresenham/AMASS state, so each FIFO callback performs only a cheap step tick.

Helix initially copied the FIFO dimensions without accounting for this
execution difference:

| Property | Current FluidNC | Initial Helix adaptation | Corrected Helix |
| --- | --- | --- | --- |
| Refill work | precomputed Bresenham tick | live quintic crossing execution | live quintic crossing execution |
| Refill threshold | 16 samples | 16 samples | 48 samples |
| Time remaining at interrupt | 32 us | 32 us | 96 us |
| Eight-sample refill lead | 32-48 us | 32-48 us | 96-112 us |
| Measured worst refill on Rodent | not measured here | 56.5 us observed | 66.4 us observed, within budget |

Thus the recurring slow, pulsed homing was a serializer underrun risk, not a
TMC2160 chopper-mode effect. Helix now asks for service with 48 of 64 samples
remaining and reloads eight, giving its solver a 23,040-cycle/96 us budget at
240 MHz. `HELIX_OUTPUT_STATUS` exposes the threshold, reload, cycle budget, and
deadline-miss count so a heavier configuration cannot silently inherit this
qualification. The additional 64 us of worst-case committed output corresponds
to only 1.28 um at the tested 20 mm/s homing speed.

`HELIX_OUTPUT_STATUS MCU=<name>` reports the live output word, transfer count,
wire bitrate, and average/worst CPU-cycle cost. On the Rodent V1.1 lab board,
the 20 MHz transfer measured 354 cycles average and 394 cycles worst at a
240 MHz CPU. A 2 mm G1 at 2 mm/s produced exactly 4,002 runtime writes for
2,000 microsteps (rise plus fall), ended at the matching host/firmware
position, and reported zero trajectory drops and zero TMC lost steps. The
TMC2160 `MSCNT` also advanced by the exact expected modulo-1024 count, proving
that the driver received the edge stream.

The initially weak motor had a separate current-scaling cause. Early Rodent
schematics and example configurations incorrectly specified
`r_sense_ohms: 0.022`; the correct Klipper setting is
`sense_resistor: 0.075`. The obsolete value underdrives the motor by about
3.4x: a requested 0.6 A produces only about 0.18 A. A temporary 1.5 A request
with the bad value produced roughly 0.44 A and immediately restored torque.
Correcting the value restored torque, but did not by itself resolve the later
progressive homing slowdown; that was the FIFO execution-budget defect above.

`HELIX_OUTPUT_STATUS MCU=rodent STEP_BIT=2 DIR_BIT=1` resets and enables that
instrumentation for Rodent's X socket. A later query without the bit arguments
reports minimum/average/maximum rise intervals and high widths in 20 MHz MCU
timer ticks, plus direction changes observed at rising STEP edges, raw
trajectory toggles, active serialized-stepper registry size, and deadline
misses.

On 2026-07-23, the corrected image completed three consecutive full `G28 Z`
cycles. Each ended homed at the expected post-home position with no recovery
hold. The three runs emitted 169,789, 176,004, and 90,998 STEP rises; raw
toggles were exactly twice the rise count, every recorded high pulse was
80 timer ticks (4 us), and there were zero deadline misses. The measured
maximum refill costs were 12,598, 15,040, and 15,940 CPU cycles
(52.5, 62.7, and 66.4 us), all below the 23,040-cycle budget.

This is a one-active-trajectory-axis hardware qualification. Refill work grows
with the number of simultaneously active I2S trajectory steppers, so a
multi-axis Rodent configuration must repeat the cycle-budget and physical
motion gate. A nonzero deadline-miss count is a qualification failure; it must
not be hidden by making the FIFO lead arbitrarily deep because stop latency is
also part of the contract.

The serializer itself occupies about 1.5 us. Configure a 4 us step pulse on
this backend so the timer dispatcher has margin between the rising snapshot
and its falling-edge deadline:

```
[stepper_z]
step_pin: rodent:I2SO2
dir_pin: rodent:I2SO1
enable_pin: !rodent:I2SO0
step_pulse_duration: 0.000004

[tmc5160 stepper_z]
cs_pin: rodent:GPIO5
spi_bus: spi3
chain_length: 4
chain_position: 1
run_current: 0.6
# Do not copy the obsolete 0.022 value from early Rodent material.
sense_resistor: 0.075
# Optional: keep GLOBALSCALER in the TMC2160 recommended analog range.
globalscaler_min: 128
```

Runtime kinematics replacements must follow the same fitter binding as normal
motion. Helix therefore rebinds its trajectory fitter when `FORCE_MOVE` or an
input-shaper wrapper replaces a stepper's kinematics; otherwise the command
would enable the driver but silently scan the previous trapq.

## RMT step generation (implemented - unvalidated on silicon)

> **IMPLEMENTED, NOT YET RUN ON HARDWARE.**  The backend below
> compiles and links in a real ESP-IDF v5.3.2 `component-rmt` image
> with `CONFIG_KLIPPER_RMT_STEP`, its
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

The ESP32 target builds with ESP-IDF (v5.3.x) rather than Helix's
Kconfig/Makefile flow:

```
git clone --depth 1 -b v5.3.2 --recurse-submodules \
    https://github.com/espressif/esp-idf.git
cd esp-idf && ./install.sh esp32 && . export.sh
cd /path/to/klipper/src/esp32
idf.py set-target esp32
idf.py menuconfig       # "Klipper firmware": architecture, WiFi SSID/
                        # password, UDP port, optional build-time PSK,
                        # session identity, TRUST_NETWORK
idf.py build flash monitor
```

Set the configured flash size to the physical board geometry before flashing
(the verified Lolin32 uses 4MB).  Temporary test credentials must be supplied
through an isolated SDKCONFIG file or `menuconfig`; an existing local
`sdkconfig` takes precedence over `SDKCONFIG_DEFAULTS` values.

The default partition table is deliberately `TWO_OTA`: factory plus two 1 MiB
OTA slots on a 4 MiB part. Do not replace it with `SINGLE_APP_LARGE`. A
single-app image still compiles the Helix flash commands, but
`esp_ota_get_next_update_partition()` has no inactive target and
`flash_begin` correctly fails. A board previously flashed with a single-app
table needs one final ROM-serial migration that writes the bootloader,
partition table, blank OTA-data page, and factory app. Its NVS partition can
be preserved, after which normal application updates use the authenticated
in-band A/B path.

**Physical qualification note (2026-07-23):** the Rodent V1.1 successfully
accepted and hash-verified a complete ROM-serial write of the bootloader,
two-OTA partition table, OTA metadata, and 837,984-byte application, then
booted version `68c52227`. The same image did **not** complete an in-band
update: `flash_begin` entered `esp_ota_begin()` but produced no
`flash_result` within 90 seconds while core 0 remained pingable. Therefore
the ESP32 in-band A/B implementation is not hardware-qualified and must not
yet be used as the sole field-update path. The likely core-affinity/flash-IPC
deadlock requires its own correction and interrupted-update test; USB-UART
ROM flashing remains the verified recovery path.

This builds the (default) component architecture; for the modem
architecture see
[Building the modem architecture](#building-the-modem-architecture)
above.  For the RMT component variant, replace the `set-target` line
with:

```
idf.py -DSDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.rmt" \
    set-target esp32
```

The port's register drivers compile against the vendored
Apache-2.0 soc headers in `lib/esp32/` (see `lib/esp32/README`), not
against the installed IDF's copies - the same pattern as
`lib/stm32*`'s CMSIS.

The IDF build replicates Helix's `compile_time_request` flow in
CMake (`src/esp32/main/CMakeLists.txt`): the Helix sources are
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

klippy speaks its normal serial protocol; the recommended bridge turns a pty
into a rotating-key authenticated session and verifies the board identity:

```
<klippy-python> lib/intentproto/tools/udp_bridge.py --session \
    --board-id <configured-session-id> --board <board-ip>:41414 \
    --psk-file ~/printer_psk \
    --pty /tmp/klipper_esp32
```

`<klippy-python>` is the Python environment that runs Klippy; it supplies the
project's required CFFI binding.  Omit `--session --board-id` only when
connecting to an older static-HMAC-only board.

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

# then point klippy (or klippy/console.py) at /tmp/klipper_udp
```

`-t` instead of `-k` selects the unauthenticated trust-network mode
(the bridge then needs `--trust-network`).  The identify handshake,
dictionary download and normal command traffic all flow through the
authenticated datagram path.

## Status / what remains

Working in desktop linux-mcu verification and on the Lolin32 component and
modem console paths: authenticated datagram transport, identify/dictionary
download, command dispatch, tx batching, rotating-key sessions, session
re-handshake, and periodic stats.  Desktop tests additionally cover explicit
auth-failure rejection and trust-network mode.

Xtensa-compiled and dictionary-verified (including `spicmds`, `i2ccmds`,
`pwmcmds`, `buttons`, `tmcuart`, `neopixel` and the software SPI/I2C
fallbacks) but, like the rest of the board code, not yet run on
silicon: the SPI, I2C and LEDC bindings.  The **RMT step backend**
(`CONFIG_KLIPPER_RMT_STEP`, `src/esp32/rmt_stepper.c`) is likewise
implemented and workstation-validated (its pulse-planning logic is
unit-tested off-hardware and the real Xtensa `component-rmt` build
compiles/links with the identical command surface) but
unvalidated on silicon - see
[RMT step generation](#rmt-step-generation-implemented---unvalidated-on-silicon)
and its bring-up checklist.

The ESP32 board code compiles and links in all three maintained build
variants with pinned ESP-IDF v5.3.2 and `xtensa-esp-elf` 13.2.0:

| variant | configuration | application image | partition free |
| --- | --- | ---: | ---: |
| component | default, ADC stream enabled | `0xe49e0` | 39% |
| component-RMT | `sdkconfig.defaults.rmt`, ADC stream enabled | `0xd0150` | 45% |
| modem | `sdkconfig.defaults.modem`, ADC stream enabled | `0xcb510` | 46% |

The first real builds exposed and fixed two issues that the stub path
missed: disabled Kconfig booleans are absent from `sdkconfig.h`, and
the private vector assembly needed the configured `EXCSAVE_1` special-
register definition.  The first modem hardware run additionally exposed a
masked APP flash-cache bus, the need for the canonical initial window-stack
save area, and ROM `setjmp`'s architectural syscall-0 window spill; all three
are now handled and the dictionary flow executes on the bare core.
The APP-CPU boot sequence remains checked against ESP-IDF v5.3.2
sources, and the SPSC ring remains TSan/ASan-tested. Fresh generated
configurations confirm `CONFIG_ESP_TASK_WDT_PANIC=y` in every variant.

The component Helix task subscribes to ESP-IDF's task watchdog; a missed
feed panics and reboots. The modem Helix core owns Timer Group 1's MWDT
directly with a 500ms system-reset stage. Its `reset` command release-publishes
a request to core 0 for `esp_restart()`, while the MWDT remains the bounded
fallback if core 0 is wedged. Component mode calls `esp_restart()` directly.

WiFi disconnect handling requests reconnect without blocking IDF's event
task. Ordinary Helix clock queries continue at about 1Hz while motion is
idle, so they already keep the UDP/session path active; a separate semantic
keepalive packet would duplicate that traffic. The socket stays bound across
station reassociation, and the rebooting watchdog covers a wedged task.

The component and modem console images have run on hardware. Remaining work,
in rough order:

* Complete the modem [devkit checklist](#devkit-bring-up-checklist): scope
  timer/step dispatch jitter, exercise the expanded fault record, and measure
  the register-level peripheral paths. APP-CPU boot, private vectors,
  flash-cache access, shared-ring console, and both authentication modes pass.
* A level-1 bare-core timer ISR through `appcpu_vectors.S` (replacing
  polled dispatch) once hardware allows comparing the two; then
  reinstating `rmt_step.c` on the bare core.
* Measure whether the optional `fec_k=2` path's 50% packet overhead helps the
  target WiFi loss profile. Controlled first-packet loss recovery already
  passes on the Lolin32; this remaining item is a deployment tradeoff study.
* On-silicon bring-up of the RMT step backend (see the
  [RMT step bring-up checklist](#rmt-step-bring-up-checklist)); PCNT
  step verification as a homing-position cross-check; FOC backend
  integration.
* Ethernet (RMII) bringup variant of `wifi.c`.
* A native klippy UDP transport (FD-0001 doc 05) replacing the pty bridge is
  an optional host-path simplification; the tested bridge is the supported
  correctness path today.
