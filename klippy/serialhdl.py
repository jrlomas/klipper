# Serial port management for firmware communication
#
# Copyright (C) 2016-2021  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging, threading, os
import serial

import msgproto, chelper, util
import canbus_identity

class error(Exception):
    pass

class SerialReader:
    def __init__(self, reactor, mcu_name=""):
        self.reactor = reactor
        self.warn_prefix = ""
        self.mcu_name = mcu_name
        if self.mcu_name:
            self.warn_prefix = "mcu '%s': " % (self.mcu_name)
        sq_name = ("serialq %s" % (self.mcu_name))[:15]
        self.sq_name = sq_name.encode("utf-8")
        # Serial port
        self.serial_dev = None
        self.msgparser = msgproto.MessageParser(warn_prefix=self.warn_prefix)
        # C interface
        self.ffi_main, self.ffi_lib = chelper.get_ffi()
        self.serialqueue = None
        self.default_cmd_queue = self.alloc_command_queue()
        self.stats_buf = self.ffi_main.new('char[4096]')
        # Threading
        self.lock = threading.Lock()
        self.background_thread = None
        # Message handlers
        self.handlers = {}
        self.register_response(self._handle_unknown_init, '#unknown')
        self.register_response(self.handle_output, '#output')
        # Sent message notification tracking
        self.last_notify_id = 0
        self.pending_notifications = {}
    def _bg_thread(self):
        name_short = ("serialhdl %s" % (self.mcu_name))[:15]
        self.ffi_lib.set_thread_name(name_short.encode('utf-8'))
        response = self.ffi_main.new('struct pull_queue_message *')
        while 1:
            self.ffi_lib.serialqueue_pull(self.serialqueue, response)
            count = response.len
            if count < 0:
                break
            if response.notify_id:
                params = {'#sent_time': response.sent_time,
                          '#receive_time': response.receive_time}
                completion = self.pending_notifications.pop(
                    response.notify_id, None)
                if completion is not None:
                    # A zero/zero notification is synthesized by the C queue
                    # when an unsent request is discarded at reconnect.
                    # Complete with None so raw_send_wait_ack reports a
                    # closed/canceled transaction instead of a false ack.
                    if not response.sent_time and not response.receive_time:
                        params = None
                    self.reactor.async_complete(completion, params)
                continue
            params = self.msgparser.parse(response.msg[0:count])
            params['#sent_time'] = response.sent_time
            params['#receive_time'] = response.receive_time
            hdl = (params['#name'], params.get('oid'))
            try:
                with self.lock:
                    hdl = self.handlers.get(hdl, self.handle_default)
                    hdl(params)
            except:
                logging.exception("%sException in serial callback",
                                  self.warn_prefix)
    def _error(self, msg, *params):
        raise error(self.warn_prefix + (msg % params))
    def _get_identify_data(self, eventtime):
        # Query the "data dictionary" from the micro-controller
        identify_data = b""
        while 1:
            msg = "identify offset=%d count=%d" % (len(identify_data), 40)
            try:
                params = self.send_with_response(msg, 'identify_response')
            except error as e:
                logging.exception("%sWait for identify_response",
                                  self.warn_prefix)
                return None
            if params['offset'] == len(identify_data):
                msgdata = params['data']
                if not msgdata:
                    # Done
                    return identify_data
                identify_data += msgdata
    def _start_session(self, serial_dev, serial_fd_type=b'u', client_id=0):
        self.serial_dev = serial_dev
        self.serialqueue = self.ffi_main.gc(
            self.ffi_lib.serialqueue_alloc(serial_dev.fileno(),
                                           serial_fd_type, client_id,
                                           self.sq_name),
            self.ffi_lib.serialqueue_free)
        self.background_thread = threading.Thread(target=self._bg_thread)
        self.background_thread.start()
        # Obtain and load the data dictionary from the firmware
        completion = self.reactor.register_callback(self._get_identify_data)
        identify_data = completion.wait(self.reactor.monotonic() + 5.)
        if identify_data is None:
            logging.info("%sTimeout on connect", self.warn_prefix)
            self.disconnect()
            return False
        msgparser = msgproto.MessageParser(warn_prefix=self.warn_prefix)
        msgparser.process_identify(identify_data)
        self.msgparser = msgparser
        self.register_response(self.handle_unknown, '#unknown')
        # Setup baud adjust
        if serial_fd_type == b'c':
            wire_freq = msgparser.get_constant_float('CANBUS_FREQUENCY', None)
        else:
            wire_freq = msgparser.get_constant_float('SERIAL_BAUD', None)
        if wire_freq is not None:
            self.ffi_lib.serialqueue_set_wire_frequency(self.serialqueue,
                                                        wire_freq)
        receive_window = msgparser.get_constant_int('RECEIVE_WINDOW', None)
        if receive_window is not None:
            self.ffi_lib.serialqueue_set_receive_window(
                self.serialqueue, receive_window)
        if serial_fd_type == b'c':
            receive_frame_window = msgparser.get_constant_int(
                'CANBUS_RX_FRAME_WINDOW', 0)
            self.ffi_lib.serialqueue_set_receive_frame_window(
                self.serialqueue, receive_frame_window)
        return True
    def set_send_ahead(self, seconds):
        self.ffi_lib.serialqueue_set_send_ahead(
            self.serialqueue, float(seconds))
    def _require_canfd_frame_window(self, mtu):
        if (mtu > 8 and self.msgparser.get_constant_int(
                'CANBUS_RX_FRAME_WINDOW', 0) < 2):
            self._error(
                "MCU does not advertise a CAN FD receive frame window")
    def connect_canbus(self, canbus_uuid, canbus_nodeid, canbus_iface="can0",
                       canfd_mtu=8, canfd_brs=False,
                       canfd_data_bitrate=1000000):
        import can # XXX
        if canfd_mtu not in (8, 12, 16, 20, 24, 32, 48, 64):
            self._error("Invalid CAN FD carrier MTU")
        use_canfd = canfd_mtu > 8
        txid = canbus_nodeid * 2 + 256
        filters = [{"can_id": txid+1, "can_mask": 0x7ff, "extended": False}]
        # Prep for SET_NODEID command
        try:
            uuid = int(canbus_uuid, 16)
        except ValueError:
            uuid = -1
        if uuid < 0 or uuid > 0xffffffffffff:
            self._error("Invalid CAN uuid")
        uuid = [(uuid >> (40 - i*8)) & 0xff for i in range(6)]
        CANBUS_ID_ADMIN = 0x3f0
        CMD_SET_NODEID = 0x01
        RESP_SESSION_RESET = 0x23
        set_id_cmd = [CMD_SET_NODEID] + uuid + [canbus_nodeid]
        set_id_msg = can.Message(arbitration_id=CANBUS_ID_ADMIN,
                                 data=set_id_cmd, is_extended_id=False)
        # Start connection attempt
        logging.info("%sStarting CAN connect", self.warn_prefix)
        start_time = self.reactor.monotonic()
        while 1:
            if self.reactor.monotonic() > start_time + 90.:
                self._error("Unable to connect")
            try:
                bootstrap_filters = filters + [
                    {"can_id": CANBUS_ID_ADMIN + 1, "can_mask": 0x7ff,
                     "extended": False}]
                bus = can.interface.Bus(channel=canbus_iface,
                                        can_filters=bootstrap_filters,
                                        bustype='socketcan', fd=use_canfd)
                bus.send(set_id_msg)
            except (can.CanError, os.error, IOError) as e:
                logging.warning("%sUnable to open CAN port: %s",
                                self.warn_prefix, e)
                self.reactor.pause(self.reactor.monotonic() + 5.)
                continue
            # Helix nodes acknowledge after discarding their prior framed
            # stream and restoring the Classical bootstrap carrier.  The
            # response is an ordering barrier.  A short timeout preserves
            # compatibility with stock nodes that do not implement it.
            reset_deadline = self.reactor.monotonic() + .150
            reset_ack = False
            while self.reactor.monotonic() < reset_deadline:
                timeout = reset_deadline - self.reactor.monotonic()
                msg = bus.recv(max(0., timeout))
                if msg is None:
                    break
                data = bytes(msg.data)
                if (msg.arbitration_id == CANBUS_ID_ADMIN + 1
                        and len(data) == 8
                        and data[0] == RESP_SESSION_RESET
                        and data[1:7] == bytes(uuid)
                        and data[7] == canbus_nodeid):
                    logging.info("%sCAN session reset acknowledged",
                                 self.warn_prefix)
                    reset_ack = True
                    break
            if reset_ack:
                frames, byte_count = canbus_identity.drain_session_tail(bus)
                if frames:
                    logging.info(
                        "%sDiscarded %d stale CAN session-tail frames"
                        " (%d bytes)", self.warn_prefix, frames, byte_count)
            bus.set_filters(filters)
            bus.close = bus.shutdown # XXX
            ret = self._start_session(bus, b'c', txid)
            if not ret:
                continue
            # Verify correct canbus_nodeid to canbus_uuid mapping
            try:
                params = self.send_with_response('get_canbus_id', 'canbus_id')
                got_uuid = bytearray(params['canbus_uuid'])
                if got_uuid == bytearray(uuid):
                    if use_canfd:
                        if not self.msgparser.get_constant_int(
                                'CANBUS_FD', 0):
                            self._error("MCU does not support CAN FD")
                        self._require_canfd_frame_window(canfd_mtu)
                        ret = self.ffi_lib.serialqueue_set_canfd_mode(
                            self.serialqueue, canfd_mtu, int(canfd_brs),
                            canfd_data_bitrate)
                        if ret:
                            self._error("Unable to activate CAN FD transport")
                        try:
                            params = self.send_with_response(
                                "set_canbus_transport active=1 mtu=%d brs=%d"
                                " data_bitrate=%d"
                                % (canfd_mtu, int(canfd_brs),
                                   canfd_data_bitrate),
                                'canbus_transport')
                        except Exception:
                            self.ffi_lib.serialqueue_set_canfd_mode(
                                self.serialqueue, 8, 0, 1000000)
                            raise
                        if (params['active'] != 1
                                or params['mtu'] != canfd_mtu
                                or params['brs'] != int(canfd_brs)
                                or params['data_bitrate']
                                != canfd_data_bitrate):
                            self.ffi_lib.serialqueue_set_canfd_mode(
                                self.serialqueue, 8, 0, 1000000)
                            self._error("CAN FD carrier profile mismatch")
                    break
            except:
                logging.exception("%sError in canbus_uuid check",
                                  self.warn_prefix)
            logging.info("%sFailed to match canbus_uuid - retrying..",
                         self.warn_prefix)
            self.disconnect()
    def prepare_canfd(self, mtu, brs, data_bitrate, epoch):
        self._require_canfd_frame_window(mtu)
        params = self.send_with_response(
            "prepare_canbus_transport mtu=%d brs=%d data_bitrate=%d epoch=%d"
            % (mtu, int(brs), data_bitrate, epoch), 'canbus_transport')
        if (params['state'] != 1 or params['active']
                or params['epoch'] != epoch
                or params['data_bitrate'] != data_bitrate):
            self._error("MCU refused staged CAN FD profile")
        return params
    def get_canfd_capabilities(self):
        return self.send_with_response('get_canbus_capabilities',
                                       'canbus_capabilities')
    def commit_canfd(self, epoch):
        params = self.send_with_response(
            "commit_canbus_transport epoch=%d" % (epoch,),
            'canbus_transport')
        if params['state'] != 1 or params['active'] \
                or params['epoch'] != epoch:
            self._error("MCU failed CAN FD profile commit")
        return params
    def enable_canfd(self, mtu, brs, data_bitrate, epoch):
        self._require_canfd_frame_window(mtu)
        ret = self.ffi_lib.serialqueue_set_canfd_mode(
            self.serialqueue, mtu, int(brs), data_bitrate)
        if ret:
            self._error("Unable to enable CAN_RAW_FD_FRAMES")
        params = self.send_with_response(
            "enable_canbus_transport epoch=%d" % (epoch,),
            'canbus_transport')
        if (params['state'] != 2 or not params['active']
                or params['mtu'] != mtu or params['brs'] != int(brs)
                or params['epoch'] != epoch):
            self._error("MCU failed CAN FD profile enable")
        return params
    def abort_canfd(self, epoch):
        params = self.send_with_response(
            "abort_canbus_transport epoch=%d" % (epoch,),
            'canbus_transport')
        self.ffi_lib.serialqueue_set_canfd_mode(
            self.serialqueue, 8, 0, 1000000)
        return params
    def connect_pipe(self, filename):
        logging.info("%sStarting connect", self.warn_prefix)
        start_time = self.reactor.monotonic()
        while 1:
            if self.reactor.monotonic() > start_time + 90.:
                self._error("Unable to connect")
            try:
                fd = os.open(filename, os.O_RDWR | os.O_NOCTTY)
            except OSError as e:
                logging.warning("%sUnable to open port: %s",
                                self.warn_prefix, e)
                self.reactor.pause(self.reactor.monotonic() + 5.)
                continue
            serial_dev = os.fdopen(fd, 'rb+', 0)
            ret = self._start_session(serial_dev)
            if ret:
                break
    def connect_uart(self, serialport, baud, rts=True):
        # Initial connection
        logging.info("%sStarting serial connect", self.warn_prefix)
        start_time = self.reactor.monotonic()
        while 1:
            if self.reactor.monotonic() > start_time + 90.:
                self._error("Unable to connect")
            try:
                serial_dev = serial.Serial(baudrate=baud, timeout=0,
                                           exclusive=True)
                serial_dev.port = serialport
                serial_dev.rts = rts
                serial_dev.open()
            except (OSError, IOError, serial.SerialException) as e:
                logging.warning("%sUnable to open serial port: %s",
                             self.warn_prefix, e)
                self.reactor.pause(self.reactor.monotonic() + 5.)
                continue
            stk500v2_leave(serial_dev, self.reactor)
            ret = self._start_session(serial_dev)
            if ret:
                break
    def connect_file(self, debugoutput, dictionary, pace=False):
        self.serial_dev = debugoutput
        self.msgparser.process_identify(dictionary, decompress=False)
        self.serialqueue = self.ffi_main.gc(
            self.ffi_lib.serialqueue_alloc(self.serial_dev.fileno(), b'f', 0,
                                           self.sq_name),
            self.ffi_lib.serialqueue_free)
    def set_clock_est(self, freq, conv_time, conv_clock, last_clock):
        self.ffi_lib.serialqueue_set_clock_est(
            self.serialqueue, freq, conv_time, conv_clock, last_clock)
    def disconnect(self):
        if self.serialqueue is not None:
            self.ffi_lib.serialqueue_exit(self.serialqueue)
            if self.background_thread is not None:
                self.background_thread.join()
            self.background_thread = self.serialqueue = None
        if self.serial_dev is not None:
            self.serial_dev.close()
            self.serial_dev = None
        for pn in self.pending_notifications.values():
            pn.complete(None)
        self.pending_notifications.clear()
    def reconnect(self):
        """Restart transport workers after EOF without replacing queues."""
        ret = self.ffi_lib.serialqueue_reconnect(self.serialqueue)
        if ret < 0:
            self._error("Unable to restart serial transport")
        if not ret:
            return False
        # The old pull worker was woken when the C transport observed EOF.
        # Join it before starting the consumer for the re-armed serialqueue.
        if self.background_thread is not None:
            self.background_thread.join()
        self.background_thread = threading.Thread(target=self._bg_thread)
        self.background_thread.start()
        return True
    def stats(self, eventtime):
        if self.serialqueue is None:
            return ""
        self.ffi_lib.serialqueue_get_stats(self.serialqueue,
                                           self.stats_buf, len(self.stats_buf))
        return str(self.ffi_main.string(self.stats_buf).decode())
    def get_reactor(self):
        return self.reactor
    def get_msgparser(self):
        return self.msgparser
    def get_serialqueue(self):
        return self.serialqueue
    def get_default_command_queue(self):
        return self.default_cmd_queue
    # Serial response callbacks
    def register_response(self, callback, name, oid=None):
        with self.lock:
            if callback is None:
                # Query helpers for the same response can overlap during a
                # recovery boundary.  Cleanup must be idempotent: an older
                # helper may already have removed the shared handler.
                self.handlers.pop((name, oid), None)
            else:
                self.handlers[name, oid] = callback
    # Command sending
    def raw_send(self, cmd, minclock, reqclock, cmd_queue):
        self.ffi_lib.serialqueue_send(self.serialqueue, cmd_queue,
                                      cmd, len(cmd), minclock, reqclock, 0)
    def raw_send_wait_ack(self, cmd, minclock, reqclock, cmd_queue):
        self.last_notify_id += 1
        nid = self.last_notify_id
        completion = self.reactor.completion()
        self.pending_notifications[nid] = completion
        self.ffi_lib.serialqueue_send(self.serialqueue, cmd_queue,
                                      cmd, len(cmd), minclock, reqclock, nid)
        params = completion.wait()
        if params is None:
            self._error("Serial connection closed")
        return params
    def send(self, msg, minclock=0, reqclock=0):
        cmd = self.msgparser.create_command(msg)
        self.raw_send(cmd, minclock, reqclock, self.default_cmd_queue)
    def send_with_response(self, msg, response):
        cmd = self.msgparser.create_command(msg)
        src = SerialRetryCommand(self, response)
        return src.get_response([cmd], self.default_cmd_queue)
    def alloc_command_queue(self):
        return self.ffi_main.gc(self.ffi_lib.serialqueue_alloc_commandqueue(),
                                self.ffi_lib.serialqueue_free_commandqueue)
    # Dumping debug lists
    def dump_debug(self):
        out = []
        out.append("Dumping serial stats: %s" % (
            self.stats(self.reactor.monotonic()),))
        sdata = self.ffi_main.new('struct pull_queue_message[1024]')
        rdata = self.ffi_main.new('struct pull_queue_message[1024]')
        scount = self.ffi_lib.serialqueue_extract_old(self.serialqueue, 1,
                                                      sdata, len(sdata))
        rcount = self.ffi_lib.serialqueue_extract_old(self.serialqueue, 0,
                                                      rdata, len(rdata))
        out.append("Dumping send queue %d messages" % (scount,))
        for i in range(scount):
            msg = sdata[i]
            cmds = self.msgparser.dump(msg.msg[0:msg.len])
            out.append("Sent %d %f %f %d: %s" % (
                i, msg.receive_time, msg.sent_time, msg.len, ', '.join(cmds)))
        out.append("Dumping receive queue %d messages" % (rcount,))
        for i in range(rcount):
            msg = rdata[i]
            cmds = self.msgparser.dump(msg.msg[0:msg.len])
            out.append("Receive: %d %f %f %d: %s" % (
                i, msg.receive_time, msg.sent_time, msg.len, ', '.join(cmds)))
        return '\n'.join(out)
    # Default message handlers
    def _handle_unknown_init(self, params):
        logging.debug("%sUnknown message %d (len %d) while identifying",
                      self.warn_prefix, params['#msgid'], len(params['#msg']))
    def handle_unknown(self, params):
        logging.warning("%sUnknown message type %d: %s",
                     self.warn_prefix, params['#msgid'], repr(params['#msg']))
    def handle_output(self, params):
        logging.info("%s%s: %s", self.warn_prefix,
                     params['#name'], params['#msg'])
    def handle_default(self, params):
        logging.warning("%sgot %s", self.warn_prefix, params)

