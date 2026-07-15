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
from .history import IncidentStore
from .incidents import IncidentCapture, DEFAULT_SETTLE_SECONDS
from .ipc import AssistantUnixServer
from .monitor import BaselineMonitor
from .observe import StructuredTail
from .timeline import Event, Timeline
from .view import LiveTail, TimelineFilter


STATUS_SCHEMA_VERSION = 1
DEFAULT_MAX_EVENTS = 2000
DEFAULT_HEARTBEAT = 5.0


def _current_session(timeline):
    """Return the current printer session for active diagnosis.

    Historical failures remain in the durable incident archive, but a
    successful Klipper restart must clear the panel's active diagnosis.
    """
    events = list(timeline.events)
    start = 0
    for index, event in enumerate(events):
        if event.kind == "session_start":
            start = index
    current = Timeline()
    current.anchor = dict(timeline.anchor) if timeline.anchor else None
    current.notes = list(timeline.notes)
    current.versions = dict(timeline.versions)
    for event in events[start:]:
        current.add(event)
    return current


def _event_dict(timeline, event) -> dict:
    """The public event shape consumed by the Mainsail Atlas adapter."""
    wall_time = timeline.wall_time_of_event(event)
    return {
        "seq": event.seq,
        "kind": event.kind,
        "source": event.source,
        "severity": event.severity,
        "summary": event.summary,
        "mtime": event.mtime,
        "wall_time": wall_time,
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


def build_status(timeline, diagnosis, service: dict, incidents=None,
                 monitor=None, assistant=None, occurrences=None) -> dict:
    """Build the stable daemon -> Moonraker -> Mainsail contract."""
    return {
        "schema_version": STATUS_SCHEMA_VERSION,
        "timeline": {
            "events": [_event_dict(timeline, event)
                       for event in timeline.ordered()],
            "notes": list(timeline.notes),
            "versions": dict(timeline.versions),
        },
        "diagnosis": _diagnosis_dict(diagnosis),
        "service": dict(service),
        "incidents": list(incidents or []),
        "occurrences": list(occurrences or []),
        "monitor": dict(monitor or {}),
        "assistant": dict(assistant or {"enabled": False}),
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
                 wall_clock=None, telemetry_paths=None, history_path=None,
                 baseline_path=None, assistant=None, assistant_socket=None,
                 memory_store=None, incident_dir=None,
                 incident_settle=DEFAULT_SETTLE_SECONDS,
                 printer_config=None, gcode_dir=None, repo_root=None):
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
        self.telemetry = [StructuredTail(path, self.follower.timeline)
                          for path in (telemetry_paths or [])]
        self.history = (IncidentStore(
            history_path, wall_clock=wall_clock or time.time,
            archive_dir=incident_dir)
                        if history_path else None)
        self.capture = (IncidentCapture(
            self.history, settle_seconds=incident_settle,
            wall_clock=wall_clock or time.time,
            config_path=printer_config, gcode_dir=gcode_dir,
            repo_root=repo_root) if self.history is not None else None)
        self.monitor = BaselineMonitor(baseline_path) if baseline_path else None
        self.memory_store = memory_store
        self.assistant = assistant
        self.assistant_socket = (os.path.abspath(os.path.expanduser(
            assistant_socket)) if assistant_socket else None)
        if self.assistant is not None and self.assistant_socket is None:
            raise ValueError("assistant_socket is required with assistant")
        self._assistant_server = None
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
        self._memory_error = ""
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
        if getattr(self, "assistant", None) is not None:
            self.assistant.update_grounding(self.patterns)
        return True

    def _error_text(self) -> str:
        return "; ".join(error for error in
                         (self._catalog_error, self._source_error,
                          self._memory_error) if error)

    def _service_status(self) -> dict:
        available = (self.follower.source_available
                     or any(t.source_available for t in self.telemetry))
        state = "running" if available else "waiting"
        error = self._error_text()
        if error:
            state = "degraded"
        return {
            "state": state,
            "generation": self._generation,
            "updated_at": self._clock(),
            "source": "klippy.log",
            "structured_sources": [tail.path for tail in self.telemetry],
            "event_count": len(self.follower.timeline),
            "pattern_count": len(self.patterns),
            "rotations": (self.follower.rotations
                          + sum(t.rotations for t in self.telemetry)),
            "incident_count": (len(self.history)
                               if self.history is not None else 0),
            "incident_occurrences": (
                self.history.occurrence_count()
                if self.history is not None else 0),
            "incident_pending": bool(
                self.capture and self.capture.pending is not None),
            "last_error": error,
        }

    def _record_captures_in_memory(self, captured) -> bool:
        if self.memory_store is None:
            return False
        changed = False
        for item in captured:
            if not item or not item.get("inserted", True):
                continue
            record_occurrence = getattr(
                self.memory_store, "record_incident_occurrence", None)
            if record_occurrence is None:
                item_changed = self.memory_store.record_diagnosis(
                    item["diagnosis"])
            else:
                item_changed = record_occurrence(
                    item["diagnosis"], item["occurrence_id"],
                    item["bundle"]["occurred_at"])
            changed = item_changed or changed
        return changed

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
        for tail in self.telemetry:
            try:
                new_events.extend(tail.poll())
            except OSError as exc:
                self._source_error = "structured read failed: %s" % exc
            if tail.last_error:
                self._source_error = tail.last_error
        if self.follower.max_events is not None:
            overflow = (len(self.follower.timeline.events)
                        - self.follower.max_events)
            if overflow > 0:
                del self.follower.timeline.events[:overflow]
                self.follower.timeline.note(
                    "live timeline is bounded to the latest %d events"
                    % self.follower.max_events)
        monitor_alerts = []
        if self.monitor is not None and new_events:
            monitor_alerts = self.monitor.observe(new_events)
            for alert in monitor_alerts:
                event = Event(
                    seq=self.follower.timeline.allocate_seq(), kind="anomaly",
                    source=alert["source"], severity="warning",
                    summary=("%s drifted from %.3f to %.3f" % (
                        alert["metric"], alert["baseline"], alert["value"])),
                    mtime=alert["mtime"], time_basis="machine", t_exact=True,
                    fields=alert)
                self.follower.timeline.add(event)
                new_events.append(event)
        error_changed = self._error_text() != previous_error
        rotations = (self.follower.rotations
                     + sum(t.rotations for t in self.telemetry))
        source_available = (self.follower.source_available
                            or any(t.source_available for t in self.telemetry))
        rotated = rotations != self._last_rotations
        source_changed = source_available != self._last_source_available
        now = self._clock()
        captured = (self.capture.observe(
            new_events, self.follower.timeline, self.patterns)
                    if self.capture is not None else [])
        heartbeat_due = (self._last_publish_at is not None
                         and now - self._last_publish_at >= self.heartbeat)
        changed = (force or self._last_state is None or bool(new_events)
                   or heartbeat_due or bool(captured))
        changed = (changed or catalog_changed or rotated or source_changed
                   or error_changed)
        if not changed:
            return self._last_state

        self._last_rotations = rotations
        self._last_source_available = source_available
        self._generation += 1
        diagnosis = Matcher(self.patterns).diagnose(
            _current_session(self.follower.timeline))
        if self.memory_store is not None and (new_events or captured):
            try:
                memory_changed = self._record_captures_in_memory(captured)
                if self.monitor is not None:
                    memory_changed = self.memory_store.sync_baselines(
                        self.monitor.stats) or memory_changed
                if memory_changed and self.assistant is not None:
                    self.assistant.update_grounding(
                        self.patterns, self.memory_store.memory)
                self._memory_error = ""
            except Exception as exc:
                self._memory_error = "machine memory update failed: %s" % exc
        incidents = (self.history.recent()
                     if self.history is not None else [])
        occurrences = (self.history.recent_occurrences()
                       if self.history is not None else [])
        monitor_state = {
            "enabled": self.monitor is not None,
            "metric_count": len(self.monitor.stats) if self.monitor else 0,
            "alerts": monitor_alerts,
        }
        assistant_state = (self.assistant.status()
                           if self.assistant is not None
                           else {"enabled": False})
        state = build_status(self.follower.timeline, diagnosis,
                             self._service_status(), incidents, monitor_state,
                             assistant_state, occurrences=occurrences)
        self.publisher.publish(state)
        self._last_state = state
        self._last_publish_at = now
        return state

    async def serve(self, stop_event=None) -> None:
        stop_event = stop_event or asyncio.Event()
        if self.assistant is not None:
            def handle(operation, params):
                # Inference runs in a worker thread and may take minutes on
                # CPU.  Give it a stable view instead of sharing the live
                # list that the polling loop continues to append and bound.
                timeline = Timeline()
                timeline.events = list(self.follower.timeline.events)
                timeline.notes = list(self.follower.timeline.notes)
                timeline.versions = dict(self.follower.timeline.versions)
                timeline.anchor = (dict(self.follower.timeline.anchor)
                                   if self.follower.timeline.anchor else None)
                return self.assistant.handle(
                    operation, params, timeline)
            self._assistant_server = AssistantUnixServer(
                self.assistant_socket, handle)
            await self._assistant_server.start()
        try:
            while not stop_event.is_set():
                self.poll_once()
                try:
                    await asyncio.wait_for(stop_event.wait(), self.interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            if self._assistant_server is not None:
                await self._assistant_server.close()
                self._assistant_server = None

    def close(self) -> None:
        if self.capture is not None:
            item = self.capture.flush(self.follower.timeline, self.patterns)
            try:
                self._record_captures_in_memory([item])
            except Exception as exc:
                # The occurrence archive is authoritative. A memory refresh
                # failure during process teardown must not skip closing it.
                self._memory_error = (
                    "machine memory update failed during close: %s" % exc)
        if self.history is not None:
            self.history.close()
