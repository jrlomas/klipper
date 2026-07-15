# A4 blackbox decoder — legacy klippy.log -> merged Timeline.
#
# The decoder has to be useful on a *stock* Klipper log on day one,
# before any HELIX board ships (FD-0002 §4).  A stock klippy.log is a
# stream of bare messages (the default logging formatter writes no
# per-line timestamp), so the only machine-time we can recover is:
#   - the 'Start printer at <asctime> (<systime> <monotime>)' banner,
#     which anchors the host monotonic clock to wall time, and
#   - the monotonic timestamp on each periodic 'Stats <t>:' line.
# Everything between two Stats lines is stamped with the last known
# monotonic time and marked t_exact=False — an honest "approximately
# here on the timeline".  When real machine time arrives from the trace
# plane (A1/A2) and the execution log, the same Timeline gains
# time_basis='machine' events and this fallback quietly steps aside.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import re

from ..timeline import Event, Timeline

# --- line matchers (anchored, ordered by specificity) --------------------

_RE_SESSION = re.compile(
    r"^Start printer at (?P<wall>.+?) "
    r"\((?P<systime>[\d.]+) (?P<monotime>[\d.]+)\)")
_RE_ROLLOVER = re.compile(r"^=+ Log rollover at (?P<wall>.+?) =+\s*$")
_RE_STATS = re.compile(r"^Stats (?P<t>[\d.]+):\s*(?P<body>.*)$")
_RE_SHUTDOWN = re.compile(r"^Transition to shutdown state: (?P<reason>.+)$")
_RE_MCU_SHUTDOWN = re.compile(
    r"^(?P<prev>Previous )?MCU '(?P<mcu>[^']+)' shutdown: (?P<reason>.+)$")
_RE_COMM_LOST = re.compile(
    r"^Lost communication with MCU '(?P<mcu>[^']+)'")
_RE_HEATER = re.compile(
    r"^Heater (?P<heater>\S+) not heating at expected rate")
_RE_PROTOCOL = re.compile(r"^(MCU )?Protocol error")
_RE_TRACEBACK = re.compile(r"^Traceback \(most recent call last\):")
_RE_MCU_LOADED = re.compile(
    r"^Loaded MCU '(?P<mcu>[^']+)' (?P<commands>\d+) commands "
    r"\((?P<version>[^ /()]+) /.*\)$")
_RE_PRINT_REQUEST = re.compile(
    r"SDCARD_PRINT_FILE\s+FILENAME=\\?\"(?P<filename>[^\"\\]+)\\?\"")
_RE_PRINT_START = re.compile(
    r"^Starting SD card print \(position (?P<position>\d+)\)$")
_RE_PRINT_FINISH = re.compile(r"^Finished SD card print$")
_RE_PRINT_EXIT = re.compile(
    r"^Exiting SD card print \(position (?P<position>\d+)\)$")
# A Python traceback ends with a non-indented "Type: message" line, where
# Type is a (possibly dotted) identifier — including lowercase ones like
# Klipper's own 'mcu.error'.  Message is optional.
_RE_EXC_LINE = re.compile(
    r"^(?P<exc>[A-Za-z_][\w.]*)(?::\s?(?P<msg>.*))?$")


def _parse_exc_line(line: str):
    m = _RE_EXC_LINE.match(line.strip())
    if not m:
        return (None, None)
    return (m.group("exc"), (m.group("msg") or "").strip() or None)

# Canonical MCU fault classes, keyed by the reason-string prefix Klipper
# emits.  This is honest *classification* only — it names the fault so an
# A5 pattern can key on fields.fault_class; the fix text stays in the
# (initially empty) diagnosis catalog, never here.  Prefixes mirror
# klippy/extras/error_mcu.py::Common_MCU_errors.
_FAULT_CLASSES = (
    ("Timer too close", "timer_too_close"),
    ("Missed scheduling of next ", "missed_scheduling"),
    ("ADC out of range", "adc_out_of_range"),
    ("Rescheduled timer in the past", "rescheduled_in_past"),
    ("Stepper too far in past", "stepper_too_far_past"),
    ("Command request", "command_request"),
    ("shutdown request", "shutdown_request"),
)


def _classify_fault(reason: str) -> str:
    for prefix, name in _FAULT_CLASSES:
        if reason.startswith(prefix):
            return name
    return "other"


