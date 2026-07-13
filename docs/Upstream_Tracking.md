# Tracking upstream Klipper

HELIX is a **permanent, friendly fork** of Klipper. A fork only stays
alive if it can keep absorbing upstream's work — every bug fix, every new
board, every sensor — without a painful merge each time. This document is
the contract that makes that possible, and the guard that enforces it.

## The one rule: HELIX is stock Klipper **plus an envelope**

> HELIX adds its motion-intention protocol as an **additive transport
> envelope** around unchanged stock-Klipper command blocks — **never** by
> rewriting Klipper's own protocol code. The legacy (v1) wire path stays
> byte-for-byte upstream, so an upstream merge touches only files HELIX
> also changed, and those changes are small and self-contained.

Concretely, in the running system today:

- The **application firmware** dispatches commands through stock
  `src/command.c` (unmodified). Every HELIX command — trajectory
  segments, triggers, heater-hold, execlog, syscalls — is an ordinary
  `DECL_COMMAND`, exactly like an upstream command. intentproto
  contributes only the authenticated UDP **datagram carrier**, and its
  payload is *whole legacy v1 frames* handed to stock
  `command_find_and_dispatch`.
- The **host** (`klippy/`) frames with stock `serialhdl.py` /
  `msgproto.py` (both byte-identical to upstream). HELIX's additions in
  `mcu.py` and `clocksync.py` are additive features (failure recovery,
  homing hooks, beacon sync), not framing changes.

That is why HELIX can track upstream: the parts upstream owns, HELIX
never touched.

## What must stay stock (enforced)

These files are **byte-identical to upstream Klipper** and must remain so.
`scripts/check_upstream_stock.py` hashes them against
`scripts/upstream_stock.manifest` and fails CI on any drift
(`.github/workflows/upstream-guard.yaml`):

| File | Why it must stay stock |
| --- | --- |
| `src/command.c` | The v1 command framer/dispatcher. HELIX rides it, never edits it. |
| `src/generic/crc16_ccitt.c` | The v1 wire CRC. |
| `klippy/serialhdl.py` | Host serial framing. |
| `klippy/msgproto.py` | Host dictionary parse + message pack/unpack. |

If a future upstream release legitimately changes one of these, that
change arrives *through the merge* (below) — and re-recording the baseline
is part of the merge, the only sanctioned way these hashes move.

## Where HELIX *does* diverge (the patch-point manifest)

Divergence is deliberately confined to a short, documented list. When you
merge upstream, these are the only host files where a conflict can occur,
and each carries only additive HELIX logic:

| File | HELIX delta | Nature |
| --- | --- | --- |
| `klippy/mcu.py` | ~+396 / −6 | Additive: link-loss pause/reconnect, trajectory homing/probing hooks, edge-interrupt homing. Framing untouched. |
| `klippy/clocksync.py` | ~+25 | Additive: machine-time authority / beacon discipline. |

Plus the additive, HELIX-only files that never conflict because upstream
has no counterpart: `lib/intentproto/`, `src/traj_*.c`, `src/trigger_source.c`,
`src/heater_hold.c`, `src/execlog.c`, `src/generic/udp_*`,
`src/generic/console_v2.*`, `src/generic/framing_v2.*`, the klippy v2
transport (`klippy/intentproto_transport.py`,
`klippy/extras/intentproto_transport.py`), the `klippy/extras/` HELIX
modules, and the docs.

A few upstream *generic* firmware files carry small, `#if`-gated HELIX
additions (not stock-locked, so not in the guard, but worth knowing at
merge time): `src/generic/serial_irq.c` (the `WANT_CONSOLE_FRAMING_V2`
hooks). When off — the default — these compile to the stock code.

## The merge workflow

When a new Klipper release lands:

1. **Fetch upstream.** Add `github.com/Klipper3d/klipper` as a remote and
   fetch its default branch (this environment reaches only the fork, so do
   this where upstream is reachable).
2. **Merge.** Merge upstream into the HELIX branch. Conflicts should occur
   *only* in the patch-point files above; if a conflict appears in a
   must-stay-stock file, that is expected when upstream changed it —
   accept upstream's version verbatim (HELIX added nothing there).
3. **Re-apply the additive patches** in `mcu.py` / `clocksync.py` if
   upstream reworked the surrounding code.
4. **Refresh the baseline.** If upstream changed a must-stay-stock file,
   run `python3 scripts/check_upstream_stock.py --update` and commit the
   manifest change *as part of the merge commit*, noting the upstream
   delta in the message.
5. **Run the gates.** `python3 scripts/check_upstream_stock.py` (green),
   the `lib/intentproto` suite, and a representative firmware build.

The goal state after any merge: the four stock files match the new
upstream, the guard is green, and HELIX's envelope is unchanged.

## The quarantine rule (and its sanctioned exceptions)

intentproto contains a complete, original re-implementation of the v1 wire
stack (CRC, VLQ, framing, dispatch, `identify`, dictionary) in
`lib/intentproto/src/proto.cpp` + `dict.cpp` (+ the host binding in
`host.cpp`). That re-implementation exists for consumers that **cannot**
link stock `command.c`. It must never leak into a klipper application
image, or HELIX would suddenly own two v1 stacks and lose upstream
mergeability. The guard asserts `proto`/`dict`/`host` never appear in an
application build (`src/linux/Makefile`, `src/stm32/Makefile`,
`src/esp32/main/CMakeLists.txt`).

Two consumers are **sanctioned** to link the full library — both are the
*intended* use of the MIT-licensed core, and neither is a klipper
application:

- **The in-repo bootloader** (`src/boot_app`) — a freestanding signed
  image that cannot link the application's `command.c`, so it speaks the
  protocol through intentproto's own core.
- **Third-party firmware outside this repo** — e.g. the **OpenAMS
  mainboard-firmware**, whose own (non-klipper) application is built *on*
  intentproto. This is exactly what the MIT library is for; the guard
  scopes only this repo's application images and does not police those.

## See also

- [Protocol v2](Protocol_v2.md) — the envelope's wire format, and which
  parts are live vs. sanctioned-but-deferred.
- [FD-0001 doc 10 — The Protocol Library](founding/0001-motion-intentions/10-Protocol_Library.md)
  — the library's scope and the envelope principle.
- [FD-0001 doc 14 — Heterogeneous Fleets](founding/0001-motion-intentions/14-Heterogeneous_Fleets.md)
  — coordinating firehose (v1) and intent (v2) boards on one machine.
