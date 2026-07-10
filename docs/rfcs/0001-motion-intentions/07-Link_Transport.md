# RFC 0001: Link Layer and Transports

Status: Draft / Discussion

This document specifies a **backwards-compatible** framing extension
that replaces the 16-bit CRC with BCH forward error correction, and a
UDP datagram transport that makes WiFi-attached MCUs (ESP32 class)
first-class citizens. It is separable from the motion redesign — it
could land before or independently of trajectory segments — but the
two reinforce each other: deep intention queues are what make a
jittery wireless link survivable, and FEC is what keeps the refill
stream flowing without retransmit stalls.

## Current framing and the compatibility hook

Today's frame ([src/command.h](../../../src/command.h),
[docs/Protocol.md](../../Protocol.md)):

```
<1 byte len> <1 byte seq> <payload…> <2 byte CRC16> <1 byte sync 0x7E>
```

The seq byte is `0x10 | sequence`, i.e. `MESSAGE_DEST (0x10)` ORed
with a 4-bit sequence (`MESSAGE_SEQ_MASK 0x0f`). **The upper three
bits (0xE0) are reserved and must be zero:** `command_find_block()`
rejects and naks any frame where `(seq & ~MESSAGE_SEQ_MASK) !=
MESSAGE_DEST` ([src/command.c](../../../src/command.c)). The host-side
decoder enforces the same rule
([klippy/chelper/msgblock.c](../../../klippy/chelper/msgblock.c)).

That reservation is the compatibility hook: **legacy firmware provably
cannot misinterpret a frame that sets those bits** — it rejects it and
requests retransmission. New-format frames are therefore safe to
*probe* with, and trivially safe after negotiation.

## Negotiation

1. Framing v2 capability is advertised in the data dictionary
   downloaded at `identify` time (e.g.
   `DECL_CONSTANT_STR("LINK_FEC", "bch10_t3")`), which itself is
   always transferred in legacy framing.
2. After reading the dictionary, the host enables v2 by setting a
   reserved seq-byte bit (proposed: **0x80 = extended trailer**) on
   frames it sends; the MCU mirrors the bit on its responses.
3. Legacy CRC16 framing remains the default, the bootstrap format,
   and the **permanent fallback** — a v2-capable MCU accepts both
   formats at all times (the seq bit tells it which trailer to check
   per frame).
4. The choice is **per link**: a machine may run its primary MCU over
   USB in legacy framing and a WiFi toolboard in v2 — nothing couples
   them.

## Framing v2: BCH error-correcting trailer

The CRC16 detects errors and forces a round-trip (nak → retransmit)
for every hit. On a high-latency link, each such round-trip is a bite
out of the refill horizon. A BCH code *corrects* small errors in
place:

```
<len> <seq|0x80> <payload…> <BCH parity> <sync 0x7E>
```

* Code: **shortened binary BCH over GF(2¹⁰)** (natural length
  n = 1023 bits), shortened to the frame length (≤ 61 data bytes =
  488 bits). Parity cost is 10 bits per correctable error:

  | t (errors corrected) | parity | trailer vs CRC16 |
  | --- | --- | --- |
  | 2 | 20 bits → 3 bytes | +1 byte |
  | **3 (proposed)** | 30 bits → 4 bytes | +2 bytes |
  | 4 | 40 bits → 5 bytes | +3 bytes |

  t=3 corrects any 3 bit-errors per frame and *detects* well beyond
  that (decode failure → nak → legacy retransmit path; ARQ is
  retained, FEC reduces its use rather than replacing it).
* Overhead honesty: +2 bytes on a 64-byte frame is ~3% — for that, a
  link with a 10⁻⁴ bit error rate goes from naking ~5% of frames
  (512 bits × 10⁻⁴) to essentially never retransmitting.
* MCU cost: syndrome computation is table-driven XOR folding, a few µs
  per frame in the dominant no-error case — comparable to CRC16.
  The Berlekamp–Massey/Chien correction path runs *only when a frame
  is actually damaged*, in task context, where a few hundred µs is
  irrelevant (the alternative was a multi-ms retransmit).
* Ack/nak frames and the sync byte are unchanged; sequence numbering
  and the retransmit window are unchanged.

## What FEC does and does not fix on WiFi — two layers

An honest design must state this: on a UDP/WiFi path, the 802.11 MAC
already CRC-checks every radio frame and retransmits locally, so a
datagram that *arrives* rarely contains bit errors. The dominant
impairments over WiFi are **whole-datagram loss and reordering**, plus
latency jitter from MAC retries.

Therefore the link design has two independent, individually
negotiable layers:

1. **Intra-frame BCH (framing v2, above)** — corrects bit errors.
   Chief beneficiaries: raw noisy links — long serial runs to
   toolheads, RS-485-style buses, RF serial bridges — and any
   transport without its own integrity layer.
2. **Packet-level erasure FEC (UDP transport, below)** — recovers
   *lost datagrams* without waiting out a retransmit timeout: after
   every k data datagrams, send parity datagram(s) computed across
   the block (XOR for 1-loss recovery; Reed–Solomon over GF(2⁸) for
   burst tolerance — choice flagged open). A lost datagram inside a
   protected block is reconstructed on arrival of the block's parity,
   costing bandwidth (1/k) instead of latency (an RTO).

