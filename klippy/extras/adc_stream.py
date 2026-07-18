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
SUMMARY_CLASSES = {"scheduled": 0, "prompt": 1, "telemetry": 2}
SAFETY_ACTIONS = {"none": 0, "hold": 1, "trigger": 2, "shutdown": 3}
SUMMARY_MODES = {"aggregate": 0, "latest": 1}
STATE_NAMES = {0: "stopped", 1: "armed", 2: "running", 3: "faulted"}
SUMMARY_FORMAT = (
    "oid=%c sub=%c sequence=%u epoch=%u first_clock=%u last_clock=%u"
    " uncertainty=%u status=%u count=%hu min=%u max=%u"
    " sum_lo=%u sum_hi=%u shift=%c")


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
        if getattr(self.mcu, "_helix_explicit_adc_stream", False):
            raise config.error("Only one explicit adc_stream may own an MCU")
        self.mcu._helix_explicit_adc_stream = True
        self.pins = [p["pin"] for p in pin_params]
        self.channel_names = config.getlist("channel_names", pin_descs)
        if len(self.channel_names) != len(self.pins):
            raise config.error("channel_names must match the number of pins")
        self.sample_rate = config.getfloat("sample_rate", 1000., above=0.)
        self.hardware_oversample = config.getint(
            "hardware_oversample", 1, minval=1, maxval=256)
        if self.hardware_oversample & (self.hardware_oversample - 1):
            raise config.error("hardware_oversample must be a power of two")
        default_hw_shift = self.hardware_oversample.bit_length() - 1
        self.hardware_shift = config.getint(
            "hardware_shift", default_hw_shift, minval=0, maxval=8)
        max_values = self.mcu.get_constants().get(
            "ADC_STREAM_MAX_BLOCK_VALUES", 64)
        max_scans = max_values // len(self.pins)
        self.block_scans = config.getint(
            "block_scans", max_scans, minval=1, maxval=max_scans)
        self.traffic_class = config.getchoice(
            "traffic_class", TRAFFIC_CLASSES, default="telemetry")
        self.max_pending = config.getint(
            "max_pending_samples", 4096, minval=16)
        count = len(self.pins)
        self.input_divs = config.getintlist(
            "input_div", [1] * count, count=count)
        self.oversamples = config.getintlist(
            "oversample", [1] * count, count=count)
        default_shifts = [
            value.bit_length() - 1
            if value > 0 and not value & (value - 1) else 0
            for value in self.oversamples]
        self.filter_shifts = config.getintlist(
            "filter_shift", default_shifts, count=count)
        default_report_divs = [max(
            1, (self.block_scans + inp * osr - 1) // (inp * osr))
            for inp, osr in zip(self.input_divs, self.oversamples)]
        self.report_divs = config.getintlist(
            "report_div", default_report_divs, count=count)
        if any(not 1 <= value <= 0xffff for value in self.input_divs):
            raise config.error("input_div values must be between 1 and 65535")
        if any(not 1 <= value <= 256 for value in self.oversamples):
            raise config.error("oversample values must be between 1 and 256")
        if any(not 0 <= value <= 31 for value in self.filter_shifts):
            raise config.error("filter_shift values must be between 0 and 31")
        if any(not 1 <= value <= 4096 for value in self.report_divs):
            raise config.error("report_div values must be between 1 and 4096")
        self.summaries_enabled = config.getboolean("summaries", True)
        self.raw_output = config.getboolean("raw_output", True)
        self.capture_pre_blocks = config.getint(
            "capture_pre_blocks", 0, minval=0, maxval=7)
        self.capture_post_blocks = config.getint(
            "capture_post_blocks", 0, minval=0, maxval=7)
        if self.capture_pre_blocks + self.capture_post_blocks > 7:
            raise config.error(
                "capture_pre_blocks + capture_post_blocks must be <= 7")
        if not self.summaries_enabled and not self.raw_output:
            raise config.error("adc_stream must enable summaries or raw_output")
        default_class = ("scheduled" if self.traffic_class == 0 else
                         "prompt" if self.traffic_class == 1 else
                         "telemetry")
        self.summary_class = config.getchoice(
            "summary_class", SUMMARY_CLASSES, default=default_class)
        self.summary_mode = config.getchoice(
            "summary_mode", SUMMARY_MODES, default="aggregate")
        default_deadline = .050 if self.summary_class == 0 else 0.
        self.summary_deadline = config.getfloat(
            "summary_deadline", default_deadline, minval=0.)
        default_action = "hold" if self.summary_class == 0 else "none"
        self.safety_action = config.getchoice(
            "safety_action", SAFETY_ACTIONS, default=default_action)
        self.safety_low = config.getint("safety_low", 0, minval=0)
        self.safety_high = config.getint(
            "safety_high", 0xffffffff, minval=0, maxval=0xffffffff)
        self.safety_fault_count = config.getint(
            "safety_fault_count", 0, minval=0, maxval=255)
        self.safety_trigger_oid = config.getint(
            "safety_trigger_oid", 0xff, minval=0, maxval=0xff)
        if self.summary_class == 0 and not self.summary_deadline:
            raise config.error(
                "scheduled ADC summaries require summary_deadline")
        if ((self.summary_deadline or self.safety_fault_count)
                and not self.safety_action):
            raise config.error(
                "ADC deadline/threshold policy requires safety_action")
        if self.safety_low > self.safety_high:
            raise config.error("safety_low must not exceed safety_high")
        if (self.safety_action == SAFETY_ACTIONS["trigger"]
                and self.safety_trigger_oid == 0xff):
            raise config.error(
                "trigger safety action requires safety_trigger_oid")
        self.oid = self.mcu.create_oid()
        self.adc_max = 4095.
        self.period_ticks = 0
        self.start_cmd = self.stop_cmd = self.query_cmd = None
        self.lock = threading.Lock()
        self.pending = []
        self.pending_captures = []
        self.raw_chunks = {}
        self.pending_summaries = []
        self.last_values = [None] * len(self.pins)
        self.state = "configuring"
        self.epoch = self.last_sequence = None
        self.sequence_gaps = self.host_drops = self.mcu_drops = 0
        self.ready_highwater = self.dma_errors = self.adc_errors = 0
        self.overruns = self.telemetry_drops = self.watchdog_events = 0
        self.publications = self.publication_ticks = 0
        self.publication_ticks_max = self.consumers = 0
        self.consumer_ticks = self.consumer_ticks_max = 0
        self.safety_events = 0
        self.last_safety = None
        self.summary_sequences = [None] * len(self.pins)
        self.summary_epochs = [None] * len(self.pins)
        self.summary_gaps = 0
        self.last_status = self.last_uncertainty = 0
        self.capabilities = {}
        self.calibration = {"scheme": 0}
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
        gcode.register_mux_command(
            "ADC_STREAM_CAPTURE", "SENSOR", self.name,
            self.cmd_ADC_STREAM_CAPTURE, desc=self.cmd_ADC_STREAM_CAPTURE_help)

    def _build_config(self):
        if self.mcu.try_lookup_command("config_adc_stream oid=%c") is None:
            raise self.printer.config_error(
                "MCU for adc_stream '%s' lacks DMA ADC stream support"
                % (self.name,))
        self.adc_max = float(self.mcu.get_constants().get("ADC_MAX", 4095))
        caps = self.mcu.get_constants().get("ADC_STREAM_CAPS", 0)
        if self.hardware_oversample > 1 and not caps & (1 << 8):
            raise self.printer.config_error(
                "MCU for adc_stream '%s' lacks hardware oversampling"
                % (self.name,))
        self.adc_max *= self.hardware_oversample / float(
            1 << self.hardware_shift)
        self.period_ticks = max(
            1, self.mcu.seconds_to_clock(1. / self.sample_rate))
        self.mcu.add_config_cmd("config_adc_stream oid=%d" % (self.oid,))
        self.mcu.add_config_cmd(
            "adc_stream_set_hardware_oversample oid=%d ratio=%d shift=%d"
            % (self.oid, self.hardware_oversample, self.hardware_shift))
        for pin in self.pins:
            self.mcu.add_config_cmd(
                "adc_stream_add_channel oid=%d pin=%s" % (self.oid, pin))
        if self.summaries_enabled:
            for channel, values in enumerate(zip(
                    self.input_divs, self.oversamples, self.filter_shifts,
                    self.report_divs)):
                input_div, osr, shift, report_div = values
                self.mcu.add_config_cmd(
                    "adc_stream_subscribe oid=%d sub=%d channel=%d"
                    " input_div=%d osr=%d shift=%d report_div=%d"
                    " report_class=%d" % (
                        self.oid, channel, channel, input_div, osr, shift,
                        report_div, self.summary_class))
                self.mcu.add_config_cmd(
                    "adc_stream_set_subscription_options oid=%d sub=%d"
                    " summary_mode=%d" % (
                        self.oid, channel, self.summary_mode))
                deadline_ticks = self.mcu.seconds_to_clock(
                    self.summary_deadline)
                self.mcu.add_config_cmd(
                    "adc_stream_set_safety oid=%d sub=%d"
                    " deadline_ticks=%d fail_action=%d low=%d high=%d"
                    " fault_count=%d trigger_oid=%d" % (
                        self.oid, channel, deadline_ticks,
                        self.safety_action, self.safety_low,
                        self.safety_high, self.safety_fault_count,
                        self.safety_trigger_oid))
        self.mcu.add_config_cmd(
            "adc_stream_set_options oid=%d raw_output=%d"
            % (self.oid, self.raw_output))
        if self.capture_pre_blocks or self.capture_post_blocks:
            self.mcu.add_config_cmd(
                "adc_stream_arm_capture oid=%d pre_blocks=%d post_blocks=%d"
                % (self.oid, self.capture_pre_blocks,
                   self.capture_post_blocks))
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
        self.capabilities_cmd = self.mcu.lookup_command(
            "adc_stream_get_capabilities oid=%c", cq=cq)
        self.calibration_cmd = None
        cal_format = "adc_stream_get_calibration oid=%c"
        if self.mcu.try_lookup_command(cal_format) is not None:
            self.calibration_cmd = self.mcu.lookup_command(cal_format, cq=cq)
            self.mcu.register_serial_response(
                self._handle_calibration,
                "adc_stream_calibration oid=%c scheme=%c zero_mv=%hu"
                " full_mv=%hu attenuation=%c", self.oid)
        self.capture_cmd = self.mcu.lookup_command(
            "adc_stream_arm_capture oid=%c pre_blocks=%c post_blocks=%c",
            cq=cq)
        self.ack_cmd = self.mcu.lookup_command(
            "adc_stream_ack oid=%c sub=%c sequence=%u", cq=cq)
        data_format = (
            "adc_stream_data_telemetry oid=%c sequence=%u epoch=%u"
            " first_clock=%u period_num=%u period_den=%u uncertainty=%u"
            " channels=%c status=%u offset=%c total=%c values=%*s")
        self.mcu.register_serial_response(
            self._handle_data, data_format, self.oid)
        self.mcu.register_serial_response(
            self._handle_capture_data, data_format.replace(
                "adc_stream_data_telemetry", "adc_stream_data_prompt"),
            self.oid)
        self.mcu.register_serial_response(
            self._handle_fault,
            "adc_stream_fault oid=%c status=%u dropped=%u sequence=%u",
            self.oid)
        self.mcu.register_serial_response(
            self._handle_status,
            "adc_stream_status oid=%c state=%c class=%c channels=%c"
            " block_values=%c epoch=%u sequence=%u dropped=%u status=%u"
            " ready_highwater=%c dma_errors=%u adc_errors=%u overruns=%u"
            " telemetry_drops=%u watchdog_events=%u"
            " publications=%u publication_ticks=%u"
            " publication_ticks_max=%u consumers=%u consumer_ticks=%u"
            " consumer_ticks_max=%u",
            self.oid)
        self.mcu.register_serial_response(
            self._handle_summary, "adc_stream_prompt " + SUMMARY_FORMAT,
            self.oid)
        self.mcu.register_serial_response(
            self._handle_summary, "adc_stream_telemetry " + SUMMARY_FORMAT,
            self.oid)
        self.mcu.register_serial_response(
            self._handle_scheduled,
            "adc_stream_scheduled " + SUMMARY_FORMAT + " deadline=%u",
            self.oid)
        self.mcu.register_serial_response(
            self._handle_safety,
            "adc_stream_safety oid=%c sub=%c event=%c action=%c"
            " clock=%u status=%u count=%u", self.oid)
        self.mcu.register_serial_response(
            self._handle_capabilities,
            "adc_stream_capabilities oid=%c version=%c max_channels=%c"
            " max_subscriptions=%c max_osr=%hu caps=%u dma_pool=%hu"
            " dma_used=%hu dma_claims=%c backend_caps=%u max_rate=%u"
            " max_hw_osr=%hu resolution=%c adc_count=%c watchdogs=%c"
            " timing_quality=%c", self.oid)
        self.state = "armed"

    def _handle_capture_data(self, params):
        self._handle_data(params, capture=True)

    def _handle_data(self, params, capture=False):
        channels = params["channels"]
        payload = bytes(params["values"])
        offset, total = params["offset"], params["total"]
        key = (capture, params["epoch"], params["sequence"])
        if (not total or total > 2 * 64 or offset + len(payload) > total
                or offset % 2 or len(payload) % 2):
            logging.warning("adc_stream %s received malformed raw chunk",
                            self.name)
            self.last_status |= 1 << 3
            return
        chunks = self.raw_chunks.setdefault(key, {})
        chunks[offset] = payload
        cursor, assembled = 0, bytearray()
        for chunk_offset, chunk in sorted(chunks.items()):
            if chunk_offset != cursor:
                return
            assembled.extend(chunk)
            cursor += len(chunk)
        if cursor != total:
            if len(self.raw_chunks) > 16:
                oldest = next(iter(self.raw_chunks))
                del self.raw_chunks[oldest]
                self.host_drops += 1
            return
        del self.raw_chunks[key]
        payload = bytes(assembled)
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
            destination = self.pending_captures if capture else self.pending
            room = self.max_pending - len(destination)
            if room < len(samples):
                drop = len(samples) - max(0, room)
                self.host_drops += drop
                del samples[:drop]
            destination.extend(samples)
            if raw:
                self.last_values = [v / self.adc_max for v in raw[-channels:]]
            self.last_status = params["status"]
            self.last_uncertainty = uncertainty / self.mcu.seconds_to_clock(1.)
            self.state = "running"

    def _handle_summary(self, params):
        sub = params["sub"]
        if sub >= len(self.pins) or not params["count"]:
            logging.warning("adc_stream %s received malformed summary",
                            self.name)
            return
        sequence = params["sequence"]
        epoch = params["epoch"]
        previous = (self.summary_sequences[sub]
                    if self.summary_epochs[sub] == epoch else None)
        if previous is not None:
            self.summary_gaps += (sequence - previous - 1) & 0xffffffff
        self.summary_sequences[sub] = sequence
        self.summary_epochs[sub] = epoch
        self.epoch = epoch
        total = params["sum_lo"] | (params["sum_hi"] << 32)
        scale = ((1 << self.filter_shifts[sub])
                 / float(self.oversamples[sub] * self.adc_max))
        value = total * scale / params["count"]
        last_clock = self.mcu.clock32_to_clock64(params["last_clock"])
        last_time = self.mcu.clock_to_print_time(last_clock)
        record = {
            "channel": self.channel_names[sub], "time": last_time,
            "value": value, "count": params["count"],
            "minimum": params["min"] * scale,
            "maximum": params["max"] * scale,
            "sequence": sequence, "epoch": params["epoch"],
            "status": params["status"],
        }
        with self.lock:
            self.last_values[sub] = value
            self.last_status |= params["status"]
            self.pending_summaries.append(record)
            if len(self.pending_summaries) > self.max_pending:
                self.pending_summaries.pop(0)
                self.host_drops += 1
            self.state = "running"

    def _handle_scheduled(self, params):
        # Store the safety-class sample before acknowledging it.  This makes
        # the acknowledgement mean that Klippy actually accepted the record,
        # rather than merely receiving its transport frame.
        self._handle_summary(params)
        if self.ack_cmd is not None and not self.mcu.is_fileoutput():
            self.ack_cmd.send([self.oid, params["sub"], params["sequence"]])

    def _handle_safety(self, params):
        with self.lock:
            self.safety_events = params["count"]
            self.watchdog_events = max(self.watchdog_events, params["count"])
            self.last_status |= params["status"]
            self.last_safety = {
                key: params[key] for key in (
                    "sub", "event", "action", "clock", "status", "count")}

    def _handle_capabilities(self, params):
        with self.lock:
            self.capabilities = {
                key: params[key] for key in (
                    "version", "max_channels", "max_subscriptions",
                    "max_osr", "caps", "dma_pool", "dma_used",
                    "dma_claims", "backend_caps", "max_rate",
                    "max_hw_osr", "resolution", "adc_count",
                    "watchdogs", "timing_quality")}

    def _handle_calibration(self, params):
        with self.lock:
            self.calibration = {key: params[key] for key in (
                "scheme", "zero_mv", "full_mv", "attenuation")}

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
            for key in ("ready_highwater", "dma_errors", "adc_errors",
                        "overruns", "telemetry_drops", "watchdog_events",
                        "publications", "publication_ticks",
                        "publication_ticks_max", "consumers",
                        "consumer_ticks", "consumer_ticks_max"):
                setattr(self, key, params[key])

    def _process_batch(self, eventtime):
        with self.lock:
            data, self.pending = self.pending, []
            captures, self.pending_captures = self.pending_captures, []
            summaries, self.pending_summaries = self.pending_summaries, []
            result = {
                "data": data,
                "captures": captures,
                "summaries": summaries,
                "state": self.state,
                "epoch": self.epoch,
                "sequence": self.last_sequence,
                "sequence_gaps": self.sequence_gaps,
                "summary_gaps": self.summary_gaps,
                "host_drops": self.host_drops,
                "mcu_drops": self.mcu_drops,
                "ready_highwater": self.ready_highwater,
                "dma_errors": self.dma_errors,
                "adc_errors": self.adc_errors,
                "overruns": self.overruns,
                "telemetry_drops": self.telemetry_drops,
                "watchdog_events": self.watchdog_events,
                "publications": self.publications,
                "publication_ticks": self.publication_ticks,
                "publication_ticks_max": self.publication_ticks_max,
                "consumers": self.consumers,
                "consumer_ticks": self.consumer_ticks,
                "consumer_ticks_max": self.consumer_ticks_max,
                "safety_events": self.safety_events,
                "last_safety": self.last_safety,
                "status": self.last_status,
                "uncertainty": self.last_uncertainty,
                "capabilities": dict(self.capabilities),
                "calibration": dict(self.calibration),
            }
        return result

    def get_status(self, eventtime):
        with self.lock:
            voltages = None
            if self.calibration.get("scheme"):
                zero = self.calibration["zero_mv"] / 1000.
                span = (self.calibration["full_mv"]
                        - self.calibration["zero_mv"]) / 1000.
                voltages = dict(zip(self.channel_names, [
                    None if value is None else zero + value * span
                    for value in self.last_values]))
            return {
                "state": self.state,
                "sample_rate": self.sample_rate,
                "channels": dict(zip(self.channel_names, self.last_values)),
                "epoch": self.epoch,
                "sequence": self.last_sequence,
                "sequence_gaps": self.sequence_gaps,
                "summary_gaps": self.summary_gaps,
                "host_drops": self.host_drops,
                "mcu_drops": self.mcu_drops,
                "ready_highwater": self.ready_highwater,
                "dma_errors": self.dma_errors,
                "adc_errors": self.adc_errors,
                "overruns": self.overruns,
                "telemetry_drops": self.telemetry_drops,
                "watchdog_events": self.watchdog_events,
                "publications": self.publications,
                "publication_ticks": self.publication_ticks,
                "publication_ticks_max": self.publication_ticks_max,
                "consumers": self.consumers,
                "consumer_ticks": self.consumer_ticks,
                "consumer_ticks_max": self.consumer_ticks_max,
                "safety_events": self.safety_events,
                "last_safety": self.last_safety,
                "status": self.last_status,
                "uncertainty": self.last_uncertainty,
                "capabilities": dict(self.capabilities),
                "calibration": dict(self.calibration),
                "voltages": voltages,
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
            self.capabilities_cmd.send([self.oid])
            if self.calibration_cmd is not None:
                self.calibration_cmd.send([self.oid])
        status = self.get_status(self.printer.get_reactor().monotonic())
        gcmd.respond_info(
            "adc_stream %s: state=%s rate=%.3fHz epoch=%s sequence=%s"
            " gaps=%d summary_gaps=%d host_drops=%d mcu_drops=%d status=0x%x"
            % (self.name, status["state"], status["sample_rate"],
               status["epoch"], status["sequence"], status["sequence_gaps"],
               status["summary_gaps"],
               status["host_drops"], status["mcu_drops"], status["status"]))

    cmd_ADC_STREAM_CAPTURE_help = "Arm bounded pre/post ADC fault capture"
    def cmd_ADC_STREAM_CAPTURE(self, gcmd):
        pre = gcmd.get_int("PRE", self.capture_pre_blocks,
                           minval=0, maxval=7)
        post = gcmd.get_int("POST", self.capture_post_blocks,
                            minval=0, maxval=7)
        if pre + post > 7:
            raise gcmd.error("PRE + POST must be <= 7")
        if self.capture_cmd is not None and not self.mcu.is_fileoutput():
            self.capture_cmd.send([self.oid, pre, post])


def load_config_prefix(config):
    return ADCStream(config)
