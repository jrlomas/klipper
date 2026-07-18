# Interface to Klipper micro-controller code
#
# Copyright (C) 2016-2026  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import sys, os, zlib, logging, math, struct
import serialhdl, msgproto, pins, chelper, clocksync

class error(Exception):
    pass

# Minimum time host needs to get scheduled events queued into mcu
MIN_SCHEDULE_TIME = 0.100
# The maximum number of clock cycles an MCU is expected
# to schedule into the future, due to the protocol and firmware.
MAX_SCHEDULE_TICKS = (1<<31) - 1
# Maximum time all MCUs can internally schedule into the future.
# Directly caused by the limitation of MAX_SCHEDULE_TICKS.
MAX_NOMINAL_DURATION = 3.0

def _format_reset_reason(flags):
    flags &= 0x03
    reasons = []
    if flags & 0x01:
        reasons.append("watchdog timer expiry")
    if flags & 0x02:
        reasons.append("forced watchdog reset")
    if not reasons:
        reasons.append("power-on/external/ARM reset")
    return ", ".join(reasons), flags

######################################################################
# Command transmit helper classes
######################################################################

# Generate a dummy response to query commands when in debugging mode
class DummyResponse:
    def __init__(self, serial, name, oid=None):
        params = {}
        if oid is not None:
            params['oid'] = oid
        msgparser = serial.get_msgparser()
        resp = msgparser.create_dummy_response(name, params)
        resp['#sent_time'] = 0.
        resp['#receive_time'] = 0.
        self._response = resp
    def get_response(self, cmds, cmd_queue, minclock=0, reqclock=0, retry=True):
        return dict(self._response)

# Class to retry sending of a query command until a given response is received
class RetryAsyncCommand:
    TIMEOUT_TIME = 5.0
    RETRY_TIME = 0.500
    def __init__(self, serial, name, oid=None):
        self.serial = serial
        self.name = name
        self.oid = oid
        self.reactor = serial.get_reactor()
        self.completion = self.reactor.completion()
        self.min_query_time = self.reactor.monotonic()
        self.need_response = True
        self.serial.register_response(self.handle_callback, name, oid)
    def handle_callback(self, params):
        if self.need_response and params['#sent_time'] >= self.min_query_time:
            self.need_response = False
            self.reactor.async_complete(self.completion, params)
    def get_response(self, cmds, cmd_queue, minclock=0, reqclock=0, retry=True):
        cmd, = cmds
        self.serial.raw_send_wait_ack(cmd, minclock, reqclock, cmd_queue)
        self.min_query_time = 0.
        timeout_time = query_time = self.reactor.monotonic()
        if retry:
            timeout_time += self.TIMEOUT_TIME
        while 1:
            params = self.completion.wait(query_time + self.RETRY_TIME)
            if params is not None:
                self.serial.register_response(None, self.name, self.oid)
                return params
            query_time = self.reactor.monotonic()
            if query_time > timeout_time:
                self.serial.register_response(None, self.name, self.oid)
                raise serialhdl.error("Timeout on wait for '%s' response"
                                      % (self.name,))
            self.serial.raw_send(cmd, minclock, minclock, cmd_queue)

# Wrapper around query commands
class CommandQueryWrapper:
    def __init__(self, conn_helper, msgformat, respformat, oid=None,
                 cmd_queue=None, is_async=False):
        self._serial = serial = conn_helper.get_serial()
        self._cmd = serial.get_msgparser().lookup_command(msgformat)
        serial.get_msgparser().lookup_command(respformat)
        self._response = respformat.split()[0]
        self._oid = oid
        self._error = conn_helper.get_mcu().get_printer().command_error
        self._xmit_helper = serialhdl.SerialRetryCommand
        if conn_helper.get_mcu().is_fileoutput():
            self._xmit_helper = DummyResponse
        elif is_async:
            self._xmit_helper = RetryAsyncCommand
        if cmd_queue is None:
            cmd_queue = serial.get_default_command_queue()
        self._cmd_queue = cmd_queue
    def _do_send(self, cmds, minclock, reqclock, retry):
        xh = self._xmit_helper(self._serial, self._response, self._oid)
        reqclock = max(minclock, reqclock)
        try:
            return xh.get_response(cmds, self._cmd_queue, minclock, reqclock,
                                   retry)
        except serialhdl.error as e:
            raise self._error(str(e))
    def send(self, data=(), minclock=0, reqclock=0, retry=True):
        return self._do_send([self._cmd.encode(data)], minclock, reqclock,
                             retry)
    def send_with_preface(self, preface_cmd, preface_data=(), data=(),
                          minclock=0, reqclock=0, retry=True):
        cmds = [preface_cmd._cmd.encode(preface_data), self._cmd.encode(data)]
        return self._do_send(cmds, minclock, reqclock, retry)

# Wrapper around command sending
class CommandWrapper:
    def __init__(self, conn_helper, msgformat, cmd_queue=None):
        self._serial = serial = conn_helper.get_serial()
        msgparser = serial.get_msgparser()
        self._cmd = msgparser.lookup_command(msgformat)
        if cmd_queue is None:
            cmd_queue = serial.get_default_command_queue()
        self._cmd_queue = cmd_queue
        self._msgtag = msgparser.lookup_msgid(msgformat) & 0xffffffff
        if conn_helper.get_mcu().is_fileoutput():
            # Can't use send_wait_ack when in debugging mode
            self.send_wait_ack = self.send
    def send(self, data=(), minclock=0, reqclock=0):
        cmd = self._cmd.encode(data)
        self._serial.raw_send(cmd, minclock, reqclock, self._cmd_queue)
    def send_wait_ack(self, data=(), minclock=0, reqclock=0):
        cmd = self._cmd.encode(data)
        self._serial.raw_send_wait_ack(cmd, minclock, reqclock, self._cmd_queue)
    def get_command_tag(self):
        return self._msgtag

# Wrapper for long-lived serial subscriptions (callbacks via background thread)
class AsyncResponseWrapper:
    def __init__(self, conn_helper, cfg_helper, callback, msgformat, oid=None):
        self._serial = conn_helper.get_serial()
        self._callback = callback
        self._msgformat = msgformat
        self._name = msgformat.split()[0]
        self._oid = oid
        if cfg_helper.is_config_finalized():
            self._register()
        else:
            self._serial.register_response((lambda p: None), self._name, oid)
            cfg_helper.register_post_init_callback(self._register)
    def _register(self):
        self._serial.get_msgparser().lookup_command(self._msgformat)
        self._serial.register_response(self._callback, self._name, self._oid)
    def unregister(self):
        self._serial.register_response(None, self._name, self._oid)


######################################################################
# Wrapper classes for MCU pins
######################################################################

class MCU_trsync:
    REASON_ENDSTOP_HIT = 1
    REASON_HOST_REQUEST = 2
    REASON_PAST_END_TIME = 3
    REASON_COMMS_TIMEOUT = 4
    def __init__(self, mcu, trdispatch):
        self._mcu = mcu
        self._trdispatch = trdispatch
        self._reactor = mcu.get_printer().get_reactor()
        self._steppers = []
        self._trdispatch_mcu = None
        self._oid = mcu.create_oid()
        self._cmd_queue = mcu.alloc_command_queue()
        self._response_trsync = None
        self._trsync_start_cmd = self._trsync_set_timeout_cmd = None
        self._trsync_trigger_cmd = self._trsync_query_cmd = None
        self._stepper_stop_cmds = {}
        self._trigger_completion = None
        self._home_end_clock = None
        mcu.register_config_callback(self._build_config)
        printer = mcu.get_printer()
        printer.register_event_handler("klippy:shutdown", self._shutdown)
    def get_mcu(self):
        return self._mcu
    def get_oid(self):
        return self._oid
    def get_command_queue(self):
        return self._cmd_queue
    def add_stepper(self, stepper):
        if stepper in self._steppers:
            return
        self._steppers.append(stepper)
    def get_steppers(self):
        return list(self._steppers)
    def _build_config(self):
        mcu = self._mcu
        # Setup config
        mcu.add_config_cmd("config_trsync oid=%d" % (self._oid,))
        mcu.add_config_cmd(
            "trsync_start oid=%d report_clock=0 report_ticks=0 expire_reason=0"
            % (self._oid,), on_restart=True)
        # Lookup commands
        self._trsync_start_cmd = mcu.lookup_command(
            "trsync_start oid=%c report_clock=%u report_ticks=%u"
            " expire_reason=%c", cq=self._cmd_queue)
        self._trsync_set_timeout_cmd = mcu.lookup_command(
            "trsync_set_timeout oid=%c clock=%u", cq=self._cmd_queue)
        self._trsync_trigger_cmd = mcu.lookup_command(
            "trsync_trigger oid=%c reason=%c", cq=self._cmd_queue)
        self._trsync_query_cmd = mcu.lookup_query_command(
            "trsync_trigger oid=%c reason=%c",
            "trsync_state oid=%c can_trigger=%c trigger_reason=%c clock=%u",
            oid=self._oid, cq=self._cmd_queue)
        legacy_stop = "stepper_stop_on_trigger oid=%c trsync_oid=%c"
        self._stepper_stop_cmds = {
            legacy_stop: mcu.lookup_command(legacy_stop, cq=self._cmd_queue)}
        # Create trdispatch_mcu object
        set_timeout_tag = mcu.lookup_command(
            "trsync_set_timeout oid=%c clock=%u").get_command_tag()
        trigger_cmd = mcu.lookup_command("trsync_trigger oid=%c reason=%c")
        trigger_tag = trigger_cmd.get_command_tag()
        state_cmd = mcu.lookup_command(
            "trsync_state oid=%c can_trigger=%c trigger_reason=%c clock=%u")
        state_tag = state_cmd.get_command_tag()
        ffi_main, ffi_lib = chelper.get_ffi()
        self._trdispatch_mcu = ffi_main.gc(ffi_lib.trdispatch_mcu_alloc(
            self._trdispatch, mcu._serial.get_serialqueue(), # XXX
            self._cmd_queue, self._oid, set_timeout_tag, trigger_tag,
            state_tag), ffi_lib.free)
    def _lookup_stepper_stop_cmd(self, stepper):
        # Each stepper reports its "stop on trigger" arming command
        # (trajectory steppers use traj_stop_on_trigger; legacy
        # steppers use stepper_stop_on_trigger)
        cmdname = stepper.get_stop_on_trigger_command_name()
        scmd = self._stepper_stop_cmds.get(cmdname)
        if scmd is None:
            scmd = self._mcu.lookup_command(cmdname, cq=self._cmd_queue)
            self._stepper_stop_cmds[cmdname] = scmd
        return scmd
    def _shutdown(self):
        tc = self._trigger_completion
        if tc is not None:
            self._trigger_completion = None
            tc.complete(False)
    def _handle_trsync_state(self, params):
        if not params['can_trigger']:
            tc = self._trigger_completion
            if tc is not None:
                self._trigger_completion = None
                reason = params['trigger_reason']
                is_failure = (reason >= self.REASON_COMMS_TIMEOUT)
                self._reactor.async_complete(tc, is_failure)
        elif self._home_end_clock is not None:
            clock = self._mcu.clock32_to_clock64(params['clock'])
            if clock >= self._home_end_clock:
                self._home_end_clock = None
                self._trsync_trigger_cmd.send([self._oid,
                                               self.REASON_PAST_END_TIME])
    def start(self, print_time, report_offset,
              trigger_completion, expire_timeout):
        self._trigger_completion = trigger_completion
        self._home_end_clock = None
        clock = self._mcu.print_time_to_clock(print_time)
        expire_ticks = self._mcu.seconds_to_clock(expire_timeout)
        expire_clock = clock + expire_ticks
        report_ticks = self._mcu.seconds_to_clock(expire_timeout * .3)
        report_clock = clock + int(report_ticks * report_offset + .5)
        min_extend_ticks = int(report_ticks * .8 + .5)
        ffi_main, ffi_lib = chelper.get_ffi()
        ffi_lib.trdispatch_mcu_setup(self._trdispatch_mcu, clock, expire_clock,
                                     expire_ticks, min_extend_ticks)
        self._response_trsync = self._mcu.register_serial_response(
            self._handle_trsync_state,
            "trsync_state oid=%c can_trigger=%c trigger_reason=%c clock=%u",
            self._oid)
        self._trsync_start_cmd.send([self._oid, report_clock, report_ticks,
                                     self.REASON_COMMS_TIMEOUT], reqclock=clock)
        for s in self._steppers:
            self._lookup_stepper_stop_cmd(s).send([s.get_oid(), self._oid])
        self._trsync_set_timeout_cmd.send([self._oid, expire_clock],
                                          reqclock=clock)
    def set_home_end_time(self, home_end_time):
        self._home_end_clock = self._mcu.print_time_to_clock(home_end_time)
    def stop(self):
        self._response_trsync.unregister()
        self._response_trsync = None
        self._trigger_completion = None
        if self._mcu.is_fileoutput():
            return self.REASON_ENDSTOP_HIT
        params = self._trsync_query_cmd.send([self._oid,
                                              self.REASON_HOST_REQUEST])
        for s in self._steppers:
            s.note_homing_end()
        return params['trigger_reason']

