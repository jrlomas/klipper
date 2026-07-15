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

try:
    from .usb_sof_sync import UsbSofLink
except ImportError:
    from usb_sof_sync import UsbSofLink

BEACON_INTERVAL = 0.9839    # matches clocksync.py's clock-query cadence
PRIME_COUNT = 8             # startup priming burst (doc 01 startup step 2)
PRIME_INTERVAL = 0.050
FREEWHEEL_TIME = 5.0        # doc 01 freewheel budget on beacon loss
# The discipline filter targets zero offset.  Robust endpoint fitting rejects
# the independent-USB outliers that previously required a wider workaround;
# Class-0 trust now matches FD-0001's inter-MCU budget directly.
CONVERGE_WINDOW = 0.000010  # +-10us Class-0 acceptance window
RATE_SHIFT = 24             # firmware Q8.24 local/machine tick ratio
RELAY_FIT_SAMPLES = 16      # smooth cross-link regression endpoint jitter
RELAY_FIT_MIN_SPAN = 4.0    # reject high-variance short-burst rate fits
HOST_STABLE_COUNT = 8       # consecutive steady host-model beacons
HOST_RATE_TOLERANCE_PPM = 2.0
SOF_CAPTURE_DELAY = 0.010

# timesync_state flag bits (must match src/timesync.c)
TS_ENABLED = 1
TS_PRIMED = 2
TS_CONVERGED = 4

def _get_clocksync(mcu):
    return mcu.get_clocksync()

def _clock_diagnostics(clocksync):
    clock_est = getattr(clocksync, 'clock_est', (0., 0., 0.))
    return {
        'sample_time': clock_est[0],
        'frequency': clock_est[2],
        'min_half_rtt': getattr(clocksync, 'min_half_rtt', None),
        'prediction_variance': getattr(clocksync, 'prediction_variance', None),
    }

def _median(values):
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) & 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) * .5

def _robust_endpoint_fit(samples):
    """Return a Theil-Sen rate and median-projected current endpoint.

    Independent USB clock regressions occasionally move by several
    microseconds in one update, particularly on a non-RT host.  Ordinary
    least squares gives such a point enough leverage to steer every board's
    machine-time mapping.  Pairwise-median slope and median projection retain
    the oscillator trend while rejecting isolated scheduling/USB outliers.
    """
    slopes = []
    for index, (machine_a, local_a) in enumerate(samples[:-1]):
        for machine_b, local_b in samples[index + 1:]:
            machine_delta = machine_b - machine_a
            if machine_delta > 0:
                slopes.append((local_b - local_a) / machine_delta)
    if not slopes:
        return samples[-1][1], None
    rate = _median(slopes)
    machine_now = samples[-1][0]
    endpoints = [local + rate * (machine_now - machine)
                 for machine, local in samples]
    return int(round(_median(endpoints))), rate

