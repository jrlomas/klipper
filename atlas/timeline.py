# Atlas merged timeline — the machine-time-ordered event store.
#
# This is the spine every later plane reads (FD-0002 §3-§4).  A HELIX
# machine produces events from several sources — the execution log, the
# structured trace plane, link statistics, and (on any Klipper machine,
# today) the legacy klippy.log.  Machine time is the merge key that
# orders them into one narrative (FD-0001 doc 01); until real machine
# time is available we fall back to the host monotonic clock and say so,
# because being honest about the time basis is the whole point.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

from dataclasses import dataclass, field
from typing import Optional

# Severity ladder — string labels with a total order for filtering.
SEVERITY = ("debug", "info", "notice", "warning", "error", "critical")
SEVERITY_ORDER = {name: i for i, name in enumerate(SEVERITY)}

# How trustworthy is an event's timestamp?  Ordered best-to-worst so a
# consumer can decide how much to trust cross-source ordering.
#   machine        - real machine time from the primary MCU (FD-0001)
#   host_monotonic - the host's monotonic clock (stock klippy.log stats)
#   wall           - wall-clock only (rollover markers, session banners)
#   none           - no time recovered; ordered by arrival only
TIME_BASIS = ("machine", "host_monotonic", "wall", "none")


@dataclass
class Event:
    """One thing that happened, normalized across every source."""
    seq: int                       # global arrival order; the final tiebreak
    kind: str                      # 'stats', 'mcu_shutdown', 'heater_fault', ...
    source: str                    # 'host', 'mcu', "mcu toolhead", ...
    severity: str                  # one of SEVERITY
    summary: str                   # human one-liner
    mtime: Optional[float] = None  # timeline axis, seconds; None if unknown
    time_basis: str = "none"       # one of TIME_BASIS
    t_exact: bool = False          # True if mtime came from this line itself,
    #                                False if carried forward from an earlier
    #                                timestamp (an honest "approximately here")
    fields: dict = field(default_factory=dict)  # parsed structured payload
    raw: str = ""                  # original text, kept for provenance
    wall_time: Optional[float] = None  # immutable session-local wall mapping

    def sev_rank(self) -> int:
        return SEVERITY_ORDER.get(self.severity, 1)


class Timeline:
    """An ordered collection of Events plus the session anchor.

    The anchor (from a 'Start printer at' banner) maps the host monotonic
    clock to wall time, so a host_monotonic mtime can be rendered as a
    real date without polluting the merge axis with wall-clock jitter.
    """

    def __init__(self):
        self.events: list[Event] = []
        self.anchor: Optional[dict] = None  # {wall, systime, monotime}
        self.notes: list[str] = []          # decoder provenance / caveats
        self.versions: dict = {}            # host/mcu/library versions seen
        self._next_seq = 0

    def allocate_seq(self) -> int:
        seq = self._next_seq
        self._next_seq += 1
        return seq

    def add(self, event: Event) -> Event:
        self._next_seq = max(self._next_seq, event.seq + 1)
        self.events.append(event)
        return event

    def note(self, msg: str) -> None:
        if msg not in self.notes:
            self.notes.append(msg)

    def ordered(self) -> list[Event]:
        """Events on the merged timeline.

        Timed events sort by mtime; untimed events keep arrival order and
        sort after any timed event they arrived before/after only by seq.
        seq is always the final tiebreak, so ordering is deterministic.
        """
        big = float("inf")
        return sorted(
            self.events,
            key=lambda e: (e.mtime if e.mtime is not None else big, e.seq),
        )

    def by_min_severity(self, min_severity: str) -> list[Event]:
        floor = SEVERITY_ORDER.get(min_severity, 0)
        return [e for e in self.ordered() if e.sev_rank() >= floor]

    def errors(self) -> list[Event]:
        return self.by_min_severity("error")

    def of_kind(self, *kinds: str) -> list[Event]:
        want = set(kinds)
        return [e for e in self.ordered() if e.kind in want]

    def span(self) -> tuple[Optional[float], Optional[float]]:
        times = [e.mtime for e in self.events if e.mtime is not None]
        if not times:
            return (None, None)
        return (min(times), max(times))

    def wall_time_of(self, mtime: Optional[float]) -> Optional[float]:
        """Render a monotonic mtime as a wall-clock epoch, if anchored."""
        if mtime is None or not self.anchor:
            return None
        a = self.anchor
        if "systime" not in a or "monotime" not in a:
            return None
        return a["systime"] + (mtime - a["monotime"])

    def wall_time_of_event(self, event: Event) -> Optional[float]:
        """Return wall time only when the event's clock supports that map."""
        if event.wall_time is not None:
            return event.wall_time
        if event.time_basis == "wall":
            return event.mtime
        if event.time_basis == "host_monotonic":
            return self.wall_time_of(event.mtime)
        return None

    def __len__(self) -> int:
        return len(self.events)