TRSYNC_TIMEOUT = 0.025
TRSYNC_SINGLE_MCU_TIMEOUT = 0.250

class TriggerDispatch:
    def __init__(self, mcu):
        self._mcu = mcu
        self._trigger_completion = None
        ffi_main, ffi_lib = chelper.get_ffi()
        self._trdispatch = ffi_main.gc(ffi_lib.trdispatch_alloc(), ffi_lib.free)
        self._trsyncs = [MCU_trsync(mcu, self._trdispatch)]
    def get_oid(self):
        return self._trsyncs[0].get_oid()
    def get_command_queue(self):
        return self._trsyncs[0].get_command_queue()
    def add_stepper(self, stepper):
        trsyncs = {trsync.get_mcu(): trsync for trsync in self._trsyncs}
        trsync = trsyncs.get(stepper.get_mcu())
        if trsync is None:
            trsync = MCU_trsync(stepper.get_mcu(), self._trdispatch)
            self._trsyncs.append(trsync)
        trsync.add_stepper(stepper)
        # Check for unsupported multi-mcu shared stepper rails
        sname = stepper.get_name()
        if sname.startswith('stepper_'):
            for ot in self._trsyncs:
                for s in ot.get_steppers():
                    if ot is not trsync and s.get_name().startswith(sname[:9]):
                        cerror = self._mcu.get_printer().config_error
                        raise cerror("Multi-mcu homing not supported on"
                                     " multi-mcu shared axis")
    def get_steppers(self):
        return [s for trsync in self._trsyncs for s in trsync.get_steppers()]
    def start(self, print_time):
        reactor = self._mcu.get_printer().get_reactor()
        self._trigger_completion = reactor.completion()
        expire_timeout = TRSYNC_TIMEOUT
        if len(self._trsyncs) == 1:
            expire_timeout = TRSYNC_SINGLE_MCU_TIMEOUT
        for i, trsync in enumerate(self._trsyncs):
            report_offset = float(i) / len(self._trsyncs)
            trsync.start(print_time, report_offset,
                         self._trigger_completion, expire_timeout)
        etrsync = self._trsyncs[0]
        ffi_main, ffi_lib = chelper.get_ffi()
        ffi_lib.trdispatch_start(self._trdispatch, etrsync.REASON_HOST_REQUEST)
        return self._trigger_completion
    def wait_end(self, end_time):
        etrsync = self._trsyncs[0]
        etrsync.set_home_end_time(end_time)
        if self._mcu.is_fileoutput():
            self._trigger_completion.complete(True)
        self._trigger_completion.wait()
    def stop(self):
        ffi_main, ffi_lib = chelper.get_ffi()
        ffi_lib.trdispatch_stop(self._trdispatch)
        res = [trsync.stop() for trsync in self._trsyncs]
        err_res = [r for r in res if r >= MCU_trsync.REASON_COMMS_TIMEOUT]
        if err_res:
            return err_res[0]
        return res[0]

# Number and spacing of the post-edge confirmation re-reads the firmware
# performs before it fires trsync for a hardware-triggered endstop
# (FD-0001 doc 09 "qualify-after-event").  A false edge costs one brief
# re-read burst and never fires; the whole window is bounded well under
# the firmware's QUALIFY_MAX_TICKS safety cap.
TRIGGER_QUALIFY_COUNT = 4
TRIGGER_QUALIFY_TIME = 0.000005
TRIGGER_SOURCE_TRIGGERED = 1 << 1

class MCU_endstop:
    def __init__(self, mcu, pin_params):
        self._mcu = mcu
        self._pin = pin_params['pin']
        self._pullup = pin_params['pullup']
        self._invert = pin_params['invert']
        self._oid = self._mcu.create_oid()
        self._home_cmd = self._query_cmd = None
        self._mcu.register_config_callback(self._build_config)
        self._rest_ticks = 0
        self._dispatch = TriggerDispatch(mcu)
        # Hardware interrupt trigger path (FD-0001 doc 09).  Resolved in
        # _build_config: a second oid drives config_trigger_gpio and the
        # arm/disarm/query commands; the polled config_endstop above is
        # always kept for query_endstop and as the automatic fallback.
        self._trigger_oid = None
        self._trigger_arm_cmd = self._trigger_disarm_cmd = None
        self._trigger_observe_cmd = None
        self._trigger_query_cmd = None
        self._trigger_edge = 0
        self._hw_triggered = False
        self._trigger_observing = False
    def get_mcu(self):
        return self._mcu
    def add_stepper(self, stepper):
        self._dispatch.add_stepper(stepper)
    def get_steppers(self):
        return self._dispatch.get_steppers()
    def _build_config(self):
        # Setup config (polled path - always present for query + fallback)
        self._mcu.add_config_cmd("config_endstop oid=%d pin=%s pull_up=%d"
                                 % (self._oid, self._pin, self._pullup))
        self._mcu.add_config_cmd(
            "endstop_home oid=%d clock=0 sample_ticks=0 sample_count=0"
            " rest_ticks=0 pin_value=0 trsync_oid=0 trigger_reason=0"
            % (self._oid,), on_restart=True)
        # Lookup commands
        cmd_queue = self._dispatch.get_command_queue()
        self._home_cmd = self._mcu.lookup_command(
            "endstop_home oid=%c clock=%u sample_ticks=%u sample_count=%c"
            " rest_ticks=%u pin_value=%c trsync_oid=%c trigger_reason=%c",
            cq=cmd_queue)
        self._query_cmd = self._mcu.lookup_query_command(
            "endstop_query_state oid=%c",
            "endstop_state oid=%c homing=%c next_clock=%u pin_value=%c",
            oid=self._oid, cq=cmd_queue)
        self._build_trigger_config(cmd_queue)
    def _build_trigger_config(self, cmd_queue):
        # Opt into the hardware edge-interrupt detector when the firmware
        # advertises the trigger_source command set and it is not disabled
        # on this MCU.  Falls back silently to the polled path otherwise.
        if not (self._mcu.want_hw_endstop_trigger()
                or self._mcu.want_hw_endstop_observer()):
            return
        if not self._mcu.check_valid_response(
                "config_trigger_gpio oid=%c pin=%u edge=%c pull_up=%c"
                " qualify_ticks=%u qualify_count=%c"):
            return
        # edge = the pin level that indicates a hit (matches the polled
        # path's pin_value for the default triggered=True homing move):
        # triggered(1) ^ invert.
        self._trigger_edge = 1 ^ (1 if self._invert else 0)
        qualify_ticks = self._mcu.seconds_to_clock(TRIGGER_QUALIFY_TIME)
        self._trigger_oid = self._mcu.create_oid()
        self._mcu.add_config_cmd(
            "config_trigger_gpio oid=%d pin=%s edge=%d pull_up=%d"
            " qualify_ticks=%u qualify_count=%d"
            % (self._trigger_oid, self._pin, self._trigger_edge,
               self._pullup, qualify_ticks, TRIGGER_QUALIFY_COUNT))
        self._mcu.add_config_cmd(
            "trigger_source_disarm oid=%d" % (self._trigger_oid,),
            on_restart=True)
        self._trigger_arm_cmd = self._mcu.lookup_command(
            "trigger_source_arm oid=%c trsync_oid=%c reason=%c capture=%c",
            cq=cmd_queue)
        # Always resolve the passive observer when the firmware provides the
        # trigger-source command set.  Normal homing still selects it only
        # when hardware_endstop_observer is enabled, but commissioning tools
        # may use the same edge latch without taking ownership of trsync.
        self._trigger_observe_cmd = self._mcu.lookup_command(
            "trigger_source_observe oid=%c capture=%c", cq=cmd_queue)
        self._trigger_disarm_cmd = self._mcu.lookup_command(
            "trigger_source_disarm oid=%c", cq=cmd_queue)
        self._trigger_query_cmd = self._mcu.lookup_query_command(
            "trigger_source_query oid=%c",
            "trigger_source_state oid=%c flags=%c clock=%u",
            oid=self._trigger_oid, cq=cmd_queue)
    def _use_hw_trigger(self, triggered):
        # The trigger source's edge sense is fixed at config time to the
        # triggered=True hit level; a homing move that instead waits for
        # the pin to release (triggered=False) uses the polled path, which
        # can look for either level per move.
        return (self._mcu.want_hw_endstop_trigger()
                and self._trigger_oid is not None
                and bool(triggered) == bool(self._trigger_edge ^ self._invert))
    def _use_hw_observer(self, triggered):
        return (not self._mcu.want_hw_endstop_trigger()
                and self._mcu.want_hw_endstop_observer()
                and self._trigger_observe_cmd is not None
                and bool(triggered) == bool(self._trigger_edge ^ self._invert))
    def has_edge_observer(self):
        return self._trigger_observe_cmd is not None
    def edge_observe_start(self, print_time, capture=True):
        if self._trigger_observe_cmd is None:
            raise self._mcu.get_printer().command_error(
                "MCU '%s' does not support passive edge observation"
                % (self._mcu.get_name(),))
        clock = self._mcu.print_time_to_clock(print_time)
        self._trigger_observe_cmd.send(
            [self._trigger_oid, bool(capture)], reqclock=clock)
    def edge_observe_query(self):
        if self._trigger_query_cmd is None:
            raise self._mcu.get_printer().command_error(
                "MCU '%s' does not support passive edge observation"
                % (self._mcu.get_name(),))
        params = self._trigger_query_cmd.send([self._trigger_oid])
        params['clock64'] = self._mcu.clock32_to_clock64(params['clock'])
        return params
    def edge_observe_disarm(self):
        if self._trigger_disarm_cmd is not None:
            self._trigger_disarm_cmd.send([self._trigger_oid])
    def home_start(self, print_time, sample_time, sample_count, rest_time,
                   triggered=True):
        clock = self._mcu.print_time_to_clock(print_time)
        rest_ticks = self._mcu.print_time_to_clock(print_time+rest_time) - clock
        self._rest_ticks = rest_ticks
        trigger_completion = self._dispatch.start(print_time)
        if self._use_hw_trigger(triggered):
            # Hardware edge interrupt fires trsync directly - no polling.
            self._hw_triggered = True
            self._trigger_arm_cmd.send(
                [self._trigger_oid, self._dispatch.get_oid(),
                 MCU_trsync.REASON_ENDSTOP_HIT, 1],
                reqclock=clock)
            return trigger_completion
        self._hw_triggered = False
        self._trigger_observing = self._use_hw_observer(triggered)
        if self._trigger_observing:
            # Timestamp through the GPIO ISR while the legacy poller remains
            # the only component allowed to fire trsync and stop motion.
            self._trigger_observe_cmd.send(
                [self._trigger_oid, 1], reqclock=clock)
        self._home_cmd.send(
            [self._oid, clock, self._mcu.seconds_to_clock(sample_time),
             sample_count, rest_ticks, triggered ^ self._invert,
             self._dispatch.get_oid(), MCU_trsync.REASON_ENDSTOP_HIT],
            reqclock=clock)
        return trigger_completion
    def home_wait(self, home_end_time):
        self._dispatch.wait_end(home_end_time)
        if self._hw_triggered:
            self._trigger_disarm_cmd.send([self._trigger_oid])
        else:
            self._home_cmd.send([self._oid, 0, 0, 0, 0, 0, 0, 0])
            if self._trigger_observing:
                self._trigger_disarm_cmd.send([self._trigger_oid])
        res = self._dispatch.stop()
        if res >= MCU_trsync.REASON_COMMS_TIMEOUT:
            cmderr = self._mcu.get_printer().command_error
            raise cmderr("Communication timeout during homing")
        if res != MCU_trsync.REASON_ENDSTOP_HIT:
            return 0.
        if self._mcu.is_fileoutput():
            return home_end_time
        if self._hw_triggered:
            # The firmware latched the exact edge tick (a hardware
            # input-capture timestamp when the pin is wired to one, else
            # the ISR-entry read); no rest_ticks back-dating needed.
            params = self._trigger_query_cmd.send([self._trigger_oid])
            tclock = self._mcu.clock32_to_clock64(params['clock'])
            return self._mcu.clock_to_print_time(tclock)
        if self._trigger_observing:
            params = self._trigger_query_cmd.send([self._trigger_oid])
            if params['flags'] & TRIGGER_SOURCE_TRIGGERED:
                oclock = self._mcu.clock32_to_clock64(params['clock'])
                logging.info("endstop observer mcu=%s oid=%d edge_clock=%d",
                             self._mcu.get_name(), self._trigger_oid, oclock)
            else:
                logging.warning("endstop observer mcu=%s oid=%d missed edge",
                                self._mcu.get_name(), self._trigger_oid)
        params = self._query_cmd.send([self._oid])
        next_clock = self._mcu.clock32_to_clock64(params['next_clock'])
        return self._mcu.clock_to_print_time(next_clock - self._rest_ticks)
    def query_endstop(self, print_time):
        clock = self._mcu.print_time_to_clock(print_time)
        if self._mcu.is_fileoutput():
            return 0
        params = self._query_cmd.send([self._oid], minclock=clock)
        return params['pin_value'] ^ self._invert

