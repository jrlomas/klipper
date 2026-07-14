# Machine-time authority: host-relayed sync beacon loop (FD-0001 doc 01)
#
# Machine time is the primary MCU's free-running counter. Boards on
# point-to-point links cannot hear each other, so the host relays
# beacons - it adds zero new transport capability and its relay jitter
# is filtered by the same min-RTT + regression machinery that makes
# today's clock sync work:
#
#   host -> primary:    sync_beacon_read
#   primary -> host:    sync_beacon seq=%c clock=%u
#   host -> secondary:  sync_beacon_relay seq=%c machine_clock=%u
#                                         local_est=%u
#
# `local_est` is the host's best estimate of the *secondary's* local
# clock at the moment the primary's counter read `machine_clock`,
# bridged through host time by the per-link min-RTT-anchored
# regressions in klippy/clocksync.py. Each secondary's on-board
# slew-limited PI filter (src/timesync.c) turns the beacon stream
# into its (offset, rate) machine-time mapping.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging

BEACON_INTERVAL = 0.9839    # matches clocksync.py's clock-query cadence
PRIME_COUNT = 8             # startup priming burst (doc 01 startup step 2)
PRIME_INTERVAL = 0.050
FREEWHEEL_TIME = 5.0        # doc 01 freewheel budget on beacon loss
CONVERGE_WINDOW = 0.000010  # +-10us inter-MCU sync error target

# timesync_state flag bits (must match src/timesync.c)
TS_ENABLED = 1
TS_PRIMED = 2
TS_CONVERGED = 4

def _get_clocksync(mcu):
    return mcu.get_clocksync()

class SecondaryLink:
    def __init__(self, mcu):
        self.mcu = mcu
        self.name = mcu.get_name()
        self.mcu_freq = mcu.get_constant_float('CLOCK_FREQ')
        self.relay_cmd = mcu.lookup_command(
            'sync_beacon_relay seq=%c machine_clock=%u local_est=%u')
        self.setup_cmd = mcu.lookup_command(
            'timesync_setup freewheel_ticks=%u converge_window=%u')
        self.query_cmd = mcu.lookup_query_command(
            'timesync_query',
            'timesync_state flags=%c prime_count=%c rate=%u last_err=%i'
            ' machine_ref=%u local_ref=%u')
        self.last_state = {'flags': 0, 'last_err': 0, 'rate': 0}
        self.freewheel_time = 0.
        self.last_beacon_time = None
    def setup(self, freewheel_time, converge_window):
        self.freewheel_time = freewheel_time
        self.setup_cmd.send([int(freewheel_time * self.mcu_freq) & 0xffffffff,
                             int(converge_window * self.mcu_freq)])
    def relay(self, seq, machine_clock, systime):
        local_est = int(_get_clocksync(self.mcu).systime_to_local_clock(
            systime))
        self.relay_cmd.send([seq, machine_clock & 0xffffffff,
                             local_est & 0xffffffff])
        # systime is the host-monotonic instant corresponding to the
        # primary's sampled machine clock. It is also the host's freshness
        # witness for the firmware freewheel gate.
        self.last_beacon_time = systime
    def query(self):
        self.last_state = self.query_cmd.send([])
        return self.last_state
    def is_converged(self, eventtime=None):
        if not self.last_state['flags'] & TS_CONVERGED:
            return False
        if eventtime is not None and self.freewheel_time:
            if (self.last_beacon_time is None
                    or eventtime - self.last_beacon_time
                    > self.freewheel_time):
                return False
        return True

