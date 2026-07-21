# HELIX capability introspection (FD-0001).
#
# One console command, HELIX_STATUS, that answers "what does this machine
# actually run?" - which of the fork's motion/communication capabilities
# each micro-controller's firmware advertises, and which host-side
# subsystems are loaded. It reads the served data dictionary (commands +
# constants) rather than guessing, so it reflects exactly what was built
# for each board.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import os
import sys

try:
    from importlib import util as importlib_util
except ImportError:
    importlib_util = None

# (label, a representative command format the firmware registers when the
# feature is built). Presence is checked against the served dictionary.
# Each format string MUST match the firmware's DECL_COMMAND verbatim
# (param types included) or check_valid_response never matches.
MCU_FEATURES = [
    ('trajectory motion', "config_traj_stepper oid=%c step_pin=%c dir_pin=%c"
     " invert_step=%c invert_dir=%c step_pulse_ticks=%u underrun_decel=%u"),
    ('  cubic/quintic segments',
     "queue_traj_segment_cubic oid=%c flags=%c duration=%u velocity=%i"
     " accel=%i jerk=%i"),
    ('  PWM/DAC sampled backend',
     "config_traj_pwm oid=%c pin=%u cycle_ticks=%u sample_ticks=%u scale=%u"
     " shutdown_value=%hu max_value=%hu underrun_decel=%u"),
    ('hardware trigger sources',
     "config_trigger_gpio oid=%c pin=%u edge=%c pull_up=%c"
     " qualify_ticks=%u qualify_count=%c"),
    ('heater failsafe hold',
     "config_heater_hold oid=%c heater_pin=%u sensor_pin=%u invert_sense=%c"),
    ('execution log', "config_execlog oid=%c size=%hu"),
]


def _load_fleet_modules():
    if importlib_util is None:
        raise RuntimeError("fleet contract requires Python 3")
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    loaded = []
    for name, relpath in (
            ("_helix_fleet_abi", ("atlas", "fleet", "abi.py")),
            ("_helix_fleet_coherence",
             ("atlas", "fleet", "coherence.py"))):
        path = os.path.join(root, *relpath)
        spec = importlib_util.spec_from_file_location(name, path)
        module = importlib_util.module_from_spec(spec)
        # dataclasses resolves annotations through sys.modules while loading.
        sys.modules[name] = module
        spec.loader.exec_module(module)
        loaded.append(module)
    return loaded


class HelixStatus:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.host_hash = None
        self.host_abi = 0x00010000
        self._coherence = None
        try:
            abi, self._coherence = _load_fleet_modules()
            self.host_hash = abi.host_protocol_hash()
        except Exception:
            logging.exception("HELIX_STATUS could not load fleet contract")
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command('HELIX_STATUS', self.cmd_HELIX_STATUS,
                               desc=self.cmd_HELIX_STATUS_help)

    def _mcu_line(self, name, mcu):
        feats = []
        for label, fmt in MCU_FEATURES:
            try:
                ok = mcu.check_valid_response(fmt)
            except Exception:
                ok = False
            if ok:
                feats.append(label.strip())
        # Framing v2 (BCH FEC) advertises itself as a dictionary constant.
        try:
            consts = mcu.get_constants()
        except Exception:
            consts = {}
        if consts.get('FRAMING_V2'):
            feats.append('framing v2 (FEC)')
        abi = consts.get('BOARD_SYSCALL_ABI')
        if abi:
            caps = consts.get('BOARD_SYSCALL_CAPS', 0)
            feats.append('unified syscall API v%d.%d (caps 0x%02x)'
                         % (abi >> 16, abi & 0xffff, caps))
        if not feats:
            feats.append('stock command set - no HELIX firmware features '
                         'detected')
        board_hash = consts.get('PROTOCOL_ABI_HASH', '')
        if board_hash:
            feats.append('protocol ABI %s' % board_hash)
        if self.host_hash is not None and self._coherence is not None:
            state = self._coherence.BoardState(
                name=name, protocol_hash=board_hash,
                syscall_abi=abi or 0,
                framing_v2=bool(consts.get('FRAMING_V2')))
            report = self._coherence.check_board(
                self.host_hash, self.host_abi, state)
            suffix = ("; signed flash required" if
                      report.requires_signed_flash else "")
            feats.append('fleet %s (action=%s%s)'
                         % (report.status, report.action, suffix))
        return feats

    cmd_HELIX_STATUS_help = ("Report which HELIX capabilities each MCU and the"
                             " host are running")
    def cmd_HELIX_STATUS(self, gcmd):
        lines = ["HELIX capability report", "======================="]
        # Per-MCU firmware capabilities, read from the served dictionary.
        for name, mcu in self.printer.lookup_objects(module='mcu'):
            short = mcu.get_name()
            feats = self._mcu_line(name, mcu)
            lines.append("MCU '%s':" % (short,))
            if feats:
                for f in feats:
                    lines.append("    + %s" % (f,))
            else:
                lines.append("    (stock command set - no HELIX firmware"
                             " features detected)")
        # v2 transport bridges, if any.
        bridges = self.printer.lookup_objects(module='intentproto_transport')
        for name, obj in bridges:
            st = obj.get_status(None)
            extra = ""
            if st.get('mode') == 'datagram':
                extra = (" lost=%d reordered=%d auth_fail=%d"
                         % (st.get('rx_lost', 0), st.get('rx_reordered', 0),
                            st.get('auth_failures', 0)))
            elif st.get('mode') == 'bch':
                extra = (" tx=%d rx=%d uncorrectable=%d"
                         % (st.get('tx_frames', 0), st.get('rx_frames', 0),
                            st.get('rx_uncorrectable', 0)))
            lines.append("Transport '%s': %s, %s%s"
                         % (name.split()[-1], st.get('mode'),
                            "v2 active" if st.get('v2_active')
                            else "v1 pass-through", extra))
        # Host-side subsystems that are loaded.
        lines.append("Host subsystems:")
        host_objs = [
            ('trajectory motion emitter', 'trajectory_queuing'),
            ('failure recovery / pause-and-hold', 'failure_recovery'),
            ('machine-time discipline', 'timesync'),
        ]
        any_host = False
        for label, objname in host_objs:
            if self.printer.lookup_object(objname, None) is not None:
                lines.append("    + %s" % (label,))
                any_host = True
        if not any_host:
            lines.append("    (none configured)")
        # Trajectory joints, if any.
        tq = self.printer.lookup_object('trajectory_queuing', None)
        if tq is not None:
            steppers = tq.get_trajectory_steppers()
            if steppers:
                names = ", ".join(ts.name for ts in steppers)
                lines.append("Trajectory joints: %s" % (names,))
        gcmd.respond_info("\n".join(lines))


def load_config(config):
    return HelixStatus(config)
