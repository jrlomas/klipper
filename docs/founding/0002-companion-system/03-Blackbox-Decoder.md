# FD-0002 · 03 — The Blackbox Decoder

Status: **Realized in the Atlas floor (Milestone A).** The decoder is
[`atlas/decode/klippy_log.py`](../../../atlas/decode/klippy_log.py),
reading onto the merged store in [`atlas/timeline.py`](../../../atlas/timeline.py)
and driven from the CLI (`python3 -m atlas.cli decode /path/to/klippy.log`).
It is useful on a **stock Klipper log today**, before a single new board
ships. The always-on path is realized in
[`atlas/daemon.py`](../../../atlas/daemon.py): it follows the live log across
rotation, keeps a bounded timeline, runs deterministic diagnosis, and
atomically publishes the versioned state consumed by API/UI plumbing.

A modern aircraft does not ask the pilot to remember what happened in the
three seconds before an incident — it has a flight recorder, and afterward
someone reads it into a report. HELIX now has the flight recorder (the
execution log of FD-0001, plus the trace plane of
[02-Trace-Observability.md](02-Trace-Observability.md)). The blackbox
decoder is the half that *reads the recording into a report*. This is the
first stage of Plane 2 — Understand; the second stage, turning the
narrative into a named cause and a suggested fix, is the diagnosis engine
in [04-Diagnosis-Engine.md](04-Diagnosis-Engine.md).

## What the decoder does

It merges **every board's execution log + trace events + `link_stats` +
timesync state** into one machine-time-ordered narrative and
**reconstructs machine state at the moment of a fault.** Flight recorder →
incident report. Where a print died with molten plastic parked on the bed,
the decoder answers *what the machine was actually doing* in the moments
before — not what the host thought it commanded, but what the boards
reported they executed.

The inputs it fuses are precisely the honest artifacts FD-0001 built:

- the **execution log** (`src/execlog.c`) — the uplink twin of the
  intention queue, a per-board record of what was *actually* executed;
- **trace events** from the structured trace plane
  ([02-Trace-Observability.md](02-Trace-Observability.md));
- **`link_stats`** — the per-link CRC / retransmit / timeout counters;
- **timesync state** — the machine-time relationship between boards, which
  is what lets multi-MCU events line up at all.

The **merge key is machine time** ([FD-0001 doc 01](../0001-motion-intentions/01-Time_Model.md)):
because every source is stamped against the primary MCU's counter, a
mainboard event, a toolhead event, and an accessory event drop into a
single ordered timeline instead of three logs no one can align.

## Useful on day one: the legacy `klippy.log` path

A decoder that only worked once every board shipped the new trace plane
would be useless for a year. So the decoder **also ingests the legacy
`klippy.log`** and is useful on **any Klipper machine, before a single new
board ships.** This is a deliberate on-ramp: a HELIX or stock-Klipper user
can point Atlas at the log they already have and get a merged, ordered,
state-reconstructed narrative today.

The decoder is honest about what a stock log can and cannot give. As
realized in [`atlas/decode/klippy_log.py`](../../../atlas/decode/klippy_log.py),
it recovers the **host monotonic clock from `Stats` lines**, anchored to
wall time by the `Start printer at` banner, and reconstructs the sequence
of events a stock log honestly allows. Events that fall *between* stats
lines are marked with a `~` to signal **inferred time** — the decoder
never pretends to a precision the source doesn't support. Real machine
time arrives later, automatically, when the trace plane (A1/A2) and the
execution log feed the *same* [`Timeline`](../../../atlas/timeline.py): the
same decoder, better inputs, no new code path for the user.

## Where this sits

The decoder produces the **narrative and the reconstructed state**; it
does not, by itself, name the cause. That is the job of the diagnosis
engine, which matches deterministic failure patterns over exactly this
timeline and — on a match — attaches a likely cause, a suggested fix,
provenance, and a confidence score. When *nothing* matches, the decoder's
bundle is still the thing that gets captured as a candidate case for the
knowledge base. Read on in [04-Diagnosis-Engine.md](04-Diagnosis-Engine.md)
and [05-Knowledge-Base.md](05-Knowledge-Base.md).
