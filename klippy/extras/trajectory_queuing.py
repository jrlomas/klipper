# Trajectory intention emitter: per-actuator opt-in motion protocol
#
# Owns steppers configured with 'motion_protocol: trajectory'
# (RFC 0001): configures the MCU-side segment executor, anchors the
# chained position stream with trajectory_rebase, runs the C segment
# fitter over each flush window, and ships queue_traj_segment
# commands. The legacy queue_step path is untouched for every other
# stepper — the two coexist per actuator on the same MCU.
#
# Homing/probing (RFC 0001 doc 02 "Homing, probing, and trsync"):
# MCU_trsync arms traj_stop_on_trigger (instead of
# stepper_stop_on_trigger) for opted-in steppers - see
# MCU_stepper.get_stop_on_trigger_command_name().  During the homing
# drip the emitter keeps flushing via the normal motion_queuing flush
# callback (drip_update_time calls _advance_flush_time, which invokes
# it); segfit generates strictly forward from its anchor, so
# overlapping flush calls cannot double-emit.  On trigger the MCU
# halts with NEED_REBASE and preserves the position accumulator;
# MCU_stepper.note_homing_end then calls note_rebase_needed (the old
# anchor is invalid) and re-reads the held accumulator, which is the
# sub-unit exact trigger position.  Segments emitted between trigger
# and rebase are silently dropped by the MCU (dropped counter).
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging, collections, math
import chelper

SUBUNITS = 65536.

# Segment polynomial-order flag bits (mirror TSEG_POLY_* in src/trajq.h).
TSEG_POLY_CUBIC = 1 << 6
TSEG_POLY_QUINTIC = 2 << 6

# ---- Higher-order (cubic / quintic) Bezier segments (RFC 0001 doc 02) ----
#
# Fixed-point scaling (extends v=Q16.16, a=Q0.32): each higher derivative
# carries 16 more fractional bits - jerk *2^48, snap *2^64, crackle *2^80.
# See the range-analysis block in src/trajq.c. These pure-Python integer
# routines mirror smul_shr / poly_term / trajq_end_delta_seg in
# src/trajq.c (and segfit.c) BIT FOR BIT, so the host reference endpoint
# equals the MCU's exact chained accumulator.
HO_COEFF_SHIFT = {'v': 16, 'a': 32, 'j': 48, 's': 64, 'c': 80}


def traj_round(x):
    # Round half away from zero, matching C round() used by the fitter.
    return int(math.floor(x + 0.5)) if x >= 0 else -int(math.floor(-x + 0.5))


def _clamp_i32(x):
    if x > 2147483647:
        return 2147483647
    if x < -2147483648:
        return -2147483648
    return int(x)


def _smul_shr(a, t, sh):
    # trunc_toward_zero(a * t) >> sh
    neg = a < 0
    r = (abs(a) * t) >> sh
    return -r if neg else r


def _poly_term(coeff, t, nmul, nsh, fact):
    p = coeff
    for i in range(nmul):
        p = _smul_shr(p, t, 16 if i < nsh else 0)
    if fact > 1:
        neg = p < 0
        p = abs(p) // fact
        p = -p if neg else p
    return p


def _mul64x32_half(a, b):
    neg = a < 0
    r = (abs(a) * b) >> 1
    return -r if neg else r


def py_end_delta_ho(duration, velocity, accel, jerk=0, snap=0, crackle=0):
    # Exact Q32.32 end-of-segment delta, integer-identical to the C
    # trajq_end_delta_seg()/segfit_end_delta_ho().
    delta = (velocity * duration) << 16
    if accel:
        delta += _mul64x32_half(accel * duration, duration)
    delta += _poly_term(jerk, duration, 3, 1, 6)
    delta += _poly_term(snap, duration, 4, 2, 24)
    delta += _poly_term(crackle, duration, 5, 3, 120)
    return delta


def bezier_power_derivatives(ctrl):
    # Given Bezier control points ctrl[0..n] (position, any linear unit)
    # over parameter u in [0,1], return the true time-normalized
    # derivatives-at-start [d1, d2, ... dn] where dk = q^(k)(0) for the
    # curve reparameterized so u = t and duration = 1 (callers divide by
    # D^k). dk = k! * b_k, with b_k the power-basis coefficient of u^k.
    n = len(ctrl) - 1
    derivs = []
    for k in range(1, n + 1):
        # b_k = C(n,k) * sum_{i=0}^{k} (-1)^(k-i) C(k,i) P_i
        bk = 0.
        for i in range(k + 1):
            bk += ((-1) ** (k - i)) * math.comb(k, i) * ctrl[i]
        bk *= math.comb(n, k)
        derivs.append(math.factorial(k) * bk)  # dk = k! * b_k
    return derivs