class MachineTimeSync:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.beacon_interval = config.getfloat(
            'beacon_interval', BEACON_INTERVAL, above=0.)
        self.freewheel_time = config.getfloat(
            'freewheel_time', FREEWHEEL_TIME, above=0.)
        self.converge_window = config.getfloat(
            'converge_window', CONVERGE_WINDOW, above=0.)
        self.primary = None
        self.read_cmd = None
        self.secondaries = []
        self._last_converged = {}
        self.prime_remaining = 0
        self.beacon_timer = self.reactor.register_timer(self._beacon_event)
        self.printer.register_event_handler('klippy:ready',
                                            self._handle_ready)
        self.printer.register_event_handler('klippy:disconnect',
                                            self._handle_disconnect)
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command('TIMESYNC_STATUS', self.cmd_TIMESYNC_STATUS,
                               desc=self.cmd_TIMESYNC_STATUS_help)
    def _handle_ready(self):
        primary = self.printer.lookup_object('mcu')
        if primary.is_fileoutput():
            return
        self.read_cmd = primary.try_lookup_command('sync_beacon_read')
        if self.read_cmd is None:
            logging.info("timesync: primary mcu lacks sync_beacon_read;"
                         " machine-time beacons disabled")
            return
        self.primary = primary
        self.secondaries = []
        self._last_converged = {}
        for name, mcu in self.printer.lookup_objects(module='mcu'):
            if mcu is primary:
                continue
            if mcu.try_lookup_command('sync_beacon_relay seq=%c'
                                      ' machine_clock=%u local_est=%u') \
                    is None:
                logging.info("timesync: mcu '%s' lacks sync_beacon_relay;"
                             " not disciplined", mcu.get_name())
                continue
            self.secondaries.append(SecondaryLink(mcu))
        if not self.secondaries:
            logging.info("timesync: no secondary mcus to discipline")
            return
        primary.register_serial_response(self._handle_sync_beacon,
                                         'sync_beacon seq=%c clock=%u')
        for link in self.secondaries:
            link.setup(self.freewheel_time, self.converge_window)
        # Priming burst, then drop to the 1 Hz cadence (doc 01 startup)
        self.prime_remaining = PRIME_COUNT
        logging.info("timesync: disciplining %s to machine time",
                     [s.name for s in self.secondaries])
        self.reactor.update_timer(self.beacon_timer, self.reactor.NOW)
    def _handle_disconnect(self):
        self.reactor.update_timer(self.beacon_timer, self.reactor.NEVER)
        self.primary = None
        self.secondaries = []
        self._last_converged = {}
    # Beacon relay loop
    def _beacon_event(self, eventtime):
        self.read_cmd.send()
        if self.prime_remaining:
            return eventtime + PRIME_INTERVAL
        return eventtime + self.beacon_interval
    def _handle_sync_beacon(self, params):
        # Called from the serial background thread; hop to the reactor
        self.reactor.register_async_callback(
            (lambda e, p=params: self._relay_beacon(p)))
    def _relay_beacon(self, params):
        primary = self.primary
        if primary is None:
            return
        machine_clock = primary.clock32_to_clock64(params['clock'])
        # Bridge machine time to each secondary's local clock through
        # host time (both hops are min-RTT-anchored regressions).
        systime = _get_clocksync(primary).machine_time_to_systime(
            machine_clock)
        for link in self.secondaries:
            link.relay(params['seq'], machine_clock, systime)
        if self.prime_remaining:
            self.prime_remaining -= 1
            if not self.prime_remaining:
                self._check_convergence()
        else:
            # Keep the host-side gate current after startup. This query is
            # ordered behind the relay on each MCU command queue and runs at
            # the steady beacon cadence (about 1Hz), not in the 50ms burst.
            self._check_convergence()
    def _check_convergence(self):
        for link in self.secondaries:
            state = link.query()
            converged = link.is_converged(self.reactor.monotonic())
            if self._last_converged.get(link.name) != converged:
                logging.info(
                    "timesync: mcu '%s' %s flags=%d rate=%d"
                    " last_err=%d ticks",
                    link.name, "converged" if converged else "syncing",
                    state['flags'], state['rate'], state['last_err'])
                self._last_converged[link.name] = converged
    # Host-visible counterpart of the firmware ingest gate. trajq.c calls
    # timesync_class0_ok() before accepting every segment; clients can use
    # this query to avoid sending work that a syncing secondary will refuse.
    def is_mcu_synced(self, mcu_name):
        eventtime = self.reactor.monotonic()
        for link in self.secondaries:
            if link.name == mcu_name:
                return link.is_converged(eventtime)
        # Primary (or undisciplined) boards need no beacon sync
        return True
    def get_status(self, eventtime):
        machine_time = None
        if self.primary is not None:
            # Open question resolved in favor of exposure: clients can
            # use this to synchronize cameras/sensors to machine time.
            machine_time = self.primary.estimated_print_time(eventtime)
        return {
            'machine_time': machine_time,
            'mcus': {link.name: {
                'converged': link.is_converged(eventtime),
                'last_err_ticks': link.last_state['last_err'],
                'rate': link.last_state['rate'],
            } for link in self.secondaries},
        }
    cmd_TIMESYNC_STATUS_help = "Report machine-time beacon discipline state"
    def cmd_TIMESYNC_STATUS(self, gcmd):
        if self.primary is None or not self.secondaries:
            gcmd.respond_info("timesync: no disciplined secondary mcus")
            return
        msgs = []
        eventtime = self.reactor.monotonic()
        for link in self.secondaries:
            state = link.query()
            ppm = 0.
            if state['rate']:
                ppm = (state['rate'] / (1 << 30) - 1.) * 1e6
            msgs.append(
                "mcu '%s': %s err=%.1fus rate=%+.2fppm" % (
                    link.name,
                    ("CONVERGED" if link.is_converged(eventtime)
                     else "SYNCING"),
                    state['last_err'] / link.mcu_freq * 1e6, ppm))
        gcmd.respond_info("\n".join(msgs))

def load_config(config):
    return MachineTimeSync(config)
