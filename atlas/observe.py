# Structured Atlas observability ingestion (FD-0002 Plane 1/2).

import json
import os

from .timeline import Event, SEVERITY, Timeline


class StructuredCollector:
    """Normalize trace, execution, link and timesync JSON records."""

    KINDS = {"trace", "execution", "link_stats", "timesync"}

    def __init__(self, timeline=None):
        self.timeline = timeline if timeline is not None else Timeline()

    def ingest(self, record):
        if not isinstance(record, dict):
            raise ValueError("structured record must be an object")
        kind = record.get("kind")
        if kind not in self.KINDS:
            raise ValueError("unsupported structured kind %r" % kind)
        mtime = record.get("machine_time")
        if not isinstance(mtime, (int, float)):
            raise ValueError("machine_time must be numeric")
        source = record.get("source", "mcu")
        if not isinstance(source, str) or not source:
            raise ValueError("source must be a non-empty string")
        fields = record.get("fields", {})
        if not isinstance(fields, dict):
            raise ValueError("fields must be an object")
        fields = dict(fields)
        severity = record.get("severity", self._severity(kind, fields))
        if severity not in SEVERITY:
            raise ValueError("invalid severity %r" % severity)
        summary = record.get("summary") or self._summary(kind, fields)
        event = Event(
            seq=self.timeline.allocate_seq(), kind=kind, source=source,
            severity=severity, summary=str(summary), mtime=float(mtime),
            time_basis="machine", t_exact=True, fields=fields,
            raw=json.dumps(record, sort_keys=True))
        return self.timeline.add(event)

    @staticmethod
    def _severity(kind, fields):
        if kind == "link_stats" and (
                fields.get("crc_errors", 0) or fields.get("retransmits", 0)):
            return "warning"
        if kind == "timesync" and abs(fields.get("error_us", 0)) >= 100:
            return "warning"
        return "info"

    @staticmethod
    def _summary(kind, fields):
        if kind == "execution":
            return "executed %s" % fields.get("command", "intention")
        if kind == "link_stats":
            return "link stats crc=%s retransmits=%s" % (
                fields.get("crc_errors", 0), fields.get("retransmits", 0))
        if kind == "timesync":
            return "timesync error_us=%s" % fields.get("error_us", 0)
        return "trace %s" % fields.get("event", "event")


class StructuredTail:
    """Rotation-safe incremental reader for newline-delimited JSON."""

    def __init__(self, path, timeline=None):
        self.path = os.path.abspath(os.path.expanduser(path))
        self.collector = StructuredCollector(timeline)
        self._offset = 0
        self._pending = ""
        self._identity = None
        self.source_available = False
        self.rotations = 0
        self.errors = 0
        self.last_error = ""

    def poll(self):
        try:
            stat = os.stat(self.path)
        except FileNotFoundError:
            self.source_available = False
            return []
        self.source_available = True
        identity = (stat.st_dev, stat.st_ino)
        if self._identity is not None and (
                identity != self._identity or stat.st_size < self._offset):
            self._offset = 0
            self._pending = ""
            self.rotations += 1
        self._identity = identity
        with open(self.path, "r") as handle:
            handle.seek(self._offset)
            chunk = handle.read()
            self._offset = handle.tell()
        self._pending += chunk
        *lines, self._pending = self._pending.split("\n")
        events = []
        for line in lines:
            if not line.strip():
                continue
            try:
                events.append(self.collector.ingest(json.loads(line)))
                self.last_error = ""
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self.errors += 1
                self.last_error = "structured record rejected: %s" % exc
        return events
