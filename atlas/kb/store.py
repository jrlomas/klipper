# Consent-gated KB outbox, feedback ledger, and signed catalog activation.

import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import tarfile
import tempfile
import time

from ..provision.execute import verify_detached


class ConsentError(RuntimeError):
    pass


class KnowledgeOutbox:
    def __init__(self, path, wall_clock=time.time):
        self.path = os.path.abspath(os.path.expanduser(path))
        self.clock = wall_clock
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.db = sqlite3.connect(self.path)
        os.chmod(self.path, 0o600)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS consent (
              token_hash TEXT PRIMARY KEY, content_hash TEXT NOT NULL,
              granted_at REAL NOT NULL, expires_at REAL NOT NULL,
              used_at REAL);
            CREATE TABLE IF NOT EXISTS submissions (
              content_hash TEXT PRIMARY KEY, payload TEXT NOT NULL,
              status TEXT NOT NULL, observations INTEGER NOT NULL,
              queued_at REAL NOT NULL, sent_at REAL);
            CREATE TABLE IF NOT EXISTS feedback (
              seq INTEGER PRIMARY KEY AUTOINCREMENT, content_hash TEXT NOT NULL,
              diagnosis_match INTEGER, fix_worked INTEGER, recorded_at REAL NOT NULL);
        """)
        self.db.commit()

    @staticmethod
    def _token_hash(token):
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def grant(self, bundle, ttl=900):
        if not bundle.redacted:
            raise ConsentError("only redacted bundles can receive consent")
        token = secrets.token_urlsafe(32)
        now = self.clock()
        self.db.execute(
            "INSERT INTO consent VALUES (?, ?, ?, ?, NULL)",
            (self._token_hash(token), bundle.content_hash, now, now + ttl))
        self.db.commit()
        return token

    def enqueue(self, bundle, token):
        if not bundle.redacted:
            raise ConsentError("unredacted bundle refused")
        now = self.clock()
        row = self.db.execute("""SELECT content_hash, expires_at, used_at
            FROM consent WHERE token_hash=?""",
            (self._token_hash(token),)).fetchone()
        if row is None or row[0] != bundle.content_hash:
            raise ConsentError("consent does not match this incident")
        if row[2] is not None or row[1] < now:
            raise ConsentError("consent is expired or already used")
        payload = json.dumps(bundle.to_dict(), sort_keys=True)
        self.db.execute(
            "UPDATE consent SET used_at=? WHERE token_hash=?",
            (now, self._token_hash(token)))
        self.db.execute("""INSERT INTO submissions VALUES
            (?, ?, 'queued', 1, ?, NULL)
            ON CONFLICT(content_hash) DO UPDATE SET
              observations=submissions.observations+1""",
            (bundle.content_hash, payload, now))
        self.db.commit()
        return bundle.content_hash

    def queued(self, limit=20):
        rows = self.db.execute("""SELECT content_hash, payload, observations,
            queued_at FROM submissions WHERE status='queued'
            ORDER BY queued_at LIMIT ?""", (limit,)).fetchall()
        return [{"content_hash": row[0], "payload": json.loads(row[1]),
                 "observations": row[2], "queued_at": row[3]} for row in rows]

    def mark_sent(self, content_hash):
        self.db.execute("""UPDATE submissions SET status='sent', sent_at=?
            WHERE content_hash=? AND status='queued'""",
            (self.clock(), content_hash))
        self.db.commit()

    def record_feedback(self, content_hash, diagnosis_match=None,
                        fix_worked=None):
        if diagnosis_match not in (None, True, False):
            raise ValueError("diagnosis_match must be boolean or null")
        if fix_worked not in (None, True, False):
            raise ValueError("fix_worked must be boolean or null")
        self.db.execute("INSERT INTO feedback VALUES (NULL, ?, ?, ?, ?)",
                        (content_hash, diagnosis_match, fix_worked, self.clock()))
        self.db.commit()

    def close(self):
        self.db.close()


class SignedCatalogInstaller:
    def __init__(self, destination, public_key=None, verifier=None):
        self.destination = os.path.abspath(os.path.expanduser(destination))
        self.public_key = public_key
        self.verifier = verifier

    def _verify(self, archive, signature):
        if self.verifier is not None:
            return self.verifier(archive, signature)
        return bool(self.public_key and verify_detached(
            archive, self.public_key, signature))

    def install(self, archive, signature):
        archive = os.path.abspath(archive)
        if not self._verify(archive, signature):
            raise ValueError("catalog signature verification failed")
        parent = os.path.dirname(self.destination)
        os.makedirs(parent, exist_ok=True)
        staging = tempfile.mkdtemp(prefix=".atlas-kb-", dir=parent)
        backup = self.destination + ".previous"
        displaced = False
        try:
            with tarfile.open(archive, "r:gz") as tar:
                root = os.path.realpath(staging) + os.sep
                for member in tar.getmembers():
                    if member.issym() or member.islnk():
                        raise ValueError("catalog archive contains a link")
                    target = os.path.realpath(os.path.join(staging, member.name))
                    if not target.startswith(root):
                        raise ValueError("catalog archive contains unsafe path")
                tar.extractall(staging)
            manifest_path = os.path.join(staging, "manifest.json")
            with open(manifest_path) as handle:
                manifest = json.load(handle)
            if manifest.get("schema_version") != 1 or not manifest.get("version"):
                raise ValueError("invalid catalog manifest")
            if os.path.exists(backup):
                shutil.rmtree(backup)
            if os.path.exists(self.destination):
                os.replace(self.destination, backup)
                displaced = True
            os.replace(staging, self.destination)
            return manifest
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            if (displaced and not os.path.exists(self.destination)
                    and os.path.exists(backup)):
                os.replace(backup, self.destination)
            raise

    def rollback(self):
        backup = self.destination + ".previous"
        if not os.path.isdir(backup):
            raise ValueError("no previous catalog")
        failed = self.destination + ".failed"
        if os.path.exists(failed):
            shutil.rmtree(failed)
        if os.path.exists(self.destination):
            os.replace(self.destination, failed)
        os.replace(backup, self.destination)
