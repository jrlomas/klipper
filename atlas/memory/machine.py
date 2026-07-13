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

import json
import os
import secrets
import tempfile
import time

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
        for existing in self.diagnoses:
            if (existing.get("case_hash") == case_hash
                    and existing.get("matched", "") == matched):
                existing.update(rec)
                return existing
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
        version = data.get("schema_version", MEMORY_SCHEMA_VERSION)
        if version != MEMORY_SCHEMA_VERSION:
            raise ValueError("unsupported machine memory schema_version %r"
                             % version)
        return cls(
            machine_id=data["machine_id"],
            created=data.get("created", ""),
            quirks=list(data.get("quirks", [])),
            baselines=dict(data.get("baselines", {})),
            changes=list(data.get("changes", [])),
            diagnoses=list(data.get("diagnoses", [])),
            schema_version=version)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "MachineMemory":
        return cls.from_dict(json.loads(text))


class MachineMemoryStore:
    """Atomic, mode-private owner for one machine's grounding memory."""

    def __init__(self, path, wall_clock=time.time):
        self.path = os.path.abspath(os.path.expanduser(path))
        self.clock = wall_clock
        self.memory = self._load_or_create()

    def _load_or_create(self):
        try:
            with open(self.path, encoding="utf-8") as handle:
                memory = MachineMemory.from_json(handle.read())
            os.chmod(self.path, 0o600)
        except FileNotFoundError:
            memory = MachineMemory(
                machine_id="machine-" + secrets.token_hex(16),
                created=str(self.clock()))
            self.save(memory)
        return memory

    def save(self, memory=None):
        memory = memory or getattr(self, "memory", None)
        if memory is None:
            raise ValueError("memory is required")
        directory = os.path.dirname(self.path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".atlas-memory-", dir=directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(memory.to_json())
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
            dirfd = os.open(directory, os.O_DIRECTORY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    def record_diagnosis(self, diagnosis):
        best = diagnosis.best
        if best is not None:
            case_hash = "pattern:%s" % best.pattern_id
            summary = best.cause
            matched = best.pattern_id
        elif diagnosis.case is not None:
            case_hash = diagnosis.case.case_hash
            summary = diagnosis.case.summary
            matched = ""
        else:
            return False
        before = json.dumps(self.memory.diagnoses, sort_keys=True)
        self.memory.record_diagnosis(case_hash, summary, matched)
        changed = json.dumps(self.memory.diagnoses, sort_keys=True) != before
        if changed:
            self.save()
        return changed

    def sync_baselines(self, baselines):
        value = json.loads(json.dumps(baselines, sort_keys=True))
        if self.memory.baselines.get("monitor") == value:
            return False
        self.memory.set_baseline("monitor", value)
        self.save()
        return True
