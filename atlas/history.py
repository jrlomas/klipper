# Durable, bounded incident history for Atlas.

import json
import os
import sqlite3
import time


class IncidentStore:
    def __init__(self, path, max_incidents=500, max_age_days=90,
                 wall_clock=time.time):
        self.path = os.path.abspath(os.path.expanduser(path))
        self.max_incidents = max_incidents
        self.max_age_days = max_age_days
        self.clock = wall_clock
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS incidents (
            incident_key TEXT PRIMARY KEY, first_seen REAL NOT NULL,
            last_seen REAL NOT NULL, observations INTEGER NOT NULL,
            matched_pattern TEXT NOT NULL, summary TEXT NOT NULL,
            payload TEXT NOT NULL)""")
        self.db.commit()

    def record(self, diagnosis):
        best = diagnosis.best
        if best is not None:
            key = "pattern:%s" % best.pattern_id
            pattern = best.pattern_id
            summary = best.cause
        elif diagnosis.case is not None:
            key = "case:%s" % diagnosis.case.case_hash
            pattern = ""
            summary = diagnosis.case.summary
        else:
            return None
        now = self.clock()
        payload = json.dumps({
            "matched": best is not None,
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

    def prune(self, commit=True):
        cutoff = self.clock() - self.max_age_days * 86400
        self.db.execute("DELETE FROM incidents WHERE last_seen < ?", (cutoff,))
        self.db.execute("""DELETE FROM incidents WHERE incident_key IN (
            SELECT incident_key FROM incidents ORDER BY last_seen DESC
            LIMIT -1 OFFSET ?)""", (self.max_incidents,))
        if commit:
            self.db.commit()

    def recent(self, limit=20):
        rows = self.db.execute("""SELECT incident_key, first_seen, last_seen,
            observations, matched_pattern, summary FROM incidents
            ORDER BY last_seen DESC LIMIT ?""", (limit,)).fetchall()
        return [{
            "incident_key": row[0], "first_seen": row[1],
            "last_seen": row[2], "observations": row[3],
            "matched_pattern": row[4], "summary": row[5],
        } for row in rows]

    def __len__(self):
        return self.db.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]

    def close(self):
        self.db.close()
