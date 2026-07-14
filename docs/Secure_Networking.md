# Secure Networking & Signed Firmware

> **This is Helix** — an evolution of Klipper. Helix treats networks as
> first-class transports and secures them; this page shows how to set that
> up. New to Helix? Start with the **[Helix overview](HELIX.md)**.

Klipper assumes a short, quiet, wired link — a USB cable or a short CAN
run you can trust because reaching it means physical access. Helix does
not make that assumption. Because deep intention queues absorb link
jitter, Helix carries the same protocol over **UDP (Ethernet and WiFi)**,
CAN, USB, and UART alike — and once motion commands ride a network,
the link has to be trusted the way a cable never had to be. This page is
the one place to learn how Helix authenticates that link, how the
optional secure session works, how forward error correction keeps a
lossy link flowing, and how the bootloader verifies signed firmware.

This is a setup guide, not the rationale. For *why* Helix works this way,
read the [HELIX overview](HELIX.md) and [Features](Features.md); for the
rigorous design, [FD-0001 doc 07 — Link Layer and
Transports](founding/0001-motion-intentions/07-Link_Transport.md) and
[doc 11 — First-Class Bootloader](founding/0001-motion-intentions/11-Bootloader.md).
Exact config option names live in the
[Config Reference](Config_Reference.md); the command surface is
consolidated in the [HELIX command reference](Helix_Commands.md).

---

## Why Helix secures the link

A network is hostile in ways a USB cable is not. A physical cable
requires physical access; a datagram requires only being on the same
network segment as a device that drives a 300 °C heater and moves
motors. Anyone who can send packets to an unprotected motion board can
forge a move or replay an old one. What makes putting motion on a
network *credible* in the first place is Helix's deep intention queue:
because the board already holds hundreds of milliseconds of motion and
degrades a long stall to a controlled, resumable stop rather than a
mid-print `shutdown()`, a jittery or wireless link becomes survivable
instead of fatal (see [HELIX overview](HELIX.md) and
[Features](Features.md)). Security is the other half of that bargain: a
link worth carrying motion over is a link worth authenticating.

## The layered model

Helix's link security is built in layers, from a **mandatory floor**
upward. Each layer is independent and — above the floor — individually
opt-in. From bottom to top:

### 1. Authentication by default (the mandatory floor)

Every datagram on a network transport carries a truncated **HMAC**
(hash-based message authentication code) — a short cryptographic tag
computed over the datagram's contents plus a nonce/sequence value —
keyed by a **PSK** (pre-shared key) that both the board and the host
hold. The board verifies the tag before it will act on anything inside
the datagram, and it only ever latches a source address as the reply
peer *after* that address has sent a datagram whose HMAC checks out. An
unauthenticated packet can never steal the link.

**What it defends against:** forged and replayed motion and heater
commands. An attacker on the network segment who does not hold the PSK
cannot craft a datagram the board will accept, and cannot capture and
re-send an old one to make the machine repeat a move. This floor is
**mandatory** on network transports — the firmware fails closed (refuses
to start) without a key. The only way to run without it is an explicit
"trust network" confession (`trust_network`-style build option), meant
strictly for an isolated bench VLAN. Wired point-to-point transports
(USB, UART, CAN) keep their physical-access trust model unchanged.

The HMAC is cheap — a few microseconds per datagram on any 32-bit MCU,
negligible against network latency. It is authentication only: it proves
who sent a command and that it is fresh, not secrecy of the command's
contents (the threat here is forgery and replay, not eavesdropping).

### 2. The optional secure session

Above the static-PSK floor sits an optional **DTLS-class** secure
session. DTLS (Datagram Transport Layer Security) is the standard way to
secure datagram traffic; Helix implements a purpose-built layer that
delivers the same properties a freestanding, no-heap MCU build can
actually audit, without the weight of full IETF DTLS.

A peer *offers* the upgrade with a short PSK-authenticated handshake
that exchanges nonces and a **per-board identity** the host verifies.
From the PSK and those nonces the two sides derive independent,
**rotating per-session keys** — fresh keys for each session, rotated
again on an epoch bump — with a sliding window for per-epoch replay
protection. Once a session is established, plain static-HMAC datagrams
cannot bypass it.

**What it adds over the floor:** per-session key rotation (so a captured
key ages out) and a verified board identity (so the host knows *which*
board it is talking to, not merely that the peer holds the PSK). Like
the floor, it is deliberately auth-only — no payload encryption —
because the threat is forgery and replay of motion, not secrecy. If a
peer does not support the session, both sides stay on the mandatory
static-PSK HMAC floor.

### 3. Forward error correction (availability, not security)

**FEC** (forward error correction) is the odd one out: it is part of the
same envelope but it protects *availability*, not security. It repairs a
damaged or lost frame in place instead of forcing a slow retransmit,
which keeps the board's refill stream flowing on a lossy link. There are
two independent, individually negotiable FEC mechanisms:

* **Intra-frame BCH trailer** — a framing-v2 trailer that replaces the
  classic 16-bit CRC with a **BCH** (Bose–Chaudhuri–Hocquenghem)
  error-correcting code. BCH *corrects* a small number of bit errors in
  the frame in place (the shipped code corrects up to three per frame)
  rather than detecting an error and demanding a retransmit. Its chief
  beneficiaries are raw noisy links — long serial runs to toolheads,
  RS-485-style buses, RF serial bridges — anything without its own
  integrity layer.
* **Packet-level erasure FEC** — for whole-datagram *loss* (the dominant
  impairment over WiFi, where the radio MAC already cleans up bit
  errors). After a block of data datagrams, a parity datagram lets the
  receiver reconstruct one lost datagram in the block without waiting out
  a retransmit timeout. The shipped codec is length-aware XOR parity for
  single-loss recovery.

Both are **negotiable and off by default**. On a clean switched Ethernet
link the erasure layer is typically negotiated off entirely; it earns
its ~50% packet overhead only when link-loss measurements justify it. To
be clear: FEC changes nothing about authentication — every repaired or
reconstructed frame still passes the mandatory HMAC before the board
acts on it.

### 4. Signed firmware

The three layers above secure the *link*. Signed firmware secures the
*image the board runs*. The transport HMAC proves a flash command came
from a peer holding the link key, and a whole-image CRC proves the bytes
arrived intact — but neither proves the image is one the fleet operator
actually authorised. A peer that holds (or steals) the transport key, a
compromised host, or a supply-chain swap of the combined `.bin` before
first flash could push a CRC-valid image the board would happily run.

Helix closes that gap with **Ed25519** (an elliptic-curve signature
scheme, RFC 8032) signatures. The image is signed off-device with a
release private key that never leaves the owner's server; the bootloader
carries only the matching *public* key and only ever *verifies* — it
never signs and never generates keys. At every boot, a signing-enabled
bootloader re-checks both the CRC and the Ed25519 signature over the
exact application image before it will jump into it. An unsigned,
tampered, or interrupted image simply fails the gate and leaves the
board sitting in the bootloader, reachable over the same link — a retry,
not a brick.

Signing moves trust from "whoever can talk on the wire" to "whoever
holds the release private key." It is a build-time choice and does not
fit every part — the smallest boards (16 KiB bootloader budget) stay
CRC-only, while F4-class parts (32 KiB budget) have room for the verify
code. See [doc 11 — Bootloader](founding/0001-motion-intentions/11-Bootloader.md)
for the flash-budget details and key-management workflow.

## Setup

Everything here is **opt-in** and configured per board. The transport
*wiring* — which Ethernet controller, which pins, WiFi credentials — is
covered by the transport pages; this section covers the security surface
that rides on top of it. Follow the transport page for your hardware
first:

* **WiFi / ESP32:** [ESP32.md](ESP32.md)
* **Wired Ethernet (W5500 SPI controller, or native STM32 RMII MAC):**
  [Ethernet.md](Ethernet.md)
