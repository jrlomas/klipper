# Deterministic, bounded Atlas incident capture.
#
# Failures are grouped into physical occurrences, surrounded by a small
# pre/post evidence window, and persisted locally with mode 0600.  Raw log
# text, config contents, filenames, and complete G-code are never archived.
# The model is not involved in detection, grouping, redaction, or retention.

import hashlib
import json
import math
import os
import re
import time

from .diagnosis import Matcher
from .kb import redact_fields
from .timeline import Timeline


INCIDENT_SCHEMA_VERSION = 1
DEFAULT_SETTLE_SECONDS = 2.0
DEFAULT_PRE_EVENTS = 160
DEFAULT_POST_EVENTS = 80
DEFAULT_GCODE_RADIUS = 4096
DEFAULT_GCODE_LINES = 64
MAX_TEXT = 512
MAX_HASH_BYTES = 512 * 1024 * 1024

_PATH = re.compile(r"(?<![A-Za-z0-9_])(?:/[A-Za-z0-9._~+@-]+){2,}")
_URL = re.compile(r"\b(?:https?|ftp)://\S+", re.IGNORECASE)
_EMAIL = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_MAC = re.compile(r"\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b", re.IGNORECASE)
_SECRET = re.compile(
    r"\b(password|passwd|psk|secret|token|api[_-]?key)\s*[:=]\s*\S+",
    re.IGNORECASE)
_GCODE = re.compile(
    r"^(?:N\d+\s+)?[GMT]\d+(?:\s+[A-Z][+-]?(?:\d+(?:\.\d*)?|\.\d+))*$",
    re.IGNORECASE)


def _safe_text(value):
    text = str(value or "")[:MAX_TEXT]
    text = _SECRET.sub(r"\1=<redacted>", text)
    text = _URL.sub("<url>", text)
    text = _EMAIL.sub("<email>", text)
    text = _IPV4.sub("<ip>", text)
    text = _MAC.sub("<mac>", text)
    text = _PATH.sub("<path>", text)
    return text


def _local_event(timeline, event, role):
    fields = redact_fields(event.fields)
    for key in ("reason", "exc_msg"):
        if key in event.fields:
            value = _safe_text(event.fields[key])
            if value:
                fields[key] = value
    return {
        "seq": event.seq,
        "role": role,
        "kind": event.kind,
        "source": _safe_text(event.source),
        "severity": event.severity,
        "summary": _safe_text(event.summary),
        "mtime": event.mtime,
        "wall_time": timeline.wall_time_of_event(event),
        "time_basis": event.time_basis,
        "t_exact": event.t_exact,
        "fields": fields,
    }


def _version_evidence(versions):
    # Version map keys are dynamic MCU/source names, so the export redactor
    # would correctly drop them as unclassified free text.  Shape them into
    # an explicit bounded list of structural source/version pairs instead.
    return [{"source": _safe_text(source), "version": _safe_text(version)}
            for source, version in sorted(versions.items())[:64]]


def _sha256_file(path):
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size > MAX_HASH_BYTES:
        return {"available": True, "bytes": size, "sha256": None,
                "hash_omitted": "size_limit"}
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError:
        return None
    return {"available": True, "bytes": size,
            "sha256": digest.hexdigest()}


def _git_revision(repo_root):
    if not repo_root:
        return None
    git = os.path.join(repo_root, ".git")
    try:
        if os.path.isfile(git):
            with open(git, encoding="utf-8") as handle:
                value = handle.read().strip()
            if not value.startswith("gitdir: "):
                return None
            git = value[8:]
            if not os.path.isabs(git):
                git = os.path.normpath(os.path.join(repo_root, git))
        with open(os.path.join(git, "HEAD"), encoding="utf-8") as handle:
            head = handle.read().strip()
        if head.startswith("ref: "):
            ref = head[5:]
            ref_path = os.path.join(git, *ref.split("/"))
            try:
                with open(ref_path, encoding="utf-8") as handle:
                    head = handle.read().strip()
            except FileNotFoundError:
                with open(os.path.join(git, "packed-refs"),
                          encoding="utf-8") as handle:
                    for line in handle:
                        if line.rstrip().endswith(" " + ref):
                            head = line.split(" ", 1)[0]
                            break
    except OSError:
        return None
    return head if re.fullmatch(r"[0-9a-fA-F]{40}", head) else None


def _latest(events, kind, before_seq):
    found = None
    for event in events:
        if event.seq > before_seq:
            continue
        if event.kind == kind:
            found = event
    return found


