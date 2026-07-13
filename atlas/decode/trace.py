# A2 host trace collector — decode trace-plane records into the Timeline.
#
# The firmware trace plane (A1, src/trace.c) streams compact records:
# an event id, a subsystem, a severity level, the MCU clock, and a small
# blob of typed args.  The host renders them to human strings *here*,
# using the data dictionary the firmware published (DECL_ENUMERATION
# "trace_event"/"trace_sub" and DECL_CONSTANT_STR "trace_fmt <name>").
# Because every record carries the MCU clock, and timesync maps that
# clock onto machine time (FD-0001 doc 01), traces from the mainboard, a
# CAN toolhead, and an ESP32 accessory all merge into one timeline — the
# same Timeline the klippy.log decoder (A4) fills, so a HELIX incident
# and a legacy log read the same way.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import re
import struct

from ..timeline import Event, Timeline

# Firmware level (src/trace.h) -> Atlas severity.  Lower level = more
# severe, matching the firmware's "emit at or above" test.
_LEVEL_SEVERITY = {0: "error", 1: "warning", 2: "info", 3: "debug"}

# Format-spec -> how to render one arg.  %u unsigned, %i signed, %x hex.
_ARG_RENDER = {
    "u": lambda v: str(v),
    "i": lambda v: str(_as_signed(v)),
    "x": lambda v: "0x%x" % v,
}
_RE_FMT_FIELD = re.compile(r"(\w+)=%([uix])")


def _as_signed(v: int) -> int:
    return v - (1 << 32) if v & 0x80000000 else v


class TraceEventDef:
    """One registered trace event: name + ordered (arg_name, spec) list."""

    def __init__(self, event_id: int, name: str, sub_id: int, fmt: str):
        self.event_id = event_id
        self.name = name
        self.sub_id = sub_id
        self.fmt = fmt
        self.args = _RE_FMT_FIELD.findall(fmt)  # [(arg_name, spec), ...]

    def render(self, values: list[int]) -> tuple[str, dict]:
        parts, fields = [self.name], {}
        for i, (arg_name, spec) in enumerate(self.args):
            v = values[i] if i < len(values) else 0
            fields[arg_name] = _as_signed(v) if spec == "i" else v
            parts.append("%s=%s" % (arg_name, _ARG_RENDER[spec](v)))
        return " ".join(parts), fields


# The built-in dictionary mirrors src/trace.c so the host renders
# correctly out of the box; a real connection overrides it with the MCU's
# own published dictionary via from_dictionary().
_DEFAULT_SUBS = {
    0: "core", 1: "motion", 2: "comms", 3: "heater", 4: "trigger",
}
_DEFAULT_EVENTS = {
    1: ("step_underrun", 1, "horizon_us=%u queue_depth=%u"),
    2: ("queue_refill", 1, "depth=%u added=%u"),
    3: ("comm_retransmit", 2, "seq=%u count=%u"),
    4: ("hold_enter", 0, "reason=%u"),
    5: ("rebase", 1, "new_anchor=%u"),
    6: ("trigger_fire", 4, "source_oid=%u reason=%u"),
}


class TraceDictionary:
    """Maps event/subsystem ids to names and render formats."""

    def __init__(self, events: dict, subs: dict):
        self.subs = dict(subs)
        self.events = {
            eid: TraceEventDef(eid, name, sub_id, fmt)
            for eid, (name, sub_id, fmt) in events.items()}

    @classmethod
    def default(cls) -> "TraceDictionary":
        return cls(_DEFAULT_EVENTS, _DEFAULT_SUBS)

    @classmethod
    def from_dictionary(cls, data: dict) -> "TraceDictionary":
        """Build from a Klipper-style data dictionary.

        Expects:
          data['enumerations']['trace_event'] -> {name: id}
          data['enumerations']['trace_sub']   -> {name: id}
          data['constants']['trace_fmt <name>'] -> "fmt string"
        Unknown / missing sections degrade to the built-in defaults so a
        partial dictionary never crashes the collector.
        """
        enums = data.get("enumerations", {})
        consts = data.get("constants", {})
        subs = {v: k for k, v in enums.get("trace_sub", {}).items()}
        events = {}
        for name, eid in enums.get("trace_event", {}).items():
            fmt = consts.get("trace_fmt %s" % name, "")
            # sub is not carried per-event in the dictionary; default 0
            # (core) unless a fmt convention encodes it — kept simple.
            events[eid] = (name, 0, fmt)
        if not events:
            return cls.default()
        return cls(events, subs or _DEFAULT_SUBS)


class ClockMap:
    """Linear MCU-clock -> machine-time map (the timesync contract).

    machine_time = t0 + (clock - clock0) / freq_hz, using 32-bit clock
    wraparound-aware subtraction.  With no map, the collector keeps the
    raw clock and marks the event's time basis honestly as unknown.
    """

    def __init__(self, freq_hz: float, clock0: int = 0, t0: float = 0.0):
        self.freq_hz = float(freq_hz)
        self.clock0 = clock0 & 0xFFFFFFFF
        self.t0 = t0

    def to_mtime(self, clock: int) -> float:
        delta = (clock - self.clock0) & 0xFFFFFFFF
        # Treat the top half of the range as a negative offset (the clock
        # just before clock0), so events near the anchor order correctly.
        if delta & 0x80000000:
            delta -= 1 << 32
        return self.t0 + delta / self.freq_hz


class TraceCollector:
    """Decode trace-plane records into Events on a Timeline."""

    def __init__(self, dictionary=None, clockmap=None, source="mcu",
                 timeline=None):
        self.dict = dictionary or TraceDictionary.default()
        self.clockmap = clockmap
        self.source = source
        self.timeline = timeline if timeline is not None else Timeline()

    def ingest(self, event_id, clock, sub, level, data=b"", seq=None):
        """Decode one trace_data record into an Event (and store it)."""
        values = list(struct.unpack("<%dI" % (len(data) // 4),
                                    data[: (len(data) // 4) * 4]))
        edef = self.dict.events.get(event_id)
        sub_name = self.dict.subs.get(sub, "sub%d" % sub)
        severity = _LEVEL_SEVERITY.get(level, "info")
        if edef is not None:
            summary, fields = edef.render(values)
            name = edef.name
        else:
            summary = "trace event %d %s" % (event_id, values)
            fields = {"values": values}
            name = "event%d" % event_id
        if self.clockmap is not None:
            mtime, basis, exact = self.clockmap.to_mtime(clock), "machine", True
        else:
            mtime, basis, exact = None, "none", False
        fields.update(event=name, event_id=event_id, sub=sub_name,
                      mcu_clock=clock)
        ev = Event(
            seq=self.timeline.allocate_seq(), kind="trace", severity=severity,
            source="%s/%s" % (self.source, sub_name), summary=summary,
            mtime=mtime, time_basis=basis, t_exact=exact, fields=fields,
            raw="trace event=%d sub=%d level=%d clock=%u data=%r"
                % (event_id, sub, level, clock, data))
        return self.timeline.add(ev)

    def feed(self, records) -> Timeline:
        """Ingest an iterable of record dicts/tuples.

        Each record is a mapping with keys event(/event_id), clock, sub,
        level, and optional data (bytes).  Returns the Timeline.
        """
        for r in records:
            self.ingest(
                event_id=r.get("event", r.get("event_id")),
                clock=r["clock"], sub=r["sub"], level=r["level"],
                data=r.get("data", b""), seq=r.get("seq"))
        return self.timeline