def bezier_to_wire(ctrl_su, duration):
    # Convert Bezier control points (in sub-units) spanning 'duration'
    # ticks to quantized wire coefficients. 4 control points -> cubic
    # (v,a,j); 6 -> quintic (v,a,j,s,c). Returns (order_flag, coeffs)
    # with coeffs a dict of wire ints. Quantization matches the fitter's
    # round-half-away and the 2^(16k) scalings.
    n = len(ctrl_su) - 1
    if n == 3:
        order = TSEG_POLY_CUBIC
        keys = ['v', 'a', 'j']
    elif n == 5:
        order = TSEG_POLY_QUINTIC
        keys = ['v', 'a', 'j', 's', 'c']
    else:
        raise ValueError("Bezier segment needs 4 (cubic) or 6 (quintic)"
                         " control points, got %d" % (len(ctrl_su),))
    derivs = bezier_power_derivatives(ctrl_su)  # d1..dn (u-domain)
    coeffs = {}
    D = float(duration)
    for k, key in enumerate(keys, start=1):
        # true per-tick derivative = dk / D^k ; wire = round(* 2^(16k))
        true_k = derivs[k - 1] / (D ** k)
        coeffs[key] = _clamp_i32(traj_round(true_k * (2. ** (16 * k))))
    return order, coeffs
