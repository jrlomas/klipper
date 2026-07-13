# FD-0002 · 04 — The Diagnosis Engine and the Health Monitor

Status: **Realized in the Atlas floor (Milestone A), with the first
patterns seeded (Milestone B).** The schema, matcher, and
"no match → case captured" behaviour are in
[`atlas/diagnosis/`](../../../atlas/diagnosis/) (schema
[`schema.py`](../../../atlas/diagnosis/schema.py), matcher
[`matcher.py`](../../../atlas/diagnosis/matcher.py)); the first **9 curated
failure patterns** — thermal, comms, motion — are seeded in
[`atlas/diagnosis/patterns/`](../../../atlas/diagnosis/patterns/). Run it
with `python3 -m atlas.cli diagnose /path/to/klippy.log`; the same matcher runs
continuously over live state in [`atlas/daemon.py`](../../../atlas/daemon.py).

The [blackbox decoder](03-Blackbox-Decoder.md) turns a flight recording
into a *narrative*. The diagnosis engine turns the narrative into a
*named cause and a suggested fix* — and, pointed at live telemetry instead
of a post-mortem, into an *early warning*. This is the second half of
Plane 2 (Understand) and the analytical half of Plane 3 (Act). Its
defining property is that it is **useful from the very first day, with an
empty catalog**, because an empty knowledge base is a starting condition,
not a blocker.

## The diagnosis engine: deterministic pattern matching

At its core is a **deterministic failure-pattern catalog** — YAML, in this
repo, reviewed like code — that maps a **symptom signature → likely
cause → suggested fix**, each entry carrying **provenance** and a
**confidence score**. The kinds of faults it is built to name:

- thermal runaway;
- comms-timeout → pause;
- queue underrun;
- CRC storms on a flaky wire;
- endstop bounce;
- commanded-vs-executed divergence (lost steps);
- TMC UART errors.

The signature is matched over the merged, machine-time-ordered timeline
the decoder produces, so a diagnosis is grounded in what the boards
actually reported executing — not in a guess. Because the catalog is plain
data ([`atlas/diagnosis/schema.py`](../../../atlas/diagnosis/schema.py)
defines the schema; [`matcher.py`](../../../atlas/diagnosis/matcher.py)
matches it), the whole engine is deterministic and CPU-only: the same
input always yields the same diagnosis, and every match is auditable.

## The empty-catalog principle

The engine **runs even when the catalog is empty.** The framework matches
nothing, **says so plainly** — "no known pattern (case captured)" — and
*captures the case* as a candidate for the knowledge base. This is the
detail that makes Atlas shippable before anyone has curated a single
pattern: a machine running the empty floor is already doing useful work
(recording, ordering, reconstructing, and capturing cases), and it gets
smarter as patterns land, with no change to the user's experience.

The first patterns have now been seeded — the Milestone B step — across
thermal, comms, and motion families
([`atlas/diagnosis/patterns/`](../../../atlas/diagnosis/patterns/)), so the
catalog is no longer empty. But the "no match → case captured" path
remains first-class and is exactly how new patterns are *sourced*: see the
KB lifecycle in [05-Knowledge-Base.md](05-Knowledge-Base.md).

## Where the LLM enters (intelligence tier)

The deterministic catalog is the **authority**; the model **widens its
reach**, never overrides it. Unmatched cases go to the local model
(intelligence tier only), which interprets the timeline, writes the human
explanation, and **proposes a candidate rule** for human review — never
auto-promoted. So the flow is: deterministic match if we can; model-drafted
*candidate* if we cannot; human review before anything enters the signed
catalog. The model can *propose* a pattern but can never *promote* one —
the same discipline described in [07-LLM-Layer.md](07-LLM-Layer.md) and
governed by the lifecycle in [05-Knowledge-Base.md](05-Knowledge-Base.md).

## The proactive health monitor — Plane 3, Act

The same diagnosis engine, pointed at **live** telemetry instead of a
post-mortem, becomes a **proactive health monitor**. It learns a
**baseline fingerprint** of a healthy machine and flags **drift before
failure**:

- rising CRC rate on a link;
- widening timesync error;
- creeping thermistor noise;
- slow position divergence (a joint quietly losing steps).

This is the difference between a flight recorder and a companion: a
recorder tells you why the crash happened; a monitor tells you the wire is
going bad *before* the crash. The baseline is per-machine and lives in the
machine's memory file (the same store the LLM layer grounds against — see
[07-LLM-Layer.md](07-LLM-Layer.md) and
[`atlas/memory/machine.py`](../../../atlas/memory/machine.py)), so "healthy
for *this* machine" is learned, not assumed. Baselines maturing at fleet
scale is a later-milestone goal ([08-Roadmap.md](08-Roadmap.md)); the
engine that drives it is the deterministic floor that exists now.

The current daemon supplies the live deterministic rule-evaluation loop and
status publication. Learning and comparing per-machine drift baselines remains
Milestone D work; live matching is not presented as learned anomaly detection.