class MCU_digital_out:
    def __init__(self, mcu, pin_params):
        self._mcu = mcu
        self._oid = None
        self._mcu.register_config_callback(self._build_config)
        self._pin = pin_params['pin']
        self._invert = pin_params['invert']
        self._start_value = self._shutdown_value = self._invert
        self._max_duration = 2.
        self._last_clock = 0
        self._set_cmd = None
        self._machine_time = False
        self._machine_set_cmd = None
        self._timing_query_cmd = None
    def get_mcu(self):
        return self._mcu
    def setup_max_duration(self, max_duration):
        self._max_duration = max_duration
    def setup_start_value(self, start_value, shutdown_value):
        self._start_value = (not not start_value) ^ self._invert
        self._shutdown_value = (not not shutdown_value) ^ self._invert
    def setup_machine_time(self):
        self._machine_time = True
    def get_mcus(self):
        return [self._mcu]
    def _build_config(self):
        if self._max_duration and self._start_value != self._shutdown_value:
            raise pins.error("Pin with max duration must have start"
                             " value equal to shutdown value")
        mdur_ticks = self._mcu.seconds_to_clock(self._max_duration)
        if mdur_ticks > MAX_SCHEDULE_TICKS:
            raise pins.error("Digital pin max duration too large")
        self._mcu.request_move_queue_slot()
        self._oid = self._mcu.create_oid()
        self._mcu.add_config_cmd(
            "config_digital_out oid=%d pin=%s value=%d default_value=%d"
            " max_duration=%d" % (self._oid, self._pin, self._start_value,
                                  self._shutdown_value, mdur_ticks))
        self._mcu.add_config_cmd("update_digital_out oid=%d value=%d"
                                 % (self._oid, self._start_value),
                                 on_restart=True)
        cmd_queue = self._mcu.alloc_command_queue()
        self._set_cmd = self._mcu.lookup_command(
            "queue_digital_out oid=%c clock=%u on_ticks=%u", cq=cmd_queue)
        if self._machine_time:
            self._machine_set_cmd = self._mcu.lookup_command(
                "queue_machine_digital_out oid=%c clock=%u on_ticks=%u",
                cq=cmd_queue)
        query_format = "digital_out_query oid=%c"
        response_format = ("digital_out_state oid=%c value=%c dropped=%hu"
                           " scheduled=%u actual=%u late=%i")
        if (self._mcu.try_lookup_command(query_format, cq=cmd_queue)
                is not None
                and self._mcu.check_valid_response(response_format)):
            self._timing_query_cmd = self._mcu.lookup_query_command(
                query_format, response_format, oid=self._oid, cq=cmd_queue)
    def set_digital(self, print_time, value):
        clock = self._mcu.print_time_to_clock(print_time)
        self._set_cmd.send([self._oid, clock, (not not value) ^ self._invert],
                           minclock=self._last_clock, reqclock=clock)
        self._last_clock = clock
    def set_digital_machine_time(self, print_time, machine_clock, value):
        # reqclock/minclock remain local transport scheduling metadata; only
        # the command payload is the shared primary-MCU machine timestamp.
        local_clock = self._mcu.print_time_to_clock(print_time)
        self._machine_set_cmd.send(
            [self._oid, machine_clock & 0xffffffff,
             (not not value) ^ self._invert],
            minclock=self._last_clock, reqclock=local_clock)
        self._last_clock = local_clock
    def query_digital_timing(self):
        if self._timing_query_cmd is None:
            return []
        return [(self._mcu, self._timing_query_cmd.send([self._oid]))]
    def has_digital_timing(self):
        return self._timing_query_cmd is not None

class MCU_pwm:
    def __init__(self, mcu, pin_params):
        self._mcu = mcu
        self._hardware_pwm = False
        self._cycle_time = 0.100
        self._max_duration = 2.
        self._oid = None
        self._mcu.register_config_callback(self._build_config)
        self._pin = pin_params['pin']
        self._invert = pin_params['invert']
        self._start_value = self._shutdown_value = float(self._invert)
        self._last_clock = 0
        self._last_value = .0
        self._pwm_max = 0.
        self._set_cmd = None
    def get_mcu(self):
        return self._mcu
    def setup_max_duration(self, max_duration):
        self._max_duration = max_duration
    def setup_cycle_time(self, cycle_time, hardware_pwm=False):
        self._cycle_time = cycle_time
        self._hardware_pwm = hardware_pwm
    def setup_start_value(self, start_value, shutdown_value):
        if self._invert:
            start_value = 1. - start_value
            shutdown_value = 1. - shutdown_value
        self._start_value = max(0., min(1., start_value))
        self._shutdown_value = max(0., min(1., shutdown_value))
        self._last_value = self._start_value
    def _build_config(self):
        if self._max_duration and self._start_value != self._shutdown_value:
            raise pins.error("Pin with max duration must have start"
                             " value equal to shutdown value")
        cmd_queue = self._mcu.alloc_command_queue()
        curtime = self._mcu.get_printer().get_reactor().monotonic()
        printtime = self._mcu.estimated_print_time(curtime)
        self._last_clock = self._mcu.print_time_to_clock(printtime + 0.200)
        cycle_ticks = self._mcu.seconds_to_clock(self._cycle_time)
        mdur_ticks = self._mcu.seconds_to_clock(self._max_duration)
        if mdur_ticks > MAX_SCHEDULE_TICKS:
            raise pins.error("PWM pin max duration too large")
        if self._hardware_pwm:
            self._pwm_max = self._mcu.get_constant_float("PWM_MAX")
            self._mcu.request_move_queue_slot()
            self._oid = self._mcu.create_oid()
            self._mcu.add_config_cmd(
                "config_pwm_out oid=%d pin=%s cycle_ticks=%d value=%d"
                " default_value=%d max_duration=%d"
                % (self._oid, self._pin, cycle_ticks,
                   self._start_value * self._pwm_max,
                   self._shutdown_value * self._pwm_max, mdur_ticks))
            svalue = int(self._start_value * self._pwm_max + 0.5)
            self._mcu.add_config_cmd("queue_pwm_out oid=%d clock=%d value=%d"
                                     % (self._oid, self._last_clock, svalue),
                                     on_restart=True)
            self._set_cmd = self._mcu.lookup_command(
                "queue_pwm_out oid=%c clock=%u value=%hu", cq=cmd_queue)
            return
        # Software PWM
        if self._shutdown_value not in [0., 1.]:
            raise pins.error("shutdown value must be 0.0 or 1.0 on soft pwm")
        if cycle_ticks > MAX_SCHEDULE_TICKS:
            raise pins.error("PWM pin cycle time too large")
        self._mcu.request_move_queue_slot()
        self._oid = self._mcu.create_oid()
        self._mcu.add_config_cmd(
            "config_digital_out oid=%d pin=%s value=%d"
            " default_value=%d max_duration=%d"
            % (self._oid, self._pin, self._start_value >= 1.0,
               self._shutdown_value >= 0.5, mdur_ticks))
        self._mcu.add_config_cmd(
            "set_digital_out_pwm_cycle oid=%d cycle_ticks=%d"
            % (self._oid, cycle_ticks))
        # Software PWM is prompt state, not a Class-0 motion edge. A delayed
        # CAN update should be applied immediately while the independent
        # max_duration watchdog remains armed; shutting the whole MCU down
        # with "Timer too close" defeats trajectory pause-and-recovery.
        late_policy = self._mcu.try_lookup_command(
            "set_digital_out_late_policy oid=%c apply_late=%c")
        if late_policy is not None:
            self._mcu.add_config_cmd(
                "set_digital_out_late_policy oid=%d apply_late=1"
                % (self._oid,))
        self._pwm_max = float(cycle_ticks)
        svalue = int(self._start_value * cycle_ticks + 0.5)
        self._mcu.add_config_cmd(
            "queue_digital_out oid=%d clock=%d on_ticks=%d"
            % (self._oid, self._last_clock, svalue), is_init=True)
        self._set_cmd = self._mcu.lookup_command(
            "queue_digital_out oid=%c clock=%u on_ticks=%u", cq=cmd_queue)
    def next_aligned_print_time(self, print_time, allow_early=0.):
        # Filter cases where there is no need to sync anything
        if self._hardware_pwm:
            return print_time
        if self._last_value == 1. or self._last_value == .0:
            return print_time
        # Simplify the calling and allow scheduling slightly earlier
        req_ptime = print_time - min(allow_early, 0.5 * self._cycle_time)
        cycle_ticks = self._mcu.seconds_to_clock(self._cycle_time)
        req_clock = self._mcu.print_time_to_clock(req_ptime)
        last_clock = self._last_clock
        pulses = (req_clock - last_clock + cycle_ticks - 1) // cycle_ticks
        next_clock = last_clock + pulses * cycle_ticks
        return self._mcu.clock_to_print_time(next_clock)
    def set_pwm(self, print_time, value):
        if self._invert:
            value = 1. - value
        v = int(max(0., min(1., value)) * self._pwm_max + 0.5)
        clock = self._mcu.print_time_to_clock(print_time)
        self._set_cmd.send([self._oid, clock, v],
                           minclock=self._last_clock, reqclock=clock)
        self._last_clock = clock
        self._last_value = value

