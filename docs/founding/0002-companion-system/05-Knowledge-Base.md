# FD-0002 · 05 — The Knowledge Base: the Shared Brain

Status: **Framework realized in the Atlas floor (Milestone A).** The
repo layout, the blackbox **bundle format**
([`atlas/kb/bundle.py`](../../../atlas/kb/bundle.py)), the numeric-only
**redaction** pass ([`atlas/kb/redact.py`](../../../atlas/kb/redact.py)),
and the GitHub-Issue **intake + label vocabulary**
([`atlas/kb/issue.py`](../../../atlas/kb/issue.py),
[`atlas/kb/labels.yaml`](../../../atlas/kb/labels.yaml)) all run today.
The lifecycle governance runs on GitHub Issues. Assemble a redacted,
submittable bundle with `python3 -m atlas.cli bundle /path/to/klippy.log --issue`.

This is the part that makes the repository a **shared brain** rather than a
pile of source. It is also the part with the sharpest trust, privacy, and
security questions, so it is *specified*, not hand-waved. Two things live
here: **what the knowledge base contains**, and — just as important —
**how a case becomes knowledge**, publicly, with a readable rationale for
every decision including the decision *not* to learn something.

## What the KB contains

Everything below is versioned, reviewable, and **signed** so a machine can
trust an update — reusing FD-0001's Ed25519 image-signing
(`scripts/sign_image.py`, `keys/`):

- the **failure-pattern catalog** (signature, cause, fix, provenance,
  confidence) — the data the diagnosis engine of
  [04-Diagnosis-Engine.md](04-Diagnosis-Engine.md) matches;
- the **board** and **config** catalogs — the data the provisioner of
  [06-Provisioning-Fleet-Coherence.md](06-Provisioning-Fleet-Coherence.md)
  reads;
- the **model configuration + memory files** that define our assistant
  (system prompts, RAG index build, per-family model pins) — the data the
  LLM layer of [07-LLM-Layer.md](07-LLM-Layer.md) loads.

