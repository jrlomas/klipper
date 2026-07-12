# ESP32 micro-controller (UDP/WiFi network transport)

This document describes the experimental ESP32 target and the UDP
datagram console transport it uses (RFC 0001
[doc 07](rfcs/0001-motion-intentions/07-Link_Transport.md), phase P7).
The same transport is testable on a desktop with no hardware via the
linux micro-controller's UDP option - see
[the test recipe below](#desktop-testing-the-linux-mcu-over-udp),
which is the recommended way to exercise the network stack.

## Architecture

```
klippy (serial protocol, unchanged)
   |          pty
lib/intentproto/tools/udp_bridge.py     (host)
   |          UDP datagrams: [u16 seq][u8 flags][frames][8B HMAC-SHA256]
src/generic/udp_console.c               (mcu, transport independent)
src/generic/udp_datagram.cpp            (C shim over lib/intentproto)
   |          struct udp_console_ops {recv, send, rx_accepted}
src/esp32/udp_port.c  - or -  src/linux/udp.c
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

## Core pinning (RFC 0001 doc 07)

The ESP32 is dual core; the port splits it:

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

## Building

The ESP32 target builds with ESP-IDF (v5.3.x) rather than klipper's
Kconfig/Makefile flow:

```
git clone --depth 1 -b v5.3.2 --recurse-submodules \
    https://github.com/espressif/esp-idf.git
cd esp-idf && ./install.sh esp32 && . export.sh
cd /path/to/klipper/src/esp32
idf.py set-target esp32
idf.py menuconfig       # "Klipper firmware": WiFi SSID/password, UDP
                        # port, optional build-time PSK, TRUST_NETWORK
idf.py build flash monitor
```

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

The ESP32 board code compiles and links (validated against stub IDF
headers with the dictionary flow executed for real; API names and
Kconfig options checked against ESP-IDF v5.3.2 sources), but has
**not yet been built with the xtensa toolchain or run on hardware** -
the development environment could not download the toolchain.
Remaining work, in rough order:

* First `idf.py build` + on-hardware bring-up (timer ISR latency
  measurements, WiFi soak test against udp_bridge.py).
* Keepalive datagrams during idle (NAT/AP state) and lwIP socket
  reconnect handling.
* Enable datagram erasure FEC once the glue grows in-order block
  reassembly.
* RMT/PCNT-based step pulse backend; FOC backend integration.
* Ethernet (RMII) bringup variant of `wifi.c`.
* PWM (LEDC), SPI, I2C, chip reset command, watchdog.
* A native klippy UDP transport (RFC 0001 doc 05) replacing the pty
  bridge.
