# Failure recovery orchestration: pause-and-hold host side (RFC 0001
# doc 08).
#
# Responsibilities:
#  1. Execution log: configures the per-board execlog ring, keeps a
#     rolling on-disk window of the live stream (flight recorder),
#     and reliably drains the retained ring after a shutdown so the
#     record of what the machine actually executed survives failures.
#  2. Heater failsafe hold: plumbs the per-heater opt-in policy
#     ([heater_bed] failure_policy: hold, hold_max_temp,
#     hold_max_duration) to the MCU's autonomous bang-bang holder,
#     keeps its liveness ping running, and releases it on resume.
#     Only heaters explicitly configured hold; everything else keeps
#     the stock watchdog behavior.
#  3. Link-loss pause-and-hold: an mcu configured with
#     'on_comm_timeout: pause' emits "mcu:comm_pause" instead of
#     shutting the machine down when its link is lost.  This module
#     reacts by pausing the print (pause_resume), engaging every
#     configured heater hold (belt and braces - they also self-engage
#     on MCU-side ping silence), and dropping host heater targets on
#     the lost mcu so verify_heater cannot escalate quelled (stale)
#     readings into a machine-wide shutdown.
#  4. RECONNECT_MCU MCU=<name>: re-handshake with a paused-link mcu
#     (see MCU.attempt_reconnect for what is genuinely done and the
#     remaining seams).  If the board never rebooted the clock is
#     re-synced and the operator may RESUME; if it rebooted or the
#     config CRC differs, the honest answer - a full RESTART - is
#     reported instead.
#     On "mcu:comm_resume" heater holds are deliberately NOT
#     auto-released: blindly returning heaters to host control right
#     after a reconnect could leave them uncontrolled if no host
#     target is active.  The operator (or the resume workflow) stays
#     in charge - RELEASE_HEATER_HOLD (plus restoring heater targets)
#     or resuming the print restores host control.
#  5. Manual controls for testing and recovery workflows
#     (ENGAGE_HEATER_HOLD / RELEASE_HEATER_HOLD /
#     FAILURE_RECOVERY_STATUS, EXECLOG_DUMP).
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging

PIN_MIN_TIME = 0.100
EXECLOG_DEFAULT_SIZE = 256
PING_INTERVAL = 1.0
HOLD_SAMPLE_TIME = 0.25

# Execution-log record types (mirror of src/execlog.h)
EL_SEG_DONE = 1
EL_TRIGGER = 2
EL_UNDERRUN = 3
EL_HOLD = 4
EL_REBASE = 5
EL_TRUNCATING = (EL_UNDERRUN, EL_TRIGGER)
EL_STOP_TYPES = (EL_SEG_DONE, EL_UNDERRUN, EL_HOLD, EL_TRIGGER)


