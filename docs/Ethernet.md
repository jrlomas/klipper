# Ethernet datagram console transport

This document describes wired-Ethernet transports for the FD-0001
datagram console (see
[doc 07 - Link Layer and Transports](founding/0001-motion-intentions/07-Link_Transport.md)).
Two paths are provided:

* the **W5500** - a WIZnet SPI Ethernet controller that runs the
  IP/UDP/ARP stack in silicon, usable on any board with an SPI bus;
* the **native RMII MAC** - the Ethernet MAC built into supported STM32F4/F7
  parts, driving an external RMII PHY, with a software IP layer above
  it.

!!! warning "Compile-checked, not hardware-validated here"
    The W5500 and native paths **build and link with the real ARM toolchain**;
    native RMII is covered on STM32F407, STM32F765, and the F767 reference
    configuration, and its framing and
    stateful socket adapter pass host tests. Neither Ethernet transport has
    been run against a
    physical PHY in this project. Register, DMA, clock, and pin behavior
    therefore remain a board-bring-up item, not validated firmware.

The adopted native-Ethernet reference-board plan is
[FD-0001 doc 16 - STM32F767 Ethernet Reference Board Plan](founding/0001-motion-intentions/16-STM32F767_Ethernet.md).
It covers the NUCLEO-F767ZI clock bypass and RMII routing, an MPU-managed DMA
arena, interrupt-driven rings, IEEE 1588 timestamps, runtime provisioning,
DHCP, and the shared STM32 ADC-DMA primitive.

The shared DMA ownership and completed-block substrate is defined separately
in [FD-0001 doc 17 - Unified DMA and ADC Acquisition](founding/0001-motion-intentions/17-DMA_ADC_Acquisition.md),
so Ethernet does not grow a second, incompatible DMA framework.

## Same binding, different loss model

Nothing about the protocol changes on Ethernet. FD-0001 doc 07 is
explicit: *"The same UDP binding runs unchanged over Ethernet."* These
transports are exactly that - a source of `struct udp_console_ops`
(`recv` / `send` / `rx_accepted`) for the target-independent datagram
glue in `src/generic/udp_console.c`, no different in kind from
`src/linux/udp.c` (a POSIX socket) or `src/esp32/udp_port.c` (an lwIP
socket). The HMAC-authenticated intentproto datagram
(`src/generic/udp_datagram.h`) rides inside the UDP payload byte-for-byte
as it does over WiFi.

The one honest difference is the loss model. Over WiFi the dominant
impairment is whole-datagram loss, which is why the erasure-FEC (forward
error correction) layer exists. A switched full-duplex Ethernet link has a clean loss model
(no radio jitter, per-port bandwidth, deterministic sub-millisecond
latency), so **the erasure-FEC layer can typically be negotiated off on
Ethernet** (`fec_k = 0`, the default) while everything else - datagram
sequencing, traffic-class mapping, authentication - is identical. FEC
remains available if a particular plant wants it.

## Authentication (PSK / trust)

Authentication is mandatory on network transports (FD-0001 doc 07).
Every datagram carries a truncated HMAC keyed by a pre-shared key. The
key is provisioned at build time, mirroring the ESP32 port:

* `CONFIG_W5500_PSK` or `CONFIG_RMII_PSK` - the pre-shared key string. A host connects with
  the matching key via `lib/intentproto/tools/udp_bridge.py`.
* `CONFIG_W5500_TRUST_NETWORK` or `CONFIG_RMII_TRUST_NETWORK` - the explicit "unauthenticated"
  confession, for an isolated bench VLAN only.

A datagram source is only latched as the reply peer **after** it passes
authentication (`ops.rx_accepted`), so an unauthenticated packet can
never steal the link.

## W5500 (SPI Ethernet) - primary path

The W5500 is a hardwired TCP/IP controller: the IP/UDP/ARP stack lives
in the chip, reached over SPI, so no software IP stack is needed on the
MCU. The driver (`src/generic/w5500.c`) opens socket 0 in UDP mode
(`Sn_MR = UDP`), binds the listen port with `Sn_CR = OPEN`, and moves
whole datagrams with the RX/TX ring pointers (`Sn_RX_RD` / `Sn_TX_WR`)
and `Sn_CR` `RECV` / `SEND`. A received UDP datagram is prefixed in the
socket RX buffer by an 8-byte packet-info header (source IP, source
port, length) which the driver parses to latch the peer.

