# Live Atlas structured-trace collector (FD-0002 Plane 1/2).
#
# Firmware emits compact trace_data records in each MCU's local clock domain.
# Klippy owns the authoritative per-link clock regression, so this component
# maps them onto the common print-time axis, renders the MCU dictionary, and
# appends the resulting facts to the JSONL boundary consumed by Atlas.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import json
import logging
import os
import re
import struct
import threading
import uuid

import mcu


LEVELS = {
    "error": 0,
    "warning": 1,
    "info": 2,
    "debug": 3,
    "off": 255,
}
DEFAULT_OUTPUT = "~/printer_data/logs/atlas-telemetry.jsonl"
DEFAULT_MAX_FILE_SIZE_MB = 256
DEFAULT_RETAINED_FILES = 3
LEVEL_SEVERITY = {0: "error", 1: "warning", 2: "info", 3: "debug"}
FORMAT_FIELD = re.compile(r"(\w+)=%([uix])")
EXECUTION_TYPES = {
    1: "segment_done",
    2: "trigger",
    3: "underrun",
    4: "hold",
    5: "rebase",
    6: "heater",
    7: "fault",
    8: "discipline",
    9: "edge_observed",
}


def _signed(value):
    return value - (1 << 32) if value & 0x80000000 else value


class TraceRenderer:
    def __init__(self, enumerations, constants):
        events = enumerations.get("trace_event", {})
        subs = enumerations.get("trace_sub", {})
        self.events = {value: name for name, value in events.items()}
        self.subs = {value: name for name, value in subs.items()}
        self.constants = constants

    def render(self, event_id, sub_id, level, data):
        values = struct.unpack(
            "<%dI" % (len(data) // 4), data[:len(data) // 4 * 4])
        name = self.events.get(event_id, "event%d" % event_id)
        sub = self.subs.get(sub_id, "sub%d" % sub_id)
        fmt = self.constants.get("trace_fmt_%s" % name, "")
        fields = {"event": name, "event_id": event_id, "sub": sub,
                  "mcu_clock": None}
        parts = [name]
        for index, (field, spec) in enumerate(FORMAT_FIELD.findall(fmt)):
            value = values[index] if index < len(values) else 0
            rendered = _signed(value) if spec == "i" else value
            fields[field] = rendered
            parts.append("%s=%s" % (
                field, "0x%x" % value if spec == "x" else rendered))
        if not fmt and values:
            fields["values"] = list(values)
            parts.append(str(list(values)))
        return (" ".join(parts), fields,
                LEVEL_SEVERITY.get(level, "info"))


class JsonlWriter:
    def __init__(self, path, session_id=None, max_bytes=0,
                 retained_files=0):
        self.path = os.path.abspath(os.path.expanduser(path))
        self.session_id = session_id
        self.max_bytes = max_bytes
        self.retained_files = retained_files
        self.fd = None
        self.errors = 0
        self.rotations = 0
        self.lock = threading.Lock()
        self._open()

    def _open(self):
        directory = os.path.dirname(self.path)
        if not os.path.isdir(directory):
            try:
                os.makedirs(directory, mode=0o700)
            except OSError:
                if not os.path.isdir(directory):
                    raise
        self.fd = os.open(
            self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.chmod(self.path, 0o600)

    def _ensure_current(self):
        if self.fd is None:
            self._open()
            return
        try:
            current = os.stat(self.path)
            opened = os.fstat(self.fd)
        except OSError:
            current = opened = None
        if (current is None or opened is None
                or (current.st_dev, current.st_ino)
                != (opened.st_dev, opened.st_ino)):
            if self.fd is not None:
                os.close(self.fd)
            self._open()

    def _rotate_if_needed(self, incoming):
        if not self.max_bytes:
            return
        try:
            size = os.fstat(self.fd).st_size
        except OSError:
            return
        if not size or size + incoming <= self.max_bytes:
            return
        os.close(self.fd)
        self.fd = None
        try:
            if self.retained_files:
                for index in range(self.retained_files, 1, -1):
                    source = "%s.%d" % (self.path, index - 1)
                    target = "%s.%d" % (self.path, index)
                    try:
                        os.replace(source, target)
                    except FileNotFoundError:
                        pass
                os.replace(self.path, self.path + ".1")
            else:
                os.unlink(self.path)
        finally:
            self._open()
        self.rotations += 1

    def write(self, record):
        try:
            if self.session_id is not None:
                record = dict(record)
                record.setdefault("session_id", self.session_id)
            payload = (json.dumps(record, sort_keys=True,
                                  separators=(",", ":")) + "\n").encode()
            with self.lock:
                self._ensure_current()
                self._rotate_if_needed(len(payload))
                offset = 0
                while offset < len(payload):
                    offset += os.write(self.fd, payload[offset:])
        except OSError:
            self.errors += 1
            logging.exception("atlas_trace: unable to append %s", self.path)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class AtlasTraceLink:
    def __init__(self, manager, mcu_name):
        self.manager = manager
        self.mcu_name = mcu_name
        self.mcu = mcu.get_printer_mcu(manager.printer, mcu_name)
        self.oid = self.mcu.create_oid()
        self.available = False
        self.reason = "MCU has not been identified"
        self.set_level_cmd = None
        self.stream_cmd = None
        self.query_cmd = None
        self.test_cmd = None
        self.renderer = None
        self.records = 0
        self.sequence_gaps = 0
        self.last_seq = None
        self.last_machine_time = None
        self.next_seq = 0
        self.oldest_seq = 0
        self.dropped = 0
        self.mcu.register_config_callback(self._build_config)

    def _build_config(self):
        config_cmd = self.mcu.try_lookup_command(
            "config_trace oid=%c size=%hu")
        if config_cmd is None:
            self.reason = "firmware does not advertise WANT_TRACE"
            logging.info("atlas_trace: mcu '%s' lacks trace commands; "
                         "collector disabled for this link", self.mcu_name)
            return
        self.available = True
        self.reason = ""
        self.mcu.add_config_cmd(
            "config_trace oid=%d size=%d"
            % (self.oid, self.manager.ring_size))
        cq = self.mcu.alloc_command_queue()
        self.set_level_cmd = self.mcu.lookup_command(
            "trace_set_level sub=%c level=%c", cq=cq)
        self.stream_cmd = self.mcu.lookup_command(
            "trace_stream oid=%c max_per_wake=%c", cq=cq)
        self.query_cmd = self.mcu.lookup_command("trace_query oid=%c", cq=cq)
        self.test_cmd = self.mcu.try_lookup_command("trace_test count=%hu")
        self.mcu.register_serial_response(
            self._handle_data,
            "trace_data oid=%c seq=%u clock=%u event=%hu sub=%c level=%c"
            " data=%*s", self.oid)
        self.mcu.register_serial_response(
            self._handle_status,
            "trace_status oid=%c next_seq=%u oldest_seq=%u dropped=%u",
            self.oid)
        self.renderer = TraceRenderer(
            self.mcu.get_enumerations(), self.mcu.get_constants())

    def start(self):
        if not self.available or self.mcu.is_fileoutput():
            return
        for sub_name, level in self.manager.levels.items():
            sub_id = self._sub_id(sub_name)
            if sub_id is not None:
                self.set_level_cmd.send([sub_id, level])
        self.stream_cmd.send([self.oid, self.manager.stream_max])
        self.query()

    def stop(self):
        if self.available and not self.mcu.is_fileoutput():
            self.stream_cmd.send([self.oid, 0])

    def query(self):
        if self.available and not self.mcu.is_fileoutput():
            self.query_cmd.send([self.oid])

    def set_level(self, sub_name, level):
        sub_id = self._sub_id(sub_name)
        if sub_id is None:
            raise ValueError("unknown trace subsystem '%s'" % sub_name)
        self.set_level_cmd.send([sub_id, level])

    def set_stream_max(self, maximum):
        self.stream_cmd.send([self.oid, maximum])

    def emit_test(self, count):
        if self.test_cmd is None:
            raise ValueError("firmware does not advertise trace_test")
        self.test_cmd.send([count])

    def _sub_id(self, sub_name):
        enums = self.mcu.get_enumerations().get("trace_sub", {})
        return enums.get(sub_name)

    def _handle_data(self, params):
        clock64 = self.mcu.clock32_to_clock64(params["clock"])
        machine_time = self.mcu.clock_to_print_time(clock64)
        summary, fields, severity = self.renderer.render(
            params["event"], params["sub"], params["level"],
            params.get("data", b""))
        seq = params.get("seq")
        if self.last_seq is not None and seq is not None:
            delta = (seq - self.last_seq) & 0xffffffff
            if delta > 1:
                self.sequence_gaps += delta - 1
        self.last_seq = seq
        self.last_machine_time = machine_time
        self.records += 1
        fields.update({
            "seq": seq,
            "oid": self.oid,
            "mcu_clock": params["clock"],
            "time_basis": "klipper_print_time",
        })
        self.manager.writer.write({
            "kind": "trace",
            "machine_time": machine_time,
            "source": "mcu/%s/%s" % (
                self.mcu_name, fields.get("sub", "unknown")),
            "severity": severity,
            "summary": summary,
            "fields": fields,
        })

    def _handle_status(self, params):
        self.next_seq = params["next_seq"]
        self.oldest_seq = params["oldest_seq"]
        self.dropped = params["dropped"]

    def get_status(self):
        unaccounted_gaps = max(0, self.sequence_gaps - self.dropped)
        return {
            "available": self.available,
            "reason": self.reason,
            "records": self.records,
            "sequence_gaps": self.sequence_gaps,
            "last_seq": self.last_seq,
            "last_machine_time": self.last_machine_time,
            "next_seq": self.next_seq,
            "oldest_seq": self.oldest_seq,
            "dropped": self.dropped,
            "unaccounted_gaps": unaccounted_gaps,
        }


class AtlasTrace:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.output = config.get("output", DEFAULT_OUTPUT)
        self.ring_size = config.getint(
            "ring_size", 64, minval=8, maxval=4096)
        self.stream_max = config.getint(
            "stream_max", 4, minval=0, maxval=64)
        self.query_interval = config.getfloat(
            "query_interval", 1., above=0.)
        self.max_file_size_mb = config.getint(
            "max_file_size_mb", DEFAULT_MAX_FILE_SIZE_MB,
            minval=16, maxval=4096)
        self.retained_files = config.getint(
            "retained_files", DEFAULT_RETAINED_FILES,
            minval=0, maxval=16)
        self.levels = {
            name: config.getchoice("%s_level" % name, LEVELS, "off")
            for name in ("core", "motion", "comms", "heater", "trigger")
        }
        mcu_names = config.getlist("mcus", ("mcu",))
        if len(set(mcu_names)) != len(mcu_names):
            raise config.error("atlas_trace mcus contains a duplicate")
        self.session_id = uuid.uuid4().hex
        self.writer = JsonlWriter(
            self.output, self.session_id,
            max_bytes=self.max_file_size_mb * 1024 * 1024,
            retained_files=self.retained_files)
        self.links = {
            name: AtlasTraceLink(self, name) for name in mcu_names}
        self.timer = self.reactor.register_timer(self._query_event)
        self.printer.register_event_handler("klippy:ready", self._ready)
        self.printer.register_event_handler(
            "klippy:disconnect", self._disconnect)
        self.printer.register_event_handler("klippy:shutdown", self._shutdown)
        self.printer.register_event_handler(
            "helix_can:status", self._gateway_status)
        self.printer.register_event_handler(
            "helix_can:incident", self._gateway_incident)
        gcode = self.printer.lookup_object("gcode")
        gcode.register_command(
            "ATLAS_TRACE_STATUS", self.cmd_ATLAS_TRACE_STATUS,
            desc="Report Atlas trace stream and drop counters")
        gcode.register_command(
            "ATLAS_TRACE_LEVEL", self.cmd_ATLAS_TRACE_LEVEL,
            desc="Set one MCU trace subsystem level")
        gcode.register_command(
            "ATLAS_TRACE_STREAM", self.cmd_ATLAS_TRACE_STREAM,
            desc="Set one MCU trace streaming budget")
        gcode.register_command(
            "ATLAS_TRACE_TEST", self.cmd_ATLAS_TRACE_TEST,
            desc="Emit bounded diagnostic trace records on one MCU")

    def _ready(self):
        for link in self.links.values():
            link.start()
        self.reactor.update_timer(self.timer, self.reactor.NOW)

    def _disconnect(self):
        self.reactor.update_timer(self.timer, self.reactor.NEVER)

    def _shutdown(self):
        self._disconnect()

    def _query_event(self, eventtime):
        for link in self.links.values():
            link.query()
        return eventtime + self.query_interval

    def _gateway_status(self, status):
        """Persist versioned fabric health without manufacturing incidents."""
        self.writer.write({
            "schema_version": status.get("schema_version", 1),
            "kind": "gateway_status",
            "machine_time": None,
            "source": "gateway/%s" % status.get("name", "unknown"),
            "severity": "info",
            "summary": "CAN gateway status generation=%s profile=%s" % (
                status.get("generation", 0), status.get("profile", "unknown")),
            "fields": dict(status),
        })

    def _gateway_incident(self, incident):
        self.writer.write({
            "schema_version": 1,
            "kind": "gateway_incident",
            "machine_time": None,
            "source": "gateway/%s" % incident.get("bus", "unknown"),
            "severity": "error",
            "summary": str(incident.get("kind", "gateway fault")),
            "fields": dict(incident),
        })

    def record_execution(self, mcu_obj, record):
        """Persist one MCU execution-log record on the machine-time axis."""
        seq, rtype, src, clock, pos, aux = record
        clock64 = mcu_obj.clock32_to_clock64(clock)
        machine_time = mcu_obj.clock_to_print_time(clock64)
        name = EXECUTION_TYPES.get(rtype, "type%d" % rtype)
        severity = "warning" if rtype in (3, 7) else "info"
        self.writer.write({
            "kind": "execution",
            "machine_time": machine_time,
            "source": "mcu/%s/execution" % mcu_obj.get_name(),
            "severity": severity,
            "summary": "%s src=%d pos=%d aux=%d" % (
                name, src, pos, aux),
            "fields": {
                "event": name,
                "record_type": rtype,
                "seq": seq,
                "src_oid": src,
                "mcu_clock": clock,
                "position_su": pos,
                "aux": aux,
                "time_basis": "klipper_print_time",
            },
        })

    def record_intention(self, mcu_obj, actuator, oid, fields):
        """Persist the exact host wire coefficients for pulse replay."""
        start_clock = int(fields['start_clock'])
        event = fields['event']
        payload = dict(fields)
        payload.update({
            'actuator': actuator,
            'oid': oid,
            'time_basis': 'klipper_print_time',
        })
        self.writer.write({
            'kind': 'intention',
            'machine_time': mcu_obj.clock_to_print_time(start_clock),
            'source': 'host/%s/%s' % (mcu_obj.get_name(), actuator),
            'severity': 'info',
            'summary': '%s %s oid=%d clock=%d' % (
                actuator, event, oid, start_clock),
            'fields': payload,
        })

    def _get_link(self, gcmd):
        name = gcmd.get("MCU", "mcu")
        link = self.links.get(name)
        if link is None:
            raise gcmd.error("MCU '%s' is not configured for atlas_trace"
                             % name)
        if not link.available:
            raise gcmd.error("MCU '%s': %s" % (name, link.reason))
        return link

    def cmd_ATLAS_TRACE_STATUS(self, gcmd):
        for link in self.links.values():
            link.query()
        lines = []
        for name, link in sorted(self.links.items()):
            status = link.get_status()
            lines.append(
                "%s: available=%s records=%d gaps=%d next=%d oldest=%d "
                "dropped=%d unaccounted=%d" % (
                    name, status["available"], status["records"],
                    status["sequence_gaps"], status["next_seq"],
                    status["oldest_seq"], status["dropped"],
                    status["unaccounted_gaps"]))
        gcmd.respond_info("\n".join(lines))

    def cmd_ATLAS_TRACE_LEVEL(self, gcmd):
        link = self._get_link(gcmd)
        sub = gcmd.get("SUB").lower()
        level_name = gcmd.get("LEVEL").lower()
        if level_name not in LEVELS:
            raise gcmd.error("LEVEL must be one of %s"
                             % ", ".join(sorted(LEVELS)))
        try:
            link.set_level(sub, LEVELS[level_name])
        except ValueError as exc:
            raise gcmd.error(str(exc))
        gcmd.respond_info("%s/%s trace level set to %s"
                          % (link.mcu_name, sub, level_name))

    def cmd_ATLAS_TRACE_STREAM(self, gcmd):
        link = self._get_link(gcmd)
        maximum = gcmd.get_int("MAX", minval=0, maxval=64)
        link.set_stream_max(maximum)
        gcmd.respond_info("%s trace stream max_per_wake=%d"
                          % (link.mcu_name, maximum))

    def cmd_ATLAS_TRACE_TEST(self, gcmd):
        link = self._get_link(gcmd)
        count = gcmd.get_int("COUNT", 1, minval=1, maxval=1024)
        try:
            link.emit_test(count)
        except ValueError as exc:
            raise gcmd.error(str(exc))
        gcmd.respond_info("%s emitted %d diagnostic trace records"
                          % (link.mcu_name, count))

    def get_status(self, eventtime):
        return {
            "output": self.writer.path,
            "write_errors": self.writer.errors,
            "rotations": self.writer.rotations,
            "max_file_size_mb": self.max_file_size_mb,
            "retained_files": self.retained_files,
            "ring_size": self.ring_size,
            "stream_max": self.stream_max,
            "mcus": {name: link.get_status()
                     for name, link in self.links.items()},
        }


def load_config(config):
    return AtlasTrace(config)