def _stats_position(event):
    if event is None:
        return None
    sections = event.fields.get("sections", {})
    for values in sections.values():
        if isinstance(values, dict) and "sd_pos" in values:
            value = values["sd_pos"]
            if isinstance(value, (int, float)) and value >= 0:
                return int(value)
    return None


def _safe_gcode_path(gcode_dir, filename):
    if not gcode_dir or not filename or os.path.basename(filename) != filename:
        return None
    root = os.path.realpath(gcode_dir)
    candidate = os.path.realpath(os.path.join(root, filename))
    if os.path.commonpath((root, candidate)) != root:
        return None
    return candidate if os.path.isfile(candidate) else None


def _normalized_gcode_window(path, position, radius=DEFAULT_GCODE_RADIUS,
                             max_lines=DEFAULT_GCODE_LINES):
    if path is None or position is None:
        return []
    size = os.path.getsize(path)
    position = max(0, min(int(position), size))
    start = max(0, position - radius)
    end = min(size, position + radius)
    with open(path, "rb") as handle:
        handle.seek(start)
        text = handle.read(end - start).decode("utf-8", "replace")
    lines = text.splitlines()
    if start:
        lines = lines[1:]
    if end < size and lines:
        lines = lines[:-1]
    normalized = []
    for line in lines:
        line = line.split(";", 1)[0].strip().upper()
        if line and _GCODE.fullmatch(line):
            normalized.append(line)
    if len(normalized) <= max_lines:
        return normalized
    half = max_lines // 2
    return normalized[:half] + normalized[-(max_lines - half):]


def _print_evidence(events, trigger_seq, gcode_dir):
    request = _latest(events, "print_request", trigger_seq)
    start = _latest(events, "print_start", trigger_seq)
    finish = _latest(events, "print_finish", trigger_seq)
    if request is None or start is None:
        return {"active": False}
    if finish is not None and finish.seq > start.seq:
        return {"active": False}
    stats = _latest(events, "stats", trigger_seq)
    position = _stats_position(stats)
    path = _safe_gcode_path(gcode_dir, request.fields.get("filename"))
    identity = _sha256_file(path) if path else None
    result = {
        "active": True,
        "position": position,
        "file": identity or {"available": False},
        "gcode_window": [],
    }
    if identity and position is not None:
        size = identity["bytes"]
        result["progress"] = (position / size if size else 0.0)
        try:
            result["gcode_window"] = _normalized_gcode_window(path, position)
        except OSError:
            pass
    return result


def _stats_evidence(events, trigger_seq):
    before = _latest(events, "stats", trigger_seq)
    after = None
    for event in events:
        if event.seq > trigger_seq and event.kind == "stats":
            after = event
            break

    def shape(event):
        if event is None:
            return None
        return {"seq": event.seq, "mtime": event.mtime,
                "sections": redact_fields(
                    event.fields.get("sections", {}))}
    return {"before": shape(before), "after": shape(after)}


def _role(event, first_seq, last_seq):
    if event.seq < first_seq:
        return "before"
    if event.seq <= last_seq:
        return "trigger"
    return "after"


def _scoped_timeline(timeline, events):
    scoped = Timeline()
    scoped.anchor = dict(timeline.anchor) if timeline.anchor else None
    scoped.notes = list(timeline.notes)
    scoped.versions = dict(timeline.versions)
    for event in events:
        scoped.add(event)
    return scoped


def build_incident_bundle(timeline, context, triggers, diagnosis,
                          config_path=None, gcode_dir=None, repo_root=None,
                          observed_at=None):
    first = triggers[0]
    last = triggers[-1]
    occurred_at = timeline.wall_time_of_event(first)
    if occurred_at is None or not math.isfinite(occurred_at):
        occurred_at = observed_at if observed_at is not None else time.time()
    config = _sha256_file(config_path) if config_path else None
    revision = _git_revision(repo_root)
    local_events = [_local_event(
        timeline, event, _role(event, first.seq, last.seq))
                    for event in context]
    best = diagnosis.best
    if best is not None:
        diagnosis_data = {
            "matched": True,
            "pattern_id": best.pattern_id,
            "confidence": best.confidence,
            "cause": _safe_text(best.cause),
            "fix": _safe_text(best.fix),
        }
    else:
        case = diagnosis.case
        diagnosis_data = {
            "matched": False,
            "case_hash": case.case_hash if case else "",
            "summary": _safe_text(case.summary if case else "unknown"),
        }
    return {
        "schema_version": INCIDENT_SCHEMA_VERSION,
        "occurred_at": occurred_at,
        "captured_at": observed_at if observed_at is not None else time.time(),
        "trigger": _local_event(timeline, first, "trigger"),
        "trigger_count": len(triggers),
        "diagnosis": diagnosis_data,
        "evidence": {
            "timeline": local_events,
            "timeline_event_count": len(local_events),
            "stats": _stats_evidence(timeline.events, first.seq),
            "versions": _version_evidence(timeline.versions),
            "config": config or {"available": False},
            "software": {"revision": revision} if revision else {},
            "print": _print_evidence(
                timeline.events, first.seq, gcode_dir),
        },
        "privacy": {
            "local_only": True,
            "raw_log_included": False,
            "config_contents_included": False,
            "filename_included": False,
            "full_gcode_included": False,
            "gcode_window_lines_max": DEFAULT_GCODE_LINES,
        },
    }