Knowledge is a first-class, signed, reviewable artifact — Principle 4 of
[00-Vision.md](00-Vision.md#1-principles).

## The report pipeline

When a machine hits a fault it does not automatically tell the world.
Nothing leaves the Pi without an explicit, per-event decision:

1. On a failure the companion assembles a **blackbox bundle** — the merged
   timeline, the diagnosis (or "no pattern matched"), the versions
   ([`atlas/kb/bundle.py`](../../../atlas/kb/bundle.py)).
2. It is **redacted by default** and **never leaves the Pi without
   explicit, per-event consent**
   ([`atlas/kb/redact.py`](../../../atlas/kb/redact.py)).
3. On opt-in submit, the bundle becomes a **GitHub Issue** through a
   structured template + labels — GitHub is both the intake and the audit
   log ([`atlas/kb/issue.py`](../../../atlas/kb/issue.py)).

## Redaction — numeric-only unredacted

The redaction policy is a settled decision, and it is precise because
"redact by default" is only meaningful if you can say exactly what *does*
ship. Three tiers, all versioned and unit-tested in the deterministic
floor:

- **Always-share** — versions + ABI hash, board **model** / MCU family
  (from the catalog, *not* the physical serial), kinematics type, trace
  event ids + **numeric** args, execlog numeric fields, `link_stats`
  counters, timesync numeric state, diagnosis + confidence.
- **Transform-then-share** — file paths → basename or dropped,
  string/free-text args dropped, wall-clock → relative machine-time
  offsets.
- **Never-share, no allowlist override possible** — secrets / keys / PSKs /
  tokens, hostnames / IPs / MACs, serials / UUIDs, account identifiers.

So *yes*, some fields ship unredacted — **numeric diagnostics only**;
**every string is redacted**, and secrets **cannot be allowlisted at all**.
That last clause is deliberate: an allowlist that could be widened to
include a secret is not a safety boundary. The rule is enforced in code
and unit-tested, not left to operator discipline.

## Users as real-time trainers

The UI carries lightweight feedback — "did this diagnosis match?", "did
this fix work?" — attached to the case. Verified outcomes raise a
candidate's confidence. This is how a diagnosis earns trust: not by a
maintainer's assertion, but by machines in the field confirming it worked.

## §6a — How reports become knowledge: the KB lifecycle

The knowledge base is a **public asset, so how a case becomes knowledge is
itself public.** Nothing is promoted in a back room; every decision —
accept *or* reject — leaves a readable rationale in the open. The
mechanism is ordinary GitHub, used deliberately.

### The state machine (the label *is* the audit trail)

Every submission is a GitHub Issue that moves through labelled states, and
the label sequence *is* the audit trail
([`atlas/kb/labels.yaml`](../../../atlas/kb/labels.yaml) is the canonical
vocabulary):

1. **`case/new`** — an opt-in, redacted blackbox bundle arrives as an
   Issue from a structured template (symptom, merged-timeline excerpt,
   diagnosis or "no match", firmware/host/library versions, Atlas's
   proposed rule if any). A bot validates the template and attaches a
   **content hash** and the submitter's provenance.
2. **`case/triage`** — deduplicated against existing cases and open
   patterns (Atlas assists by clustering similar bundles). Duplicates are
   linked to the canonical case, raising its **observation count**, not
   spawning noise.
3. **`case/analysis`** — a candidate **pattern** is drafted: signature →
   cause → fix, with a confidence seed. This is a proposed change to the
   catalog data, opened as a **pull request linked to the Issue**, so the
   exact diff to the shared brain is reviewable.
4. **`case/verify`** — the fix is corroborated: reproduction, a
   deterministic check that the signature is well-formed and does **not
   conflict** with an existing pattern, and real-world **"did this fix
   work?"** feedback from other machines that hit it. Confidence rises
   with independent confirmations.
5. **`accepted`** *or* **`rejected/*`** — a maintainer merges the PR (the
   pattern enters the signed catalog) *or* closes it with a
   **`rejected/<reason>`** label. Either way the closing comment states
   the rationale in plain language.

### Every decision carries its "why"

Acceptance and rejection both require a written rationale on the Issue,
drawn from a fixed, public vocabulary so reasons are consistent and
searchable:

- **Accept reasons:** `reproduced`, `multi-machine-confirmed`,
  `root-cause-clear`, `fix-verified`.
- **Reject reasons:** `rejected/not-reproducible`,
  `rejected/machine-specific` (a local quirk, not general knowledge),
  `rejected/duplicate`, `rejected/insufficient-data`,
  `rejected/unsafe-fix`, `rejected/superseded`.

A reader can therefore open the KB's Issue tracker and see not just *what*
Atlas knows, but the *entire argument* for every entry — and for
everything the project decided **not** to learn, which is often the more
instructive record.

### The promotion gate

A pattern influences other machines **only** when: its PR is merged into
the catalog on the default branch, the catalog is **Ed25519-signed** by
the project key, and machines pull the signed update. So the model can
*propose* a pattern (step 3), the community can *corroborate* it (step 4),
but only a **reviewed, merged, signed** change reaches the fleet — the
same trust discipline HELIX already applies to firmware images. **Raw
submissions never touch another machine.**

### Confidence & decay

Each accepted pattern carries a confidence that **rises** with independent
confirmations and **decays** if later cases contradict it or a HELIX/Atlas
release supersedes the underlying cause — so the brain **forgets stale
lessons instead of hoarding them.** A contradicted pattern re-enters the
lifecycle at `case/verify` rather than being silently trusted.

## Trust model & anti-poisoning

- **Privacy:** local-first; opt-in; redacted; minimized. Nothing about a
  machine is collected that the fault doesn't require.
- **KB trust — single project key now, multi-signer-ready.** One project
  Ed25519 signing key (reusing FD-0001's image-signing:
  `scripts/sign_image.py`, `keys/`), with a signature **envelope** that
  already carries a signer list + threshold, so migrating to a maintainer
  web-of-trust later is a **policy change, not a format break.**
- **Poisoning defense — concretely.** Because the only path to the fleet
  is a signed merge to the catalog, a bad actor cannot inject knowledge by
  spamming submissions: they can open Issues (public and deduplicated), but
  they cannot merge, sign, or bypass the `case/verify` corroboration.
  Submitter provenance and reputation (derived from public GitHub history)
  weight **triage priority only** — never the final gate. The human +
  deterministic gate before promotion is the wall; the signature is the
  lock.
- **Trust on pull:** machines accept KB updates only if signed by the
  project key — the same discipline as firmware images.
