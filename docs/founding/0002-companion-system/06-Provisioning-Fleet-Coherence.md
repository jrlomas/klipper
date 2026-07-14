# FD-0002 · 06 — Plane 3: Act — Provisioning & Fleet Coherence

Status: **Realized in the Atlas floor (Milestone A).** The board catalog
("pick a board, not a chip"), auto-detection, and one-touch build+flash
planner are in [`atlas/provision/`](../../../atlas/provision/) with a
**53-board catalog** in [`atlas/provision/boards/`](../../../atlas/provision/boards/);
the protocol/ABI-hash lockstep handshake and signed remediation are in
[`atlas/fleet/`](../../../atlas/fleet/) (hash derivation in
[`abi.py`](../../../atlas/fleet/abi.py), coherence in
[`coherence.py`](../../../atlas/fleet/coherence.py)).

Execution is no longer a rendered shell-plan promise: the runner in
[`atlas/provision/execute.py`](../../../atlas/provision/execute.py) uses argv
without a shell, refuses ambiguous/UNCONFIRMED targets even after confirmation,
cryptographically verifies the detached Ed25519 signature, and writes a private
job audit. [`atlas/fleet/remediate.py`](../../../atlas/fleet/remediate.py) routes
an authorized `flash-board` coherence verdict through that same runner.

Observing and understanding a machine ([02](02-Trace-Observability.md)–[04](04-Diagnosis-Engine.md))
is half of a companion. The other half is *acting* — getting a board set
up correctly in the first place, and keeping a whole fleet in agreement
about the wire contract afterward. This document covers the two acting
capabilities that turn out to be **one mechanism**: provisioning (build +
flash the right image) and fleet coherence (make sure every board *speaks
the same protocol*). The health-monitor half of Plane 3 lives with the
engine that drives it, in [04-Diagnosis-Engine.md](04-Diagnosis-Engine.md).

## Board catalog + one-touch provisioning

Today, bringing up a Klipper board means knowing your MCU, your pins, your
flash method, and hand-assembling a Kconfig. Atlas replaces that with:
**pick a board, not a chip.** The catalog entry for "BTT Octopus" / "OAMS
mainboard" / "ESP32 devkit" carries its MCU, pin aliases, flash method,
the Kconfig fragment, and a **curated default config** — with **"Custom"**
as the full escape hatch for anyone who needs it.

The companion then does the mechanical work:

- **auto-detects connected boards** over USB / CAN / DFU / Katapult
  ([`atlas/provision/detect.py`](../../../atlas/provision/detect.py));
- **matches them to the catalog** entry ([`atlas/provision/catalog.py`](../../../atlas/provision/catalog.py));
- **builds + flashes the right image in one action**
  ([`atlas/provision/plan.py`](../../../atlas/provision/plan.py)), over the
  first-class in-band bootloader FD-0001 already ships.

The execution gate verifies the detached Ed25519 signature, builds from the
catalog Kconfig, requires that build to byte-match the verified release
artifact, and passes that exact artifact path to the flasher. A successfully
verified decoy beside a different `out/klipper.*` can therefore never satisfy
the gate.

Detection is deliberately confidence-bounded. A running Klipper USB device
uses the shared `1d50:614e` identity: its product string proves the MCU family,
not the physical PCB. Atlas therefore returns the matching catalog family,
carries the stable `/dev/serial/by-id/` path forward, and requires confirmation
of the exact board. A bootloader-specific signature can narrow the set, but an
ambiguous signature never becomes an automatic guess.

The catalog is **data in this repo** ([`atlas/provision/boards/`](../../../atlas/provision/boards/),
a 54-board catalog across the major vendors), reviewed like code and
versioned like everything else in the shared brain
([05-Knowledge-Base.md](05-Knowledge-Base.md)). Pick a board, click once,
run.

## Fleet coherence — the lockstep answer

This is the keystone, and it ties flashing to *correctness*. Protocol
correctness in HELIX depends on three parties agreeing on the wire
contract: the **host**, the **`intentproto` library**, and **every
board's firmware**. If any one of them is behind, the machine is subtly —
or catastrophically — wrong. The naive framing treats "keep my firmware up
to date" and "make sure my boards agree on the protocol" as two chores.
They are the same chore.

So the **library is the single version authority.** A **protocol/ABI hash**
derived from `intentproto` ([`atlas/fleet/abi.py`](../../../atlas/fleet/abi.py))
is generated into every image's ordinary Klipper data dictionary and checked
by the host during the existing identify handshake — no additional command or
round trip is required —
building on FD-0001's `HELIX_STATUS` / `BOARD_SYSCALL_ABI` / `FRAMING_V2`
capability advertisement. When a board is **behind**, the host offers or
performs the in-band **signed** flash that brings it into lockstep
([`atlas/fleet/coherence.py`](../../../atlas/fleet/coherence.py)).

The consequence is worth stating slowly: **auto-flash and
protocol-correctness become the same mechanism, not two features.** The
version-sync worry ("are all my boards on the compatible firmware?") and
the flashing pain ("ugh, I have to reflash three toolheads") are revealed
as *one problem*, and it is solved once. A board that disagrees about the
protocol is, by definition, a board that needs flashing — and the same
signed, in-band path that provisions it is the path that reconciles it.

Reusing the **signed** image path is not incidental. The remediation flash
is authenticated with the same Ed25519 discipline as every other image and
every KB pull ([05-Knowledge-Base.md](05-Knowledge-Base.md)): a board only
accepts an update signed by the project key, so "bring the fleet into
lockstep" can never become a vector for pushing an unauthorized image.

## One config repository

Underneath both capabilities is a **versioned board + machine config
repository** with sensible defaults and an update path that can flash the
fleet to the matching release. The catalog is data in this repo, reviewed
like code — the same principle as the failure-pattern catalog and the
model config. Provisioning, coherence, and the knowledge base are three
readers of one versioned, signed source of truth.