def _parse_stats_body(body: str) -> dict:
    """Parse a 'Stats' body into {section: {key: value}}.

    Klipper stats look like:
        gcodein=0 mcu: mcu_awake=0.00 bytes_retransmit=0 ...
        heater_bed: target=60 temp=59.8 pwm=0.4
    Tokens are 'key=value'; a bare 'name:' token opens a new section.
    Values are coerced to float/int where possible, else kept as strings.
    """
    sections: dict = {"_": {}}
    section = "_"
    for tok in body.split():
        if tok.endswith(":") and "=" not in tok:
            section = tok[:-1]
            sections.setdefault(section, {})
            continue
        if "=" not in tok:
            continue
        key, _, val = tok.partition("=")
        sections[section][key] = _coerce(val)
    if not sections["_"]:
        del sections["_"]
    return sections


def _coerce(val: str):
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        return val


class KlippyLogDecoder:
    """Decode a klippy.log (as text or lines) into a Timeline."""

    def __init__(self, timeline=None):
        self.timeline = timeline if timeline is not None else Timeline()
        self._clock = None          # last known monotonic time (seconds)
        self._basis = "none"
        self._in_traceback = False
        self._tb_lines: list[str] = []

    # -- event emission ---------------------------------------------------

    def _emit(self, kind, source, severity, summary, t_exact=False,
              mtime=None, **fields) -> Event:
        if mtime is None:
            mtime = self._clock
        ev = Event(
            seq=self.timeline.allocate_seq(), kind=kind, source=source,
            severity=severity,
            summary=summary, mtime=mtime,
            time_basis=self._basis if mtime is not None else "none",
            t_exact=t_exact, fields=fields, raw=fields.pop("_raw", ""),
            wall_time=self.timeline.wall_time_of(mtime))
        return self.timeline.add(ev)

    # -- driver -----------------------------------------------------------

    def feed(self, text) -> Timeline:
        lines = text.splitlines() if isinstance(text, str) else list(text)
        for line in lines:
            self.feed_line(line)
        self.finalize()
        return self.timeline

    def feed_line(self, line: str) -> None:
        """Feed one line; keeps decoder state (for live tailing, A3)."""
        self._line(line.rstrip("\n"))

    def finalize(self) -> Timeline:
        """Flush any open traceback and record provenance notes.

        Idempotent: notes dedupe and a closed traceback flush is a no-op,
        so a follow-mode viewer may call this after each poll.
        """
        self._flush_traceback()
        self._finalize()
        return self.timeline

    def _finalize(self):
        tl = self.timeline
        if any(e.time_basis == "host_monotonic" and not e.t_exact
               for e in tl.events):
            tl.note("some events carry an inferred (carried-forward) "
                    "monotonic time; precision is bounded by the stats "
                    "interval. Real machine time requires the trace plane.")
        if not tl.anchor:
            tl.note("no 'Start printer at' banner found; monotonic times "
                    "cannot be rendered as wall-clock.")

    # -- per-line dispatch ------------------------------------------------

    def _line(self, line: str):
        if self._in_traceback:
            if line.startswith((" ", "\t")) or line.startswith("Traceback"):
                self._tb_lines.append(line)
                return
            # The first non-indented line ends the traceback and is its
            # exception line (Python always emits it last, non-indented).
            self._tb_lines.append(line)
            exc, msg = _parse_exc_line(line)
            self._flush_traceback(exc=exc, msg=msg)
            return

        stripped = line.strip()
        if not stripped:
            return

        m = _RE_SESSION.match(line)
        if m:
            systime = float(m.group("systime"))
            monotime = float(m.group("monotime"))
            self._clock = monotime
            self._basis = "host_monotonic"
            self.timeline.anchor = {
                "wall": m.group("wall"), "systime": systime,
                "monotime": monotime}
            self._emit("session_start", "host", "info",
                       "printer session start", t_exact=True,
                       wall=m.group("wall"), systime=systime,
                       monotime=monotime, _raw=line)
            return

        m = _RE_ROLLOVER.match(line)
        if m:
            self._emit("rollover", "host", "notice",
                       "log rollover", _raw=line, wall=m.group("wall"))
            return

        m = _RE_STATS.match(line)
        if m:
            t = float(m.group("t"))
            self._clock = t
            self._basis = "host_monotonic"
            sections = _parse_stats_body(m.group("body"))
            self._ingest_versions_from_stats(sections)
            self._emit("stats", "host", "info",
                       "periodic stats", t_exact=True, sections=sections,
                       _raw=line)
            return

        m = _RE_MCU_LOADED.match(line)
        if m:
            mcu_name = m.group("mcu")
            version = m.group("version")
            self.timeline.versions["mcu:%s" % mcu_name] = version
            self._emit(
                "mcu_identified", "mcu %s" % mcu_name, "info",
                "MCU '%s' identified" % mcu_name, mcu=mcu_name,
                command_count=int(m.group("commands")), version=version,
                _raw=line)
            return

        m = _RE_PRINT_REQUEST.search(line)
        if m:
            filename = m.group("filename")
            self._emit(
                "print_request", "host", "info", "SD print requested",
                filename=filename, _raw=line)
            return

        m = _RE_PRINT_START.match(line)
        if m:
            self._emit(
                "print_start", "host", "notice", "SD print started",
                position=int(m.group("position")), _raw=line)
            return

        if _RE_PRINT_FINISH.match(line):
            self._emit("print_finish", "host", "notice",
                       "SD print finished", _raw=line)
            return

        m = _RE_PRINT_EXIT.match(line)
        if m:
            self._emit(
                "print_exit", "host", "notice", "SD print exited",
                position=int(m.group("position")), _raw=line)
            return

        m = _RE_SHUTDOWN.match(line)
        if m:
            self._emit("shutdown", "host", "critical",
                       "transition to shutdown: %s" % (m.group("reason"),),
                       reason=m.group("reason"), _raw=line)
            return

        m = _RE_MCU_SHUTDOWN.match(line)
        if m:
            reason = m.group("reason")
            fault = _classify_fault(reason)
            prev = bool(m.group("prev"))
            self._emit("mcu_shutdown", "mcu %s" % (m.group("mcu"),),
                       "critical",
                       "MCU '%s' shutdown: %s" % (m.group("mcu"), reason),
                       mcu=m.group("mcu"), reason=reason, fault_class=fault,
                       previous=prev, _raw=line)
            return

        m = _RE_COMM_LOST.match(line)
        if m:
            self._emit("comm_lost", "mcu %s" % (m.group("mcu"),), "critical",
                       "lost communication with MCU '%s'" % (m.group("mcu"),),
                       mcu=m.group("mcu"), fault_class="comm_lost", _raw=line)
            return

        m = _RE_HEATER.match(line)
        if m:
            self._emit("heater_fault", "host", "error",
                       "heater '%s' not heating at expected rate"
                       % (m.group("heater"),),
                       heater=m.group("heater"),
                       fault_class="heater_not_heating", _raw=line)
            return

        if _RE_PROTOCOL.match(line):
            self._emit("protocol_error", "host", "error",
                       "MCU protocol error (version mismatch likely)",
                       fault_class="protocol_error", _raw=line)
            return

        if _RE_TRACEBACK.match(line):
            self._in_traceback = True
            self._tb_lines = [line]
            return

        # Anything else: keep it, but as a low-severity generic line so the
        # narrative stays complete without drowning the typed events.
        self._emit("log", "host", "info", stripped[:200], _raw=line)

    def _flush_traceback(self, exc=None, msg=None):
        if not self._in_traceback:
            return
        self._in_traceback = False
        block = "\n".join(self._tb_lines)
        self._tb_lines = []
        if exc is None:
            # Traceback ran to EOF or was interrupted; try to recover the
            # last non-indented line as the exception.
            for ln in reversed(block.splitlines()):
                if ln.startswith((" ", "\t")) or ln.startswith("Traceback"):
                    continue
                exc, msg = _parse_exc_line(ln)
                if exc is not None:
                    break
        self._emit("traceback", "host", "error",
                   "host exception: %s" % (exc or "unknown",),
                   exc_type=exc, exc_msg=msg, _raw=block)

    def _ingest_versions_from_stats(self, sections: dict):
        # Some builds report versions in stats sections; capture opportun-
        # istically so a captured case carries what it ran (A8 needs this).
        for sect, kv in sections.items():
            for key in ("mcu_version", "version", "sw_version"):
                if key in kv:
                    self.timeline.versions[sect] = kv[key]


def decode_klippy_log(text) -> Timeline:
    """Convenience: decode klippy.log text/lines into a Timeline."""
    return KlippyLogDecoder().feed(text)