Every reset/socket-command wait is bounded. The receive header is checked
against both the actual RX count and the 2KiB socket buffer before copying,
and unstable hardware counters cannot spin forever. A 250ms health check
detects a reset/closed socket; reinitialization retries once per second and
clears the authenticated reply peer, so no pre-reset address is trusted after
recovery.

### Wiring

Any SPI bus plus one GPIO chip-select and (optionally) an interrupt
line. The driver polls the received-size register, so an interrupt pin
is not required.

| W5500 | MCU |
| --- | --- |
| SCLK / MOSI / MISO | the chosen SPI bus pins |
| SCSn | the chip-select GPIO (`CONFIG_W5500_CS_PIN`) |
| RSTn | tie to the board reset or a spare GPIO |
| INTn | optional, unused by the polled driver |

### Build configuration

Enable the feature under **Optional features** (it is off by default and
never defaulted on for limited-code-size boards):

```
CONFIG_WANT_ETHERNET_W5500=y     # depends on WANT_SPI
```

To make the W5500 the board's **console** (STM32: "Communication
interface" -> *Ethernet datagram console via W5500 (SPI)*), the link is
brought up at startup from the static configuration:

```
CONFIG_CONSOLE_W5500=y
CONFIG_W5500_SPI_BUS=0           # index into the port's spi_bus table
CONFIG_W5500_CS_PIN=4            # encoded (port-'A')*16 + num; PA4 = 4
CONFIG_W5500_UDP_PORT=1234
CONFIG_W5500_IP=0xC0A800FE       # 192.168.0.254
CONFIG_W5500_NETMASK=0xFFFFFF00
CONFIG_W5500_GATEWAY=0xC0A80001
CONFIG_W5500_PSK="your-pre-shared-key"
```

The host then reaches the MCU exactly as for the ESP32 UDP console -
through `udp_bridge.py`, which presents a local pseudo-serial device to
`klippy`.

### `config_w5500` runtime command

When the W5500 is used as a **secondary** transport (the board keeps a
USB/serial bootstrap console) the link can instead be brought up at
runtime by the standard command surface:

```
config_w5500 spi_bus=<n> cs_pin=<pin> ip=<u32> netmask=<u32> \
    gateway=<u32> port=<u16>
```

Network parameters come from the host (matching the `config_spi`
idiom - host-encoded pin numbers); the PSK/trust choice stays build
time. Note the bootstrap subtlety: a console **cannot** carry the
command that configures the very transport carrying the console, which
is why the console path (`CONFIG_CONSOLE_W5500`) uses build-time static
configuration instead.

## Native RMII MAC

Supported STM32F4/F7 parts have a built-in Ethernet MAC that needs an
external RMII PHY **and a software IP/UDP stack** above it (the MAC delivers
only raw Ethernet frames). The implementation stays deliberately small and
is split at one replaceable seam:

```
   rx frame  --> nano_udp_input()      (or lwIP: ethernet_input)
   tx frame  <-- eth_mac_emit()        (or lwIP: low_level_output)
```

* `src/stm32/eth_mac.c` is the **MAC/DMA half and console binding**:
  configurable AF11 pins and PHY reset, an HCLK-correct bounded MDIO path,
  PHY identity checks, IEEE 10/100 auto-negotiation and reconnect polling,
  DMA descriptor rings allocated from the shared non-cacheable arena,
  IRQ-to-`acq_ring` RX publication with bounded overrun accounting, a
  UID-derived local MAC,
  and the standard `console_sendf()` / `console_receive_buffer()` hooks.