# Default deviation tolerance: max(half a microstep, ~5um) is decided
# host-side in sub-units; see RFC 0001 doc 02.
DEFAULT_TOLERANCE_SU = SUBUNITS / 2.
DEFAULT_SAMPLE_TIME = 0.001
DEFAULT_UNDERRUN_DECEL = 5000.  # mm/s^2
# Rolling intention record depth (the host twin of the MCU execlog
# window - RFC 0001 doc 08); sized like the execlog ring by default.
DEFAULT_INTENTION_RECORD = 256


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
        self.cubic_cmd = self.quintic_cmd = None
        self.anchored = False
        self.need_rebase = True
        self.su_per_mm = 1.
        # Rolling record of intentions SENT: the host twin the resume
        # reconciler (RFC 0001 doc 08) diffs against what the board
        # actually executed.  Each entry is
        # (start_clock, end_clock, end_pos_subunits) taken from the
        # exact chained anchor the fitter maintains (segfit_get_anchor
        # is the Q32.32 accumulator).  Bounded like the execlog ring.
        record_size = config.getint('motion_intention_record',
                                    DEFAULT_INTENTION_RECORD, minval=16)
        self.intentions = collections.deque(maxlen=record_size)
        # Per-joint recovery disposition after a board RESET (RFC 0001
        # doc 08, HELIX simplified model).  HELIX uses no encoders and no
        # closed-loop position feedback: a resume assumes the joint is
        # still at the last coordinates it was commanded to, with the
        # homing reference it had, and continues.  The only case that
        # cannot recover automatically is one where that homing was
        # *truly lost* and must be re-established by re-homing.  So the
        # per-joint knob is binary -- does this joint's homing survive a
        # board reset?
        #   * Relative axes (extruders) always do: re-prime and continue.
        #   * Absolute axes are assumed to retain their homing by default;
        #     set 'motion_homing_volatile: True' for a joint whose
        #     reference genuinely cannot be trusted across a reset, which
        #     forces a re-home before the print resumes.
        self.is_relative = self.name.startswith('extruder')
        self.homing_volatile = config.getboolean('motion_homing_volatile',
                                                 False)
        # Back-compat: the retired three-way 'motion_recovery_class'
        # collapses onto this model.  'extruder' was relative (already
        # detected by name); 'reference'/'none' both blocked the resume
        # pending re-homing == volatile homing.
        legacy = config.get('motion_recovery_class', None)
        if legacy is not None:
            if legacy not in ('extruder', 'reference', 'none'):
                raise config.error(
                    "motion_recovery_class in '%s' must be extruder,"
                    " reference or none (deprecated: prefer"
                    " motion_homing_volatile)" % (config.get_name(),))
            logging.warning(
                "trajectory_queuing: 'motion_recovery_class' is deprecated;"
                " mapping '%s' onto the homing-retained model for '%s'",
                legacy, config.get_name())
            if legacy in ('reference', 'none'):
                self.homing_volatile = True

    def homing_retained(self):
        # A joint recovers automatically when its homing survives the
        # reset: always for a relative axis, and for an absolute axis
        # unless it was declared volatile.
        return self.is_relative or not self.homing_volatile

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
        # Higher-order commands exist only if the firmware was built with
        # CONFIG_WANT_TRAJECTORY_HIGHER_ORDER; look them up optionally.
        self.cubic_cmd = self.mcu.try_lookup_command(
            "queue_traj_segment_cubic oid=%c flags=%c duration=%u"
            " velocity=%i accel=%i jerk=%i")
        self.quintic_cmd = self.mcu.try_lookup_command(
            "queue_traj_segment_quintic oid=%c flags=%c duration=%u"
            " velocity=%i accel=%i jerk=%i snap=%i crackle=%i")

    def connect(self):
        sk = self.mcu_stepper.get_stepper_kinematics()
        freq = self.mcu.seconds_to_clock(1.)
        self.ffi_lib.segfit_setup(self.segfit, sk, freq, self.su_per_mm,
                                  self.tolerance_su, self.sample_time)

    def note_rebase_needed(self):
        self.anchored = False
        self.need_rebase = True

    def commanded_pos_su(self):
        # Current commanded joint position in sub-units, from the host
        # kinematics (the ground truth an idle re-anchor uses).
        sk = self.mcu_stepper.get_stepper_kinematics()
        pos_mm = self.ffi_lib.itersolve_get_commanded_pos(sk)
        return int(round(pos_mm * self.su_per_mm))

    def describe(self):
        # Console-facing snapshot of this joint's trajectory state.
        li = self.last_intention()
        return {
            'name': self.name,
            'oid': self.oid,
            'anchored': bool(self.anchored),
            'need_rebase': bool(self.need_rebase),
            'su_per_mm': self.su_per_mm,
            'commanded_pos_su': self.commanded_pos_su(),
            'last_intention_pos_su': (li[2] if li else None),
            'higher_order': self.cubic_cmd is not None,
            'homing_volatile': bool(self.homing_volatile),
        }

    def bezier_move(self, duration_s, ctrl_su):
        # Advanced/commissioning primitive: drive THIS joint alone along a
        # cubic (4 control points) or quintic (6) Bezier, bypassing the
        # kinematic planner (like FORCE_MOVE).  Requires the caller to have
        # flushed and be holding a print_time; anchors at ctrl_su[0], emits
        # the segment, and syncs the stepper's position to the exact end.
        # The toolhead kinematic position is intentionally NOT updated - the
        # caller must correct it (SET_KINEMATIC_POSITION) afterward, exactly
        # as FORCE_MOVE requires.  Returns the end position in sub-units.
        if self.cubic_cmd is None:
            raise self.mcu.error(
                "Firmware for %s lacks higher-order trajectory support"
                % (self.name,))
        toolhead = self.owner.printer.lookup_object('toolhead')
        toolhead.flush_step_generation()
        print_time = toolhead.get_last_move_time()
        # Anchor a hair in the future so the rebase clock is not in the past.
        anchor_time = print_time + 0.100
        clock = self.mcu.print_time_to_clock(anchor_time)
        duration = int(round(duration_s * self.mcu.seconds_to_clock(1.)))
        if duration <= 0:
            raise self.mcu.error("BEZIER_MOVE duration must be positive")
        anchor_su = int(ctrl_su[0])
        # Rebase this joint at the first control point, then emit.
        self.note_rebase_needed()
        self.rebase_cmd.send([self.oid, clock & 0xffffffff, anchor_su])
        self.ffi_lib.segfit_set_anchor(self.segfit, anchor_time,
                                       anchor_su << 32)
        self.anchored = False   # standalone emit; not fitter-driven
        end_delta = self.queue_bezier_segment(duration, ctrl_su)
        end_su = anchor_su + int(end_delta >> 32)
        toolhead.dwell(duration_s + 0.150)
        toolhead.flush_step_generation()
        # Keep klippy's stepper bookkeeping consistent with the hardware.
        self.mcu_stepper.sync_to_held_position(end_su)
        self.note_rebase_needed()
        return end_su

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
        # Record the (re-)anchor point in the host intention twin.
        self.intentions.append((int(clock), int(clock), int(pos_su)))

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
        prev_acc = self.ffi_lib.segfit_get_anchor(self.segfit)
        prev_time = self.ffi_lib.segfit_get_gen_time(self.segfit)
        n = self.ffi_lib.segfit_generate(self.segfit, flush_time)
        if n < 0:
            logging.warning("segfit overflow on %s", self.name)
            n = 0
        self._send_segs(n)
        self._record_intention(prev_acc, prev_time)
        if not active_time:
            # Motion has ended: flush the partial span so the joint
            # lands exactly on target, then drop the anchor (the next
            # motion re-anchors with a fresh rebase).
            prev_acc = self.ffi_lib.segfit_get_anchor(self.segfit)
            prev_time = self.ffi_lib.segfit_get_gen_time(self.segfit)
            n = self.ffi_lib.segfit_finalize(self.segfit)
            if n > 0:
                self._send_segs(n)
            self._record_intention(prev_acc, prev_time)
            self.anchored = False

    def _record_intention(self, prev_acc, prev_time):
        # Append (start_clock, end_clock, end_pos_subunits) for the span
        # just emitted, straight off the fitter's exact chained anchor.
        acc = self.ffi_lib.segfit_get_anchor(self.segfit)
        if acc == prev_acc:
            return  # nothing emitted / anchor unchanged
        end_time = self.ffi_lib.segfit_get_gen_time(self.segfit)
        try:
            start_clock = int(self.mcu.print_time_to_clock(prev_time))
            end_clock = int(self.mcu.print_time_to_clock(end_time))
        except Exception:
            return
        self.intentions.append((start_clock, end_clock, int(acc >> 32)))

    # ---- resume reconciliation (RFC 0001 doc 08) ----
    def get_intention_record(self):
        return list(self.intentions)

    def last_intention(self):
        # (start_clock, end_clock, end_pos_subunits) of the last span the
        # host emitted, or None.  This is what the host INTENDED - the
        # persisted twin used to report a reset joint whose board no
        # longer holds an authoritative accumulator.
        return self.intentions[-1] if self.intentions else None

    def read_held(self):
        # Authoritative board-held accumulator (sub-unit exact) when the
        # board never rebooted.  Returns (clock64, pos_subunits) or None.
        return self.mcu_stepper.read_traj_held_subunits()

    def resume_reconcile(self, clock, pos_su):
        # Re-anchor the board's segment executor at its authoritative
        # held accumulator and bring the host fitter + mcu-position
        # offset back into agreement, so the firmware is in a valid
        # anchored state and the next flush generates from ground truth
        # rather than teleporting to a stale commanded position.
        if self.rebase_cmd is None:
            return
        clock = int(clock)
        pos_su = int(pos_su)
        self.rebase_cmd.send([self.oid, clock & 0xffffffff, pos_su])
        try:
            print_time = self.mcu.clock_to_print_time(clock)
            self.ffi_lib.segfit_set_anchor(self.segfit, print_time,
                                           pos_su << 32)
            self.anchored = True
            self.need_rebase = False
        except Exception:
            # Fall back to a lazy re-anchor on the next motion.
            self.note_rebase_needed()
        self.mcu_stepper.sync_to_held_position(pos_su)
        self.intentions.append((clock, clock, pos_su))

    def note_resume_reanchor(self):
        # Board RESET, homing retained: the board's volatile accumulator
        # is gone, but the host still knows where this joint was (its
        # last commanded position) and trusts the homing it had.  Re-anchor
        # at the host's current commanded position on the next motion and
        # continue.  Same mechanism for a relative axis (extruder re-prime)
        # and an absolute axis whose homing survived the reset.
        self.note_rebase_needed()

    # Retained name for callers/tests predating the homing-retained model.
    note_reprime = note_resume_reanchor

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

    def queue_bezier_segment(self, duration, ctrl_su):
        # Emit one cubic (4 control points) or quintic (6 control points)
        # Bezier segment. ctrl_su are positions in sub-units spanning
        # 'duration' ticks; the chained encoding means ctrl_su[0] must be
        # the current anchor position (only the relative shape is sent).
        # Returns the exact Q32.32 end delta (mirrors the MCU accumulator)
        # so the caller can advance its anchor without any drift.
        duration = int(duration)
        order, c = bezier_to_wire(ctrl_su, duration)
        base_flags = 0
        if order == TSEG_POLY_CUBIC:
            if self.cubic_cmd is None:
                raise self.mcu.error(
                    "Firmware for %s lacks higher-order trajectory support"
                    % (self.name,))
            self.cubic_cmd.send([self.oid, base_flags, duration,
                                 c['v'], c['a'], c['j']])
            return py_end_delta_ho(duration, c['v'], c['a'], c['j'])
        if self.quintic_cmd is None:
            raise self.mcu.error(
                "Firmware for %s lacks higher-order trajectory support"
                % (self.name,))
        self.quintic_cmd.send([self.oid, base_flags, duration,
                               c['v'], c['a'], c['j'], c['s'], c['c']])
        return py_end_delta_ho(duration, c['v'], c['a'], c['j'],
                               c['s'], c['c'])