Both layers ride the same negotiation mechanism (dictionary
capability + header bits).

## UDP transport binding (WiFi and Ethernet)

* **Datagram = one or more complete frames** (typical WiFi MTU fits
  ~20 frames; batching amortizes per-packet overhead exactly as
  today's 64-byte blocks amortize per-byte overhead).
* **Datagram-level 16-bit sequence number** prepended per datagram:
  detects loss/reorder at the transport layer and widens the effective
  window — the in-frame 4-bit sequence (`MESSAGE_SEQ_MASK`) was sized
  for wired RTTs and stays untouched for compatibility.
* **ARQ tuning:** the existing RTT-estimated retransmit machinery
  (`serialqueue.c`, RFC 6298-style) applies with WiFi-appropriate
  floors; keepalive datagrams maintain liveness and NAT/AP state
  during idle.
* **Class mapping** ([03-Traffic_Classes.md](03-Traffic_Classes.md)):
  Class 0 and 1 are acked and erasure-protected; Class 2 telemetry may
  be sent as unacked datagrams and simply lost under congestion — the
  class semantics were designed for exactly this.
* **Ethernet is the preferred wired network transport.** The same UDP
  binding runs unchanged over Ethernet, and for new board designs
  Ethernet is architecturally *more* universal than UART: switched
  full-duplex links with per-port bandwidth, deterministic sub-ms
  latency (no radio jitter), one cable standard from mainboard to
  toolhead to peripheral, cheap PHYs/MACs (W5500-class SPI parts,
  RMII PHYs on STM32/ESP32), and optional PoE for single-cable
  toolheads. WiFi and Ethernet differ only in the loss model: over
  Ethernet the erasure-FEC layer can typically be negotiated off,
  while everything else — datagram sequencing, class mapping,
  authentication — is identical. UART/USB/CAN remain fully supported;
  they stop being the only respectable options.
* **ESP32 as a target:** dual-core 240 MHz with the radio stack pinned
  to one core and motion execution on the other fits the 32-bit floor
  of this RFC ([00-Vision.md](00-Vision.md)) comfortably (and the
  chip offers RMII Ethernet as well as WiFi). One honest caution: the
  WiFi stack's interrupt behavior makes tick-precise step generation
  on the same silicon genuinely hard — core pinning helps, and the
  RMT/PCNT pulse peripherals are the likely escape hatch for the
  stepper backend; the FOC backend (its own timer, tolerant of µs-level
  ISR jitter) is frankly a better first citizen of that chip. The
  port specifics belong to the migration plan, not this protocol
  document.
* **Jitter budget — the synergy argument:** with a 0.5–1 s MCU-side
  intention horizon and the underrun ramp of
  [02-Intention_Protocol.md](02-Intention_Protocol.md), the link may
  stall for up to (horizon − refill margin) — hundreds of
  milliseconds — with *zero* effect on motion, and a longer stall
  degrades to a controlled, resumable stop. Today's architecture
  answers the same stall with a mid-print shutdown. This is why
  WiFi-attached motion boards are credible under this RFC and are not
  credible today.

## Security: mandatory for network transports

Today *everything* in the printer is unauthenticated — any process
that can reach the serial device, CAN bus, or (worse) the network
socket controls motors and heaters. A physical cable requires
physical access; a datagram requires being on the network segment of
a device that drives a 300 °C heater. Virtual networks and firewalls
can wrap the problem, but the protocol itself is due for the upgrade,
and going WiFi/Ethernet makes it non-optional.

Position this RFC takes: **authentication is mandatory in v1 of the
UDP transport** — not an open question, not a later phase:

* Every datagram carries a truncated **HMAC** (e.g. HMAC-SHA256/8
  bytes) over its contents plus a nonce/sequence, keyed by a
  pre-shared key established at pairing time. Cost is a few µs per
  datagram on any 32-bit MCU — negligible against WiFi latencies —
  and it kills both forgery and blind replay.
* An unauthenticated mode exists only as an explicit
  `trust_network: true` configuration confession, for lab benches and
  isolated VLANs.
* Heavier machinery (DTLS, key rotation, per-board identities) is
  deferred, but the datagram layout reserves the header space so
  adopting it later is not a re-framing.
* Wired point-to-point transports (USB, UART, CAN) keep their current
  physical-access trust model — unchanged, but now that model is a
  *stated* decision rather than an accident.

Key provisioning (how the PSK gets onto the board: build-time,
bootstrap-over-USB at first pairing, or NVS storage) is the open
design item — the *requirement* is not.

## Open questions

* BCH t parameter (t=3 proposed) and whether it should be
  link-configurable.
* Erasure layer code: XOR (k+1, simple) vs Reed–Solomon (burst
  tolerant); and the default k.
* PSK provisioning flow and storage (the authentication requirement
  itself is settled above).
* Whether framing v2 should also lift `MESSAGE_MAX` (64) for
  high-MTU transports, or keep frame size and batch instead
  (proposed: keep 64, batch datagrams).
