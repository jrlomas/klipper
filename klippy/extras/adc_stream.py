# Host interface for DMA-backed MCU ADC acquisition streams.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import struct
import threading

from . import bulk_sensor


TRAFFIC_CLASSES = {"critical": 0, "prompt": 1, "telemetry": 2}
STATE_NAMES = {0: "stopped", 1: "armed", 2: "running", 3: "faulted"}


class ADCStream:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]
        pin_descs = config.getlist("pins")
        if not pin_descs or len(pin_descs) > 4:
            raise config.error("adc_stream requires between one and four pins")
        ppins = self.printer.lookup_object("pins")
        pin_params = [ppins.lookup_pin(pin) for pin in pin_descs]
        self.mcu = pin_params[0]["chip"]
        if any(p["chip"] is not self.mcu for p in pin_params):
            raise config.error("All adc_stream pins must use the same MCU")
        self.pins = [p["pin"] for p in pin_params]
        self.channel_names = config.getlist("channel_names", pin_descs)
        if len(self.channel_names) != len(self.pins):
            raise config.error("channel_names must match the number of pins")
        self.sample_rate = config.getfloat("sample_rate", 1000., above=0.)
        max_scans = 16 // len(self.pins)
        self.block_scans = config.getint(
            "block_scans", max_scans, minval=1, maxval=max_scans)
        self.traffic_class = config.getchoice(
            "traffic_class", TRAFFIC_CLASSES, default="telemetry")
        self.max_pending = config.getint(
            "max_pending_samples", 4096, minval=16)
        self.oid = self.mcu.create_oid()
        self.adc_max = 4095.
        self.period_ticks = 0
        self.start_cmd = self.stop_cmd = self.query_cmd = None
        self.lock = threading.Lock()
        self.pending = []
        self.last_values = [None] * len(self.pins)
        self.state = "configuring"
        self.epoch = self.last_sequence = None
        self.sequence_gaps = self.host_drops = self.mcu_drops = 0
        self.last_status = self.last_uncertainty = 0
        self.mcu.register_config_callback(self._build_config)

        self.batch_helper = bulk_sensor.BatchBulkHelper(
            self.printer, self._process_batch)
        self.batch_helper.add_mux_endpoint(
            "adc_stream/dump_adc", "sensor", self.name,
            {"header": ("time",) + tuple(self.channel_names),
             "raw_adc_max": int(self.adc_max)})
        gcode = self.printer.lookup_object("gcode")
        gcode.register_mux_command(
            "ADC_STREAM_START", "SENSOR", self.name,
            self.cmd_ADC_STREAM_START, desc=self.cmd_ADC_STREAM_START_help)
        gcode.register_mux_command(
            "ADC_STREAM_STOP", "SENSOR", self.name,
            self.cmd_ADC_STREAM_STOP, desc=self.cmd_ADC_STREAM_STOP_help)
        gcode.register_mux_command(
            "ADC_STREAM_STATUS", "SENSOR", self.name,
            self.cmd_ADC_STREAM_STATUS, desc=self.cmd_ADC_STREAM_STATUS_help)

    def _build_config(self):
        if self.mcu.try_lookup_command("config_adc_stream oid=%c") is None:
            raise self.printer.config_error(
                "MCU for adc_stream '%s' lacks DMA ADC stream support"
                % (self.name,))
        self.adc_max = float(self.mcu.get_constants().get("ADC_MAX", 4095))
        self.period_ticks = max(
            1, self.mcu.seconds_to_clock(1. / self.sample_rate))
        self.mcu.add_config_cmd("config_adc_stream oid=%d" % (self.oid,))
        for pin in self.pins:
            self.mcu.add_config_cmd(
                "adc_stream_add_channel oid=%d pin=%s" % (self.oid, pin))
        start_clock = self.mcu.get_query_slot(self.oid)
        block_values = self.block_scans * len(self.pins)
        self.mcu.add_config_cmd(
            "adc_stream_start oid=%d clock=%d period_ticks=%d"
            " block_values=%d traffic_class=%d"
            % (self.oid, start_clock, self.period_ticks, block_values,
               self.traffic_class), is_init=True)
        cq = self.mcu.alloc_command_queue()
        self.start_cmd = self.mcu.lookup_command(
            "adc_stream_start oid=%c clock=%u period_ticks=%u"
            " block_values=%c traffic_class=%c", cq=cq)
        self.stop_cmd = self.mcu.lookup_command(
            "adc_stream_stop oid=%c", cq=cq)
        self.query_cmd = self.mcu.lookup_command(
            "adc_stream_get_status oid=%c", cq=cq)
        data_format = (
            "adc_stream_data_telemetry oid=%c sequence=%u epoch=%u class=%c"
            " first_clock=%u period_num=%u period_den=%u uncertainty=%u"
            " channels=%c status=%u values=%*s")
        self.mcu.register_serial_response(
            self._handle_data, data_format, self.oid)
        self.mcu.register_serial_response(
            self._handle_fault,
            "adc_stream_fault oid=%c status=%u dropped=%u sequence=%u",
            self.oid)
        self.mcu.register_serial_response(
            self._handle_status,
            "adc_stream_status oid=%c state=%c class=%c channels=%c"
            " block_values=%c epoch=%u sequence=%u dropped=%u status=%u",
            self.oid)
        self.state = "armed"

    def _handle_data(self, params):
        channels = params["channels"]
        payload = bytes(params["values"])
        if (channels != len(self.pins) or not channels
                or len(payload) % (2 * channels)):
            logging.warning("adc_stream %s received malformed block", self.name)
            self.last_status |= 1 << 3
            return
        raw = struct.unpack("<%dH" % (len(payload) // 2), payload)
        epoch, sequence = params["epoch"], params["sequence"]
        if self.epoch != epoch:
            self.epoch, self.last_sequence = epoch, None
        if self.last_sequence is not None:
            gap = (sequence - self.last_sequence - 1) & 0xffffffff
            if gap:
                self.sequence_gaps += gap
        self.last_sequence = sequence

        first_clock = self.mcu.clock32_to_clock64(params["first_clock"])
        period = float(params["period_num"]) / params["period_den"]
        uncertainty = float(params["uncertainty"])
        samples = []
        for offset in range(0, len(raw), channels):
            scan = offset // channels
            ptime = self.mcu.clock_to_print_time(first_clock + scan * period)
            values = raw[offset:offset + channels]
            samples.append([ptime] + [value / self.adc_max for value in values])
        with self.lock:
            room = self.max_pending - len(self.pending)
            if room < len(samples):
                drop = len(samples) - max(0, room)
                self.host_drops += drop
                del samples[:drop]
            self.pending.extend(samples)
            if raw:
                self.last_values = [v / self.adc_max for v in raw[-channels:]]
            self.last_status = params["status"]
            self.last_uncertainty = uncertainty / self.mcu.seconds_to_clock(1.)
            self.state = "running"

    def _handle_fault(self, params):
        with self.lock:
            self.state = "faulted"
            self.last_status = params["status"]
            self.mcu_drops = params["dropped"]
            self.last_sequence = params["sequence"]

    def _handle_status(self, params):
        with self.lock:
            self.state = STATE_NAMES.get(params["state"], "unknown")
            self.mcu_drops = params["dropped"]
            self.last_status = params["status"]
            self.epoch = params["epoch"]
            self.last_sequence = params["sequence"]

    def _process_batch(self, eventtime):
        with self.lock:
            data, self.pending = self.pending, []
            result = {
                "data": data,
                "state": self.state,
                "epoch": self.epoch,
                "sequence": self.last_sequence,
                "sequence_gaps": self.sequence_gaps,
                "host_drops": self.host_drops,
                "mcu_drops": self.mcu_drops,
                "status": self.last_status,
                "uncertainty": self.last_uncertainty,
            }
        return result

    def get_status(self, eventtime):
        with self.lock:
            return {
                "state": self.state,
                "sample_rate": self.sample_rate,
                "channels": dict(zip(self.channel_names, self.last_values)),
                "epoch": self.epoch,
                "sequence": self.last_sequence,
                "sequence_gaps": self.sequence_gaps,
                "host_drops": self.host_drops,
                "mcu_drops": self.mcu_drops,
                "status": self.last_status,
                "uncertainty": self.last_uncertainty,
            }

    def _start(self):
        if self.start_cmd is None or self.mcu.is_fileoutput():
            return
        reactor = self.printer.get_reactor()
        print_time = self.mcu.estimated_print_time(reactor.monotonic()) + .100
        start_clock = self.mcu.print_time_to_clock(print_time)
        self.start_cmd.send([
            self.oid, start_clock, self.period_ticks,
            self.block_scans * len(self.pins), self.traffic_class])
        self.state = "armed"

    cmd_ADC_STREAM_START_help = "Start a DMA-backed ADC acquisition stream"
    def cmd_ADC_STREAM_START(self, gcmd):
        self._start()

    cmd_ADC_STREAM_STOP_help = "Stop a DMA-backed ADC acquisition stream"
    def cmd_ADC_STREAM_STOP(self, gcmd):
        if self.stop_cmd is not None and not self.mcu.is_fileoutput():
            self.stop_cmd.send([self.oid])
        self.state = "stopped"

    cmd_ADC_STREAM_STATUS_help = "Report DMA ADC stream state and counters"
    def cmd_ADC_STREAM_STATUS(self, gcmd):
        if self.query_cmd is not None and not self.mcu.is_fileoutput():
            self.query_cmd.send([self.oid])
        status = self.get_status(self.printer.get_reactor().monotonic())
        gcmd.respond_info(
            "adc_stream %s: state=%s rate=%.3fHz epoch=%s sequence=%s"
            " gaps=%d host_drops=%d mcu_drops=%d status=0x%x"
            % (self.name, status["state"], status["sample_rate"],
               status["epoch"], status["sequence"], status["sequence_gaps"],
               status["host_drops"], status["mcu_drops"], status["status"]))


def load_config_prefix(config):
    return ADCStream(config)