class TrajectoryQueuing:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.steppers = []
        self.printer.register_event_handler("klippy:connect",
                                            self._handle_connect)
        # Advanced single-joint Bezier move is opt-in and hazardous (it
        # bypasses the kinematic planner and desyncs the toolhead
        # position), exactly like [force_move] enable_force_move.
        self.enable_bezier_move = config.getboolean('enable_bezier_move',
                                                    False)
        # Make the machine-wide HELIX_STATUS command available whenever the
        # trajectory subsystem is configured (it also loads standalone via
        # a [helix_status] section).
        self.printer.load_object(config, 'helix_status')
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command('TRAJECTORY_STATUS', self.cmd_TRAJECTORY_STATUS,
                               desc=self.cmd_TRAJECTORY_STATUS_help)
        if self.enable_bezier_move:
            gcode.register_command('BEZIER_MOVE', self.cmd_BEZIER_MOVE,
                                   desc=self.cmd_BEZIER_MOVE_help)

    def get_status(self, eventtime=None):
        return {'trajectory_steppers': [ts.describe() for ts in self.steppers]}

    cmd_TRAJECTORY_STATUS_help = ("Report the state of every actuator on the"
                                  " trajectory-intention motion path")
    def cmd_TRAJECTORY_STATUS(self, gcmd):
        if not self.steppers:
            gcmd.respond_info("No steppers use 'motion_protocol: trajectory'")
            return
        lines = []
        for ts in self.steppers:
            d = ts.describe()
            lines.append(
                "%s: anchored=%d need_rebase=%d higher_order=%d"
                " pos=%d su (%.4f mm) su/mm=%.1f%s"
                % (d['name'], d['anchored'], d['need_rebase'],
                   d['higher_order'], d['commanded_pos_su'],
                   d['commanded_pos_su'] / d['su_per_mm'], d['su_per_mm'],
                   " [homing volatile]" if d['homing_volatile'] else ""))
        gcmd.respond_info("\n".join(lines))

    cmd_BEZIER_MOVE_help = ("Drive one trajectory joint along a cubic/quintic"
                            " Bezier (advanced; bypasses kinematics)")
    def cmd_BEZIER_MOVE(self, gcmd):
        name = gcmd.get('STEPPER')
        ts = None
        for cand in self.steppers:
            if cand.name == name:
                ts = cand
                break
        if ts is None:
            raise gcmd.error("'%s' is not a trajectory stepper (needs"
                             " 'motion_protocol: trajectory')" % (name,))
        duration = gcmd.get_float('DURATION', above=0.)
        # Control points P0..P3 (cubic) or P0..P5 (quintic), absolute joint
        # positions in mm; P0 must be the current position (the anchor).
        pts_mm = []
        for i in range(6):
            v = gcmd.get_float('P%d' % (i,), None)
            if v is None:
                break
            pts_mm.append(v)
        if len(pts_mm) not in (4, 6):
            raise gcmd.error("BEZIER_MOVE needs P0..P3 (cubic) or P0..P5"
                             " (quintic); got %d points" % (len(pts_mm),))
        ctrl_su = [int(round(p * ts.su_per_mm)) for p in pts_mm]
        cur = ts.commanded_pos_su()
        if abs(ctrl_su[0] - cur) > 1:
            raise gcmd.error("P0 (%.4f mm) must equal the current position"
                             " (%.4f mm) - anchor the move where the joint is"
                             % (pts_mm[0], cur / ts.su_per_mm))
        ctrl_su[0] = cur
        end_su = ts.bezier_move(duration, ctrl_su)
        gcmd.respond_info(
            "BEZIER_MOVE %s: %d-point Bezier over %.3fs, ended at %.4f mm."
            " Kinematic position is now stale - run SET_KINEMATIC_POSITION."
            % (name, len(pts_mm), duration, end_su / ts.su_per_mm))

    def register_stepper(self, mcu_stepper, config):
        ts = TrajectoryStepper(self, mcu_stepper, config)
        self.steppers.append(ts)
        mcu = mcu_stepper.get_mcu()
        mcu.register_response(self._handle_underrun, "traj_underrun",
                              mcu_stepper.get_oid())
        return ts

    def get_trajectory_steppers(self):
        return list(self.steppers)

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
