# Real, durable configuration apply/undo boundary for Atlas.

import fcntl
import os
import sqlite3
import tempfile
import time

from .pipeline import ApplyPipeline, Proposal


class StaleConfigError(RuntimeError):
    pass


class PersistentApplyPipeline:
    """Compare-and-swap config writes with persistent restart-safe undo."""

    def __init__(self, config_path, journal_path, reload_callback=None,
                 wall_clock=time.time):
        self.config_path = os.path.abspath(os.path.expanduser(config_path))
        self.journal_path = os.path.abspath(os.path.expanduser(journal_path))
        self.reload_callback = reload_callback
        self.clock = wall_clock
        os.makedirs(os.path.dirname(self.journal_path), exist_ok=True)
        self.db = sqlite3.connect(self.journal_path)
        os.chmod(self.journal_path, 0o600)
        self.db.execute("""CREATE TABLE IF NOT EXISTS changes (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, applied_at REAL NOT NULL,
            tier INTEGER NOT NULL, action TEXT NOT NULL, before_text TEXT NOT NULL,
            after_text TEXT NOT NULL, rationale TEXT NOT NULL, source TEXT NOT NULL,
            reverted INTEGER NOT NULL DEFAULT 0, reverted_at REAL)""")
        self.db.commit()
        self.lock_path = self.config_path + ".atlas.lock"

    def _atomic_write(self, text):
        directory = os.path.dirname(self.config_path)
        mode = os.stat(self.config_path).st_mode & 0o777
        fd, tmp = tempfile.mkstemp(prefix=".atlas-config-", dir=directory)
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "w") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.config_path)
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

    def _reload(self):
        if self.reload_callback is not None:
            self.reload_callback()

    def apply(self, proposal: Proposal, confirmed=False):
        with open(self.lock_path, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with open(self.config_path) as handle:
                current = handle.read()
            if current != proposal.before:
                raise StaleConfigError(
                    "config changed since the proposal was drafted")
            pipeline = ApplyPipeline()
            result = pipeline.process(proposal, confirmed=confirmed)
            if not result.applied:
                return result
            self._atomic_write(proposal.after)
            try:
                self._reload()
            except Exception:
                self._atomic_write(proposal.before)
                raise
            self.db.execute("""INSERT INTO changes
                (applied_at, tier, action, before_text, after_text,
                 rationale, source) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (self.clock(), int(result.tier), result.action,
                 proposal.before, proposal.after, proposal.rationale,
                 proposal.source))
            self.db.commit()
            return result

    def undo(self):
        with open(self.lock_path, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            row = self.db.execute("""SELECT seq, before_text, after_text
                FROM changes WHERE reverted=0 ORDER BY seq DESC LIMIT 1""").fetchone()
            if row is None:
                raise ValueError("nothing to undo")
            seq, before, after = row
            with open(self.config_path) as handle:
                current = handle.read()
            if current != after:
                raise StaleConfigError(
                    "config changed since Atlas applied this journal entry")
            self._atomic_write(before)
            try:
                self._reload()
            except Exception:
                self._atomic_write(after)
                raise
            self.db.execute(
                "UPDATE changes SET reverted=1, reverted_at=? WHERE seq=?",
                (self.clock(), seq))
            self.db.commit()
            return before

    def entries(self):
        rows = self.db.execute("""SELECT seq, applied_at, tier, action,
            rationale, source, reverted, reverted_at FROM changes
            ORDER BY seq""").fetchall()
        keys = ("seq", "applied_at", "tier", "action", "rationale",
                "source", "reverted", "reverted_at")
        return [dict(zip(keys, row)) for row in rows]

    def close(self):
        self.db.close()
