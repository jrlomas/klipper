# Trajectory intention emitter: per-actuator opt-in motion protocol
#
# Owns steppers configured with 'motion_protocol: trajectory'
# (RFC 0001): configures the MCU-side segment executor, anchors the
# chained position stream with trajectory_rebase, runs the C segment
# fitter over each flush window, and ships queue_traj_segment
# commands. The legacy queue_step path is untouched for every other
# stepper — the two coexist per actuator on the same MCU.
#
# v1 limitations (documented in RFC 0001 doc 06): homing/probing an
# opted-in stepper is not yet supported — home before opting in, or
# keep homed axes on the legacy protocol.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import chelper

SUBUNITS = 65536.
# Default deviation tolerance: max(half a microstep, ~5um) is decided
# host-side in sub-units; see RFC 0001 doc 02.
DEFAULT_TOLERANCE_SU = SUBUNITS / 2.
DEFAULT_SAMPLE_TIME = 0.001
DEFAULT_UNDERRUN_DECEL = 5000.  # mm/s^2


class TrajectoryStepper:
    def __init__(self, tq_owner, mcu_stepper, config):
        self.owner = tq_owner
        self.mcu_stepper = mcu_stepper
        self.mcu = mcu_stepper.get_mcu()
        self.oid = mcu_stepper.get_oid()
        self.name = mcu_stepper.get_name()
        self.underrun_decel = config.getfloat(
            'motion_underrun_decel', DEFAULT_UNDERRUN_DECEL, above=0.)
        self.tolerance_su = config.getfloat(
            'motion_tolerance', DEFAULT_TOLERANCE_SU, above=0.)
        self.sample_time = config.getfloat(
            'motion_sample_time', DEFAULT_SAMPLE_TIME, above=0.)
        ffi_main, self.ffi_lib = chelper.get_ffi()
        self.segfit = ffi_main.gc(self.ffi_lib.segfit_alloc(),
                                  self.ffi_lib.segfit_free)
        self.queue_cmd = self.hold_cmd = self.rebase_cmd = None
        self.anchored = False
        self.need_rebase = True
        self.su_per_mm = 1.

    # Called from MCU_stepper._build_config
    def build_config(self, step_pin, dir_pin, invert_step, invert_dir,
                     step_pulse_ticks):
        step_dist = self.mcu_stepper.get_step_dist()
        self.su_per_mm = SUBUNITS / step_dist
        freq = self.mcu.seconds_to_clock(1.)
        # underrun_decel wire units: sub-units/tick^2 with 32
        # fractional bits
        decel_wire = int(self.underrun_decel * self.su_per_mm
                         / (freq * freq) * 2.**32 + .5)
        self.mcu.add_config_cmd(
            "config_traj_stepper oid=%d step_pin=%s dir_pin=%s"
            " invert_step=%d invert_dir=%d step_pulse_ticks=%u"
            " underrun_decel=%u"
            % (self.oid, step_pin, dir_pin, invert_step, invert_dir,
               step_pulse_ticks, decel_wire))
        cmd_queue = self.mcu.alloc_command_queue()
        self.queue_cmd = self.mcu.lookup_command(
            "queue_traj_segment oid=%c flags=%c duration=%u"
            " velocity=%i accel=%i", cq=cmd_queue)
        self.hold_cmd = self.mcu.lookup_command(
            "traj_hold oid=%c duration=%u", cq=cmd_queue)
        self.rebase_cmd = self.mcu.lookup_command(
            "trajectory_rebase oid=%c clock=%u pos=%i", cq=cmd_queue)

    def connect(self):
        sk = self.mcu_stepper.get_stepper_kinematics()
        freq = self.mcu.seconds_to_clock(1.)
        self.ffi_lib.segfit_setup(self.segfit, sk, freq, self.su_per_mm,
                                  self.tolerance_su, self.sample_time)

    def note_rebase_needed(self):
        self.anchored = False
        self.need_rebase = True

    def _anchor(self, print_time):
        sk = self.mcu_stepper.get_stepper_kinematics()
        pos_mm = self.ffi_lib.itersolve_get_commanded_pos(sk)
        pos_su = int(round(pos_mm * self.su_per_mm))
        acc = pos_su << 32
        clock = self.mcu.print_time_to_clock(print_time)
        self.rebase_cmd.send([self.oid, clock & 0xffffffff, pos_su])
        self.ffi_lib.segfit_set_anchor(self.segfit, print_time, acc)
        self.anchored = True
        self.need_rebase = False

    def flush(self, flush_time, step_gen_time):
        sk = self.mcu_stepper.get_stepper_kinematics()
        if sk is None:
            return
        active_time = self.ffi_lib.itersolve_check_active(sk, flush_time)
        if not self.anchored:
            if not active_time:
                return
            # Anchor slightly before the first activity in the window
            anchor_time = max(active_time - 0.001, 0.)
            self._anchor(anchor_time)
        n = self.ffi_lib.segfit_generate(self.segfit, flush_time)
        if n < 0:
            logging.warning("segfit overflow on %s", self.name)
            n = 0
        self._send_segs(n)
        if not active_time:
            # Motion has ended: flush the partial span so the joint
            # lands exactly on target, then drop the anchor (the next
            # motion re-anchors with a fresh rebase).
            n = self.ffi_lib.segfit_finalize(self.segfit)
            if n > 0:
                self._send_segs(n)
            self.anchored = False

    def _send_segs(self, n):
        segs = self.ffi_lib.segfit_get_segs(self.segfit)
        for i in range(n):
            s = segs[i]
            if not s.duration:
                continue
            if not s.velocity and not s.accel:
                self.hold_cmd.send([self.oid, s.duration])
            else:
                self.queue_cmd.send([self.oid, s.flags, s.duration,
                                     s.velocity, s.accel])


class TrajectoryQueuing:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.steppers = []
        self.printer.register_event_handler("klippy:connect",
                                            self._handle_connect)

    def register_stepper(self, mcu_stepper, config):
        ts = TrajectoryStepper(self, mcu_stepper, config)
        self.steppers.append(ts)
        mcu = mcu_stepper.get_mcu()
        mcu.register_response(self._handle_underrun, "traj_underrun",
                              mcu_stepper.get_oid())
        return ts

    def _handle_connect(self):
        for ts in self.steppers:
            ts.connect()
        if self.steppers:
            mq = self.printer.lookup_object('motion_queuing')
            mq.register_flush_callback(self._flush)

    def _flush(self, flush_time, step_gen_time):
        for ts in self.steppers:
            ts.flush(flush_time, step_gen_time)

    def _handle_underrun(self, params):
        oid = params['oid']
        for ts in self.steppers:
            if ts.oid == oid:
                ts.note_rebase_needed()
                logging.warning(
                    "Trajectory underrun on %s: clock=%d pos=%d",
                    ts.name, params['clock'], params['pos'])
                break


def load_config(config):
    return TrajectoryQueuing(config)