class MCU_adc:
    def __init__(self, mcu, pin_params):
        self._mcu = mcu
        self._pin = pin_params['pin']
        self._min_sample = self._max_sample = 0.
        self._sample_time = self._report_time = 0.
        self._sample_count = self._batch_num = self._range_check_count = 0
        self._report_clock = 0
        self._last_state = (0., 0.)
        self._oid = self._callback = None
        self._use_adc_stream = False
        self._adc_stream_class = 1
        self._adc_config_built = False
        self._mcu.register_config_callback(self._build_config)
        self._inv_max_adc = 0.
        self._unpack_from = struct.Struct('<H').unpack_from
        all_adcs = getattr(self._mcu, "_helix_all_adcs", None)
        if all_adcs is None:
            all_adcs = self._mcu._helix_all_adcs = []
        all_adcs.append(self)
        if getattr(self._mcu, "_adc_stream_mode", "off") != "off":
            self.setup_adc_stream(report_class=2)
    def get_mcu(self):
        return self._mcu
    def setup_adc_sample(self, report_time, sample_time=0., sample_count=1,
                         batch_num=1, minval=0., maxval=1.,
                         range_check_count=0):
        self._report_time = report_time
        self._sample_time = sample_time
        self._sample_count = sample_count
        self._batch_num = max(1, min(48 // 2, batch_num))
        self._min_sample = minval
        self._max_sample = maxval
        self._range_check_count = range_check_count
    def setup_adc_callback(self, callback):
        self._callback = callback
    def setup_adc_stream(self, report_class=1):
        """Prefer the merged DMA engine, retaining automatic legacy fallback."""
        if report_class not in (1, 2):
            raise ValueError("ADC stream report_class must be 1 or 2")
        self._use_adc_stream = True
        self._adc_stream_class = report_class
        manager = getattr(self._mcu, "_helix_adc_stream_manager", None)
        if manager is None:
            manager = MCUADCStreamManager(self._mcu)
            self._mcu._helix_adc_stream_manager = manager
        manager.add_adc(self)
    def get_last_value(self):
        return self._last_state
    def _build_config(self):
        if self._adc_config_built:
            return
        if self._use_adc_stream:
            return
        self._build_legacy_config()
    def _build_legacy_config(self):
        if self._adc_config_built or not self._sample_count:
            return
        self._adc_config_built = True
        self._oid = self._mcu.create_oid()
        self._mcu.add_config_cmd("config_analog_in oid=%d pin=%s"
                                 % (self._oid, self._pin))
        clock = self._mcu.get_query_slot(self._oid)
        sample_ticks = self._mcu.seconds_to_clock(self._sample_time)
        mcu_adc_max = self._mcu.get_constant_float("ADC_MAX")
        max_adc = self._sample_count * mcu_adc_max
        if max_adc >= (1<<16):
            raise self._mcu.get_printer().config_error(
                "ADC sample_count=%d too large for MCU" % (self._sample_count,))
        self._inv_max_adc = 1.0 / max_adc
        self._report_clock = self._mcu.seconds_to_clock(self._report_time)
        min_sample = max(0, min(0xffff, int(self._min_sample * max_adc)))
        max_sample = max(0, min(0xffff, int(
            math.ceil(self._max_sample * max_adc))))
        # Setup periodic query and register response handler
        oldcmd = (
            "query_analog_in oid=%c clock=%u sample_ticks=%u sample_count=%c"
            " rest_ticks=%u min_value=%hu max_value=%hu range_check_count=%c")
        if (self._batch_num == 1
            and self._mcu.try_lookup_command(oldcmd) is not None):
            self._mcu.add_config_cmd(
                "query_analog_in oid=%d clock=%d sample_ticks=%d"
                " sample_count=%d rest_ticks=%d"
                " min_value=%d max_value=%d range_check_count=%d" % (
                    self._oid, clock, sample_ticks, self._sample_count,
                    self._report_clock, min_sample, max_sample,
                    self._range_check_count), is_init=True)
            self._mcu.register_serial_response(
                self._old_handle_analog_in_state,
                "analog_in_state oid=%c next_clock=%u value=%hu", self._oid)
            return
        BYTES_PER_SAMPLE = 2
        bytes_per_report = self._batch_num * BYTES_PER_SAMPLE
        self._mcu.add_config_cmd(
            "query_analog_in oid=%d clock=%d sample_ticks=%d sample_count=%d"
            " rest_ticks=%d bytes_per_report=%d"
            " min_value=%d max_value=%d range_check_count=%d" % (
                self._oid, clock, sample_ticks, self._sample_count,
                self._report_clock, bytes_per_report, min_sample, max_sample,
                self._range_check_count), is_init=True)
        self._mcu.register_serial_response(
            self._handle_analog_in_state,
            "analog_in_state oid=%c next_clock=%u values=%*s", self._oid)
    def _old_handle_analog_in_state(self, params):
        last_value = params['value'] * self._inv_max_adc
        next_clock = self._mcu.clock32_to_clock64(params['next_clock'])
        last_read_clock = next_clock - self._report_clock
        last_read_time = self._mcu.clock_to_print_time(last_read_clock)
        self._last_state = (last_read_time, last_value)
        if self._callback is not None:
            self._callback([(last_read_time, last_value)])
    def _handle_analog_in_state(self, params):
        values = self._unpack_from(params['values'])
        next_clock = self._mcu.clock32_to_clock64(params['next_clock'])
        ctpt = self._mcu.clock_to_print_time
        num = len(values)
        samples = [(ctpt(next_clock - (num - i)*self._report_clock),
                    values[i] * self._inv_max_adc) for i in range(num)]
        self._last_state = samples[-1]
        if self._callback is not None:
            self._callback(samples)


ADC_STREAM_SUMMARY_FORMAT = (
    "oid=%c sub=%c sequence=%u epoch=%u first_clock=%u last_clock=%u"
    " uncertainty=%u status=%u count=%hu min=%u max=%u"
    " sum_lo=%u sum_hi=%u shift=%c")


def _largest_common_divisor_at_most(values, limit):
    """Return the slowest exact base cadence bounded by a backend limit."""
    common = values[0]
    for value in values[1:]:
        common = math.gcd(common, value)
    if not limit or common <= limit:
        return common
    # Backend limits are intentionally small hardware counter ranges (the
    # RP2040 bound is 16384 ticks/channel), so this bounded configuration-time
    # search is clearer and less error-prone than a factorization routine.
    for candidate in range(limit, 0, -1):
        if common % candidate == 0:
            return candidate
    return 1


class MCUADCStreamManager:
    """Merge explicitly opted legacy ADC consumers onto one DMA scan engine."""
    def __init__(self, mcu):
        self._mcu = mcu
        self._adcs = []
        self._oid = None
        self._mcu.register_config_callback(self._build_config)
    def add_adc(self, adc):
        if adc not in self._adcs:
            self._adcs.append(adc)
    def _fallback(self, reason):
        if getattr(self._mcu, "_adc_stream_mode", "off") == "force":
            raise self._mcu.get_printer().config_error(
                "MCU '%s' ADC DMA adapter required but unavailable: %s"
                % (self._mcu.get_name(), reason))
        logging.info("MCU '%s' ADC DMA adapter disabled: %s; using legacy ADC",
                     self._mcu.get_name(), reason)
        for adc in self._adcs:
            adc._use_adc_stream = False
            adc._build_legacy_config()
    def _build_config(self):
        if not self._adcs:
            return
        all_adcs = getattr(self._mcu, "_helix_all_adcs", ())
        if any(adc._sample_count and adc not in self._adcs
               for adc in all_adcs):
            self._fallback("another ADC consumer still owns the legacy engine")
            return
        if getattr(self._mcu, "_helix_explicit_adc_stream", False):
            self._fallback("an explicit adc_stream section owns the engine")
            return
        subscribe_format = (
            "adc_stream_subscribe oid=%c sub=%c channel=%c input_div=%hu"
            " osr=%hu shift=%c report_div=%hu report_class=%c")
        safety_format = (
            "adc_stream_set_safety oid=%c sub=%c deadline_ticks=%u"
            " fail_action=%c low=%u high=%u fault_count=%c trigger_oid=%c")
        if (self._mcu.get_constants().get("ADC_STREAM_V1") != 1
                or self._mcu.try_lookup_command(subscribe_format) is None):
            self._fallback("firmware does not advertise ADC_STREAM_V1")
            return
        max_channels = self._mcu.get_constants().get(
            "ADC_STREAM_MAX_CHANNELS", 0)
        max_subscriptions = self._mcu.get_constants().get(
            "ADC_STREAM_MAX_SUBSCRIPTIONS", 0)
        if len(self._adcs) > min(max_channels, max_subscriptions):
            self._fallback("too many opted-in channels")
            return
        if any(not adc._sample_count for adc in self._adcs):
            self._fallback("consumer has no sampling schedule")
            return
        if any(adc._batch_num != 1 for adc in self._adcs):
            self._fallback(
                "consumer requires unsupported legacy batch semantics")
            return
        has_safety = any(adc._range_check_count for adc in self._adcs)
        if (has_safety
                and self._mcu.try_lookup_command(safety_format) is None):
            self._fallback("firmware lacks local ADC threshold safety")
            return
        if any(adc._sample_time <= 0. or adc._report_time <= 0.
               for adc in self._adcs):
            self._fallback(
                "consumer requires legacy immediate conversion timing")
            return
        # Legacy analog_in takes a short burst and then rests.  A uniform DMA
        # engine cannot express arbitrary per-channel burst gaps without
        # reprogramming the shared ADC.  Preserve the number of samples, the
        # report deadline, and the average by distributing those samples
        # evenly across each report interval.  This eliminates the otherwise
        # wasteful 1kscan/s continuous emulation of an 8ms/300ms thermistor
        # burst and makes the schedule exactly phase-lockable across clients.
        desired_ticks = []
        for adc in self._adcs:
            ticks = max(1, self._mcu.seconds_to_clock(
                adc._report_time / adc._sample_count))
            if self._mcu.seconds_to_clock(adc._sample_time) > ticks:
                self._fallback("sample aperture exceeds distributed interval")
                return
            actual_report = ticks * adc._sample_count
            requested_report = self._mcu.seconds_to_clock(adc._report_time)
            if abs(actual_report - requested_report) > adc._sample_count:
                self._fallback("report period cannot be represented in ticks")
                return
            desired_ticks.append(ticks)
        max_ticks_per_channel = self._mcu.get_constants().get(
            "ADC_STREAM_MAX_SCAN_TICKS_PER_CHANNEL", 0)
        max_scan_ticks = max_ticks_per_channel * len(self._adcs)
        base_ticks = _largest_common_divisor_at_most(
            desired_ticks, max_scan_ticks)
        schedules = []
        for adc, ticks in zip(self._adcs, desired_ticks):
            input_div = ticks // base_ticks
            if not 1 <= input_div <= 0xffff:
                self._fallback("distributed sample divisor exceeds limit")
                return
            osr = adc._sample_count
            if osr > self._mcu.get_constants().get("ADC_STREAM_MAX_OSR", 0):
                self._fallback("oversample count exceeds firmware limit")
                return
            schedules.append((input_div, osr, 1))

        max_block_values = self._mcu.get_constants().get(
            "ADC_STREAM_MAX_BLOCK_VALUES", 16)
        max_block_scans = max_block_values // len(self._adcs)
        report_scans = [input_div * osr * report_div
                        for input_div, osr, report_div in schedules]
        # End every logical reporting cycle on a DMA block boundary.  A block
        # that merely fits below report_scans can otherwise make summaries
        # arrive in alternating short/long bursts (for example, 16-scan DMA
        # blocks around a 20-scan report cycle).  Prefer the largest bounded
        # divisor so IRQ load stays low while delivery remains periodic.
        block_scans = min([max_block_scans] + report_scans)
        while block_scans > 1 and any(
                cycle % block_scans for cycle in report_scans):
            block_scans -= 1
        if block_scans < 1:
            self._fallback("no bounded block schedule fits")
            return
        self._oid = self._mcu.create_oid()
        self._mcu.add_config_cmd("config_adc_stream oid=%d" % (self._oid,))
        for adc in self._adcs:
            self._mcu.add_config_cmd(
                "adc_stream_add_channel oid=%d pin=%s"
                % (self._oid, adc._pin))
        for sub, (adc, schedule) in enumerate(zip(self._adcs, schedules)):
            input_div, osr, report_div = schedule
            self._mcu.add_config_cmd(
                "adc_stream_subscribe oid=%d sub=%d channel=%d"
                " input_div=%d osr=%d shift=0 report_div=%d report_class=%d"
                % (self._oid, sub, sub, input_div, osr, report_div,
                   adc._adc_stream_class))
            self._mcu.add_config_cmd(
                "adc_stream_set_subscription_options oid=%d sub=%d"
                " summary_mode=1" % (self._oid, sub))
            adc_max = adc._sample_count * int(
                self._mcu.get_constants().get("ADC_MAX", 4095))
            if adc._range_check_count:
                low = max(0, min(0xffffffff,
                    int(adc._min_sample * adc_max)))
                high = max(0, min(0xffffffff,
                    int(math.ceil(adc._max_sample * adc_max))))
                action, fault_count = 3, adc._range_check_count
            else:
                low, high, action, fault_count = 0, 0xffffffff, 0, 0
            self._mcu.add_config_cmd(
                "adc_stream_set_safety oid=%d sub=%d deadline_ticks=0"
                " fail_action=%d low=%d high=%d fault_count=%d"
                " trigger_oid=255" % (
                    self._oid, sub, action, low, high, fault_count))
        self._mcu.add_config_cmd(
            "adc_stream_set_options oid=%d raw_output=0" % (self._oid,))
        start_clock = self._mcu.get_query_slot(self._oid)
        period_ticks = base_ticks
        self._mcu.add_config_cmd(
            "adc_stream_start oid=%d clock=%d period_ticks=%d"
            " block_values=%d traffic_class=%d" % (
                self._oid, start_clock, period_ticks,
                block_scans * len(self._adcs), 0 if has_safety else 2),
            is_init=True)
        for message in ("adc_stream_prompt ", "adc_stream_telemetry "):
            self._mcu.register_serial_response(
                self._handle_summary, message + ADC_STREAM_SUMMARY_FORMAT,
                self._oid)
        self._mcu.register_serial_response(
            self._handle_fault,
            "adc_stream_fault oid=%c status=%u dropped=%u sequence=%u",
            self._oid)
    def _handle_summary(self, params):
        sub = params["sub"]
        if sub >= len(self._adcs) or not params["count"]:
            logging.warning("MCU '%s' received invalid ADC DMA summary",
                            self._mcu.get_name())
            return
        adc = self._adcs[sub]
        total = params["sum_lo"] | (params["sum_hi"] << 32)
        denominator = params["count"] * adc._sample_count
        adc_max = float(self._mcu.get_constants().get("ADC_MAX", 4095))
        value = total / (denominator * adc_max)
        read_clock = self._mcu.clock32_to_clock64(params["last_clock"])
        read_time = self._mcu.clock_to_print_time(read_clock)
        adc._last_state = (read_time, value)
        if adc._callback is not None:
            adc._callback([(read_time, value)])
    def _handle_fault(self, params):
        logging.error("MCU '%s' ADC DMA acquisition fault: status=0x%x"
                      " dropped=%d sequence=%d", self._mcu.get_name(),
                      params["status"], params["dropped"], params["sequence"])


######################################################################
# Main MCU class (and its helper classes)
######################################################################

# Support for restarting a micro-controller
class MCURestartHelper:
    def __init__(self, config, conn_helper):
        self._printer = printer = config.get_printer()
        self._conn_helper = conn_helper
        self._mcu = mcu = conn_helper.get_mcu()
        self._serial = conn_helper.get_serial()
        self._clocksync = conn_helper.get_clocksync()
        self._reactor = printer.get_reactor()
        self._name = mcu.get_name()
        # Restart tracking
        restart_methods = [None, 'arduino', 'cheetah', 'command', 'rpi_usb']
        self._restart_method = 'command'
        serialport, baud = conn_helper.get_serialport()
        if baud:
            self._restart_method = config.getchoice('restart_method',
                                                    restart_methods, None)
        self._reset_cmd = self._config_reset_cmd = None
        self._is_mcu_bridge = False
        # Register handlers
        printer.register_event_handler("klippy:firmware_restart",
                                       self._firmware_restart)
        printer.register_event_handler("klippy:disconnect", self._disconnect)
        printer.register_event_handler("klippy:mcu_identify",
                                       self._mcu_identify)
    # Connection phase
    def _check_restart(self, reason):
        start_reason = self._printer.get_start_args().get("start_reason")
        if start_reason == 'firmware_restart':
            return
        logging.info("Attempting automated MCU '%s' restart: %s",
                     self._name, reason)
        self._printer.request_exit('firmware_restart')
        self._reactor.pause(self._reactor.monotonic() + 2.000)
        raise error("Attempt MCU '%s' restart failed" % (self._name,))
    def check_restart_on_crc_mismatch(self):
        self._check_restart("CRC mismatch")
    def check_restart_on_send_config(self):
        if self._restart_method == 'rpi_usb':
            # Only configure mcu after usb power reset
            self._check_restart("full reset before config")
    def check_restart_on_attach(self):
        resmeth = self._restart_method
        serialport, baud = self._conn_helper.get_serialport()
        if resmeth == 'rpi_usb' and not os.path.exists(serialport):
            # Try toggling usb power
            self._check_restart("enable power")
    def lookup_attach_uart_rts(self):
        # Cheetah boards require RTS to be deasserted
        # else a reset will trigger the built-in bootloader.
        return (self._restart_method != "cheetah")
    def _mcu_identify(self):
        self._reset_cmd = self._mcu.try_lookup_command("reset")
        self._config_reset_cmd = self._mcu.try_lookup_command("config_reset")
        ext_only = self._reset_cmd is None and self._config_reset_cmd is None
        msgparser = self._serial.get_msgparser()
        mbaud = msgparser.get_constant('SERIAL_BAUD', None)
        if self._restart_method is None and mbaud is None and not ext_only:
            self._restart_method = 'command'
        if msgparser.get_constant('CANBUS_BRIDGE', 0):
            self._is_mcu_bridge = True
            self._printer.register_event_handler("klippy:firmware_restart",
                                                 self._firmware_restart_bridge)
    def _disconnect(self):
        self._serial.disconnect()
    def _restart_arduino(self):
        logging.info("Attempting MCU '%s' reset", self._name)
        self._disconnect()
        serialport, baud = self._conn_helper.get_serialport()
        serialhdl.arduino_reset(serialport, self._reactor)
    def _restart_cheetah(self):
        logging.info("Attempting MCU '%s' Cheetah-style reset", self._name)
        self._disconnect()
        serialport, baud = self._conn_helper.get_serialport()
        serialhdl.cheetah_reset(serialport, self._reactor)
    def _restart_via_command(self):
        if ((self._reset_cmd is None and self._config_reset_cmd is None)
            or not self._clocksync.is_active()):
            logging.info("Unable to issue reset command on MCU '%s'",
                         self._name)
            return
        if self._reset_cmd is None:
            # Attempt reset via config_reset command
            logging.info("Attempting MCU '%s' config_reset command", self._name)
            self._conn_helper.force_local_shutdown()
            self._reactor.pause(self._reactor.monotonic() + 0.015)
            self._config_reset_cmd.send()
        else:
            # Attempt reset via reset command
            logging.info("Attempting MCU '%s' reset command", self._name)
            self._reset_cmd.send()
        self._reactor.pause(self._reactor.monotonic() + 0.015)
        self._disconnect()
    def _restart_rpi_usb(self):
        logging.info("Attempting MCU '%s' reset via rpi usb power", self._name)
        self._disconnect()
        chelper.run_hub_ctrl(0)
        self._reactor.pause(self._reactor.monotonic() + 2.)
        chelper.run_hub_ctrl(1)
    def _firmware_restart(self, force=False):
        if self._is_mcu_bridge and not force:
            return
        if self._restart_method == 'rpi_usb':
            self._restart_rpi_usb()
        elif self._restart_method == 'command':
            self._restart_via_command()
        elif self._restart_method == 'cheetah':
            self._restart_cheetah()
        else:
            self._restart_arduino()
    def _firmware_restart_bridge(self):
        for name, bus in self._printer.lookup_objects(module='helix_can'):
            if bus.owns_bridge(self._name):
                bus.quiesce('bridge firmware restart')
        self._firmware_restart(True)

# Low-level mcu connection management helper
class MCUConnectHelper:
    def __init__(self, config, mcu, clocksync):
        self._mcu = mcu
        self._clocksync = clocksync
        self._printer = printer = config.get_printer()
        self._reactor = printer.get_reactor()
        self._name = name = mcu.get_name()
        # Serial port
        self._serial = serialhdl.SerialReader(self._reactor, mcu_name=name)
        self._baud = 0
        self._canbus_iface = None
        self._helix_can_bus = None
        canbus_uuid = config.get('canbus_uuid', None)
        board_id = config.get('board_id', None)
        helix_can_name = config.get('canbus', None)
        if canbus_uuid is not None and board_id is not None:
            raise config.error("Specify board_id or canbus_uuid, not both")
        if board_id is not None:
            if helix_can_name is None:
                raise config.error("board_id requires a named canbus")
            self._helix_can_bus = self._printer.load_object(
                config, 'helix_can %s' % (helix_can_name,))
            self._canbus_iface = self._helix_can_bus.get_interface()
            cbid = self._printer.load_object(config, 'canbus_ids')
            self._serialport = cbid.add_board_id(
                config, board_id, self._canbus_iface)
            self._helix_can_bus.add_required_node(self._serialport)
            self._printer.load_object(config, 'canbus_stats %s' % (name,))
        elif canbus_uuid is not None:
            self._serialport = canbus_uuid
            self._canbus_iface = config.get('canbus_interface', 'can0')
            cbid = self._printer.load_object(config, 'canbus_ids')
            cbid.add_uuid(config, canbus_uuid, self._canbus_iface)
            self._printer.load_object(config, 'canbus_stats %s' % (name,))
        else:
            self._serialport = config.get('serial')
            if not (self._serialport.startswith("/dev/rpmsg_")
                    or self._serialport.startswith("/tmp/klipper_host_")):
                self._baud = config.getint('baud', 250000, minval=2400)
        # Shutdown tracking
        self._emergency_stop_cmd = None
        self._is_shutdown = self._is_timeout = False
        self._shutdown_msg = ""
        # Communication timeout policy (FD-0001 doc 08).  'shutdown'
        # keeps the stock behavior (a lost link takes the whole machine
        # down); 'pause' turns a lost link to this (secondary) mcu into
        # a host-side pause-and-hold: a "mcu:comm_pause" event is sent,
        # this mcu enters a 'paused-link' state, and motion already
        # sent toward it underruns and holds on the MCU side.
        self._on_comm_timeout = config.getchoice(
            'on_comm_timeout', {'shutdown': 'shutdown', 'pause': 'pause'},
            'shutdown')
        if self._on_comm_timeout == 'pause' and name == 'mcu':
            raise config.error(
                "on_comm_timeout: pause is not supported on the primary mcu")
        self._is_paused_link = False
        self._saw_starting_while_paused = False
        self._link_check_timer = None
        # Register handlers
        printer.register_event_handler("klippy:mcu_identify",
                                       self._mcu_identify)
        self._restart_helper = MCURestartHelper(config, self)
        printer.register_event_handler("klippy:shutdown", self._shutdown)
        printer.register_event_handler("klippy:analyze_shutdown",
                                       self._analyze_shutdown)
        if self._on_comm_timeout == 'pause':
            printer.register_event_handler("klippy:ready",
                                           self._start_link_checks)
        if self._helix_can_bus is not None:
            self._helix_can_bus.register_connection(self)
    def get_mcu(self):
        return self._mcu
    def get_serial(self):
        return self._serial
    def get_clocksync(self):
        return self._clocksync
    def get_serialport(self):
        return self._serialport, self._baud
    def get_restart_helper(self):
        return self._restart_helper
    def is_helix_can(self):
        return self._helix_can_bus is not None
    def get_can_capabilities(self):
        params = self._serial.get_canfd_capabilities()
        return {'fd': bool(params['fd']),
                'bitrate_mask': params['bitrate_mask'],
                'max_payload': params['max_payload'],
                'transceiver_max': params['transceiver_max']}
    def prepare_can_profile(self, profile, epoch):
        return self._serial.prepare_canfd(
            profile['mtu'], profile['brs'], profile['data_bitrate'], epoch)
    def enable_can_profile(self, profile, epoch):
        return self._serial.enable_canfd(
            profile['mtu'], profile['brs'], profile['data_bitrate'], epoch)
    def commit_can_profile(self, profile, epoch):
        return self._serial.commit_canfd(epoch)
    def abort_can_profile(self, epoch):
        return self._serial.abort_canfd(epoch)
    def _handle_shutdown(self, params):
        if self._is_shutdown:
            return
        self._is_shutdown = True
        self._shutdown_msg = msg = params['static_string_id']
        shutdown_clock = params.get("clock")
        if shutdown_clock is not None:
            shutdown_clock = self._mcu.clock32_to_clock64(shutdown_clock)
        event_type = params['#name']
        if self._is_paused_link:
            # The board became reachable again while in the paused-link
            # state and reported an MCU-side shutdown.  Do not take the
            # rest of the machine (and its engaged heater holds) down;
            # RECONNECT_MCU will report that a restart is required.
            logging.error("MCU '%s' reported shutdown ('%s') while its"
                          " link was paused; a restart will be required",
                          self._name, msg)
            return
        self._printer.invoke_async_shutdown(
            "MCU shutdown", {"reason": msg, "mcu": self._name,
                             "event_type": event_type,
                             "shutdown_clock": shutdown_clock})
    def _handle_starting(self, params):
        if self._is_paused_link:
            # Boot detection while holding: remember that the board
            # restarted (its volatile state is gone) so a subsequent
            # RECONNECT_MCU can honestly demand a full RESTART, without
            # shutting down the boards that are still holding.
            self._saw_starting_while_paused = True
            logging.error("MCU '%s' sent 'starting' while its link was"
                          " paused - the board has restarted; a RESTART"
                          " will be required", self._name)
            return
        if not self._is_shutdown:
            self._printer.invoke_async_shutdown("MCU '%s' spontaneous restart"
                                                % (self._name,))
    def log_info(self):
        msgparser = self._serial.get_msgparser()
        message_count = len(msgparser.get_messages())
        version, build_versions = msgparser.get_version_info()
        log_info = [
            "Loaded MCU '%s' %d commands (%s / %s)"
            % (self._name, message_count, version, build_versions),
            "MCU '%s' config: %s" % (self._name, " ".join(
                ["%s=%s" % (k, v)
                 for k, v in msgparser.get_constants().items()]))]
        return "\n".join(log_info)
    def _attach_file(self):
        # In a debugging mode.  Open debug output file and read data dictionary
        start_args = self._printer.get_start_args()
        if self._name == 'mcu':
            out_fname = start_args.get('debugoutput')
            dict_fname = start_args.get('dictionary')
        else:
            out_fname = start_args.get('debugoutput') + "-" + self._name
            dict_fname = start_args.get('dictionary_' + self._name)
        outfile = open(out_fname, 'wb')
        dfile = open(dict_fname, 'rb')
        dict_data = dfile.read()
        dfile.close()
        self._serial.connect_file(outfile, dict_data)
        self._clocksync.connect_file(self._serial)
    def _attach(self):
        self._restart_helper.check_restart_on_attach()
        try:
            if self._canbus_iface is not None:
                cbid = self._printer.lookup_object('canbus_ids')
                nodeid = cbid.get_nodeid(self._serialport,
                                         self._canbus_iface)
                legacy_handle = cbid.resolve_legacy_handle(
                    self._serialport, self._canbus_iface)
                profile = {'mtu': 8, 'brs': False,
                           'data_bitrate': 1000000}
                if self._helix_can_bus is not None:
                    profile = self._helix_can_bus.get_connection_profile()
                self._serial.connect_canbus(
                    legacy_handle, nodeid, self._canbus_iface,
                    canfd_mtu=profile['mtu'], canfd_brs=profile['brs'],
                    canfd_data_bitrate=profile['data_bitrate'])
            elif self._baud:
                rts = self._restart_helper.lookup_attach_uart_rts()
                self._serial.connect_uart(self._serialport, self._baud, rts)
            else:
                self._serial.connect_pipe(self._serialport)
            self._clocksync.connect(self._serial)
        except serialhdl.error as e:
            raise error(str(e))
    def _mcu_identify(self):
        if self._mcu.is_fileoutput():
            self._attach_file()
        else:
            self._attach()
        logging.info(self.log_info())
        # Setup shutdown handling
        self._emergency_stop_cmd = self._mcu.lookup_command("emergency_stop")
        self._serial.register_response(self._handle_shutdown, 'shutdown')
        self._serial.register_response(self._handle_shutdown, 'is_shutdown')
        self._serial.register_response(self._handle_starting, 'starting')
    def _analyze_shutdown(self, msg, details):
        if self._mcu.is_fileoutput():
            return
        logging.info("MCU '%s' shutdown: %s\n%s\n%s", self._name,
                     self._shutdown_msg, self._clocksync.dump_debug(),
                     self._serial.dump_debug())
    def _shutdown(self, force=False):
        if (self._emergency_stop_cmd is None
            or (self._is_shutdown and not force)):
            return
        self._emergency_stop_cmd.send()
    def force_local_shutdown(self):
        self._is_shutdown = True
        self._shutdown(force=True)
    def check_timeout(self, eventtime):
        if (self._clocksync.is_active() or self._mcu.is_fileoutput()
            or self._is_timeout):
            return
        self._is_timeout = True
        logging.info("Timeout with MCU '%s' (eventtime=%f)",
                     self._name, eventtime)
        if self._on_comm_timeout == 'pause' and not self._is_shutdown:
            self._enter_paused_link()
            return
        self._printer.invoke_shutdown("Lost communication with MCU '%s'" % (
            self._name,))
    # Link-loss pause-and-hold handling (FD-0001 doc 08)
    def _start_link_checks(self):
        # The stock timeout detection only runs from the periodic
        # motion_queuing.stats() calibration (every ~5 seconds).  With
        # 'on_comm_timeout: pause' the host must detect the loss before
        # stale heater readings are quelled (~7s) and escalated by
        # verify_heater, so poll the link state once a second.
        if self._mcu.is_fileoutput():
            return
        self._link_check_timer = self._reactor.register_timer(
            self._link_check_event, self._reactor.monotonic() + 1.)
    def _link_check_event(self, eventtime):
        self.check_timeout(eventtime)
        return eventtime + 1.
    def _enter_paused_link(self):
        self._is_paused_link = True
        self._saw_starting_while_paused = False
        # Quiesce the periodic clock queries and drop the 'clock'
        # response handler: after a long outage the 32-bit clock
        # extension in clocksync would alias (>~2^31 ticks between
        # samples) and poison the clock regression.  The regression
        # state itself is left untouched so a successful reconnect can
        # resume from the healthy pre-outage frequency estimate.
        self._reactor.update_timer(self._clocksync.get_clock_timer,
                                   self._reactor.NEVER)
        try:
            self._serial.register_response(None, 'clock')
        except KeyError:
            pass
        logging.error(
            "MCU '%s': lost communication - entering paused-link state"
            " (on_comm_timeout: pause). Motion toward this MCU will"
            " underrun and hold on the MCU side. Reseat the connection"
            " and run RECONNECT_MCU MCU=%s.", self._name, self._name)
        try:
            self._printer.send_event("mcu:comm_pause", self._name)
        except Exception:
            logging.exception("MCU '%s': error in mcu:comm_pause handlers",
                              self._name)
    def is_link_paused(self):
        return self._is_paused_link
    def _probe_response(self, msg, response_name, retries=5, timeout=1.0):
        # Bounded request/response probe that cannot wedge on a dead
        # link.  The regular query helpers block indefinitely waiting
        # for the transport-level ack (raw_send_wait_ack has no
        # timeout), which is unacceptable while the link state is
        # unknown, so send unacknowledged and wait with a deadline.
        reactor = self._reactor
        completion = reactor.completion()
        state = {'done': False}
        def handle_probe(params):
            if not state['done']:
                state['done'] = True
                reactor.async_complete(completion, params)
        self._serial.register_response(handle_probe, response_name)
        try:
            cmd = self._serial.get_msgparser().create_command(msg)
            cq = self._serial.get_default_command_queue()
            params = None
            for i in range(retries):
                self._serial.raw_send(cmd, 0, 0, cq)
                params = completion.wait(reactor.monotonic() + timeout)
                if params is not None:
                    break
        finally:
            try:
                self._serial.register_response(None, response_name)
            except KeyError:
                pass
        return params
    def _reopen_serial_fd(self):
        # Open the port anew and splice the new descriptor into the
        # existing serialqueue with dup2().  The serialqueue (and the
        # C-level steppersync/trdispatch/command_queue objects holding
        # pointers to it, plus both sides' transport sequence numbers)
        # all survive.  ARQ retains frames already transmitted; transport
        # re-arm discards commands that never reached the wire during the
        # outage so stale timer/macro work cannot burst into the MCU.
        serial_dev = self._serial.serial_dev
        if serial_dev is None:
            raise error("MCU '%s' has no active serial session"
                        % (self._name,))
        old_fd = serial_dev.fileno()
        if self._canbus_iface is not None:
            import can # XXX
            cbid = self._printer.lookup_object('canbus_ids')
            nodeid = cbid.get_nodeid(self._serialport, self._canbus_iface)
            txid = nodeid * 2 + 256
            filters = [{"can_id": txid + 1, "can_mask": 0x7ff,
                        "extended": False}]
            legacy_handle = cbid.resolve_legacy_handle(
                self._serialport, self._canbus_iface)
            uuid = int(legacy_handle, 16)
            uuid = [(uuid >> (40 - i*8)) & 0xff for i in range(6)]
            set_id_msg = can.Message(arbitration_id=0x3f0,
                                     data=[0x01] + uuid + [nodeid],
                                     is_extended_id=False)
            profile = {'mtu': 8}
            if self._helix_can_bus is not None:
                profile = self._helix_can_bus.get_connection_profile()
            bus = can.interface.Bus(channel=self._canbus_iface,
                                    can_filters=filters, bustype='socketcan',
                                    fd=profile['mtu'] > 8)
            try:
                bus.send(set_id_msg)
                os.dup2(bus.fileno(), old_fd)
            finally:
                bus.shutdown()
        elif self._baud:
            # Deliberately skip the stk500v2/arduino baud dance used on
            # first attach - toggling the port here could reset a board
            # whose live state is exactly what is being preserved.
            rts = self._restart_helper.lookup_attach_uart_rts()
            new_dev = serialhdl.serial.Serial(baudrate=self._baud, timeout=0,
                                              exclusive=True)
            new_dev.port = self._serialport
            new_dev.rts = rts
            new_dev.open()
            os.dup2(new_dev.fileno(), old_fd)
            new_dev.close()
        else:
            new_fd = os.open(self._serialport, os.O_RDWR | os.O_NOCTTY)
            os.dup2(new_fd, old_fd)
            os.close(new_fd)
    def attempt_reconnect(self, config_crc):
        # Re-handshake with an MCU in the paused-link state; returns
        # (success, message).  See MCU.attempt_reconnect() for the
        # design notes and remaining seams.
        if self._mcu.is_fileoutput():
            return False, "RECONNECT_MCU not supported in debug output mode"
        if not self._is_paused_link:
            return False, ("MCU '%s' is not in a paused-link state"
                           % (self._name,))
        prev_clock = self._clocksync.last_clock
        # Re-open the port if possible; if that fails (e.g. the device
        # node never went away and this process still holds it, or the
        # device has not re-enumerated yet) probe the existing link -
        # electrical-only interruptions recover in place.
        try:
            self._reopen_serial_fd()
            reopen_note = "port reopened"
        except Exception as e:
            logging.info("MCU '%s' reconnect: could not reopen port (%s);"
                         " probing the existing link", self._name, e)
            reopen_note = "port not reopened (%s)" % (e,)
        # EOF stops both serialqueue worker threads.  Re-open alone is not
        # sufficient: the preserved queue must be explicitly re-armed before
        # it can transmit the liveness probes or consume their responses.
        try:
            if self._serial.reconnect():
                reopen_note += ", transport re-armed"
        except serialhdl.error as e:
            return False, ("MCU '%s' transport restart failed: %s. Retry"
                           " RECONNECT_MCU." % (self._name, str(e)))
        # Probe for liveness and boot state (get_uptime returns the
        # true 64-bit clock, immune to 32-bit extension aliasing)
        uptime_params = self._probe_response('get_uptime', 'uptime')
        if uptime_params is None:
            return False, ("MCU '%s' did not respond (%s). Check the"
                           " connection and retry RECONNECT_MCU."
                           % (self._name, reopen_note))
        new_clock = (uptime_params['high'] << 32) | uptime_params['clock']
        config_params = self._probe_response('get_config', 'config')
        if config_params is None:
            return False, ("MCU '%s' answered get_uptime but not"
                           " get_config. Retry RECONNECT_MCU."
                           % (self._name,))
        # Boot / state detection heuristics
        if self._is_shutdown or config_params['is_shutdown']:
            return False, ("MCU '%s' is reachable but in a shutdown state"
                           " (%s). A FIRMWARE_RESTART and RESTART are"
                           " required." % (self._name,
                                           self._shutdown_msg or "unknown"))
        if (self._saw_starting_while_paused
            or not config_params['is_config'] or new_clock < prev_clock):
            return False, ("MCU '%s' rebooted while disconnected - its"
                           " volatile state (positions, queues) is gone."
                           " In-place reconfigure is not attempted; a full"
                           " RESTART is required." % (self._name,))
        if config_params['crc'] != config_crc:
            return False, ("MCU '%s' reports a different config CRC"
                           " (%d vs %d expected). A full RESTART is"
                           " required." % (self._name, config_params['crc'],
                                           config_crc))
        # Never-rebooted board with matching config: re-discipline the
        # clock.  clocksync.connect() re-anchors last_clock/averages
        # from get_uptime, takes a fresh burst of samples, re-registers
        # the 'clock' handler and re-arms the periodic query timer;
        # for a secondary mcu it also re-computes the print_time
        # adjustment against the primary.
        try:
            self._clocksync.connect(self._serial)
        except serialhdl.error as e:
            return False, ("MCU '%s' clock re-sync failed: %s. Retry"
                           " RECONNECT_MCU." % (self._name, str(e)))
        # Clear the paused-link state
        self._is_timeout = False
        self._is_paused_link = False
        self._saw_starting_while_paused = False
        # Promptly re-anchor the C-level steppersync/stepcompress clock
        # estimates (normally refreshed only every few seconds)
        motion_queuing = self._printer.lookup_object('motion_queuing', None)
        if motion_queuing is not None:
            motion_queuing.stats(self._reactor.monotonic())
        logging.info("MCU '%s' reconnected: no reboot detected (config crc"
                     " match, uptime %d -> %d); clock re-synced (%s)",
                     self._name, prev_clock, new_clock, reopen_note)
        try:
            self._printer.send_event("mcu:comm_resume", self._name)
        except Exception:
            logging.exception("MCU '%s': error in mcu:comm_resume handlers",
                              self._name)
        return True, ("MCU '%s' reconnected: the board never rebooted"
                      " (config CRC matches, uptime continuous) and its"
                      " clock has been re-synced. The print can be resumed"
                      " with RESUME." % (self._name,))
    def is_shutdown(self):
        return self._is_shutdown
    def get_shutdown_msg(self):
        return self._shutdown_msg

# Handle statistics reporting
class MCUStatsHelper:
    def __init__(self, config, conn_helper):
        self._printer = printer = config.get_printer()
        self._mcu = mcu = conn_helper.get_mcu()
        self._serial = conn_helper.get_serial()
        self._clocksync = conn_helper.get_clocksync()
        self._reactor = printer.get_reactor()
        self._name = mcu.get_name()
        # Statistics tracking
        self._mcu_freq = 0.
        self._get_status_info = {}
        self._stats_sumsq_base = 0.
        self._mcu_tick_avg = 0.
        self._mcu_tick_stddev = 0.
        self._mcu_tick_awake = 0.
        # Register handlers
        printer.register_event_handler("klippy:ready", self._ready)
        printer.register_event_handler("klippy:mcu_identify",
                                       self._mcu_identify)
    def _handle_mcu_stats(self, params):
        count = params['count']
        tick_sum = params['sum']
        c = 1.0 / (count * self._mcu_freq)
        self._mcu_tick_avg = tick_sum * c
        tick_sumsq = params['sumsq'] * self._stats_sumsq_base
        diff = count*tick_sumsq - tick_sum**2
        self._mcu_tick_stddev = c * math.sqrt(max(0., diff))
        self._mcu_tick_awake = tick_sum / self._mcu_freq
    def _mcu_identify(self):
        self._mcu_freq = self._mcu.get_constant_float('CLOCK_FREQ')
        self._stats_sumsq_base = self._mcu.get_constant_float(
            'STATS_SUMSQ_BASE')
        msgparser = self._serial.get_msgparser()
        version, build_versions = msgparser.get_version_info()
        self._get_status_info['mcu_version'] = version
        self._get_status_info['mcu_build_versions'] = build_versions
        self._get_status_info['mcu_constants'] = msgparser.get_constants()
        self._serial.register_response(self._handle_mcu_stats, 'stats')
    def _ready(self):
        if self._mcu.is_fileoutput():
            return
        # Check that reported mcu frequency is in range
        mcu_freq = self._mcu_freq
        systime = self._reactor.monotonic()
        get_clock = self._clocksync.get_clock
        calc_freq = get_clock(systime + 1) - get_clock(systime)
        freq_diff = abs(mcu_freq - calc_freq)
        mcu_freq_mhz = int(mcu_freq / 1000000. + 0.5)
        calc_freq_mhz = int(calc_freq / 1000000. + 0.5)
        if freq_diff > mcu_freq*0.01 and mcu_freq_mhz != calc_freq_mhz:
            pconfig = self._printer.lookup_object('configfile')
            msg = ("MCU '%s' configured for %dMhz but running at %dMhz!"
                    % (self._name, mcu_freq_mhz, calc_freq_mhz))
            pconfig.runtime_warning(msg)
    def get_status(self, eventtime=None):
        return dict(self._get_status_info)
    def stats(self, eventtime):
        load = "mcu_awake=%.03f mcu_task_avg=%.06f mcu_task_stddev=%.06f" % (
            self._mcu_tick_awake, self._mcu_tick_avg, self._mcu_tick_stddev)
        stats = ' '.join([load, self._serial.stats(eventtime),
                          self._clocksync.stats(eventtime)])
        parts = [s.split('=', 1) for s in stats.split()]
        last_stats = {k:(float(v) if '.' in v else int(v)) for k, v in parts}
        self._get_status_info['last_stats'] = last_stats
        return False, '%s: %s' % (self._name, stats)

# Handle process of configuring an mcu
class MCUConfigHelper:
    def __init__(self, config, conn_helper):
        self._printer = printer = config.get_printer()
        self._conn_helper = conn_helper
        self._mcu = mcu = conn_helper.get_mcu()
        self._serial = conn_helper.get_serial()
        self._clocksync = conn_helper.get_clocksync()
        self._reactor = printer.get_reactor()
        self._name = mcu.get_name()
        # Configuration tracking
        self._config_finalized = False
        self._oid_count = 0
        self._config_callbacks = []
        self._post_init_callbacks = []
        self._config_cmds = []
        self._restart_cmds = []
        self._init_cmds = []
        self._config_crc = 0
        self._mcu_freq = 0.
        self._reserved_move_slots = 0
        # Register handlers
        printer.lookup_object('pins').register_chip(self._name, mcu)
        printer.register_event_handler("klippy:mcu_identify",
                                       self._mcu_identify)
        printer.register_event_handler("klippy:connect", self._connect)
    def _finalize_config(self):
        # Build config commands
        for cb in self._config_callbacks:
            cb()
        self._config_finalized = True
        self._config_cmds.insert(0, "allocate_oids count=%d"
                                 % (self._oid_count,))
        # Resolve pin names
        ppins = self._printer.lookup_object('pins')
        pin_resolver = ppins.get_pin_resolver(self._name)
        for cmdlist in (self._config_cmds, self._restart_cmds, self._init_cmds):
            for i, cmd in enumerate(cmdlist):
                cmdlist[i] = pin_resolver.update_command(cmd)
        # Calculate config CRC
        encoded_config = '\n'.join(self._config_cmds).encode()
        self._config_crc = zlib.crc32(encoded_config) & 0xffffffff
        self._config_cmds.append("finalize_config crc=%d" % (self._config_crc,))
    def _send_cfg_init_commands(self, cmds):
        try:
            for c in cmds:
                self._serial.send(c)
        except msgproto.enumeration_error as e:
            enum_name, enum_value = e.get_enum_params()
            if enum_name == 'pin':
                # Raise pin name errors as a config error (not a protocol error)
                raise self._printer.config_error(
                    "Pin '%s' is not a valid pin name on mcu '%s'"
                    % (enum_value, self._name))
            raise
    def _send_get_config(self):
        get_config_cmd = self._mcu.lookup_query_command(
            "get_config",
            "config is_config=%c crc=%u is_shutdown=%c move_count=%hu")
        if self._mcu.is_fileoutput():
            return { 'is_config': 0, 'move_count': 500, 'crc': 0 }
        config_params = get_config_cmd.send()
        if self._conn_helper.is_shutdown():
            raise error("MCU '%s' error during config: %s" % (
                self._name, self._conn_helper.get_shutdown_msg()))
        if config_params['is_shutdown']:
            raise error("Can not update MCU '%s' config as it is shutdown" % (
                self._name,))
        return config_params
    def _log_reset_reason(self):
        # New RP2040 firmware exposes the watchdog reason register.  Keep this
        # optional so older firmware and other architectures connect exactly
        # as before.  Query before get_config so a board already in shutdown
        # still leaves useful reset evidence in klippy.log.
        if (self._mcu.is_fileoutput()
            or not self._mcu.check_valid_response("get_reset_reason")
            or not self._mcu.check_valid_response("reset_reason flags=%c")):
            return
        query = self._mcu.lookup_query_command(
            "get_reset_reason", "reset_reason flags=%c")
        params = query.send()
        reason, flags = _format_reset_reason(params['flags'])
        logging.info("MCU '%s' reset reason: %s (flags=0x%x)",
                     self._name, reason, flags)
    def _connect(self):
        # Finalize the config and check if a restart is needed
        restart_helper = self._conn_helper.get_restart_helper()
        self._log_reset_reason()
        config_params = self._send_get_config()
        if not config_params['is_config']:
            # Not configured - sending full config will be required
            restart_helper.check_restart_on_send_config()
            self._finalize_config()
            cfg_init_cmds = self._config_cmds + self._init_cmds
            logging.info("Sending MCU '%s' printer configuration...",
                         self._name)
        else:
            # Already configured - may need to only send init commands
            start_reason = self._printer.get_start_args().get("start_reason")
            if start_reason == 'firmware_restart':
                raise error("Failed automated reset of MCU '%s'"
                            % (self._name,))
            self._finalize_config()
            if self._config_crc != config_params['crc']:
                restart_helper.check_restart_on_crc_mismatch()
                raise error("MCU '%s' CRC does not match config"
                            % (self._name,))
            cfg_init_cmds = self._restart_cmds + self._init_cmds
        # Send config and init messages
        self._send_cfg_init_commands(cfg_init_cmds)
        config_params = self._send_get_config()
        if not config_params['is_config'] and not self._mcu.is_fileoutput():
            raise error("Unable to configure MCU '%s'" % (self._name,))
        # Run post_init callbacks
        for cb in self._post_init_callbacks:
            cb()
        # Setup steppersync with the move_count returned by get_config
        move_count = config_params['move_count']
        if move_count < self._reserved_move_slots:
            raise error("Too few moves available on MCU '%s'" % (self._name,))
        ss_move_count = move_count - self._reserved_move_slots
        motion_queuing = self._printer.lookup_object('motion_queuing')
        motion_queuing.setup_mcu_movequeue(
            self._mcu, self._serial.get_serialqueue(), ss_move_count)
        # Log config information
        move_msg = "Configured MCU '%s' (%d moves)" % (self._name, move_count)
        logging.info(move_msg)
        log_info = self._conn_helper.log_info() + "\n" + move_msg
        self._printer.set_rollover_info(self._name, log_info, log=False)
    def _mcu_identify(self):
        self._mcu_freq = self._mcu.get_constant_float('CLOCK_FREQ')
        ppins = self._printer.lookup_object('pins')
        pin_resolver = ppins.get_pin_resolver(self._name)
        for cname, value in self._mcu.get_constants().items():
            if cname.startswith("RESERVE_PINS_"):
                for pin in value.split(','):
                    pin_resolver.reserve_pin(pin, cname[13:])
        if MAX_NOMINAL_DURATION * self._mcu_freq > MAX_SCHEDULE_TICKS:
            max_possible = MAX_SCHEDULE_TICKS * 1 / self._mcu_freq
            raise error("Too high clock speed for MCU '%s'"
                        " to be able to resolve a maximum nominal duration"
                        " of %ds. Max possible duration: %ds"
                        % (self._name, MAX_NOMINAL_DURATION, max_possible))
    def _verify_not_finalized(self):
        if self._config_finalized:
            raise error("Internal error! MCU already configured")
    # Config creation helpers
    def is_config_finalized(self):
        return self._config_finalized
    def get_config_crc(self):
        return self._config_crc
    def setup_pin(self, pin_type, pin_params):
        self._verify_not_finalized()
        pcs = {'endstop': MCU_endstop,
               'digital_out': MCU_digital_out, 'pwm': MCU_pwm, 'adc': MCU_adc}
        if pin_type not in pcs:
            raise pins.error("pin type %s not supported on mcu" % (pin_type,))
        return pcs[pin_type](self._mcu, pin_params)
    def create_oid(self):
        self._verify_not_finalized()
        self._oid_count += 1
        return self._oid_count - 1
    def register_config_callback(self, cb):
        self._verify_not_finalized()
        self._config_callbacks.append(cb)
    def add_config_cmd(self, cmd, is_init=False, on_restart=False):
        self._verify_not_finalized()
        if is_init:
            self._init_cmds.append(cmd)
        elif on_restart:
            self._restart_cmds.append(cmd)
        else:
            self._config_cmds.append(cmd)
    def register_post_init_callback(self, cb):
        self._verify_not_finalized()
        self._post_init_callbacks.append(cb)
    def get_query_slot(self, oid):
        slot = self.seconds_to_clock(oid * .01)
        t = int(self._mcu.estimated_print_time(self._reactor.monotonic()) + 1.5)
        return self._mcu.print_time_to_clock(t) + slot
    def seconds_to_clock(self, time):
        return int(time * self._mcu_freq)
    def request_move_queue_slot(self):
        self._reserved_move_slots += 1

# Main MCU class
class MCU:
    error = error
    def __init__(self, config, clocksync):
        self._printer = printer = config.get_printer()
        self._clocksync = clocksync
        self._name = config.get_name()
        if self._name.startswith('mcu '):
            self._name = self._name[4:]
        # Low-level connection and helpers
        self._conn_helper = MCUConnectHelper(config, self, clocksync)
        self._serial = self._conn_helper.get_serial()
        self._config_helper = MCUConfigHelper(self, self._conn_helper)
        self._stats_helper = MCUStatsHelper(self, self._conn_helper)
        # Interrupt-driven homing (FD-0001 doc 09): when the firmware
        # exposes the trigger_source command set, endstop/probe detection
        # runs off a hardware edge interrupt instead of a polled timer
        # list.  Enabled by default and auto-disabled when the firmware
        # lacks the commands; set False to force the legacy polled path.
        self._hw_endstop_trigger = config.getboolean(
            'hardware_endstop_trigger', True)
        # Commissioning-only shadow measurement: with active triggering off,
        # timestamp the edge while the legacy poller still owns the stop.
        self._hw_endstop_observer = config.getboolean(
            'hardware_endstop_observer', False)
        if self._hw_endstop_trigger and self._hw_endstop_observer:
            raise config.error("hardware_endstop_observer requires"
                               " hardware_endstop_trigger: False")
        # Capability-gated migration of legacy MCU_adc clients onto the
        # merged DMA engine.  Auto falls back atomically; force is useful for
        # qualification because it makes any incompatibility explicit.
        self._adc_stream_mode = config.getchoice(
            'adc_stream_mode', {'off': 'off', 'auto': 'auto',
                                'force': 'force'}, 'auto')
        printer.load_object(config, "error_mcu")
        # Alter time reporting when debugging
        if self.is_fileoutput():
            def dummy_estimated_print_time(eventtime):
                return 0.
            self.estimated_print_time = dummy_estimated_print_time
    def get_name(self):
        return self._name
    def is_helix_can(self):
        return self._conn_helper.is_helix_can()
    def get_printer(self):
        return self._printer
    def is_fileoutput(self):
        return self._printer.get_start_args().get('debugoutput') is not None
    def want_hw_endstop_trigger(self):
        return self._hw_endstop_trigger
    def want_hw_endstop_observer(self):
        return self._hw_endstop_observer
    # MCU Configuration wrappers
    def setup_pin(self, pin_type, pin_params):
        return self._config_helper.setup_pin(pin_type, pin_params)
    def create_oid(self):
        return self._config_helper.create_oid()
    def register_config_callback(self, cb):
        self._config_helper.register_config_callback(cb)
    def add_config_cmd(self, cmd, is_init=False, on_restart=False):
        self._config_helper.add_config_cmd(cmd, is_init, on_restart)
    def request_move_queue_slot(self):
        self._config_helper.request_move_queue_slot()
    def get_query_slot(self, oid):
        return self._config_helper.get_query_slot(oid)
    def seconds_to_clock(self, time):
        return self._config_helper.seconds_to_clock(time)
    # Command Handler helpers
    def min_schedule_time(self):
        return MIN_SCHEDULE_TIME
    def max_nominal_duration(self):
        return MAX_NOMINAL_DURATION
    def lookup_command(self, msgformat, cq=None):
        return CommandWrapper(self._conn_helper, msgformat, cq)
    def lookup_query_command(self, msgformat, respformat, oid=None,
                             cq=None, is_async=False):
        return CommandQueryWrapper(self._conn_helper, msgformat, respformat,
                                   oid, cq, is_async)
    def try_lookup_command(self, msgformat, cq=None):
        try:
            return self.lookup_command(msgformat, cq=cq)
        except self._serial.get_msgparser().error as e:
            return None
    def alloc_command_queue(self):
        return self._serial.alloc_command_queue()
    def register_serial_response(self, cb, msg, oid=None):
        return AsyncResponseWrapper(self._conn_helper, self._config_helper,
                                    cb, msg, oid)
    def check_valid_response(self, msgformat):
        try:
            self._serial.get_msgparser().lookup_command(msgformat)
        except self._serial.get_msgparser().error as e:
            return False
        return True
    # MsgParser wrappers
    def get_enumerations(self):
        return self._serial.get_msgparser().get_enumerations()
    def get_constants(self):
        return self._serial.get_msgparser().get_constants()
    def get_constant_float(self, name):
        return self._serial.get_msgparser().get_constant_float(name)
    # ClockSync wrappers
    def get_clocksync(self):
        # Machine-time beacon relays need both directions of the per-link
        # host/MCU regression, which are intentionally implemented by the
        # ClockSync object rather than duplicated here.
        return self._clocksync
    def print_time_to_clock(self, print_time):
        return self._clocksync.print_time_to_clock(print_time)
    def clock_to_print_time(self, clock):
        return self._clocksync.clock_to_print_time(clock)
    def estimated_print_time(self, eventtime):
        return self._clocksync.estimated_print_time(eventtime)
    def clock32_to_clock64(self, clock32):
        return self._clocksync.clock32_to_clock64(clock32)
    def calibrate_clock(self, print_time, eventtime):
        offset, freq = self._clocksync.calibrate_clock(print_time, eventtime)
        self._conn_helper.check_timeout(eventtime)
        return offset, freq
    # Link-loss pause-and-hold (FD-0001 doc 08)
    def is_link_paused(self):
        return self._conn_helper.is_link_paused()
    def attempt_reconnect(self):
        """Re-handshake with this mcu after a link loss (paused-link state).

        Returns (success, message).  Implements the FD-0001 doc 08
        "replugged toolhead" resume flow as far as klippy's layering
        cleanly allows:

        * The serial port is re-opened and the new file descriptor is
          spliced into the *existing* C serialqueue via dup2().  A full
          serialhdl disconnect/re-connect (new serialqueue) is
          deliberately NOT used: the C-level steppersync, trdispatch
          and command_queue objects hold raw pointers into the original
          serialqueue (command queues stay linked into its internal
          lists), so swapping it out would corrupt or silently orphan
          every existing command stream.  Keeping the serialqueue also
          keeps both sides' transport sequence numbers, so in the
          never-rebooted case the session literally resumes and the
          retransmit machinery reliably delivers everything queued
          during the outage.
        * Because the transport session (and therefore the identify
          data dictionary) is preserved rather than renegotiated,
          "re-running identify" takes the form of boot-detection
          probes: get_uptime (true 64-bit clock - a reboot makes it go
          backwards), get_config (the founding document's
          is_config/is_shutdown flags
          and config CRC - config lives in RAM, so is_config=1 with a
          matching CRC proves the same configured session), plus any
          'starting'/'shutdown' messages observed while paused.
        * Never-rebooted case: the clock is re-disciplined
          (clocksync.connect), the paused-link state is cleared, the
          "mcu:comm_resume" event fires, and the operator may RESUME.
        * Rebooted / CRC-mismatch / MCU-shutdown cases: reported
          honestly as requiring a full RESTART - in-place reconfigure
          of a live printer is not attempted.

        Remaining seams (would need deeper surgery):
        * Commands that were queued during the outage with scheduled
          clocks (e.g. stale heater PWM refreshes) are delivered late
          on reconnect; stock firmware will answer with an MCU-side
          "Missed scheduling" shutdown, which is then reported as
          "restart required".  Boards implementing the founding document's
          pause-and-hold (underrun-hold + autonomous heater hold) are
          expected to tolerate/ignore them.
        * If the link dies again in the middle of the final
          clocksync.connect() the reconnect can block until the link
          returns (its query helpers wait on transport acks).
        """
        return self._conn_helper.attempt_reconnect(
            self._config_helper.get_config_crc())
    # Statistics wrappers
    def get_status(self, eventtime=None):
        return self._stats_helper.get_status(eventtime)
    def stats(self, eventtime):
        return self._stats_helper.stats(eventtime)

def add_printer_objects(config):
    printer = config.get_printer()
    reactor = printer.get_reactor()
    mainsync = clocksync.ClockSync(reactor)
    printer.add_object('mcu', MCU(config.getsection('mcu'), mainsync))
    for s in config.get_prefix_sections('mcu '):
        printer.add_object(s.section, MCU(
            s, clocksync.SecondarySync(reactor, mainsync)))

def get_printer_mcu(printer, name):
    if name == 'mcu':
        return printer.lookup_object(name)
    return printer.lookup_object('mcu ' + name)
