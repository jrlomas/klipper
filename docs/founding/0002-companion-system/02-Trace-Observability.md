# FD-0002 · 02 — Plane 1: Observe (the Structured Trace Plane)

Status: **Realized in the Atlas floor (Milestone A).** The firmware macro
and IRAM-safe ring are in [`src/trace.c`](../../../src/trace.c) /
[`src/trace.h`](../../../src/trace.h) (gated on `WANT_TRACE`, registered in
the stock Klipper data dictionary, F072-fit); the live and offline host
collectors are [`klippy/extras/atlas_trace.py`](../../../klippy/extras/atlas_trace.py)
and [`atlas/decode/trace.py`](../../../atlas/decode/trace.py); the merged store
is [`atlas/timeline.py`](../../../atlas/timeline.py); the live viewer is
[`atlas/view.py`](../../../atlas/view.py). On 2026-07-13 the firmware and live
collector were qualified over USB on a 12 MHz RP2040 SKR Pico and a 64 MHz
STM32G0B1 EBB36: registered diagnostic records merged on machine time, and
deliberate ring overload reconciled every host sequence gap to a firmware
overwrite. The constrained F072 fit is build-measured but still awaits its own
on-target run, and the real `step_underrun` call-site remains part of motion
qualification. The streamer drains at most one configured batch per wake; a
periodic host status query paces any backlog so the MCU response queue cannot
become a second, unaccounted drop point.

Everything else in Atlas reads a timeline. Before there can be a decoder,
a diagnosis engine, or a companion that answers "why did my print fail?",
the machine has to be able to *say what it is doing* — cheaply, on the
constrained board, in a form a program can reason about. This is Plane 1,
and it lands first because every later plane gets easier once the machine
can talk.

## The gap: there is no real MCU debug

The problem was named directly: HELIX has no real micro-controller debug
channel. The OAMS CAN `printf` proved both how badly it's needed and how
useful it is to *see what an MCU is actually doing* mid-operation. But
free-form `printf` is the wrong answer for HELIX for three concrete
reasons, each of which would sink something downstream:

- It is **expensive on an F072** — string formatting and buffers the
  16 KB board cannot spare.
- It **bloats the wire** — full strings where a few bytes would do.
- It is **unparseable** — free text kills every downstream ambition in
  this document: you cannot decode, diagnose, or aggregate prose.

So the need is real and the obvious answer is disqualified. The HELIX
answer keeps the ergonomics and throws away the cost.

## The HELIX-native answer: a structured, registered trace channel

A `DECL_TRACE` / `LOG(event, args…)` macro emits an **event id + typed
args**, machine-time-stamped, on Class-2 telemetry, and is **rendered to a
human string on the host** via the dictionary — exactly the
annotation/self-description mechanism the command registry already uses.
The firmware author gets `printf` ergonomics:

```c
LOG(step_underrun, horizon_us, queue_depth);
```

The **wire gets a few bytes** (an id and a couple of integers). The
**host gets a stream a machine can reason about** — and, rendered through
the dictionary, a human-readable line too. Per-subsystem trace levels mean
a subsystem can be silent until you want it loud, so the channel is
**near-zero cost when off** and cheap when on. This is the same trick
FD-0001's command registry already plays: the *symbols* live on the host,
the *wire* carries indices.

The application firmware follows HELIX's additive-envelope architecture: its
dispatcher and live trace messages remain stock Klipper v1. Trace event names,
subsystems, and format strings therefore use Klipper's existing
`DECL_ENUMERATION` / `DECL_CONSTANT_STR` data dictionary. This mirrors the
annotation model used by `intentproto`, but it does not route the trace stream
through the datagram carrier. Datagram/FEC/modem validation remains a separate
transport sign-off.

## Why machine time is the whole point

Because every event carries **machine time** — FD-0001's primary-MCU
counter, the timeline all intentions are already scheduled against — traces
from the mainboard, a CAN toolhead, and an ESP32 accessory **merge into
one timeline**. That merged, machine-time-ordered store
([`atlas/timeline.py`](../../../atlas/timeline.py)) is the substrate
Planes 2–4 all read. Without a shared clock you would have three logs that
cannot be lined up; with FD-0001's shared clock you get one narrative of
the whole machine, for free.

This is why Plane 1 is not just "a debug feature." It is the *substrate*.
The decoder ([03-Blackbox-Decoder.md](03-Blackbox-Decoder.md)) reconstructs
state from this timeline; the diagnosis engine
([04-Diagnosis-Engine.md](04-Diagnosis-Engine.md)) matches patterns over
it; the health monitor and the LLM interpreter all read the same store.

## Deliverables — and what is built

- **Firmware macro + IRAM-safe ring** — a small ring that is safe to write
  from interrupt context, so tracing a hot path never itself becomes the
  fault. *Built:* [`src/trace.c`](../../../src/trace.c),
  [`src/trace.h`](../../../src/trace.h), behind `WANT_TRACE`, F072-fit.
- **Host trace collector** — decode trace records via the dictionary onto
  the merged timeline. *Built:* the live Klippy bridge in
  [`klippy/extras/atlas_trace.py`](../../../klippy/extras/atlas_trace.py), the
  offline decoder in [`atlas/decode/trace.py`](../../../atlas/decode/trace.py),
  and the trace/execution/link/timesync JSONL boundary in
  [`atlas/observe.py`](../../../atlas/observe.py).
- **Merged-timeline store** — the machine-time-ordered stream across all
  MCUs. *Built:* [`atlas/timeline.py`](../../../atlas/timeline.py).
- **Live viewer** — a Mainsail panel if it reaches, else a standalone
  view; live tail + filter by subsystem / severity / board. *Built:*
  [`atlas/view.py`](../../../atlas/view.py), reachable via
  `python3 -m atlas.cli view /path/to/klippy.log --min-severity warning`.

The Pico/EBB36 qualification used a bounded `trace_probe` event: three clean
records per board produced no gaps or write errors. A subsequent 256-record
burst into each 64-record ring produced 192 firmware drops and exactly 192
host sequence gaps on both links, with zero `unaccounted_gaps`; all 64
survivors drained in paced four-record batches. This proves the ring,
dictionary rendering, paced uplink, drop accounting, and cross-MCU time merge
on those targets without claiming motion-path timing or F072 silicon proof.

This lands first because everything else gets easier once the machine can
talk — and now it does.
