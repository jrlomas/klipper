# Durable, bounded incident history for Atlas.

import json
import os
import sqlite3
import tempfile
import time


class IncidentStore:
    def __init__(self, path, max_incidents=500, max_age_days=90,
                 wall_clock=time.time, archive_dir=None,
                 max_occurrences=1000):
        if max_incidents < 1 or max_occurrences < 1:
            raise ValueError("incident retention limits must be positive")
        if max_age_days <= 0:
            raise ValueError("max_age_days must be positive")
        self.path = os.path.abspath(os.path.expanduser(path))
        self.max_incidents = max_incidents
        self.max_age_days = max_age_days
        self.max_occurrences = max_occurrences
        self.clock = wall_clock
        self.archive_dir = os.path.abspath(os.path.expanduser(
            archive_dir or os.path.join(os.path.dirname(self.path),
                                        "incidents")))
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        os.makedirs(self.archive_dir, mode=0o700, exist_ok=True)
        os.chmod(self.archive_dir, 0o700)
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS incidents (
            incident_key TEXT PRIMARY KEY, first_seen REAL NOT NULL,
            last_seen REAL NOT NULL, observations INTEGER NOT NULL,
            matched_pattern TEXT NOT NULL, summary TEXT NOT NULL,
            payload TEXT NOT NULL)""")
        self._add_column("incidents", "latest_occurrence", "TEXT", "''")
        self._add_column("incidents", "last_trigger_kind", "TEXT", "''")
        self._add_column("incidents", "last_trigger_source", "TEXT", "''")
        self.db.execute("""CREATE TABLE IF NOT EXISTS occurrences (
            occurrence_id TEXT PRIMARY KEY, incident_key TEXT NOT NULL,
            occurred_at REAL NOT NULL, trigger_kind TEXT NOT NULL,
            trigger_source TEXT NOT NULL, severity TEXT NOT NULL,
            summary TEXT NOT NULL, bundle_name TEXT NOT NULL,
            bundle_sha256 TEXT NOT NULL, payload TEXT NOT NULL)""")
        self.db.execute("""CREATE INDEX IF NOT EXISTS
            occurrence_time_idx ON occurrences(occurred_at DESC)""")
        self.db.commit()

    def _add_column(self, table, name, decl, default):
        columns = {row[1] for row in self.db.execute(
            "PRAGMA table_info(%s)" % table)}
        if name not in columns:
            self.db.execute(
                "ALTER TABLE %s ADD COLUMN %s %s NOT NULL DEFAULT %s"
                % (table, name, decl, default))

    @staticmethod
    def _identity(diagnosis):
        best = diagnosis.best
        if best is not None:
            return ("pattern:%s" % best.pattern_id, best.pattern_id,
                    best.cause)
        if diagnosis.case is not None:
            return ("case:%s" % diagnosis.case.case_hash, "",
                    diagnosis.case.summary)
        return (None, "", "")

    def record(self, diagnosis):
        key, pattern, summary = self._identity(diagnosis)
        if key is None:
            return None
        now = self.clock()
        payload = json.dumps({
            "matched": bool(pattern),
            "pattern_id": pattern,
            "summary": summary,
        }, sort_keys=True)
        self.db.execute("""INSERT INTO incidents
            (incident_key, first_seen, last_seen, observations,
             matched_pattern, summary, payload) VALUES (?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(incident_key) DO UPDATE SET
              last_seen=excluded.last_seen,
              observations=incidents.observations+1,
              payload=excluded.payload""",
            (key, now, now, pattern, summary, payload))
        self.prune(commit=False)
        self.db.commit()
        return key

    def record_occurrence(self, diagnosis, bundle):
        """Atomically retain one private, bounded incident occurrence."""
        import hashlib

        key, pattern, summary = self._identity(diagnosis)
        if key is None:
            return None
        payload = json.loads(json.dumps(bundle, sort_keys=True))
        trigger = payload.get("trigger", {})
        occurred_at = float(payload.get("occurred_at", self.clock()))
        # The id describes the physical occurrence, not the time Atlas
        # happened to process it.  Replaying the same log after a daemon
        # restart therefore does not manufacture another occurrence.
        occurrence_seed = "%s|%.6f|%s|%s|%s" % (
            key, occurred_at, trigger.get("kind", ""),
            trigger.get("source", ""), trigger.get("mtime", ""))
        occurrence_id = hashlib.sha256(
            occurrence_seed.encode("utf-8")).hexdigest()[:24]
        bundle_name = "%s.json" % occurrence_id
        payload["occurrence_id"] = occurrence_id
        payload["incident_key"] = key
        canonical = json.dumps(
            payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        bundle_sha256 = hashlib.sha256(canonical).hexdigest()

        exists = self.db.execute(
            "SELECT 1 FROM occurrences WHERE occurrence_id=?",
            (occurrence_id,)).fetchone()
        if exists:
            return occurrence_id
        self._write_bundle(bundle_name, canonical)
        public = json.dumps({
            "occurrence_id": occurrence_id,
            "incident_key": key,
            "occurred_at": occurred_at,
            "trigger_kind": trigger.get("kind", ""),
            "trigger_source": trigger.get("source", ""),
            "severity": trigger.get("severity", "error"),
            "summary": summary,
            "bundle_sha256": bundle_sha256,
        }, sort_keys=True)
        try:
            self.db.execute("BEGIN")
            self.db.execute("""INSERT INTO occurrences
                (occurrence_id, incident_key, occurred_at, trigger_kind,
                 trigger_source, severity, summary, bundle_name,
                 bundle_sha256, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
                    occurrence_id, key, occurred_at,
                    trigger.get("kind", ""), trigger.get("source", ""),
                    trigger.get("severity", "error"), summary, bundle_name,
                    bundle_sha256, public))
            self.db.execute("""INSERT INTO incidents
                (incident_key, first_seen, last_seen, observations,
                 matched_pattern, summary, payload, latest_occurrence,
                 last_trigger_kind, last_trigger_source)
                VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(incident_key) DO UPDATE SET
                  last_seen=excluded.last_seen,
                  observations=incidents.observations+1,
                  payload=excluded.payload,
                  latest_occurrence=excluded.latest_occurrence,
                  last_trigger_kind=excluded.last_trigger_kind,
                  last_trigger_source=excluded.last_trigger_source""", (
                    key, occurred_at, occurred_at, pattern, summary, public,
                    occurrence_id, trigger.get("kind", ""),
                    trigger.get("source", "")))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            try:
                os.unlink(os.path.join(self.archive_dir, bundle_name))
            except FileNotFoundError:
                pass
            raise
        self.prune()
        return occurrence_id

    def _write_bundle(self, name, data):
        fd, tmp = tempfile.mkstemp(prefix=".atlas-incident-",
                                   dir=self.archive_dir)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, os.path.join(self.archive_dir, name))
            dirfd = os.open(self.archive_dir, os.O_DIRECTORY)
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

    def prune(self, commit=True):
        cutoff = self.clock() - self.max_age_days * 86400
        stale = self.db.execute("""SELECT bundle_name FROM occurrences
            WHERE occurred_at < ? OR occurrence_id IN (
              SELECT occurrence_id FROM occurrences ORDER BY occurred_at DESC
              LIMIT -1 OFFSET ?)""",
            (cutoff, self.max_occurrences)).fetchall()
        self.db.execute("""DELETE FROM occurrences
            WHERE occurred_at < ? OR occurrence_id IN (
              SELECT occurrence_id FROM occurrences ORDER BY occurred_at DESC
              LIMIT -1 OFFSET ?)""", (cutoff, self.max_occurrences))
        self.db.execute("DELETE FROM incidents WHERE last_seen < ?", (cutoff,))
        self.db.execute("""DELETE FROM incidents WHERE incident_key IN (
            SELECT incident_key FROM incidents ORDER BY last_seen DESC
            LIMIT -1 OFFSET ?)""", (self.max_incidents,))
        if commit:
            self.db.commit()
            for row in stale:
                try:
                    os.unlink(os.path.join(self.archive_dir, row[0]))
                except FileNotFoundError:
                    pass

    def recent(self, limit=20):
        rows = self.db.execute("""SELECT incident_key, first_seen, last_seen,
            observations, matched_pattern, summary, latest_occurrence,
            last_trigger_kind, last_trigger_source FROM incidents
            ORDER BY last_seen DESC LIMIT ?""", (limit,)).fetchall()
        return [{
            "incident_key": row[0], "first_seen": row[1],
            "last_seen": row[2], "observations": row[3],
            "matched_pattern": row[4], "summary": row[5],
            "latest_occurrence_id": row[6], "last_trigger_kind": row[7],
            "last_trigger_source": row[8],
        } for row in rows]

    def recent_occurrences(self, limit=20):
        rows = self.db.execute("""SELECT occurrence_id, incident_key,
            occurred_at, trigger_kind, trigger_source, severity, summary,
            bundle_sha256 FROM occurrences
            ORDER BY occurred_at DESC LIMIT ?""", (limit,)).fetchall()
        return [{
            "occurrence_id": row[0], "incident_key": row[1],
            "occurred_at": row[2], "trigger_kind": row[3],
            "trigger_source": row[4], "severity": row[5],
            "summary": row[6], "bundle_sha256": row[7],
        } for row in rows]

    def occurrence_count(self):
        return self.db.execute(
            "SELECT COUNT(*) FROM occurrences").fetchone()[0]

    def get_occurrence(self, occurrence_id):
        row = self.db.execute(
            "SELECT bundle_name FROM occurrences WHERE occurrence_id=?",
            (occurrence_id,)).fetchone()
        if row is None:
            return None
        with open(os.path.join(self.archive_dir, row[0]),
                  encoding="utf-8") as handle:
            return json.load(handle)

    def __len__(self):
        return self.db.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]

    def close(self):
        self.db.close()
