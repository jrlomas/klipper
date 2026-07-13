# A3 trace viewer — filter and tail the merged timeline (FD-0002 §3).
#
# "A Mainsail panel if it reaches; else a small standalone view"
# (HANDOFF §5).  This is the standalone view: filter the merged timeline
# by subsystem, severity, and board/source, render it, and — in follow
# mode — live-tail a growing klippy.log the way `tail -f` does, feeding
# new lines through the same A4 decoder so the narrative stays coherent.
#
# The Vue/Mainsail panel is a thin front-end over exactly this filter +
# render contract; keeping the logic here means the panel, the CLI, and
# the tests all agree on what "the timeline, filtered" means.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
import time
from dataclasses import dataclass, field

from .timeline import SEVERITY_ORDER, Event, Timeline


@dataclass
class TimelineFilter:
    """A view over the timeline: which events pass, and in what order."""
    min_severity: str = "debug"          # keep events at/above this
    sources: list = field(default_factory=list)   # substring match (OR)
    kinds: list = field(default_factory=list)      # exact kind (OR)
    subsystems: list = field(default_factory=list)  # trace subsystem (OR)
    ordered: bool = True                 # machine-time order vs arrival

    def passes(self, e: Event) -> bool:
        if e.sev_rank() < SEVERITY_ORDER.get(self.min_severity, 0):
            return False
        if self.sources and not any(s in e.source for s in self.sources):
            return False
        if self.kinds and e.kind not in self.kinds:
            return False
        if self.subsystems:
            sub = e.fields.get("sub")
            if sub not in self.subsystems:
                return False
        return True

    def select(self, timeline: Timeline) -> list:
        events = timeline.ordered() if self.ordered else timeline.events
        return [e for e in events if self.passes(e)]


def format_event(timeline: Timeline, e: Event, wall: bool = False) -> str:
    if e.mtime is None:
        stamp = "        ?   "
    elif wall and timeline.wall_time_of(e.mtime) is not None:
        lt = time.localtime(timeline.wall_time_of(e.mtime))
        stamp = time.strftime("%H:%M:%S", lt) + ("" if e.t_exact else "~")
    else:
        stamp = ("%s%10.3f" % (" " if e.t_exact else "~", e.mtime))
    return "%s  %-8s %-16s %-14s %s" % (
        stamp, e.severity, e.source[:16], e.kind, e.summary)


def render(timeline: Timeline, filt: TimelineFilter = None,
           wall: bool = False) -> list:
    filt = filt or TimelineFilter()
    return [format_event(timeline, e, wall) for e in filt.select(timeline)]


class LiveTail:
    """Follow a growing klippy.log, emitting newly-decoded events.

    Stateful across polls: it keeps one decoder and one file offset, so a
    line split across two polls (or a multi-line traceback) is handled
    correctly.  poll() returns the events decoded since the last call, in
    arrival order (what a live tail wants).
    """

    def __init__(self, path, filt: TimelineFilter = None,
                 max_events: int = None, timeline=None):
        from .decode.klippy_log import KlippyLogDecoder
        self.path = path
        self.filt = filt or TimelineFilter(ordered=False)
        self.decoder = KlippyLogDecoder(timeline=timeline)
        if max_events is not None and max_events < 1:
            raise ValueError("max_events must be positive")
        self.max_events = max_events
        self._offset = 0
        self._pending = ""   # an unterminated trailing line, held back
        self._identity = None
        self.source_available = False
        self.rotations = 0

    @property
    def timeline(self) -> Timeline:
        return self.decoder.timeline

    def _source_ready(self) -> bool:
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            self.source_available = False
            return False
        self.source_available = True
        identity = (st.st_dev, st.st_ino)
        rotated = (self._identity is not None
                   and (identity != self._identity
                        or st.st_size < self._offset))
        if rotated:
            # Keep the existing bounded narrative, but start reading the new
            # source at byte zero.  This handles rename+create and copytruncate
            # without losing the incident that caused a rollover.
            self.decoder.finalize()
            self._offset = 0
            self._pending = ""
            self.rotations += 1
            self.timeline.note("klippy.log rotated; continued from the new "
                               "file at byte zero")
        self._identity = identity
        return True

    def _prune(self) -> None:
        if self.max_events is None:
            return
        overflow = len(self.timeline.events) - self.max_events
        if overflow <= 0:
            return
        del self.timeline.events[:overflow]
        self.timeline.note("live timeline is bounded to the latest %d events"
                           % self.max_events)

    def poll(self) -> list:
        if not self._source_ready():
            return []
        try:
            with open(self.path, "r") as fh:
                fh.seek(self._offset)
                chunk = fh.read()
                self._offset = fh.tell()
        except FileNotFoundError:
            # The file may rotate between stat() and open().  Treat that as a
            # normal waiting poll; the next pass attaches to the replacement.
            self.source_available = False
            return []
        self._pending += chunk
        # Feed only complete (newline-terminated) lines; keep any partial
        # final line for the next poll.  Do NOT finalize mid-stream — an
        # open traceback must stay open until its terminator arrives (a
        # completed traceback self-flushes inside the decoder).
        *complete, self._pending = self._pending.split("\n")
        before = len(self.timeline.events)
        for line in complete:
            self.decoder.feed_line(line)
        tl = self.decoder.timeline
        new = tl.events[before:]
        selected = [e for e in new if self.filt.passes(e)]
        self._prune()
        return selected

    def follow(self, out=None, interval=0.5, wall=False, _max_polls=None):
        """Blocking tail loop.  _max_polls bounds it for testing."""
        import sys
        out = out or sys.stdout
        polls = 0
        tl = self.decoder.timeline
        while _max_polls is None or polls < _max_polls:
            for e in self.poll():
                out.write(format_event(tl, e, wall) + "\n")
                out.flush()
            polls += 1
            if _max_polls is not None and polls >= _max_polls:
                break
            time.sleep(interval)