# Class to send a query command and return the received response
class SerialRetryCommand:
    def __init__(self, serial, name, oid=None):
        self.serial = serial
        self.name = name
        self.oid = oid
        self.last_params = None
        self.serial.register_response(self.handle_callback, name, oid)
    def handle_callback(self, params):
        self.last_params = params
    def get_response(self, cmds, cmd_queue, minclock=0, reqclock=0,
                     retry=True):
        retries = 5
        retry_delay = .010
        if not retry:
            retries = 0
        while 1:
            for cmd in cmds[:-1]:
                self.serial.raw_send(cmd, minclock, reqclock, cmd_queue)
            try:
                self.serial.raw_send_wait_ack(cmds[-1], minclock, reqclock,
                                              cmd_queue)
            except error:
                # Reconnect may cancel a query that was queued but never put
                # on the wire.  Do not leave its response handler installed
                # while propagating that expected transport boundary.
                self.serial.register_response(None, self.name, self.oid)
                raise
            params = self.last_params
            if params is not None:
                self.serial.register_response(None, self.name, self.oid)
                return params
            if retries <= 0:
                self.serial.register_response(None, self.name, self.oid)
                raise error("Unable to obtain '%s' response" % (self.name,))
            reactor = self.serial.reactor
            reactor.pause(reactor.monotonic() + retry_delay)
            retries -= 1
            retry_delay *= 2.