class HeaterHold:
    def __init__(self, fr, config, heater_section):
        self.printer = config.get_printer()
        self.name = heater_section
        hconfig = config.getsection(heater_section)
        self.max_temp_cfg = hconfig.getfloat('max_temp')
        self.hold_max_temp = hconfig.getfloat(
            'hold_max_temp', 110., above=0., maxval=self.max_temp_cfg)
        self.hold_max_duration = hconfig.getfloat(
            'hold_max_duration', 3600., above=0.)
        self.hold_ping_timeout = hconfig.getfloat(
            'hold_ping_timeout', 5.0, above=1.)
        self.heater_pin = hconfig.get('heater_pin')
        self.sensor_pin = hconfig.get('sensor_pin')
        self.sensor_type = hconfig.get('sensor_type')
        ppins = self.printer.lookup_object('pins')
        pin_params = ppins.parse_pin(self.heater_pin, True, False)
        sensor_params = ppins.parse_pin(self.sensor_pin, False, False)
        if pin_params['chip'] is not sensor_params['chip']:
            raise config.error(
                "heater hold: heater and sensor must share an mcu")
        self.mcu = pin_params['chip']
        self.pin = pin_params['pin']
        self.sensor = sensor_params['pin']
        self.oid = self.mcu.create_oid()
        self.mcu.register_config_callback(self._build_config)
        self.ping_cmd = self.setup_cmd = None
        self.engage_cmd = self.release_cmd = None
        self.heater = None
        self.engaged = False

    def _build_config(self):
        # Thermistor-style dividers read hotter as lower ADC counts
        self.mcu.add_config_cmd(
            "config_heater_hold oid=%d heater_pin=%s sensor_pin=%s"
            " invert_sense=0" % (self.oid, self.pin, self.sensor))
        cq = self.mcu.alloc_command_queue()
        self.setup_cmd = self.mcu.lookup_command(
            "heater_hold_setup oid=%c target=%hu ceiling=%hu band=%hu"
            " min_valid=%hu max_valid=%hu ping_timeout=%u sample_ticks=%u"
            " max_samples=%u max_deviation=%c", cq=cq)
        self.ping_cmd = self.mcu.lookup_command(
            "heater_hold_ping oid=%c", cq=cq)
        self.engage_cmd = self.mcu.lookup_command(
            "heater_hold_engage oid=%c", cq=cq)
        self.release_cmd = self.mcu.lookup_command(
            "heater_hold_release oid=%c", cq=cq)

    def _temp_to_adc(self, temp):
        # The MCU works in raw ADC counts; convert through the
        # heater's own sensor calibration.
        pheaters = self.printer.lookup_object('heaters')
        heater = pheaters.lookup_heater(self.name.split()[-1])
        sensor = heater.sensor
        adc_value = sensor.adc_convert.calc_adc(temp)
        return max(0, min(0xffff, int(adc_value * 65535. + .5)))

    def arm(self, target_temp):
        if self.setup_cmd is None:
            return
        try:
            target = self._temp_to_adc(min(target_temp, self.hold_max_temp))
            ceiling = self._temp_to_adc(self.hold_max_temp)
            band_lo = self._temp_to_adc(max(0., target_temp - 15.))
            band = abs(band_lo - target)
            min_valid = self._temp_to_adc(self.max_temp_cfg)
            max_valid = self._temp_to_adc(0.)
            if min_valid > max_valid:
                min_valid, max_valid = max_valid, min_valid
        except Exception:
            logging.exception("heater hold: sensor conversion failed")
            return
        freq = self.mcu.seconds_to_clock(1.)
        sample_ticks = self.mcu.seconds_to_clock(HOLD_SAMPLE_TIME)
        max_samples = int(self.hold_max_duration / HOLD_SAMPLE_TIME)
        ping_ticks = self.mcu.seconds_to_clock(self.hold_ping_timeout)
        self.setup_cmd.send([self.oid, target, ceiling, max(1, band),
                             min_valid, max_valid, ping_ticks, sample_ticks,
                             max_samples, 8])

    def disarm(self):
        if self.setup_cmd is not None:
            # Zero sample_ticks disables the policy
            self.setup_cmd.send([self.oid, 0, 0, 0, 0, 0, 0, 0, 0, 0])

    def ping(self):
        if self.ping_cmd is not None:
            self.ping_cmd.send([self.oid])

    def engage(self):
        if self.engage_cmd is not None:
            self.engage_cmd.send([self.oid])
            self.engaged = True

    def release(self):
        if self.release_cmd is not None:
            self.release_cmd.send([self.oid])
            self.engaged = False


class McuExecLog:
    def __init__(self, fr, mcu, size):
        self.mcu = mcu
        self.size = size
        self.oid = mcu.create_oid()
        mcu.register_config_callback(self._build_config)
        self.query_cmd = self.dump_cmd = None
        self.records = []

    def _build_config(self):
        self.mcu.add_config_cmd("config_execlog oid=%d size=%d"
                                % (self.oid, self.size))
        cq = self.mcu.alloc_command_queue()
        self.query_cmd = self.mcu.lookup_query_command(
            "execlog_query oid=%c",
            "execlog_status oid=%c next_seq=%u oldest_seq=%u dropped=%u",
            oid=self.oid, cq=cq)
        self.dump_cmd = self.mcu.lookup_command(
            "execlog_dump oid=%c seq=%u count=%c", cq=cq)
        self.mcu.register_response(self._handle_data, "execlog_data",
                                   self.oid)

    def _handle_data(self, params):
        self.records.append(
            (params['seq'], params['type'], params['src'],
             params['clock'], params['pos'], params['aux']))

    # Reliable post-failure drain (Class-1 pull)
    def drain(self):
        if self.query_cmd is None or self.mcu.is_fileoutput():
            return []
        self.records = []
        try:
            status = self.query_cmd.send([self.oid])
        except Exception:
            logging.exception("execlog drain failed")
            return []
        seq = status['oldest_seq']
        end = status['next_seq']
        while seq < end:
            count = min(16, end - seq)
            self.dump_cmd.send([self.oid, seq, count])
            seq += count
        return self.records


