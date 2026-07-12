# The per-machine memory file (FD-0002 §6, §7).
#
# A versioned, redacted record of what makes this machine this machine:
# its quirks, its learned baselines, and the journal of every change Atlas
# applied (so "what did Atlas change?" and undo are always answerable).
# It serializes to plain JSON-able data, round-trips losslessly, and the
# machine id is an opaque, caller-supplied token — never a serial or a
# hostname (those never leave the Pi; see kb/redact.py).
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

from dataclasses import dataclass, field

MEMORY_SCHEMA_VERSION = 1


@dataclass
class MachineMemory:
    machine_id: str                       # opaque token, not a serial
    created: str = ""                     # caller-supplied timestamp string
    quirks: list = field(default_factory=list)      # free-form notes
    baselines: dict = field(default_factory=dict)   # healthy fingerprints
    changes: list = field(default_factory=list)     # applied-change journal
    diagnoses: list = field(default_factory=list)   # past incidents
    schema_version: int = MEMORY_SCHEMA_VERSION

    # -- mutation --------------------------------------------------------

    def add_quirk(self, note: str) -> None:
        if note not in self.quirks:
            self.quirks.append(note)

    def set_baseline(self, name: str, fingerprint: dict) -> None:
        self.baselines[name] = dict(fingerprint)

    def record_change(self, entry) -> dict:
        """Persist an apply-layer JournalEntry (or dict) into memory."""
        rec = entry if isinstance(entry, dict) else {
            "seq": entry.seq,
            "tier": int(entry.tier),
            "action": entry.action,
            "changes": [{"section": c.section, "key": c.key, "op": c.op,
                         "old": c.old, "new": c.new} for c in entry.changes],
            "reverted": entry.reverted,
        }
        self.changes.append(rec)
        return rec

    def record_diagnosis(self, case_hash: str, summary: str,
                         matched: str = "") -> dict:
        rec = {"case_hash": case_hash, "summary": summary,
               "matched": matched}
        self.diagnoses.append(rec)
        return rec

    # -- serialization ---------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "machine_id": self.machine_id,
            "created": self.created,
            "quirks": list(self.quirks),
            "baselines": dict(self.baselines),
            "changes": list(self.changes),
            "diagnoses": list(self.diagnoses),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MachineMemory":
        return cls(
            machine_id=data["machine_id"],
            created=data.get("created", ""),
            quirks=list(data.get("quirks", [])),
            baselines=dict(data.get("baselines", {})),
            changes=list(data.get("changes", [])),
            diagnoses=list(data.get("diagnoses", [])),
            schema_version=data.get("schema_version", MEMORY_SCHEMA_VERSION))

    def to_json(self) -> str:
        import json
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "MachineMemory":
        import json
        return cls.from_dict(json.loads(text))