class SecondaryLink:
    def __init__(self, mcu, primary_freq, primary_clocksync=None):
        self.mcu = mcu
        self.name = mcu.get_name()
        self.mcu_freq = mcu.get_constant_float('CLOCK_FREQ')
        self.primary_freq = primary_freq
        self.primary_clocksync = primary_clocksync
        # ClockSync has already regressed both oscillator rates before
        # klippy:ready.  Seed firmware from that long-lived estimate instead
        # of forcing it to infer ppm error from the 350ms beacon burst.
        primary_est = getattr(primary_clocksync, 'clock_est', (0., 0., 0.))[2]
        local_est = getattr(_get_clocksync(mcu), 'clock_est', (0., 0., 0.))[2]
        if primary_est <= 0. or local_est <= 0.:
            primary_est, local_est = primary_freq, self.mcu_freq
        self.nominal_rate = local_est / primary_est
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
        # Unit/file-output users without a primary ClockSync retain legacy
        # behavior.  Live multi-MCU links must prove the two host clock models
        # have stopped moving before host-side Class-0 preflight succeeds.
        self.host_model_stable = primary_clocksync is None
        self.host_stable_count = 0
        self.last_host_rtt = None
        self.host_rate = None
        self.host_rate_error_ppm = None
        self.interval_supported = False
        self.interval_samples = 0
        self.interval_reference = None
        self.interval_diagnostics = None
        self.sof_link = None
        self.sof_relay_cmd = None
        self.sof_pending_beacon = None
        self.sof_unpaired_beacons = 0
        self.sof_rates = []
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
        self.host_model_stable = self.primary_clocksync is None
        self.host_stable_count = 0
        self.last_host_rtt = None
        self.host_rate = None
        self.host_rate_error_ppm = None
        self.interval_samples = 0
        self.interval_reference = None
        self.interval_diagnostics = None
        self.sof_pending_beacon = None
        self.sof_unpaired_beacons = 0
        self.sof_rates = []
    def setup(self, freewheel_time, converge_window):
        self.freewheel_time = freewheel_time
        self.setup_cmd.send([int(freewheel_time * self.mcu_freq) & 0xffffffff,
                             int(converge_window * self.mcu_freq),
                             self.nominal_rate_raw])
    def note_interval(self, seq, machine_clock, primary_sent,
                      primary_received, local_clock, secondary_sent,
                      secondary_received):
        """Record fresh two-link RTT evidence without changing discipline.

        Each MCU timestamp is known only to have occurred between the host
        send and receive times on that link.  With no symmetry assumption,
        the relative phase midpoint uncertainty is therefore the sum of the
        two half-RTTs.  The midpoint-change diagnostic is referenced to the
        first paired sample and uses the current oscillator regressions; it
        is useful evidence, but is deliberately not used as a trust gate.
        """
        values = (primary_sent, primary_received,
                  secondary_sent, secondary_received)
        if (not all(value is not None for value in values)
                or primary_received < primary_sent
                or secondary_received < secondary_sent):
            return
        primary_rtt = primary_received - primary_sent
        secondary_rtt = secondary_received - secondary_sent
        primary_mid = .5 * (primary_sent + primary_received)
        secondary_mid = .5 * (secondary_sent + secondary_received)
        primary_freq = _clock_diagnostics(
            self.primary_clocksync)['frequency']
        local_freq = _clock_diagnostics(_get_clocksync(
            self.mcu))['frequency']
        midpoint_change = None
        if primary_freq > 0. and local_freq > 0.:
            if self.interval_reference is None:
                self.interval_reference = (
                    machine_clock, primary_mid, local_clock, secondary_mid)
            ref_machine, ref_pmid, ref_local, ref_smid = (
                self.interval_reference)
            primary_offset_change = (
                (machine_clock - ref_machine) / primary_freq
                - (primary_mid - ref_pmid))
            local_offset_change = (
                (local_clock - ref_local) / local_freq
                - (secondary_mid - ref_smid))
            midpoint_change = local_offset_change - primary_offset_change
        self.interval_samples += 1
        self.interval_diagnostics = {
            'seq': seq,
            'primary_machine_clock': machine_clock,
            'primary_host_sent': primary_sent,
            'primary_host_received': primary_received,
            'primary_rtt_us': primary_rtt * 1.e6,
            'secondary_local_clock': local_clock,
            'secondary_host_sent': secondary_sent,
            'secondary_host_received': secondary_received,
            'secondary_rtt_us': secondary_rtt * 1.e6,
            'relative_half_width_us': (
                .5 * (primary_rtt + secondary_rtt) * 1.e6),
            'relative_midpoint_change_us': (
                None if midpoint_change is None
                else midpoint_change * 1.e6),
        }
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
        fit_span = self.relay_samples[-1][0] - self.relay_samples[0][0]
        if (len(self.relay_samples) >= 5
                and fit_span >= self.primary_freq * RELAY_FIT_MIN_SPAN):
            # Robustly estimate both the oscillator ratio and the endpoint.
            # See _robust_endpoint_fit() for why an OLS fit is unsafe here.
            local_est, self.relay_rate = _robust_endpoint_fit(
                self.relay_samples)
        if self.primary_clocksync is not None:
            primary_diag = _clock_diagnostics(self.primary_clocksync)
            local_diag = _clock_diagnostics(_get_clocksync(self.mcu))
            host_rtt = (primary_diag['min_half_rtt'],
                        local_diag['min_half_rtt'])
            rtt_changed = (self.last_host_rtt is not None
                           and host_rtt != self.last_host_rtt)
            self.last_host_rtt = host_rtt
            if primary_diag['frequency'] and local_diag['frequency']:
                self.host_rate = (local_diag['frequency']
                                  / primary_diag['frequency'])
            rate_steady = False
            if self.relay_rate is not None and self.host_rate:
                self.host_rate_error_ppm = (
                    self.relay_rate / self.host_rate - 1.) * 1.e6
                rate_steady = (abs(self.host_rate_error_ppm)
                               <= HOST_RATE_TOLERANCE_PPM)
            if rtt_changed or not rate_steady:
                self.host_stable_count = 0
                self.host_model_stable = False
            else:
                self.host_stable_count = min(
                    HOST_STABLE_COUNT, self.host_stable_count + 1)
                self.host_model_stable = (
                    self.host_stable_count >= HOST_STABLE_COUNT)
        self.last_machine_clock = machine_clock
        self.last_local_est = local_est
        self.last_raw_local_est = raw_local_est
        self.relay_cmd.send([seq, machine_clock & 0xffffffff,
                             local_est & 0xffffffff])
        # systime is the host-monotonic instant corresponding to the
        # primary's sampled machine clock. It is also the host's freshness
        # witness for the firmware freewheel gate.
        self.last_beacon_time = systime
    def relay_sof(self, seq, machine_clock, local_clock, eventtime):
        had_pending_beacon = self.sof_pending_beacon is not None
        if self.last_machine_clock is not None:
            machine_delta = machine_clock - self.last_machine_clock
            local_delta = local_clock - self.last_raw_local_est
            if machine_delta > 0:
                self.sample_rate = local_delta / machine_delta
                self.host_rate = self.sample_rate
                # An exact same-frame pair is a better rate observation than
                # the software-derived ClockSync estimates used to seed the
                # firmware. Establish SOF rate consistency from those exact
                # pairs themselves; otherwise a biased startup USB estimate
                # can permanently veto the hardware measurement.
                reference = (_median(self.sof_rates)
                             if self.sof_rates else self.sample_rate)
                self.host_rate_error_ppm = (
                    self.sample_rate / reference - 1.) * 1.e6
                if abs(self.host_rate_error_ppm) <= HOST_RATE_TOLERANCE_PPM:
                    self.sof_rates.append(self.sample_rate)
                    del self.sof_rates[:-HOST_STABLE_COUNT]
                    self.host_stable_count = min(
                        HOST_STABLE_COUNT, self.host_stable_count + 1)
                else:
                    # Begin a fresh candidate interval sequence. A genuine
                    # oscillator-rate change can reacquire; one bad pair
                    # cannot remain hidden in the reference window.
                    self.sof_rates = [self.sample_rate]
                    self.host_stable_count = 1
        self.host_model_stable = (
            self.host_stable_count >= HOST_STABLE_COUNT)
        self.last_machine_clock = machine_clock
        self.last_local_est = self.last_raw_local_est = local_clock
        self.last_beacon_time = eventtime
        self.sof_pending_beacon = None
        if not had_pending_beacon:
            # The exact pair arrived before sync_beacon_read's response.
            # Suppress that response's later software-derived relay so one
            # beacon cannot discipline the firmware twice.
            self.sof_unpaired_beacons += 1
        self.sof_relay_cmd.send([
            seq, machine_clock & 0xffffffff, local_clock & 0xffffffff])
    def query(self):
        self.last_state = self.query_cmd.send([])
        return self.last_state
    def is_converged(self, eventtime=None):
        if not self.host_model_stable:
            return False
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
        self.usb_sof = config.getboolean('usb_sof', False)
        self.primary = None
        self.read_cmd = None
        self.secondaries = []
        self._primary_intervals = {}
        self._last_converged = {}
        self._paused_mcus = set()
        self.prime_remaining = 0
        self.beacon_timer = self.reactor.register_timer(self._beacon_event)
        self.sof_timer = self.reactor.register_timer(self._sof_event)
        self.sof_primary = None
        self.sof_links = []
        self.sof_seq = 0
        self.sof_capture_seq = None
        self.sof_commissioning = False
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
        self._primary_intervals = {}
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
            link = SecondaryLink(
                mcu, primary.get_constant_float('CLOCK_FREQ'),
                _get_clocksync(primary))
            if mcu.check_valid_response(
                    'sync_beacon_ack seq=%c local_rx=%u'):
                link.interval_supported = True
                mcu.register_serial_response(
                    (lambda params, target=link:
                     self._handle_sync_ack(target, params)),
                    'sync_beacon_ack seq=%c local_rx=%u')
            self.secondaries.append(link)
        if not self.secondaries:
            logging.info("timesync: no secondary mcus to discipline")
            return
        primary.register_serial_response(self._handle_sync_beacon,
                                         'sync_beacon seq=%c clock=%u')
        for link in self.secondaries:
            link.setup(self.freewheel_time, self.converge_window)
        self._setup_sof()
        # Priming burst, then drop to the 1 Hz cadence (doc 01 startup)
        self.prime_remaining = PRIME_COUNT
        logging.info("timesync: disciplining %s to machine time",
                     [s.name for s in self.secondaries])
        self.reactor.update_timer(self.beacon_timer, self.reactor.NOW)
    def _handle_disconnect(self):
        self.reactor.update_timer(self.beacon_timer, self.reactor.NEVER)
        self.reactor.update_timer(self.sof_timer, self.reactor.NEVER)
        self.primary = None
        self.secondaries = []
        self._primary_intervals = {}
        self._last_converged = {}
        self._paused_mcus = set()
        self.sof_primary = None
        self.sof_links = []
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
    def _setup_sof(self):
        self.sof_primary = None
        self.sof_links = []
        if not self.usb_sof:
            return
        primary_link = UsbSofLink(self.primary)
        if not primary_link.setup():
            logging.warning("timesync: primary lacks USB SOF timestamps;"
                            " using host-relayed clock estimates")
            return
        for link in self.secondaries:
            sof_link = UsbSofLink(link.mcu)
            relay_cmd = link.mcu.try_lookup_command(
                'sync_sof_relay seq=%c machine_clock=%u local_clock=%u')
            if relay_cmd is None or not sof_link.setup():
                logging.warning("timesync: mcu '%s' lacks USB SOF discipline;"
                                " using host-relayed clock estimates",
                                link.name)
                continue
            link.sof_link = sof_link
            link.sof_relay_cmd = relay_cmd
            link.host_model_stable = False
            link.host_stable_count = 0
            self.sof_links.append(link)
        if self.sof_links:
            self.sof_primary = primary_link
            logging.info("timesync: using matched USB SOF timestamps for %s",
                         [link.name for link in self.sof_links])

    def set_sof_commissioning(self, active):
        self.sof_commissioning = active
        if active and self.sof_primary is not None:
            self.reactor.update_timer(self.sof_timer, self.reactor.NEVER)
            self.sof_primary.enable(False)
            for link in self.sof_links:
                link.sof_link.enable(False)
                self._fallback_sof_link(link)
            self.sof_capture_seq = None

    def _start_sof_capture(self, eventtime):
        if (self.sof_primary is None or self.sof_commissioning
                or self.primary.get_name() in self._paused_mcus):
            return
        active = [link for link in self.sof_links
                  if link.name not in self._paused_mcus]
        if not active:
            return
        self.sof_primary.enable(True)
        for link in active:
            link.sof_link.enable(True)
        self.sof_capture_seq = self.sof_seq
        self.sof_seq = (self.sof_seq + 1) & 0xff
        self.reactor.update_timer(
            self.sof_timer, eventtime + SOF_CAPTURE_DELAY)

    def _fallback_sof_link(self, link):
        pending = link.sof_pending_beacon
        if pending is not None:
            link.sof_pending_beacon = None
            link.relay(*pending)

    def _sof_event(self, eventtime):
        active = [link for link in self.sof_links
                  if link.name not in self._paused_mcus]
        try:
            primary = self.sof_primary.query()
            if not primary['found']:
                raise self.printer.command_error(
                    "primary USB SOF ring has no sample")
            machine_clock = self.primary.clock32_to_clock64(
                primary['clock'])
            seq = self.sof_capture_seq
            for link in active:
                secondary = link.sof_link.query(primary['frame'])
                if not secondary['found']:
                    logging.warning(
                        "timesync: mcu '%s' missed USB SOF frame %d;"
                        " using host estimate", link.name, primary['frame'])
                    self._fallback_sof_link(link)
                    continue
                local_clock = link.mcu.clock32_to_clock64(
                    secondary['clock'])
                link.relay_sof(seq, machine_clock, local_clock, eventtime)
        except self.printer.command_error as exc:
            logging.warning("timesync: USB SOF capture failed (%s); using"
                            " host estimates", exc)
            for link in active:
                self._fallback_sof_link(link)
        finally:
            if self.sof_primary is not None:
                self.sof_primary.enable(False)
            for link in self.sof_links:
                link.sof_link.enable(False)
            self.sof_capture_seq = None
        self._check_convergence()
        return self.reactor.NEVER

    def _beacon_event(self, eventtime):
        if (self.primary is None
                or self.primary.get_name() in self._paused_mcus):
            return eventtime + self.beacon_interval
        self._start_sof_capture(eventtime)
        self.read_cmd.send()
        if self.prime_remaining:
            return eventtime + PRIME_INTERVAL
        return eventtime + self.beacon_interval
    def _handle_sync_beacon(self, params):
        # Called from the serial background thread; hop to the reactor
        self.reactor.register_async_callback(
            (lambda e, p=params: self._relay_beacon(p)))
    def _handle_sync_ack(self, link, params):
        # Called from the serial background thread; clock extension and state
        # publication belong on the reactor thread.
        self.reactor.register_async_callback(
            (lambda e, target=link, p=params:
             self._record_sync_interval(target, p)))
    def _record_sync_interval(self, link, params):
        primary_sample = self._primary_intervals.get(params['seq'])
        if primary_sample is None or link not in self.secondaries:
            return
        machine_clock, primary_sent, primary_received = primary_sample
        local_clock = link.mcu.clock32_to_clock64(params['local_rx'])
        link.note_interval(
            params['seq'], machine_clock, primary_sent, primary_received,
            local_clock, params.get('#sent_time'),
            params.get('#receive_time'))
    def _relay_beacon(self, params):
        primary = self.primary
        if (primary is None or primary.get_name() in self._paused_mcus):
            return
        machine_clock = primary.clock32_to_clock64(params['clock'])
        self._primary_intervals[params['seq']] = (
            machine_clock, params.get('#sent_time'),
            params.get('#receive_time'))
        # Bridge machine time to each secondary's local clock through
        # host time (both hops are min-RTT-anchored regressions).
        systime = _get_clocksync(primary).machine_time_to_systime(
            machine_clock)
        for link in self.secondaries:
            if link.name in self._paused_mcus:
                continue
            if (link in self.sof_links and not self.sof_commissioning
                    and self.sof_capture_seq is None
                    and link.sof_unpaired_beacons):
                link.sof_unpaired_beacons -= 1
                continue
            if (link in self.sof_links and not self.sof_commissioning
                    and self.sof_capture_seq is not None):
                link.sof_pending_beacon = (
                    self.sof_capture_seq, machine_clock, systime)
                continue
            link.relay(params['seq'], machine_clock, systime)
        if self.prime_remaining:
            self.prime_remaining -= 1
            if not self.prime_remaining:
                # _beacon_event scheduled this response's successor while
                # prime_remaining was still non-zero. Replace that pending
                # priming event with the steady cadence; otherwise the first
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
    def get_mcu_mapping(self, mcu_name):
        """Return a fresh firmware machine-to-local clock mapping."""
        for link in self.secondaries:
            if link.name == mcu_name:
                state = link.query()
                return {
                    'mcu_freq': link.mcu_freq,
                    'machine_ref': state['machine_ref'],
                    'local_ref': state['local_ref'],
                    'rate': state['rate'],
                    'flags': state['flags'],
                    'converged': link.is_converged(
                        self.reactor.monotonic()),
                }
        return None
    def get_status(self, eventtime):
        machine_time = None
        primary_clock = None
        if self.primary is not None:
            # Open question resolved in favor of exposure: clients can
            # use this to synchronize cameras/sensors to machine time.
            machine_time = self.primary.estimated_print_time(eventtime)
            primary_clock = _clock_diagnostics(
                _get_clocksync(self.primary))
        return {
            'machine_time': machine_time,
            'primary_host_clock': primary_clock,
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
                'host_model_stable': link.host_model_stable,
                'host_stable_count': link.host_stable_count,
                'host_rate': link.host_rate,
                'host_rate_error_ppm': link.host_rate_error_ppm,
                'interval_supported': link.interval_supported,
                'interval_samples': link.interval_samples,
                'interval_diagnostics': link.interval_diagnostics,
                'usb_sof': link in self.sof_links,
                'host_clock': _clock_diagnostics(
                    _get_clocksync(link.mcu)),
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
            interval = ""
            if link.interval_diagnostics is not None:
                interval = " observed_rtt_bound=+/-%.1fus" % (
                    link.interval_diagnostics['relative_half_width_us'],)
            msgs.append(
                "mcu '%s': %s err=%.1fus rate=%+.2fppm%s" % (
                    link.name,
                    ("CONVERGED" if link.is_converged(eventtime)
                     else "SYNCING"),
                    state['last_err'] / link.mcu_freq * 1e6, ppm,
                    interval))
        gcmd.respond_info("\n".join(msgs))

def load_config(config):
    return MachineTimeSync(config)
