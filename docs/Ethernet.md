# Ethernet datagram console transport

This document describes wired-Ethernet transports for the RFC 0001
datagram console (see
[doc 07 - Link Layer and Transports](rfcs/0001-motion-intentions/07-Link_Transport.md)).
Two paths are provided:

* the **W5500** - a WIZnet SPI Ethernet controller that runs the
  IP/UDP/ARP stack in silicon, usable on any board with an SPI bus;
* the **native RMII MAC** - the Ethernet MAC built into STM32F4/F7/H7
  parts, driving an external RMII PHY, with a software IP layer above
  it.

!!! warning "Compile-checked, not hardware-validated here"
    Both transports in this fork **build and link** (STM32F407,
    STM32G0B1) and the RMII path's IP helpers pass host unit tests, but
    **neither has been run against physical Ethernet hardware**.  Treat
    the register/DMA/pin details as a starting point to bring up on a
    bench, not as validated firmware.

## Same binding, different loss model

Nothing about the protocol changes on Ethernet. RFC 0001 doc 07 is
explicit: *"The same UDP binding runs unchanged over Ethernet."* These
transports are exactly that - a source of `struct udp_console_ops`
(`recv` / `send` / `rx_accepted`) for the target-independent datagram
glue in `src/generic/udp_console.c`, no different in kind from
`src/linux/udp.c` (a POSIX socket) or `src/esp32/udp_port.c` (an lwIP
socket). The HMAC-authenticated intentproto datagram
(`src/generic/udp_datagram.h`) rides inside the UDP payload byte-for-byte
as it does over WiFi.

The one honest difference is the loss model. Over WiFi the dominant
impairment is whole-datagram loss, which is why the erasure-FEC layer
exists. A switched full-duplex Ethernet link has a clean loss model
(no radio jitter, per-port bandwidth, deterministic sub-millisecond
latency), so **the erasure-FEC layer can typically be negotiated off on
Ethernet** (`fec_k = 0`, the default) while everything else - datagram
sequencing, traffic-class mapping, authentication - is identical. FEC
remains available if a particular plant wants it.

## Authentication (PSK / trust)

Authentication is mandatory on network transports (RFC 0001 doc 07).
Every datagram carries a truncated HMAC keyed by a pre-shared key. The
key is provisioned at build time, mirroring the ESP32 port:

* `CONFIG_W5500_PSK` - the pre-shared key string. A host connects with
  the matching key via `lib/intentproto/tools/udp_bridge.py`.
* `CONFIG_W5500_TRUST_NETWORK` - the explicit "unauthenticated"
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

## Native RMII MAC - design + seam

STM32F4/F7/H7 parts have a built-in Ethernet MAC that needs an external
RMII PHY **and a software IP/UDP stack** above it (the MAC delivers only
raw ethernet frames). A full lwIP integration is large and cannot be
built or validated in this environment, so it is **not vendored here**.

Instead the path is split at a single documented seam:

```
   rx frame  --> nano_udp_input()      (or lwIP: ethernet_input)
   tx frame  <-- eth_mac_emit()        (or lwIP: low_level_output)
```

* `src/stm32/eth_mac.c` is the **MAC/DMA half**: RMII pin/clock
  bring-up, the DMA descriptor rings, and the OWN-bit handshakes that
  move frames. The board-specific pieces that cannot be validated blind
  - the exact RMII pin map, the PHY address and its auto-negotiation,
  and the IP-config source - are marked `TODO(board)`.
* `src/generic/nano_udp.c` is the **pluggable IP layer**: a minimal
  single-socket UDP/IP/ARP responder (ARP replies so a host can find
  us, IPv4 header + checksum, UDP demux to the console). It is small,
  freestanding, and **host unit-tested** - so the RMII path is
  functional without lwIP. Swapping in lwIP is a matter of re-pointing
  the two seam calls.

### Scope decision (honest)

The primary, buildable, coherent path is the **W5500**: it needs no
software IP stack and works on any SPI board. The native RMII path is
delivered as a **MAC/DMA skeleton + a functional, tested nano-UDP IP
layer + the lwIP seam**, with the DMA bring-up's board-specific details
left as marked TODOs. This was chosen over stubbing nano-UDP because a
minimal single-socket responder is tractable and independently testable
(checksums, ARP/UDP framing against known-good byte vectors), whereas a
blind lwIP port would be large, untestable here, and no more honest.

Enable it (off by default; STM32 F407/F429/F7/H7 only):

```
CONFIG_WANT_ETHERNET_RMII=y      # depends on HAVE_ETH_MAC
```

### Testing nano-UDP on the host

```
test/nano_udp/run.sh
```

This builds `src/generic/nano_udp.c` with `-DNANO_UDP_TEST` and checks
the Internet checksum against the canonical RFC 1071 IPv4 example and
the ARP/UDP framing against known-good byte vectors.

## Files

| File | Role |
| --- | --- |
| `src/generic/w5500.c` / `.h` | W5500 SPI Ethernet transport + `config_w5500` |
| `src/generic/nano_udp.c` / `.h` | minimal UDP/IP/ARP responder (RMII IP layer) |
| `src/stm32/eth_mac.c` | RMII MAC/DMA bring-up skeleton + lwIP/nano-UDP seam |
| `src/generic/udp_console.c` | shared, transport-independent datagram console glue (unchanged) |
| `test/nano_udp/nano_udp_test.c` | host unit test for the nano-UDP framing helpers |