# Attempt to place an AVR stk500v2 style programmer into normal mode
def stk500v2_leave(ser, reactor):
    logging.debug("Starting stk500v2 leave programmer sequence")
    util.clear_hupcl(ser.fileno())
    origbaud = ser.baudrate
    # Request a dummy speed first as this seems to help reset the port
    ser.baudrate = 2400
    ser.read(1)
    # Send stk500v2 leave programmer sequence
    ser.baudrate = 115200
    reactor.pause(reactor.monotonic() + 0.100)
    ser.read(4096)
    ser.write(b'\x1b\x01\x00\x01\x0e\x11\x04')
    reactor.pause(reactor.monotonic() + 0.050)
    res = ser.read(4096)
    logging.debug("Got %s from stk500v2", repr(res))
    ser.baudrate = origbaud

def cheetah_reset(serialport, reactor):
    # Fysetc Cheetah v1.2 boards have a weird stateful circuitry for
    # configuring the bootloader. This sequence takes care of disabling it for
    # sure.
    # Open the serial port with RTS asserted
    ser = serial.Serial(baudrate=2400, timeout=0, exclusive=True)
    ser.port = serialport
    ser.rts = True
    ser.open()
    ser.read(1)
    reactor.pause(reactor.monotonic() + 0.100)
    # Toggle DTR
    ser.dtr = True
    reactor.pause(reactor.monotonic() + 0.100)
    ser.dtr = False
    # Deassert RTS
    reactor.pause(reactor.monotonic() + 0.100)
    ser.rts = False
    reactor.pause(reactor.monotonic() + 0.100)
    # Toggle DTR again
    ser.dtr = True
    reactor.pause(reactor.monotonic() + 0.100)
    ser.dtr = False
    reactor.pause(reactor.monotonic() + 0.100)
    ser.close()

# Attempt an arduino style reset on a serial port
def arduino_reset(serialport, reactor):
    # First try opening the port at a different baud
    ser = serial.Serial(serialport, 2400, timeout=0, exclusive=True)
    ser.read(1)
    reactor.pause(reactor.monotonic() + 0.100)
    # Then toggle DTR
    ser.dtr = True
    reactor.pause(reactor.monotonic() + 0.100)
    ser.dtr = False
    reactor.pause(reactor.monotonic() + 0.100)
    ser.close()
