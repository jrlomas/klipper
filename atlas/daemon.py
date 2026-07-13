# Atlas always-on companion service (FD-0002 §3, §4, §7).
#
# The daemon owns the merged timeline store and deterministic diagnosis.  It
# publishes a small, versioned JSON snapshot for Moonraker to expose and
# Mainsail to render; the API layer remains plumbing and never recomputes facts.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import asyncio
import json
import os
import tempfile
import time

from .diagnosis import Matcher, load_catalog
from .view import LiveTail, TimelineFilter


STATUS_SCHEMA_VERSION = 1
DEFAULT_MAX_EVENTS = 2000
DEFAULT_HEARTBEAT = 5.0


def _event_dict(event) -> dict:
    """The public event shape consumed by the Mainsail Atlas adapter."""
    return {
        "seq": event.seq,
        "kind": event.kind,
        "source": event.source,
        "severity": event.severity,
        "summary": event.summary,
        "mtime": event.mtime,
        "time_basis": event.time_basis,
        "t_exact": event.t_exact,
        "fields": dict(event.fields),
    }


def _diagnosis_dict(diagnosis) -> dict:
    matches = [{
        "pattern_id": match.pattern_id,
        "confidence": match.confidence,
        "cause": match.cause,
        "fix": match.fix,
        "provenance": match.provenance,
        "matched_seqs": list(match.matched_seqs),
    } for match in diagnosis.matches]
    case = diagnosis.case
    return {
        "matched": bool(matches),
        "matches": matches,
        "case": None if case is None else {
            "case_hash": case.case_hash,
            "summary": case.summary,
            "note": case.note,
        },
        "notes": list(diagnosis.notes),
    }


def build_status(timeline, diagnosis, service: dict) -> dict:
    """Build the stable daemon -> Moonraker -> Mainsail contract."""
    return {
        "schema_version": STATUS_SCHEMA_VERSION,
        "timeline": {
            "events": [_event_dict(event) for event in timeline.ordered()],
            "notes": list(timeline.notes),
            "versions": dict(timeline.versions),
        },
        "diagnosis": _diagnosis_dict(diagnosis),
        "service": dict(service),
    }


class AtomicStatePublisher:
    """Write a complete snapshot with rename atomicity for API readers."""

    def __init__(self, path: str):
        self.path = os.path.abspath(os.path.expanduser(path))

    def publish(self, state: dict) -> None:
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".atlas-state-", suffix=".tmp",
                                   dir=directory)
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(state, fh, indent=2, sort_keys=True)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise


class AtlasDaemon:
    """Continuously decode one klippy.log into published Atlas state."""

    def __init__(self, log_path: str, state_path: str, catalog_path: str,
                 interval: float = 0.5, max_events: int = DEFAULT_MAX_EVENTS,
                 heartbeat: float = DEFAULT_HEARTBEAT, patterns=None,
                 wall_clock=None):
        if interval <= 0:
            raise ValueError("interval must be positive")
        if heartbeat <= 0:
            raise ValueError("heartbeat must be positive")
        self.log_path = os.path.abspath(os.path.expanduser(log_path))
        self.catalog_path = os.path.abspath(os.path.expanduser(catalog_path))
        self.interval = interval
        self.heartbeat = heartbeat
        self.follower = LiveTail(
            self.log_path, TimelineFilter(ordered=False),
            max_events=max_events)
        self.publisher = AtomicStatePublisher(state_path)
        self._fixed_patterns = patterns is not None
        self.patterns = list(patterns or [])
        self._catalog_signature = None
        self._clock = wall_clock or time.time
        self._last_state = None
        self._last_publish_at = None
        self._generation = 0
        self._catalog_error = ""
        self._source_error = ""
        self._last_rotations = 0
        self._last_source_available = False
        if not self._fixed_patterns:
            self._reload_catalog(force=True)

    def _catalog_files(self) -> tuple:
        if not os.path.isdir(self.catalog_path):
            return ()
        names = sorted(name for name in os.listdir(self.catalog_path)
                       if name.endswith((".yaml", ".yml")))
        signature = []
        for name in names:
            path = os.path.join(self.catalog_path, name)
            st = os.stat(path)
            signature.append((name, st.st_mtime_ns, st.st_size))
        return tuple(signature)

    def _reload_catalog(self, force=False) -> bool:
        try:
            signature = self._catalog_files()
            if not force and signature == self._catalog_signature:
                return False
            patterns = load_catalog(self.catalog_path)
        except Exception as exc:
            # A malformed update must not take down live observation.  Keep
            # the last known-good catalog and expose the degraded state.
            error = "catalog reload failed: %s" % exc
            changed = error != self._catalog_error
            self._catalog_error = error
            return changed
        self.patterns = patterns
        self._catalog_signature = signature
        self._catalog_error = ""
        return True

    def _error_text(self) -> str:
        return "; ".join(error for error in
                         (self._catalog_error, self._source_error) if error)

    def _service_status(self) -> dict:
        state = "running" if self.follower.source_available else "waiting"
        error = self._error_text()
        if error:
            state = "degraded"
        return {
            "state": state,
            "generation": self._generation,
            "updated_at": self._clock(),
            "source": "klippy.log",
            "event_count": len(self.follower.timeline),
            "pattern_count": len(self.patterns),
            "rotations": self.follower.rotations,
            "last_error": error,
        }

    def poll_once(self, force=False) -> dict:
        catalog_changed = (False if self._fixed_patterns
                           else self._reload_catalog())
        previous_error = self._error_text()
        try:
            new_events = self.follower.poll()
        except OSError as exc:
            # Observation remains alive with its last good facts.  A
            # permissions or transient filesystem failure is state to expose,
            # not a reason to discard the timeline or crash the service.
            self._source_error = "log read failed: %s" % exc
            new_events = []
        else:
            self._source_error = ""
        error_changed = self._error_text() != previous_error
        rotated = self.follower.rotations != self._last_rotations
        source_changed = (self.follower.source_available
                          != self._last_source_available)
        now = self._clock()
        heartbeat_due = (self._last_publish_at is not None
                         and now - self._last_publish_at >= self.heartbeat)
        changed = (force or self._last_state is None or bool(new_events)
                   or heartbeat_due)
        changed = (changed or catalog_changed or rotated or source_changed
                   or error_changed)
        if not changed:
            return self._last_state

        self._last_rotations = self.follower.rotations
        self._last_source_available = self.follower.source_available
        self._generation += 1
        diagnosis = Matcher(self.patterns).diagnose(self.follower.timeline)
        state = build_status(self.follower.timeline, diagnosis,
                             self._service_status())
        self.publisher.publish(state)
        self._last_state = state
        self._last_publish_at = now
        return state

    async def serve(self, stop_event=None) -> None:
        stop_event = stop_event or asyncio.Event()
        while not stop_event.is_set():
            self.poll_once()
            try:
                await asyncio.wait_for(stop_event.wait(), self.interval)
            except asyncio.TimeoutError:
                pass
