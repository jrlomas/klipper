# Failure-pattern catalog (the knowledge base)

This directory holds the deterministic failure-pattern catalog — the
`signature → cause → fix` knowledge Atlas matches against a decoded
timeline (FD-0002 §4, §6).

**Milestone A ships it empty, on purpose.** The diagnosis harness runs
and reports with zero patterns here: it matches nothing, says so plainly,
and *captures the case* as a candidate for the knowledge base. An empty
catalog is a starting condition, not a blocker — the first curated
patterns arrive in Milestone B, through the public GitHub-Issues
lifecycle (FD-0002 §6a).

## Pattern format

Each `*.yaml` file is one pattern (or a list of patterns):

```yaml
id: mcu-timer-too-close
version: 1
signature:                 # a conjunction of predicates (all must hold)
  event_kind: [mcu_shutdown]
  fault_class: [timer_too_close]
cause: >
  The MCU was asked to act on a timer whose deadline had already passed —
  usually host overload, not a printer fault.
fix: >
  Check host CPU/swap/thermals; reduce host load; verify the link.
provenance: seed          # seed | user | model-proposed | multi-machine
confidence: 0.6           # [0, 1]; rises with independent confirmations
```

Supported predicates (see `../schema.py`): `event_kind`, `fault_class`,
`summary_regex`, `min_severity`, `field_min`.

## Trust

A pattern only reaches the fleet when its PR is reviewed, merged, and the
catalog is **Ed25519-signed** by the project key (single key today,
multi-signer-ready envelope) — the same discipline HELIX applies to
firmware images. Raw submissions never touch another machine.