class IncidentCapture:
    """Group failure events and persist one occurrence after a quiet tail."""

    def __init__(self, store, settle_seconds=DEFAULT_SETTLE_SECONDS,
                 pre_events=DEFAULT_PRE_EVENTS,
                 post_events=DEFAULT_POST_EVENTS, wall_clock=time.time,
                 config_path=None, gcode_dir=None, repo_root=None):
        if settle_seconds <= 0:
            raise ValueError("settle_seconds must be positive")
        if pre_events < 0 or post_events < 0:
            raise ValueError("incident event bounds must be non-negative")
        self.store = store
        self.settle_seconds = settle_seconds
        self.pre_events = pre_events
        self.post_events = post_events
        self.clock = wall_clock
        self.config_path = config_path
        self.gcode_dir = gcode_dir
        self.repo_root = repo_root
        self.pending = None
        self._last_boundary_seq = -1

    @staticmethod
    def _is_failure(event):
        return event.sev_rank() >= 4

    def _event_gap(self, event):
        previous = self.pending["triggers"][-1]
        if (event.mtime is None or previous.mtime is None
                or event.time_basis != previous.time_basis):
            return None
        return event.mtime - previous.mtime

    def observe(self, new_events, timeline, patterns):
        captured = []
        now = self.clock()
        for event in sorted(new_events, key=lambda item: item.seq):
            if self.pending is not None:
                gap = self._event_gap(event)
                separate = (event.kind == "session_start"
                            or (gap is not None
                                and gap > self.settle_seconds))
                if separate:
                    captured.append(self._finalize(timeline, patterns, now))
            if self._is_failure(event):
                if self.pending is None:
                    self.pending = {
                        "triggers": [], "last_failure_at": now,
                        "last_seen_seq": event.seq}
                self.pending["triggers"].append(event)
                self.pending["last_failure_at"] = now
            if self.pending is not None:
                self.pending["last_seen_seq"] = event.seq
        if (self.pending is not None
                and now - self.pending["last_failure_at"]
                >= self.settle_seconds):
            captured.append(self._finalize(timeline, patterns, now))
        return [item for item in captured if item is not None]

    def flush(self, timeline, patterns):
        if self.pending is None:
            return None
        return self._finalize(timeline, patterns, self.clock())

    def _finalize(self, timeline, patterns, now):
        pending, self.pending = self.pending, None
        if pending is None or not pending["triggers"]:
            return None
        triggers = pending["triggers"]
        first, last = triggers[0], triggers[-1]
        arrival = sorted(timeline.events, key=lambda event: event.seq)
        first_index = next((i for i, event in enumerate(arrival)
                            if event.seq == first.seq), 0)
        last_index = next((i for i, event in enumerate(arrival)
                           if event.seq == last.seq), first_index)
        seen_index = next((i for i, event in enumerate(arrival)
                           if event.seq == pending["last_seen_seq"]),
                          last_index)
        start_index = max(0, first_index - self.pre_events)
        while (start_index < first_index
               and arrival[start_index].seq <= self._last_boundary_seq):
            start_index += 1
        end_index = min(len(arrival), last_index + self.post_events + 1,
                        seen_index + 1)
        context = arrival[start_index:end_index]
        scoped = _scoped_timeline(timeline, context)
        diagnosis = Matcher(patterns).diagnose(scoped)
        bundle = build_incident_bundle(
            timeline, context, triggers, diagnosis,
            config_path=self.config_path, gcode_dir=self.gcode_dir,
            repo_root=self.repo_root, observed_at=now)
        count_before = self.store.occurrence_count()
        occurrence_id = self.store.record_occurrence(diagnosis, bundle)
        inserted = self.store.occurrence_count() > count_before
        self._last_boundary_seq = max(
            self._last_boundary_seq, pending["last_seen_seq"])
        return {"occurrence_id": occurrence_id, "inserted": inserted,
                "diagnosis": diagnosis, "bundle": bundle}