class FailureRecovery:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.execlog_size = config.getint(
            'execlog_size', EXECLOG_DEFAULT_SIZE, minval=16, maxval=4096)
        self.holds = {}
        # Opt-in: route the execlog drain through the asyncio<->reactor
        # bridge seam (RFC 0001 doc 05) instead of the direct reactor
        # path. Defaults off so the working reactor path is unchanged;
        # this is the proof-of-consumer that the seam is real, not a
        # wholesale migration (the shutdown flight-recorder drain and
        # every other reactor call stay on the reactor).
        self.asyncio_drain = config.getboolean('asyncio_drain', False)
        self.bridge = None
        if self.asyncio_drain:
            self.bridge = self.printer.load_object(config, 'asyncio_bridge')
        # Per-heater opt-in policy
        pconfig = config.get_printer().lookup_object('configfile')
        for section in config.get_prefix_sections(''):
            name = section.get_name()
            if not (name.startswith('heater_') or name == 'extruder'
                    or name.startswith('extruder')):
                continue
            policy = section.get('failure_policy', 'off')
            if policy == 'hold':
                self.holds[name] = HeaterHold(self, config, name)
            elif policy != 'off':
                raise config.error("Unknown failure_policy '%s'" % (policy,))
        self.execlogs = []
        self.execlog_mcu_names = config.getlist('execlog_mcus', ('mcu',))
        self.link_paused_mcus = set()
        # Last motion-resume reconciliation result (for status)
        self.last_recovery = None
        self.printer.register_event_handler("klippy:mcu_identify",
                                            self._handle_mcu_identify)
        self.printer.register_event_handler("klippy:connect",
                                            self._handle_connect)
        self.printer.register_event_handler("klippy:shutdown",
                                            self._handle_shutdown)
        self.printer.register_event_handler("mcu:comm_pause",
                                            self._handle_comm_pause)
        self.printer.register_event_handler("mcu:comm_resume",
                                            self._handle_comm_resume)
        self.ping_timer = None
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command("FAILURE_RECOVERY_STATUS",
                               self.cmd_STATUS,
                               desc="Report failure recovery state")
        gcode.register_command("ENGAGE_HEATER_HOLD", self.cmd_ENGAGE,
                               desc="Engage the heater failsafe hold")
        gcode.register_command("RELEASE_HEATER_HOLD", self.cmd_RELEASE,
                               desc="Release the heater failsafe hold")
        gcode.register_command("EXECLOG_DUMP", self.cmd_EXECLOG_DUMP,
                               desc="Drain and log MCU execution logs")
        gcode.register_command("RECONNECT_MCU", self.cmd_RECONNECT_MCU,
                               desc="Re-handshake with an MCU whose link"
                               " was lost (paused-link state)")
        gcode.register_command("RESUME_MOTION", self.cmd_RESUME_MOTION,
                               desc="Reconcile intentions-sent against the"
                               " execution log, rebase every joint at its"
                               " held position, and resume the print")

    def _handle_mcu_identify(self):
        # Configure execution logs on the mcus that support them
        for name in self.execlog_mcu_names:
            objname = 'mcu' if name == 'mcu' else 'mcu ' + name
            mcu = self.printer.lookup_object(objname, None)
            if mcu is None:
                logging.warning("failure_recovery: no mcu named '%s'", name)
                continue
            if mcu.try_lookup_command("execlog_query oid=%c") is None:
                logging.info("failure_recovery: mcu '%s' lacks execlog",
                             name)
                continue
            self.execlogs.append(McuExecLog(self, mcu, self.execlog_size))

    def _handle_connect(self):
        # Arm heater holds at their configured targets and start pings
        reactor = self.printer.get_reactor()
        if self.holds:
            for hold in self.holds.values():
                hold.arm(hold.hold_max_temp)
            self.ping_timer = reactor.register_timer(
                self._ping_event, reactor.monotonic() + PING_INTERVAL)

    def _ping_event(self, eventtime):
        for hold in self.holds.values():
            try:
                hold.ping()
            except Exception:
                logging.exception("heater hold ping failed")
        return eventtime + PING_INTERVAL

    def _handle_shutdown(self):
        # Flight recorder: drain what the boards actually executed
        for el in self.execlogs:
            records = el.drain()
            if records:
                logging.info("execlog(%s): %d records: %s",
                             el.mcu.get_status(None).get('mcu', '?'),
                             len(records), records[-32:])
    # Link-loss pause-and-hold (mcu 'on_comm_timeout: pause')
    def _handle_comm_pause(self, mcu_name):
        # Invoked (in reactor context) by mcu.py when a link times out
        self.link_paused_mcus.add(mcu_name)
        logging.error(
            "failure_recovery: LINK LOST to MCU '%s' - pause-and-hold"
            " engaged. Pausing the print; motion and held heaters on that"
            " board continue autonomously. Reseat the connection and run"
            " RECONNECT_MCU MCU=%s, then RESUME.", mcu_name, mcu_name)
        # Engage every configured heater hold (belt and braces - the
        # MCU-side holders also self-engage on ping silence).  Commands
        # to the lost mcu are queued and delivered if/when the link
        # returns; commands to healthy mcus take effect immediately.
        for name, hold in sorted(self.holds.items()):
            try:
                hold.engage()
                logging.info("failure_recovery: engaged heater hold on %s",
                             name)
            except Exception:
                logging.exception("failure_recovery: engaging hold on %s"
                                  " failed", name)
        self._drop_heater_targets(mcu_name)
        # Pause the print from a reactor callback (mirrors the filament
        # runout sensor flow); heaters on healthy mcus keep their
        # targets under normal host control while paused.
        self.printer.get_reactor().register_callback(self._comm_pause_event)
    def _drop_heater_targets(self, mcu_name):
        # Zero the host-side target of every heater living on the lost
        # mcu: no PWM can reach it anyway, its readings will shortly be
        # quelled to 0 (heaters.py QUELL_STALE_TIME), and a nonzero
        # target plus a 0 reading makes verify_heater shut down the
        # whole machine.  The MCU-side hold policy (if configured)
        # keeps the heater warm autonomously.
        pheaters = self.printer.lookup_object('heaters', None)
        if pheaters is None:
            return
        eventtime = self.printer.get_reactor().monotonic()
        for hname in pheaters.get_all_heaters():
            try:
                heater = pheaters.lookup_heater(hname.split()[-1])
                if heater.mcu_pwm.get_mcu().get_name() != mcu_name:
                    continue
                temp, target = heater.get_temp(eventtime)
                if not target:
                    continue
                heater.set_temp(0.)
                logging.error(
                    "failure_recovery: heater %s is on lost MCU '%s' -"
                    " host target %.1f dropped to 0 (MCU-side hold policy,"
                    " if configured, keeps it warm)", hname, mcu_name, target)
            except Exception:
                logging.exception("failure_recovery: dropping target of %s"
                                  " failed", hname)
    def _comm_pause_event(self, eventtime):
        pause_resume = self.printer.lookup_object('pause_resume', None)
        if pause_resume is None:
            logging.error("failure_recovery: [pause_resume] not configured -"
                          " unable to pause the print after MCU link loss")
            return
        if pause_resume.get_status(eventtime)['is_paused']:
            return
        idle_timeout = self.printer.lookup_object('idle_timeout', None)
        if (idle_timeout is not None
                and idle_timeout.get_status(eventtime)['state'] != "Printing"):
            logging.info("failure_recovery: not printing - no print pause"
                         " needed after MCU link loss")
            return
        pause_resume.send_pause_command()
        try:
            gcode = self.printer.lookup_object('gcode')
            gcode.run_script("PAUSE")
        except Exception:
            logging.exception("failure_recovery: error running PAUSE after"
                              " MCU link loss")
    def _handle_comm_resume(self, mcu_name):
        # Deliberately do NOT auto-release the heater holds here:
        # handing a heater back to host control right after a reconnect
        # would leave it uncontrolled unless a host target is active
        # again.  The operator (or the resume workflow) restores
        # control: RELEASE_HEATER_HOLD plus restoring heater targets,
        # or resuming the print.
        self.link_paused_mcus.discard(mcu_name)
        logging.error(
            "failure_recovery: link to MCU '%s' RESTORED. Heater holds"
            " remain engaged - RELEASE_HEATER_HOLD (and restore heater"
            " targets) or resume the print to return heaters to host"
            " control. Run RESUME to continue a paused print.", mcu_name)
    def cmd_RECONNECT_MCU(self, gcmd):
        name = gcmd.get('MCU')
        objname = 'mcu' if name == 'mcu' else 'mcu ' + name
        mcu = self.printer.lookup_object(objname, None)
        if mcu is None:
            raise gcmd.error("Unknown MCU '%s'" % (name,))
        ok, msg = mcu.attempt_reconnect()
        logging.info("failure_recovery: RECONNECT_MCU MCU=%s -> %s (%s)",
                     name, "ok" if ok else "failed", msg)
        gcmd.respond_info(msg)

    # ---- Motion resume reconciliation (RFC 0001 doc 08) ----
    # The intention queue went down; the execution log comes up; resume
    # is the host reconciling the two.  Per opted-in trajectory stepper:
    # drain the board's execlog, read its authoritative held
    # accumulator, diff intentions-sent against executions-logged,
    # rebase every joint at its held position, restore heater ownership,
    # and resume the print from the reconciled position.
    def cmd_RESUME_MOTION(self, gcmd):
        self._resume_motion(gcmd)

    def _board_state(self, ts):
        # 'alive'  - board never rebooted; its held accumulator is
        #            authoritative (the replugged-toolhead case).
        # 'reset'  - board rebooted / still unreachable / shut down; its
        #            volatile accumulators are gone or untrusted.
        mcu = ts.mcu
        if mcu.is_fileoutput():
            return 'alive'
        if mcu.get_name() in self.link_paused_mcus:
            return 'reset'
        try:
            if mcu.is_shutdown():
                return 'reset'
        except Exception:
            pass
        return 'alive'

    def _reconcile_oid(self, ts, records, held_su):
        # Diff the persisted intention twin against what the board
        # logged.  The held accumulator is authoritative; the log tells
        # the story (last completed segment vs. an underrun/trigger that
        # truncated the stream, and at what clock).
        last = None
        truncated = False
        for rec in records:
            seq, rtype, src, clock, pos, aux = rec
            if src != ts.oid:
                continue
            if rtype in EL_STOP_TYPES:
                last = (rtype, clock, pos)
            if rtype in EL_TRUNCATING:
                truncated = True
        intended = ts.last_intention()
        intended_pos = intended[2] if intended else None
        gap = (intended_pos - held_su) if intended_pos is not None else None
        return {'last': last, 'truncated': truncated,
                'intended_pos': intended_pos, 'held_pos': held_su,
                'gap': gap}

    def _resume_motion(self, gcmd=None):
        def info(msg):
            logging.info("failure_recovery: %s", msg)
            if gcmd is not None:
                gcmd.respond_info(msg)
        tq = self.printer.lookup_object('trajectory_queuing', None)
        steppers = tq.get_trajectory_steppers() if tq is not None else []
        # (a) Drain each board's execution log once (reliable Class-1
        # pull); recovery never depends on droppable live records.
        drained = {}
        for el in self.execlogs:
            try:
                drained[el.mcu.get_name()] = el.drain()
            except Exception:
                logging.exception("failure_recovery: execlog drain failed"
                                  " during resume")
                drained[el.mcu.get_name()] = []
        reconciled = []
        reset = []
        blocking = False
        for ts in steppers:
            state = self._board_state(ts)
            recs = drained.get(ts.mcu.get_name(), [])
            if state == 'alive':
                held = None
                try:
                    held = ts.read_held()
                except Exception:
                    logging.exception("failure_recovery: held-position"
                                      " readback failed on %s", ts.name)
                if held is not None:
                    clock, pos_su = held
                    story = self._reconcile_oid(ts, recs, pos_su)
                    # (c) rebase this joint at its held position
                    ts.resume_reconcile(clock, pos_su)
                    reconciled.append((ts.name, int(pos_su), story))
                    continue
                # Unreadable board: fall through to reset handling.
                state = 'reset'
            # (per-joint recovery after a board RESET), HELIX simplified
            # model (RFC 0001 doc 08): assume the joint is still at the
            # last coordinates it was commanded to, with the homing it
            # had, and continue -- unless that homing was truly lost, in
            # which case the axis must be re-homed first.
            retained = ts.homing_retained()
            entry = {'joint': ts.name, 'homing_retained': retained,
                     'relative': ts.is_relative,
                     'last_intention': ts.last_intention(), 'action': ''}
            if retained:
                # Re-anchor at the host's current commanded position on
                # the next motion (the last coordinates the joint was in).
                ts.note_resume_reanchor()
                if ts.is_relative:
                    entry['action'] = ("relative axis - re-prime and"
                                       " continue at last position"
                                       " (auto-resumable)")
                else:
                    entry['action'] = ("homing retained - resume at last"
                                       " known coordinates and continue")
            else:
                blocking = True
                entry['action'] = ("homing lost (motion_homing_volatile) -"
                                   " re-home this axis, then RESUME_MOTION")
            reset.append(entry)
        self.last_recovery = {'reconciled': reconciled, 'reset': reset,
                              'blocked': blocking}
        # (d) restore heater ownership (release holds back to the host)
        for name, hold in sorted(self.holds.items()):
            try:
                hold.release()
                info("released heater hold on %s (host control restored)"
                     % (name,))
            except Exception:
                logging.exception("failure_recovery: releasing hold on %s"
                                  " failed", name)
        # Report
        for jname, pos_su, story in reconciled:
            note = ""
            if story['truncated']:
                note = " (stream truncated by underrun/trigger)"
            if story['gap']:
                note += " [%d su short of intended]" % (story['gap'],)
            info("joint %s: rebased at held position %d su%s"
                 % (jname, pos_su, note))
        for e in reset:
            li = e['last_intention']
            info("joint %s RESET (homing %s): %s%s"
                 % (e['joint'],
                    "retained" if e['homing_retained'] else "LOST",
                    e['action'],
                    (" last intended pos=%d su" % (li[2],)) if li else ""))
        if reconciled:
            # Doc 08 print-quality honesty - log it, do not try to solve
            # blemish-free resume in v1.
            info("v1 resume is mechanically exact but not cosmetically"
                 " invisible - a blemish is likely (RFC 0001 doc 08)")
        if blocking:
            info("resume BLOCKED: joint(s) need re-qualification or operator"
                 " judgment; the print was NOT resumed")
            return
        # (e) resume the print from the reconciled position
        self._do_resume(info)

    def _do_resume(self, info):
        pause_resume = self.printer.lookup_object('pause_resume', None)
        if pause_resume is None:
            info("[pause_resume] not configured - motion reconciled but the"
                 " print was not auto-resumed")
            return
        eventtime = self.printer.get_reactor().monotonic()
        if not pause_resume.get_status(eventtime)['is_paused']:
            info("print is not paused - motion reconciled, nothing to resume")
            return
        try:
            self.printer.lookup_object('gcode').run_script("RESUME")
            info("print resumed from the reconciled position")
        except Exception:
            logging.exception("failure_recovery: error running RESUME after"
                              " motion reconciliation")

    def get_status(self, eventtime=None):
        holds = dict((name, {'engaged': h.engaged})
                     for name, h in self.holds.items())
        disposition = {}
        tq = self.printer.lookup_object('trajectory_queuing', None)
        if tq is not None:
            disposition = dict(
                (ts.name, 'retained' if ts.homing_retained() else 'volatile')
                for ts in tq.get_trajectory_steppers())
        return {'paused_link_mcus': sorted(self.link_paused_mcus),
                'heater_holds': holds,
                'recovery_disposition': disposition,
                'last_recovery': self.last_recovery}

    def cmd_STATUS(self, gcmd):
        parts = []
        for name, hold in sorted(self.holds.items()):
            parts.append("%s: policy=hold max_temp=%.0f max_duration=%.0fs"
                         " engaged=%d"
                         % (name, hold.hold_max_temp,
                            hold.hold_max_duration, hold.engaged))
        if not parts:
            parts.append("no heaters configured with failure_policy: hold")
        if self.link_paused_mcus:
            parts.append("paused-link mcus: %s (run RECONNECT_MCU MCU=<name>"
                         " once reconnected, then RESUME_MOTION)"
                         % (", ".join(sorted(self.link_paused_mcus)),))
        tq = self.printer.lookup_object('trajectory_queuing', None)
        if tq is not None:
            for ts in tq.get_trajectory_steppers():
                if ts.is_relative:
                    disp = "relative (auto re-prime)"
                elif ts.homing_retained():
                    disp = "homing retained (auto resume)"
                else:
                    disp = "homing volatile (re-home required)"
                parts.append("joint %s: reset-recovery = %s"
                             % (ts.name, disp))
        if self.last_recovery is not None:
            lr = self.last_recovery
            parts.append("last RESUME_MOTION: %d joint(s) rebased, %d reset,"
                         " %s" % (len(lr['reconciled']), len(lr['reset']),
                                  "BLOCKED" if lr['blocked'] else "resumed"))
            for e in lr['reset']:
                parts.append("  RESET %s (homing %s): %s"
                             % (e['joint'],
                                "retained" if e['homing_retained'] else "LOST",
                                e['action']))
        gcmd.respond_info("\n".join(parts))

    def cmd_ENGAGE(self, gcmd):
        heater = gcmd.get('HEATER', None)
        for name, hold in self.holds.items():
            if heater is None or name.endswith(heater):
                hold.engage()
                gcmd.respond_info("Engaged hold on %s" % (name,))

    def cmd_RELEASE(self, gcmd):
        heater = gcmd.get('HEATER', None)
        for name, hold in self.holds.items():
            if heater is None or name.endswith(heater):
                hold.release()
                gcmd.respond_info("Released hold on %s" % (name,))

    async def _drain_all_coro(self):
        # asyncio-native orchestration of the execlog drain. The MCU
        # I/O itself is reactor-only, so each board's drain hops back
        # into reactor context through the bridge's call_reactor seam -
        # exercising BOTH directions of the doc-05 handoff on a real
        # consumer (reactor -> run_coro -> here -> call_reactor ->
        # reactor -> back here -> completion on the reactor).
        out = []
        for el in self.execlogs:
            records = await self.bridge.call_reactor(
                lambda et, el=el: el.drain())
            out.append((el, records))
        return out

    def _drain_all(self):
        # [(execlog, records)] for every configured board. Uses the
        # asyncio bridge when enabled and running; any bridge failure
        # falls back to the direct reactor drain so the working extra
        # never regresses.
        if (self.asyncio_drain and self.bridge is not None
                and self.bridge.running):
            try:
                return self.bridge.run_coro_wait(self._drain_all_coro())
            except Exception:
                logging.exception("failure_recovery: asyncio drain failed;"
                                  " falling back to direct reactor drain")
        return [(el, el.drain()) for el in self.execlogs]

    def cmd_EXECLOG_DUMP(self, gcmd):
        total = 0
        for el, records in self._drain_all():
            total += len(records)
            for r in records:
                logging.info("execlog: seq=%d type=%d src=%d clock=%d"
                             " pos=%d aux=%d", *r)
        gcmd.respond_info("Drained %d execution log records (see log)"
                          % (total,))


def load_config(config):
    return FailureRecovery(config)