* **Desktop, no hardware:** the Linux-MCU-over-UDP recipe in
  [ESP32.md](ESP32.md#desktop-testing-the-linux-mcu-over-udp) exercises
  the whole security stack — bridge, HMAC, sessions, FEC — on a desktop.
  This is the recommended way to get familiar before touching a board.

### Step 1 — Provision a pre-shared key (mandatory)

The PSK is the floor; nothing on a network transport runs without it (or
the explicit trust-network confession). Generate a key and give the
**same bytes** to both the board and the host. The documented generator
is:

```
python3 -c "import secrets; print(secrets.token_hex(32))" > ~/printer_psk
```

Where the key is provisioned depends on the target, and the exact option
names live on each transport page:

* **ESP32:** read in order of preference from NVS (namespace `klipper`,
  key `udp_psk`) or the build-time Kconfig option `CONFIG_KLIPPER_PSK`.
  NVS is preferred — a key baked into the image is readable by anyone
  holding the image, and an NVS key survives reflashes of the app. See
  [ESP32.md — PSK provisioning](ESP32.md#psk-provisioning).
* **Wired Ethernet:** provisioned at build time via `CONFIG_W5500_PSK`
  (W5500) or `CONFIG_RMII_PSK` (native RMII). See
  [Ethernet.md — Authentication](Ethernet.md#authentication-psk-trust).

An empty key **fails closed** unless you explicitly set the matching
trust-network option (`CONFIG_KLIPPER_TRUST_NETWORK`,
`CONFIG_W5500_TRUST_NETWORK`, or `CONFIG_RMII_TRUST_NETWORK`) — reserved
for isolated lab segments only.

### Step 2 — Connect the host

klippy speaks its normal serial protocol; a bridge presents the network
board as a local pseudo-serial device and carries the authenticated
datagrams. The recommended bridge establishes the rotating-key **secure
session** and verifies the board identity:

```
<klippy-python> lib/intentproto/tools/udp_bridge.py --session \
    --board-id <configured-session-id> --board <board-ip>:<port> \
    --psk-file ~/printer_psk \
    --pty /tmp/klipper_net
```

Then point the MCU at that pseudo-serial device in `printer.cfg`:

```
[mcu]
serial: /tmp/klipper_net
```

Omit `--session --board-id` only when connecting to an older,
static-HMAC-only board. The `--psk-file` must contain the same bytes you
provisioned in Step 1. See the full invocation and options on each
transport page ([ESP32.md — Connecting klippy](ESP32.md#connecting-klippy)).

Helix also documents a native host-side transport section,
`[intentproto_transport NAME]`, which lets klippy speak the v2 envelope
(authentication + FEC around stock command frames) directly to a network
board (`mode: datagram`) or a serial board with the BCH framing
(`mode: bch`), with `[mcu NAME] serial:` pointed at its pseudo-terminal.
For the exact options of that section, see the
[HELIX command reference](Helix_Commands.md) and the
[Config Reference](Config_Reference.md).

### Step 3 — The secure session

On the ESP32 port the secure session is enabled in firmware by
`CONFIG_KLIPPER_DATAGRAM_SESSION` (on by default), and requested by the
host bridge with `--session --board-id <id>` as shown above. Once a
session is established the board will not fall back to accepting plain
static-HMAC datagrams. The static envelope remains available only for
backward-compatible bootstrap.

There is no single `printer.cfg` line that "turns on" the session — it
is a firmware capability that the bridge negotiates at connect time — so
this page does not invent one. For the authoritative option names on
your target, see the [Config Reference](Config_Reference.md) and your
transport page.

### Step 4 — Forward error correction (optional)

FEC is negotiable and **off by default**; enable it only when your link
loss profile justifies the overhead. The knobs are per-transport and
documented on the transport pages — for example, the packet-level
erasure pair-parity option (`CONFIG_KLIPPER_FEC_PAIR` on ESP32, the
Linux MCU's `-f 2`, or `CONFIG_RMII_FEC_PAIR` on native RMII), and the
serial BCH framing capability (Kconfig flag `WANT_CONSOLE_FRAMING_V2`,
reported by `HELIX_STATUS`). On clean switched Ethernet, the guidance is
to leave erasure FEC off. See [Ethernet.md](Ethernet.md) and
[ESP32.md](ESP32.md) for the exact options, and the
[Config Reference](Config_Reference.md) if in doubt.

### Step 5 — Signed firmware (optional)

Signed images are compiled *out* unless the bootloader is built with
signing on — `make bootloader SIGNED=1` / `CONFIG_WANT_SIGNED_IMAGES`
(reported by `HELIX_STATUS` as the `WANT_SIGNED_IMAGES` capability). A
CRC-only bootloader is byte-for-byte unchanged and still boots unsigned
images; a signing-enabled bootloader *enforces* signatures and refuses
an unsigned image. Signing itself happens off-device with a private key
held on your own server (`scripts/sign_image.py`,
`scripts/build_combined.py --sign-key`), and the bootloader embeds only
the matching public key.

Signed firmware is an F4-class (32 KiB bootloader budget) feature; the
smallest parts (STM32F072/G0B1, 16 KiB budget) stay CRC-only by design.
The committed dev keypair is DEV/TEST-only and **must be rotated before
any real release**. Do not treat the details here as complete key
management — follow the full workflow, flash-budget table, and
`keys/README.md` pointer in
[doc 11 — Bootloader](founding/0001-motion-intentions/11-Bootloader.md).

## A note on maturity

Match your trust to the evidence. These features are **opt-in** and, in
several cases, further along in source than on silicon — carry that
honesty forward and validate on your own bench before relying on any of
them:

* The desktop Linux-MCU-over-UDP path (bridge, HMAC, datagram
  sequencing, sessions, FEC, auth-failure rejection, trust-network mode)
  is exercised by host tests, and the ESP32 console and both
  authentication modes (static HMAC and rotating-key session) have run
  on a dual-core Lolin32. ESP32 *motion, peripheral, and timing*
  qualification remains pending — see the banners in [ESP32.md](ESP32.md).
* The wired Ethernet transports (W5500 and native RMII) **compile and
  link with the real ARM toolchain and pass host framing tests, but have
  not been run against a physical PHY** in this project. Register, DMA,
  clock, and pin behavior remain a board-bring-up item, not validated
  firmware — see the maturity banner at the top of [Ethernet.md](Ethernet.md).
* Signed images are implemented and workstation-tested; per-target
  hardware proof remains, per
  [doc 11](founding/0001-motion-intentions/11-Bootloader.md).

Read the design-doc banners, keep security and signing opt-in until
you've validated them on your own hardware, and remember that the
classic Klipper transports are all still there, untouched, if you want
them. This mirrors the [User Guide's note on
maturity](Helix_User_Guide.md#a-note-on-maturity).

---

*See also: [HELIX overview](HELIX.md) · [Features](Features.md) · [HELIX
command reference](Helix_Commands.md) · [ESP32](ESP32.md) ·
[Ethernet](Ethernet.md) · [Config Reference](Config_Reference.md) ·
[FD-0001 doc 07](founding/0001-motion-intentions/07-Link_Transport.md) ·
[FD-0001 doc 11](founding/0001-motion-intentions/11-Bootloader.md).*