* `src/generic/nano_udp.c` is the **pluggable IP layer**: a minimal
  single-socket UDP/IP/ARP responder (ARP replies so a host can find
  us, validated IPv4 and UDP checksums and lengths, fragment rejection,
  destination filtering, and UDP demultiplexing to the console. Its
  one-slot receive queue commits the candidate return address atomically
  with the datagram, so a dropped packet cannot redirect an authenticated
  reply. Swapping in lwIP remains possible by replacing the two seam calls.

The small IP layer intentionally has no DHCP, gateway, VLAN, ICMP, TCP, or
fragment reassembly. Configure a static address and place the host bridge on
the same layer-2 subnet. A broader network stack is outside this deterministic
single-socket console's scope.

`eth_mac_get_status` reports link/ready state, RX/TX frames, RX-ring overruns,
fatal DMA errors, ready-queue high-water, and shared pool size/use. ETH MAC and
ETH DMA are exclusive `dma_resource` claims; descriptors and payload buffers
are no longer a second static DMA arena. On F7 the same MPU policy therefore
covers Ethernet and ADC cache coherency.

### Build configuration

Select **Communication interface -> Ethernet datagram console via native
RMII MAC** on STM32F407, STM32F429, or a supported STM32F7 target. This is a
console choice, so USB, UART, and CAN console implementations are not linked
beside it. The generated configuration contains:

```
CONFIG_STM32_ETHERNET_RMII=y
CONFIG_RMII_PSK="your-pre-shared-key"
CONFIG_RMII_IP=0xC0A800FE
CONFIG_RMII_UDP_PORT=1234
CONFIG_RMII_PHY_ADDR=0
CONFIG_RMII_PHY_RESET_PIN=-1     # or an encoded GPIO number
CONFIG_RMII_REF_CLK_PIN=1        # PA1; pins use (port-'A')*16 + number
CONFIG_RMII_MDIO_PIN=2           # PA2
CONFIG_RMII_MDC_PIN=33           # PC1
CONFIG_RMII_CRS_DV_PIN=7         # PA7
CONFIG_RMII_RXD0_PIN=36          # PC4
CONFIG_RMII_RXD1_PIN=37          # PC5
CONFIG_RMII_TX_EN_PIN=27         # PB11
CONFIG_RMII_TXD0_PIN=28          # PB12
CONFIG_RMII_TXD1_PIN=29          # PB13
```

The pin defaults are the common AF11 mapping, not a promise that a particular
board routes them to its PHY. Confirm the schematic, PHY strap address,
50MHz reference-clock direction, reset polarity, and power/reset timing
before flashing. An empty PSK fails closed unless
`CONFIG_RMII_TRUST_NETWORK=y` explicitly confesses an isolated trusted link.
`CONFIG_RMII_FEC_PAIR=y` enables pair parity, although switched Ethernet
normally should leave it off.

### Workstation evidence

Persistent CI configurations cover both native-RMII families, the F767
reference image, and the W5500 console. With `arm-none-eabi-gcc` 13.2.1 the
earlier isolated transports compile as follows:

| configuration | session mode | text | data | bss |
| --- | --- | ---: | ---: | ---: |
| `stm32f407-w5500.config` | authenticated | 65,138 | 64 | 16,880 |
| `stm32f407-rmii.config` | authenticated | 65,622 | 64 | 27,616 |
| `stm32f765-rmii.config` | authenticated + pair FEC | 63,230 | 64 | 27,632 |

These configurations are included automatically by `scripts/ci-build.sh`.

The combined `stm32f767-nucleo-ethernet-adc.config` image has text 86,586,
data 64, and BSS 36,920 bytes. Its single 16 KiB `.dma_buffer` is map-verified
at non-cacheable SRAM `0x20020000`; Ethernet and ADC allocate from that region.
This is build/map and ownership evidence. Frame traffic concurrent with ADC on
the physical NUCLEO/PHY remains a live hardware gate.
They establish configuration, compiler, linker, and flash/RAM-fit evidence;
they do not establish electrical behavior or packet flow on a real PHY.

### Testing nano-UDP on the host

```
test/nano_udp/run.sh
```

This runs the pure framing vectors plus the stateful socket-adapter test. The
suite checks Internet, IPv4, and UDP checksums; ARP/UDP framing; malformed
length and fragment rejection; destination filtering; and candidate-peer
stability while the receive slot is occupied.

## Files

| File | Role |
| --- | --- |
| `src/generic/w5500.c` / `.h` | W5500 SPI Ethernet transport + `config_w5500` |
| `src/generic/nano_udp.c` / `.h` | minimal UDP/IP/ARP responder (RMII IP layer) |
| `src/stm32/eth_mac.c` | STM32F4/F7 RMII MAC/DMA console + lwIP/nano-UDP seam |
| `src/generic/udp_console.c` | shared, transport-independent datagram console glue (unchanged) |
| `test/nano_udp/nano_udp_test.c` | host unit test for the nano-UDP framing helpers |
| `test/nano_udp/nano_udp_state_test.c` | host unit test for receive/candidate-peer state |
