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
# The discipline filter still targets zero offset.  This is the operational
# Class-0 trust window, sized above the 10-13us endpoint-estimation noise
# measured on independent USB links so a healthy mapping does not flap while
# motion is active.
CONVERGE_WINDOW = 0.000020  # +-20us Class-0 acceptance window
RATE_SHIFT = 24             # firmware Q8.24 local/machine tick ratio
RELAY_FIT_SAMPLES = 16      # smooth cross-link regression endpoint jitter

# timesync_state flag bits (must match src/timesync.c)
TS_ENABLED = 1
TS_PRIMED = 2
TS_CONVERGED = 4

def _get_clocksync(mcu):
    return mcu.get_clocksync()

class SecondaryLink:
    def __init__(self, mcu, primary_freq):
        self.mcu = mcu
        self.name = mcu.get_name()
        self.mcu_freq = mcu.get_constant_float('CLOCK_FREQ')
        self.nominal_rate = self.mcu_freq / primary_freq
        self.nominal_rate_raw = round(
            self.nominal_rate * (1 << RATE_SHIFT))
        self.relay_cmd = mcu.lookup_command(
            'sync_beacon_relay seq=%c machine_clock=%u local_est=%u')
        self.setup_cmd = mcu.lookup_command(
            'timesync_setup freewheel_ticks=%u converge_window=%u'
            ' nominal_rate=%u')
        self.query_cmd = mcu.lookup_query_command(
            'timesync_query',
            'timesync_state flags=%c prime_count=%c rate=%u last_err=%i'
            ' machine_ref=%u local_ref=%u')
        self.last_state = {'flags': 0, 'last_err': 0, 'rate': 0}
        self.freewheel_time = 0.
        self.last_beacon_time = None
        self.last_machine_clock = None
        self.last_local_est = None
        self.last_raw_local_est = None
        self.sample_rate = None
        self.relay_rate = None
        self.relay_samples = []
    def reset_relay_history(self):
        # The firmware mapping is intentionally retained while a board
        # freewheels.  Host endpoint-fit samples straddling a USB outage are
        # not valid, however, and must not influence the first post-reconnect
        # relay estimate.
        self.last_state = {'flags': 0, 'last_err': 0, 'rate': 0}
        self.last_beacon_time = None
        self.last_machine_clock = None
        self.last_local_est = None
        self.last_raw_local_est = None
        self.sample_rate = None
        self.relay_rate = None
        self.relay_samples = []
    def setup(self, freewheel_time, converge_window):
        self.freewheel_time = freewheel_time
        self.setup_cmd.send([int(freewheel_time * self.mcu_freq) & 0xffffffff,
                             int(converge_window * self.mcu_freq),
                             self.nominal_rate_raw])
    def relay(self, seq, machine_clock, systime):
        raw_local_est = int(_get_clocksync(self.mcu).systime_to_local_clock(
            systime))
        if self.last_machine_clock is not None:
            machine_delta = machine_clock - self.last_machine_clock
            local_delta = raw_local_est - self.last_raw_local_est
            if machine_delta > 0:
                self.sample_rate = local_delta / machine_delta
        self.relay_samples.append((machine_clock, raw_local_est))
        del self.relay_samples[:-RELAY_FIT_SAMPLES]
        local_est = raw_local_est
        if len(self.relay_samples) >= 3:
            # Regress with x relative to the current machine clock. This
            # preserves float precision for long-running 64-bit epochs and
            # estimates the current endpoint from the whole trailing span,
            # reducing the leverage of one noisy USB regression update.
            xs = [m - machine_clock for m, _l in self.relay_samples]
            ys = [l for _m, l in self.relay_samples]
            xmean = sum(xs) / len(xs)
            ymean = sum(ys) / len(ys)
            variance = sum((x - xmean) ** 2 for x in xs)
            if variance:
                covariance = sum((x - xmean) * (y - ymean)
                                 for x, y in zip(xs, ys))
                self.relay_rate = covariance / variance
                local_est = int(round(ymean - self.relay_rate * xmean))
        self.last_machine_clock = machine_clock
        self.last_local_est = local_est
        self.last_raw_local_est = raw_local_est
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
        self._paused_mcus = set()
        self.prime_remaining = 0
        self.beacon_timer = self.reactor.register_timer(self._beacon_event)
        self.printer.register_event_handler('klippy:ready',
                                            self._handle_ready)
        self.printer.register_event_handler('klippy:disconnect',
                                            self._handle_disconnect)
        self.printer.register_event_handler('mcu:comm_pause',
                                            self._handle_comm_pause)
        self.printer.register_event_handler('mcu:comm_resume',
                                            self._handle_comm_resume)
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
        self._paused_mcus = set()
        for name, mcu in self.printer.lookup_objects(module='mcu'):
            if mcu is primary:
                continue
            if mcu.try_lookup_command('sync_beacon_relay seq=%c'
                                      ' machine_clock=%u local_est=%u') \
                    is None:
                logging.info("timesync: mcu '%s' lacks sync_beacon_relay;"
                             " not disciplined", mcu.get_name())
                continue
            self.secondaries.append(SecondaryLink(
                mcu, primary.get_constant_float('CLOCK_FREQ')))
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
        self._paused_mcus = set()
    def _handle_comm_pause(self, mcu_name):
        self._paused_mcus.add(mcu_name)
        for link in self.secondaries:
            if link.name == mcu_name:
                link.reset_relay_history()
                self._last_converged.pop(mcu_name, None)
                break
        logging.info("timesync: suspended traffic to paused-link MCU '%s'",
                     mcu_name)
    def _handle_comm_resume(self, mcu_name):
        self._paused_mcus.discard(mcu_name)
        # clocksync.connect() has already re-anchored this serial link before
        # mcu.py emits comm_resume.  Start a fresh host relay fit; the next
        # normal beacon revalidates the firmware's retained/freewheeling map.
        for link in self.secondaries:
            if link.name == mcu_name:
                link.reset_relay_history()
                self._last_converged.pop(mcu_name, None)
                break
        logging.info("timesync: resumed traffic to MCU '%s'", mcu_name)
    # Beacon relay loop
    def _beacon_event(self, eventtime):
        if (self.primary is None
                or self.primary.get_name() in self._paused_mcus):
            return eventtime + self.beacon_interval
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
        if (primary is None or primary.get_name() in self._paused_mcus):
            return
        machine_clock = primary.clock32_to_clock64(params['clock'])
        # Bridge machine time to each secondary's local clock through
        # host time (both hops are min-RTT-anchored regressions).
        systime = _get_clocksync(primary).machine_time_to_systime(
            machine_clock)
        for link in self.secondaries:
            if link.name in self._paused_mcus:
                continue
            link.relay(params['seq'], machine_clock, systime)
        if self.prime_remaining:
            self.prime_remaining -= 1
            if not self.prime_remaining:
                # _beacon_event scheduled this response's successor while
                # prime_remaining was still non-zero. Replace that pending
                # 50ms event with the steady cadence; otherwise the first
                # disciplined sample sees startup USB jitter over a tiny
                # denominator and can kick the PI loop far from nominal.
                self.reactor.update_timer(
                    self.beacon_timer,
                    self.reactor.monotonic() + self.beacon_interval)
                self._check_convergence()
        else:
            # Keep the host-side gate current after startup. This query is
            # ordered behind the relay on each MCU command queue and runs at
            # the steady beacon cadence (about 1Hz), not in the 50ms burst.
            self._check_convergence()
    def _check_convergence(self):
        for link in self.secondaries:
            if link.name in self._paused_mcus:
                continue
            try:
                state = link.query()
            except self.printer.command_error:
                # A query can already be waiting for its transport ack when
                # comm_pause is emitted.  Reconnect deliberately cancels
                # never-transmitted work; let that pre-loss timer invocation
                # unwind without taking down the reactor.  Fresh sampling
                # resumes only after mcu:comm_resume.
                if link.name not in self._paused_mcus:
                    raise
                logging.info("timesync: query to MCU '%s' canceled at link"
                             " recovery boundary", link.name)
                continue
            logging.debug(
                "timesync sample: mcu='%s' relay_m=%s relay_l=%s"
                " raw_l=%s sample_rate=%s relay_rate=%s"
                " flags=%d prime=%d rate=%d err=%d"
                " map_m=%d map_l=%d",
                link.name, link.last_machine_clock, link.last_local_est,
                link.last_raw_local_est, link.sample_rate, link.relay_rate,
                state['flags'], state['prime_count'], state['rate'],
                state['last_err'], state['machine_ref'], state['local_ref'])
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
        if mcu_name in self._paused_mcus:
            return False
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
                'link_paused': link.name in self._paused_mcus,
                'flags': link.last_state['flags'],
                'prime_count': link.last_state.get('prime_count', 0),
                'last_err_ticks': link.last_state['last_err'],
                'rate': link.last_state['rate'],
                'machine_ref': link.last_state.get('machine_ref', 0),
                'local_ref': link.last_state.get('local_ref', 0),
                'relay_machine_clock': link.last_machine_clock,
                'relay_local_est': link.last_local_est,
                'raw_local_est': link.last_raw_local_est,
                'sample_rate': link.sample_rate,
                'relay_rate': link.relay_rate,
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
            if link.name in self._paused_mcus:
                msgs.append("mcu '%s': PAUSED-LINK" % (link.name,))
                continue
            state = link.query()
            ppm = 0.
            if state['rate']:
                applied_rate = state['rate'] / (1 << RATE_SHIFT)
                ppm = (applied_rate / link.nominal_rate - 1.) * 1e6
            msgs.append(
                "mcu '%s': %s err=%.1fus rate=%+.2fppm" % (
                    link.name,
                    ("CONVERGED" if link.is_converged(eventtime)
                     else "SYNCING"),
                    state['last_err'] / link.mcu_freq * 1e6, ppm))
        gcmd.respond_info("\n".join(msgs))

def load_config(config):
    return MachineTimeSync(config)
