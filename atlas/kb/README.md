# Atlas knowledge base — the shared brain (FD-0002 §6)

This is the repo layout for the versioned, signed knowledge that makes
"our" Atlas *ours*. Everything here is reviewable data, not code, and the
only path to the fleet is a **reviewed, merged, Ed25519-signed** change —
the same trust discipline HELIX applies to firmware images.

## Layout

| Path | Contents | Status (Milestone A) |
| --- | --- | --- |
| `../diagnosis/patterns/` | failure-pattern catalog (`signature → cause → fix`) | live, **empty on purpose** |
| `../provision/boards/` | board catalog (MCU, flash method, Kconfig, config) | 50+ boards |
| `labels.yaml` | the §6a lifecycle label vocabulary | ✓ |
| `redact.py` | the numeric-only redaction pass (floor-tested) | ✓ |
| `bundle.py` | the blackbox bundle format | ✓ |
| `issue.py` | GitHub-Issue intake + label vocabulary | ✓ |
| `store.py` | single-use per-incident consent outbox, feedback ledger, signed catalog activation + rollback | ✓ |

Nothing transmits merely because a bundle exists. `KnowledgeOutbox` issues a
short-lived token bound to one redacted content hash; enqueue atomically consumes
it once. A network/API worker may claim that local queue, but cannot manufacture
consent. Catalog updates are Ed25519-verified before safe extraction and atomic
activation, retain one rollback generation, and reject links/path traversal even
inside a correctly signed archive.
| `../../.github/ISSUE_TEMPLATE/atlas-case.yml` | the structured case form | ✓ |

Model configuration + memory files (system prompts, RAG index build,
per-family model pins) join this directory in Milestone C.

## The lifecycle (why it's public)

A case becomes knowledge in the open (FD-0002 §6a):

```
case/new → case/triage → case/analysis → case/verify → accepted
                                                     ↘ rejected/<reason>
```

The **label is the audit trail**. Every accept and reject carries a
written rationale from the fixed vocabulary in `labels.yaml`, so a reader
can see not just *what* Atlas knows but the *entire argument* for every
entry — and for everything it decided **not** to learn.

## Redaction (the promise)

A bundle is **redacted by default** and never leaves the Pi without
explicit, per-event consent. The settled policy (numeric-only unredacted):

- **Shared raw:** numeric diagnostics + safe structural strings (MCU/board
  family, kinematics, event name, fault class, versions, ABI hash).
- **Transformed:** file paths → basename; other free-text → dropped;
  absolute wall-clock → dropped (relative machine-time kept).
- **Never shared, not even if allowlisted:** secrets, keys, PSKs, tokens,
  hostnames/IPs/MACs, serials/UUIDs, account identifiers.

## Trust

Machines accept KB updates only if signed by the project key (single key
today, multi-signer-ready envelope). Submitter reputation weights triage
priority, never the promotion gate. A bad actor can open Issues (public,
deduplicated) but cannot merge, sign, or bypass corroboration.
