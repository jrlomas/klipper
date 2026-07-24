# -*- coding: utf-8 -*-
# Trajectory intention emitter: per-actuator opt-in motion protocol
#
# Owns steppers configured with 'motion_protocol: trajectory'
# (FD-0001): configures the MCU-side segment executor, anchors the
# chained position stream with trajectory_rebase, runs the C segment
# fitter over each flush window, and ships queue_traj_segment
# commands. The legacy queue_step path is untouched for every other
# stepper — the two coexist per actuator on the same MCU.
#
# Homing/probing (FD-0001 doc 02 "Homing, probing, and trsync"):
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

import logging, collections, math, secrets
import chelper

SUBUNITS = 65536.

# A completed host path always ends in an explicit zero-velocity segment.
# Besides making the intended terminal state unambiguous, this prevents a
# sub-unit fixed-point residue in the final fitted segment from being mistaken
# for a live velocity when the MCU queue drains (which would correctly invoke
# its emergency underrun ramp).  One millisecond is long enough to be a safe
# scheduled timer interval on every supported trajectory MCU and has no
# position delta.
TERMINAL_HOLD_TIME = .001
BEZIER_QUEUE_MARGIN = .250
BEZIER_WAIT_MARGIN = .050
BEZIER_MAX_SEGMENTS = 1024
# segfit.c owns a fixed output array of this size.  A full return means the
# requested time horizon may not have been reached yet; drain another batch
# before finalizing the activity window.
SEGFIT_BATCH_MAX = 256
# The host may perform synchronous work (notably sensorless-homing TMC UART
# setup/restore) between creating a drip trapq and the trajectory callback
# that transmits its first rebase.  Reserve one motion-queue generation
# window so the scheduled rebase is still safely in the future when it
# reaches the MCU.  This also retains enough trapq history for kinematic
# pre/post scan windows used by pressure advance and input shaping.
TRAJECTORY_KIN_FLUSH_DELAY = .100

# Segment polynomial-order flag bits (mirror TSEG_POLY_* in src/trajq.h).
TSEG_POLY_CUBIC = 1 << 6
TSEG_POLY_QUINTIC = 2 << 6
TSEG_POLY_MASK = 3 << 6
TSEG_LOCAL_TIME = 1 << 1

# ---- Higher-order (cubic / quintic) Bezier segments (FD-0001 doc 02) ----
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


def _signed_i32(x):
    x = int(x) & 0xffffffff
    return x - (1 << 32) if x & 0x80000000 else x


def _snap_bezier_anchor(requested_su, current_su):
    if abs(requested_su - current_su) > SUBUNITS:
        return None
    return current_su


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


def _checked_smul_shr(value, ticks, shift):
    # Mirror src/trajq.c smul_shr()'s signed-int64 guard.  Return None
    # instead of allowing a commissioning command to shut down the MCU.
    raw = abs(value) * ticks
    if (raw >> 32) >> (31 + shift):
        return None
    result = raw >> shift
    return -result if value < 0 else result


def _checked_poly_term(coeff, ticks, nmul, nsh, fact):
    value = coeff
    for i in range(nmul):
        value = _checked_smul_shr(
            value, ticks, 16 if i < nsh else 0)
        if value is None:
            return None
    if fact > 1:
        value = abs(value) // fact * (-1 if value < 0 else 1)
    return value


def _higher_order_wire_safe(duration, coeffs):
    # Preflight every fixed-point intermediate used by
    # trajq_end_delta_seg().  A polynomial's final divided term may fit in
    # int64 even when its pre-division multiply does not, which is why a
    # long standalone Bezier must sometimes be subdivided.
    duration = int(duration)
    if duration <= 0:
        return False
    velocity = int(coeffs.get('v', 0))
    accel = int(coeffs.get('a', 0))
    dv = velocity * duration
    if dv >= (1 << 47) or dv <= -(1 << 47):
        return False
    delta = dv << 16
    if accel:
        # This is the intended signed-int64 range of mul64x32_half().
        aterm = abs(accel * duration) * duration >> 1
        if aterm >= (1 << 63):
            return False
        delta += -aterm if accel < 0 else aterm
    terms = (
        ('j', 3, 1, 6),
        ('s', 4, 2, 24),
        ('c', 5, 3, 120),
    )
    for key, nmul, nsh, fact in terms:
        term = _checked_poly_term(
            int(coeffs.get(key, 0)), duration, nmul, nsh, fact)
        if term is None:
            return False
        delta += term
        if delta < -(1 << 63) or delta >= (1 << 63):
            return False
    return True


def _split_bezier(ctrl, u):
    # de Casteljau subdivision.  The two returned curves reproduce the
    # original exactly over [0,u] and [u,1] before wire quantization.
    levels = [[float(value) for value in ctrl]]
    while len(levels[-1]) > 1:
        prev = levels[-1]
        levels.append([
            prev[i] + (prev[i + 1] - prev[i]) * u
            for i in range(len(prev) - 1)])
    left = [level[0] for level in levels]
    right = [level[-1] for level in reversed(levels)]
    return left, right


def safe_bezier_segments(ctrl_su, duration, max_segments=BEZIER_MAX_SEGMENTS):
    # Return wire-safe (duration, integer control points) sub-curves.  Split
    # at the exact tick ratio so their durations still sum to the requested
    # duration and their time parameterization remains continuous.  Continue
    # refinement after overflow safety is reached until every interval and
    # the chained endpoint are within half a microstep of the source curve.
    curves = [(int(duration), [float(value) for value in ctrl_su])]
    tolerance_q32 = int(SUBUNITS / 2) << 32
    desired_delta_q32 = (traj_round(ctrl_su[-1])
                         - traj_round(ctrl_su[0])) << 32
    while True:
        result = []
        total_delta = 0
        safe_and_faithful = True
        for ticks, ctrl in curves:
            quantized = [traj_round(value) for value in ctrl]
            order, coeffs = bezier_to_wire(quantized, ticks)
            if not _higher_order_wire_safe(ticks, coeffs):
                safe_and_faithful = False
                break
            end_delta = py_end_delta_ho(
                ticks, coeffs.get('v', 0), coeffs.get('a', 0),
                coeffs.get('j', 0), coeffs.get('s', 0),
                coeffs.get('c', 0))
            expected_delta = (quantized[-1] - quantized[0]) << 32
            if abs(end_delta - expected_delta) > tolerance_q32:
                safe_and_faithful = False
            total_delta += end_delta
            result.append((ticks, quantized, order, coeffs))
        if (safe_and_faithful
                and abs(total_delta - desired_delta_q32) <= tolerance_q32):
            return result
        if len(curves) * 2 > max_segments:
            raise ValueError("Bezier requires too many safe segments")
        refined = []
        for ticks, ctrl in curves:
            if ticks < 2:
                raise ValueError("Bezier cannot be represented safely on MCU")
            left_ticks = ticks // 2
            right_ticks = ticks - left_ticks
            left, right = _split_bezier(ctrl, left_ticks / float(ticks))
            refined.append((left_ticks, left))
            refined.append((right_ticks, right))
        curves = refined
# Default deviation tolerance: max(half a microstep, ~5um) is decided
# host-side in sub-units; see FD-0001 doc 02.
DEFAULT_TOLERANCE_SU = SUBUNITS / 2.
DEFAULT_SAMPLE_TIME = 0.001
DEFAULT_UNDERRUN_DECEL = 5000.  # mm/s^2
# Rolling intention record depth (the host twin of the MCU execlog
# window - FD-0001 doc 08); sized like the execlog ring by default.
DEFAULT_INTENTION_RECORD = 256

TGF_CONFIGURED = 1 << 0
TGF_ARMED = 1 << 1
TGF_EXPIRED = 1 << 2
TGR_OK = 0
TRAJECTORY_GROUP_STATE_FORMAT = (
    "trajectory_group_state group_id=%u epoch_hi=%u epoch_lo=%u"
    " sequence=%u machine_clock=%u local_clock=%u flags=%c"
    " reject_reason=%c accepted=%hu rejected=%hu")


class TrajectoryGroupMember:
    def __init__(self, owner, mcu):
        self.owner = owner
        self.mcu = mcu
        self.name = mcu.get_name()
        self.command_queue = None
        self.config_cmd = self.grant_cmd = self.query_cmd = None
        self.state = None

    def connect(self):
        self.command_queue = self.mcu.alloc_command_queue()
        cq = self.command_queue
        self.config_cmd = self.mcu.try_lookup_command(
            "trajectory_group_config group_id=%u epoch_hi=%u epoch_lo=%u",
            cq=cq)
        self.grant_cmd = self.mcu.try_lookup_command(
            "trajectory_group_grant group_id=%u epoch_hi=%u epoch_lo=%u"
            " sequence=%u machine_clock=%u local_clock=%u", cq=cq)
        self.query_cmd = self.mcu.try_lookup_command(
            "trajectory_group_query", cq=cq)
        if None in (self.config_cmd, self.grant_cmd, self.query_cmd):
            raise self.mcu.error(
                "Firmware for %s lacks the trajectory execution-grant ABI"
                % (self.name,))
        self.mcu.register_serial_response(
            self._handle_state, TRAJECTORY_GROUP_STATE_FORMAT)

    def _handle_state(self, params):
        self.state = dict(params)
        self.owner._handle_group_state(self, self.state)

    def configure(self, group_id, epoch_hi, epoch_lo):
        self.config_cmd.send([group_id, epoch_hi, epoch_lo])

    def grant(self, group_id, epoch_hi, epoch_lo, sequence,
              machine_clock, local_clock):
        self.grant_cmd.send([
            group_id, epoch_hi, epoch_lo, sequence,
            machine_clock & 0xffffffff, local_clock & 0xffffffff])

    def is_paused(self):
        checker = getattr(self.mcu, 'is_link_paused', None)
        return bool(checker is not None and checker())


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
        self.g1_segment_order = tq_owner.g1_segment_order
        ffi_main, self.ffi_lib = chelper.get_ffi()
        self.segfit = ffi_main.gc(self.ffi_lib.segfit_alloc(),
                                  self.ffi_lib.segfit_free)
        self.queue_cmd = self.hold_cmd = self.local_hold_cmd = None
        self.rebase_cmd = None
        self.local_rebase_cmd = None
        self.recovery_rebase_cmd = None
        self.local_recovery_rebase_cmd = None
        self.status_cmd = None
        self.capacity_cmd = None
        self.cubic_cmd = self.quintic_cmd = None
        self.anchored = False
        self.need_rebase = True
        # An MCU underrun is a coordination-group event, not merely a local
        # queue condition.  Once latched, every trajectory emitter stays
        # silent until failure_recovery has read the authoritative held
        # positions and scheduled one coordinated future rebase.
        self.recovery_hold = False
        self.rebase_requires_hold = False
        self.recovery_rebase = False
        self.rebase_min_clock = 0
        self.rebase_min_execution_clock = 0
        self.wire_clock = None
        # Primary-machine clock through which nonzero physical motion has
        # actually been queued.  The toolhead's last_move_time is a planning
        # horizon and may lead the MCU during an otherwise idle startup, so it
        # cannot distinguish an active grant renewal from an idle one.
        self.motion_horizon_clock = None
        self.wire_acc = None
        self.activity_cursor = 0.
        self.su_per_mm = 1.
        self.connected = False
        # Rolling record of intentions SENT: the host twin the resume
        # reconciler (FD-0001 doc 08) diffs against what the board
        # actually executed.  Each entry is
        # (start_clock, end_clock, end_pos_subunits) taken from the
        # exact unwrapped host twin advanced from the same quantized wire
        # coefficients as the MCU's modulo-Q32.32 phase. Bounded like the
        # execlog ring.
        record_size = config.getint('motion_intention_record',
                                    DEFAULT_INTENTION_RECORD, minval=16)
        self.intentions = collections.deque(maxlen=record_size)
        # Per-joint recovery disposition after a board RESET (FD-0001
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

    def _machine_mcu(self):
        getter = getattr(getattr(self, 'owner', None),
                         'get_machine_mcu', None)
        return getter() if getter is not None else self.mcu

    def _machine_freq(self):
        return self._machine_mcu().seconds_to_clock(1.)

    def _machine_clock(self, print_time):
        return self._machine_mcu().print_time_to_clock(print_time)

    def _machine_duration(self, local_duration):
        local_freq = self.mcu.seconds_to_clock(1.)
        return max(1, int(round(
            local_duration * self._machine_freq() / local_freq)))

    def _local_clock_for_machine_clock(self, machine_clock):
        machine_mcu = self._machine_mcu()
        print_time = machine_mcu.clock_to_print_time(machine_clock)
        return self.mcu.print_time_to_clock(print_time)

    def _execution_clock_for_record(self, machine_clock):
        # Test/minimal owners without an MCU use one clock domain.  Live
        # secondary-MCU records retain both domains so the flight recorder
        # can be reconciled against the exact local execution timestamps.
        if getattr(self, 'mcu', None) is None:
            return int(machine_clock)
        return int(self._local_clock_for_machine_clock(machine_clock))

    # Called from MCU_stepper._build_config
    def build_config(self, step_pin, dir_pin, invert_step, invert_dir,
                     step_pulse_ticks):
        step_dist = self.mcu_stepper.get_step_dist()
        self.su_per_mm = SUBUNITS / step_dist
        local_freq = self.mcu.seconds_to_clock(1.)
        self.terminal_hold_ticks = max(
            1, int(round(TERMINAL_HOLD_TIME * local_freq)))
        # underrun_decel wire units: sub-units/tick^2 with 32
        # fractional bits
        decel_wire = int(self.underrun_decel * self.su_per_mm
                         / (local_freq * local_freq) * 2.**32 + .5)
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
        self.local_hold_cmd = self.mcu.try_lookup_command(
            "traj_hold_local oid=%c duration=%u", cq=cmd_queue)
        self.rebase_cmd = self.mcu.lookup_command(
            "trajectory_rebase oid=%c clock=%u pos=%i mcu_pos=%i",
            cq=cmd_queue)
        self.local_rebase_cmd = self.mcu.try_lookup_command(
            "trajectory_rebase_local oid=%c machine_clock=%u"
            " local_clock=%u pos=%i mcu_pos=%i", cq=cmd_queue)
        self.recovery_rebase_cmd = self.mcu.try_lookup_command(
            "trajectory_rebase_recovery oid=%c clock=%u pos=%i mcu_pos=%i",
            cq=cmd_queue)
        self.local_recovery_rebase_cmd = self.mcu.try_lookup_command(
            "trajectory_rebase_recovery_local oid=%c machine_clock=%u"
            " local_clock=%u pos=%i mcu_pos=%i", cq=cmd_queue)
        # Higher-order commands exist only if the firmware was built with
        # CONFIG_WANT_TRAJECTORY_HIGHER_ORDER; look them up optionally.
        self.cubic_cmd = self.mcu.try_lookup_command(
            "queue_traj_segment_cubic oid=%c flags=%c duration=%u"
            " velocity=%i accel=%i jerk=%i", cq=cmd_queue)
        self.quintic_cmd = self.mcu.try_lookup_command(
            "queue_traj_segment_quintic oid=%c flags=%c duration=%u"
            " velocity=%i accel=%i jerk=%i snap=%i crackle=%i",
            cq=cmd_queue)
        if self.mcu.try_lookup_command("traj_query oid=%c",
                                       cq=cmd_queue) is not None:
            self.status_cmd = self.mcu.lookup_query_command(
                "traj_query oid=%c",
                "traj_status oid=%c flags=%c queued=%hu dropped=%hu"
                " horizon_clock=%u pos=%i",
                oid=self.oid, cq=cmd_queue)
        if self.mcu.try_lookup_command("traj_capacity_query oid=%c",
                                       cq=cmd_queue) is not None:
            self.capacity_cmd = self.mcu.lookup_query_command(
                "traj_capacity_query oid=%c",
                "traj_capacity oid=%c total=%hu free=%hu slot_bytes=%c",
                oid=self.oid, cq=cmd_queue)

    def _setup_fitter_kinematics(self, sk):
        freq = self.mcu.seconds_to_clock(1.)
        self.ffi_lib.segfit_setup(self.segfit, sk, freq, self.su_per_mm,
                                  self.tolerance_su, self.sample_time)
        # A zero-acceleration cruise has an exact, division-light MCU step
        # solver.  Let the motion fitter use its normal tolerance budget to
        # discard sub-step ramp/chaining residue and select that realization.
        self.ffi_lib.segfit_set_cruise_fastpath(self.segfit, 1)
        self.ffi_lib.segfit_set_order(self.segfit, self.g1_segment_order)

    def update_kinematics(self, sk):
        # set_stepper_kinematics() is also used during construction, before
        # build_config has established su_per_mm. Runtime replacements are
        # safe because their callers flush the old path first.
        if self.connected:
            self._setup_fitter_kinematics(sk)

    def connect(self):
        if self.recovery_rebase_cmd is None:
            raise self.mcu.error(
                "Firmware for %s lacks the trajectory recovery-rebase ABI"
                % (self.name,))
        if (self.mcu is not self._machine_mcu()
                and (self.local_rebase_cmd is None
                     or self.local_hold_cmd is None
                     or self.local_recovery_rebase_cmd is None)):
            raise self.mcu.error(
                "Firmware for %s lacks the local-clock rebase/hold/recovery ABI"
                " required by secondary-MCU trajectory streams"
                % (self.name,))
        if self.g1_segment_order == TSEG_POLY_QUINTIC >> 6 \
                and self.quintic_cmd is None:
            raise self.mcu.error(
                "Firmware for %s lacks quintic support required by"
                " [trajectory_queuing] g1_segment_order: quintic"
                % (self.name,))
        self._setup_fitter_kinematics(
            self.mcu_stepper.get_stepper_kinematics())
        self.connected = True

    def note_rebase_needed(self, stopped=False):
        if self.anchored and not stopped:
            # A kinematic position change can replace a trapq while the
            # previous path is still queued (for example, a Z safety lift
            # immediately followed by Z homing).  Preserve that fact so the
            # next anchor seals the old path before its rebase barrier.
            self.rebase_requires_hold = True
        self.anchored = False
        self.need_rebase = True
        if stopped:
            # A trsync/query or underrun event proves the backend is idle, so
            # no previous planned horizon needs to delay the next rebase.
            self.rebase_requires_hold = False
            self.rebase_min_clock = 0
            self.rebase_min_execution_clock = 0
            self.motion_horizon_clock = None
            self.recovery_rebase = True

    def note_homing_held(self, clock, pos_su):
        # trsync has stopped the executor, so its accumulator is now the
        # authoritative physical position.  HomingMove will immediately call
        # toolhead.set_position(); MCU_stepper.set_position() deliberately
        # preserves wire_acc across that coordinate-frame change.  Make sure
        # it preserves the held position, not the unexecuted endpoint of the
        # host's homing plan.
        self.wire_acc = int(pos_su) << 32
        self.execution_clock = int(clock)

    def commanded_pos_su(self):
        # Current commanded joint position in sub-units, from the host
        # wire twin.  Trajectory steppers intentionally bypass itersolve's
        # step generator, so its commanded-position cache is stale after a
        # normal move.  Convert the exact physical wire accumulator back
        # through the stepper offset instead.
        wire_acc = getattr(self, 'wire_acc', None)
        if wire_acc is not None:
            return self.mcu_stepper.mcu_to_commanded_position_su(
                int(wire_acc) >> 32)
        # Before the first wire anchor, kinematics is the only available
        # source and is also the position the first anchor will use.
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
            'recovery_hold': bool(getattr(self, 'recovery_hold', False)),
            'su_per_mm': self.su_per_mm,
            'commanded_pos_su': self.commanded_pos_su(),
            'last_intention_pos_su': (li[2] if li else None),
            'higher_order': self.cubic_cmd is not None,
            'g1_segment_order': ('quintic' if self.g1_segment_order == 2
                                 else 'quadratic'),
            'homing_volatile': bool(self.homing_volatile),
        }

    def firmware_status(self):
        if self.status_cmd is None:
            return None
        status = self.status_cmd.send([self.oid])
        if self.capacity_cmd is not None:
            status.update({
                'pool_' + key: value for key, value in
                self.capacity_cmd.send([self.oid]).items()
                if key != 'oid'})
        return status

    def bezier_move(self, duration_s, ctrl_su):
        # Advanced/commissioning primitive: drive THIS joint alone along a
        # cubic (4 control points) or quintic (6) Bezier, bypassing the
        # kinematic planner (like FORCE_MOVE).  Requires the caller to have
        # flushed and be holding a print_time; anchors at ctrl_su[0], emits
        # the segment, and retains the exact end in the wire twin.
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
        reactor = self.owner.printer.get_reactor()
        est_print_time = self.mcu.estimated_print_time(reactor.monotonic())
        # A standalone command can arrive after the normal toolhead timeline
        # has gone idle.  Anchor from the live MCU estimate as well as the
        # queued toolhead horizon, otherwise the firmware correctly rejects
        # the stale rebase clock as already in the past.
        anchor_time = max(print_time, est_print_time + BEZIER_QUEUE_MARGIN)
        duration = int(round(duration_s * self.mcu.seconds_to_clock(1.)))
        if duration <= 0:
            raise self.mcu.error("BEZIER_MOVE duration must be positive")
        # The public control points are commanded joint coordinates, while
        # trajectory_rebase and the MCU accumulator live in physical step
        # space.  Preserve the homing/SET_POSITION offset exactly as normal
        # fitted motion does in _anchor().
        wire_ctrl_su = [
            self.mcu_stepper.commanded_to_mcu_position_su(
                point / self.su_per_mm)
            for point in ctrl_su]
        anchor_su = int(wire_ctrl_su[0])
        anchor_wire_su = _signed_i32(anchor_su)
        # Rebase this joint at the first control point, then emit.
        self.note_rebase_needed()
        anchor_mcu_pos = int(round(anchor_su / SUBUNITS))
        self._send_rebase(anchor_time, anchor_su, anchor_mcu_pos)
        self.ffi_lib.segfit_set_anchor(self.segfit, anchor_time,
                                       anchor_wire_su << 32)
        self.ffi_lib.segfit_set_anchor_position(self.segfit, anchor_su)
        self.anchored = False   # standalone emit; not fitter-driven
        try:
            segments = safe_bezier_segments(wire_ctrl_su, duration)
        except ValueError as e:
            raise self.mcu.error("Unsafe BEZIER_MOVE for %s: %s"
                                 % (self.name, str(e)))
        total_delta = 0
        current_wire_su = anchor_su
        for seg_duration, seg_ctrl, _order, _coeffs in segments:
            # Quantization may leave a sub-unit mismatch at a split.  Shift
            # the entire following control polygon onto the exact chained
            # wire accumulator; translation leaves all derivatives intact.
            offset_su = current_wire_su - int(seg_ctrl[0])
            seg_ctrl = [int(point) + offset_su for point in seg_ctrl]
            end_delta = self.queue_bezier_segment(seg_duration, seg_ctrl)
            total_delta += end_delta
            current_wire_su = anchor_su + int(total_delta >> 32)
        self.last_bezier_segments = len(segments)
        end_commanded_su = int(ctrl_su[0]) + int(total_delta >> 32)
        # Standalone commissioning moves do not pass through flush()'s idle
        # path, so terminate them explicitly instead of allowing an empty
        # queue to be interpreted as an underrun after the polynomial ends.
        self._queue_terminal_hold()
        end_time = anchor_time + duration_s + TERMINAL_HOLD_TIME
        motion_queuing = self.owner.printer.lookup_object('motion_queuing')
        motion_queuing.note_mcu_movequeue_activity(end_time)
        toolhead.dwell(max(0., end_time - print_time) + BEZIER_WAIT_MARGIN)
        toolhead.flush_step_generation()
        toolhead.wait_moves()
        # Keep the existing commanded-to-physical offset until the required
        # SET_KINEMATIC_POSITION. sync_to_held_position() is a recovery
        # primitive that derives a new offset from itersolve's commanded-pos
        # cache; trajectory motion bypasses that cache, so using it here would
        # make a correct nonzero endpoint appear near zero in status.
        self.note_rebase_needed()
        return end_commanded_su

    def _clamp_rebase_time_to_committed_hold(self, print_time):
        # A pressure-advance island may be appended after the previous flush
        # already committed its 1ms terminal hold.  Its pre-active boundary
        # can then land a few ticks inside that immutable hold.  We cannot
        # retract a command already sent to the MCU; start the new island at
        # the exact committed horizon and sample its position there.  Keep
        # the adjustment bounded by the terminal-hold duration so a real
        # planning overlap still fails closed in _send_rebase().
        requested_time = print_time
        min_machine = getattr(self, 'rebase_min_clock', 0)
        min_local = getattr(self, 'rebase_min_execution_clock', 0)
        if not min_machine and not min_local:
            return print_time
        machine_clock = int(self._machine_clock(print_time))
        local_clock = int(self.mcu.print_time_to_clock(print_time))
        machine_freq = float(self._machine_freq())
        local_freq = float(self.mcu.seconds_to_clock(1.))
        overlap = max(
            ((min_machine - machine_clock) / machine_freq
             if min_machine > machine_clock else 0.),
            ((min_local - local_clock) / local_freq
             if min_local > local_clock else 0.))
        max_tick = 1. / min(machine_freq, local_freq)
        if overlap <= 0. or overlap > TERMINAL_HOLD_TIME + max_tick:
            return print_time
        if min_machine:
            print_time = max(
                print_time,
                self._machine_mcu().clock_to_print_time(min_machine))
        if min_local:
            print_time = max(
                print_time, self.mcu.clock_to_print_time(min_local))
        # Float conversion at a clock boundary can round one tick down.
        # Advance only by the missing exact ticks, then nextafter once.
        for _ in range(2):
            machine_clock = int(self._machine_clock(print_time))
            local_clock = int(self.mcu.print_time_to_clock(print_time))
            correction = max(
                ((min_machine - machine_clock) / machine_freq
                 if min_machine > machine_clock else 0.),
                ((min_local - local_clock) / local_freq
                 if min_local > local_clock else 0.))
            if correction <= 0.:
                break
            print_time += correction
        print_time = math.nextafter(print_time, math.inf)
        logging.info(
            "Trajectory boundary for %s clipped %.1fus to committed hold",
            self.name, (print_time - requested_time) * 1.e6)
        return print_time

    def _send_rebase(self, print_time, pos_su, mcu_pos):
        clock = int(self._machine_clock(print_time))
        local_clock = int(self.mcu.print_time_to_clock(print_time))
        min_machine = getattr(self, 'rebase_min_clock', 0)
        min_local = getattr(self, 'rebase_min_execution_clock', 0)
        if min_machine and clock < min_machine:
            raise self.mcu.error(
                "Trajectory boundary for %s overlaps the previous hold:"
                " rebase machine clock %d < horizon %d" % (
                    self.name, clock, min_machine))
        if min_local and local_clock < min_local:
            raise self.mcu.error(
                "Trajectory boundary for %s overlaps the previous local"
                " horizon: rebase clock %d < horizon %d" % (
                    self.name, local_clock, min_local))
        wire_pos_su = _signed_i32(pos_su)
        # minclock is a *transmission release* barrier, not merely command
        # ordering metadata.  Reusing the previous execution horizon here
        # held a valid queued rebase on the host until that horizon had
        # already passed; disconnected pressure-advance islands only ~1ms
        # apart then reached a 64MHz toolhead MCU hundreds of microseconds
        # late and triggered "Timer too close".  All commands for this joint
        # share one command queue, and the MCU validates the rebase clock
        # against its queued horizon, so transmit the barrier ahead of time.
        recovery = getattr(self, 'recovery_rebase', False)
        if self.mcu is self._machine_mcu():
            cmd = self.recovery_rebase_cmd if recovery else self.rebase_cmd
            if cmd is None:
                raise self.mcu.error(
                    "Firmware for %s lacks the trajectory recovery-rebase ABI"
                    % (self.name,))
            cmd.send(
                [self.oid, clock & 0xffffffff, wire_pos_su, mcu_pos],
                minclock=0, reqclock=local_clock)
        else:
            cmd = (self.local_recovery_rebase_cmd if recovery
                   else self.local_rebase_cmd)
            if cmd is None:
                raise self.mcu.error(
                    "Firmware for %s lacks the local-clock rebase barrier"
                    % (self.name,))
            cmd.send(
                [self.oid, clock & 0xffffffff, local_clock & 0xffffffff,
                 wire_pos_su, mcu_pos],
                minclock=0, reqclock=local_clock)
        self.recovery_rebase = False
        self._wire_rebase(clock, pos_su, mcu_pos,
                          execution_clock=local_clock)
        self.rebase_min_clock = 0
        self.rebase_min_execution_clock = 0
        return clock

    def _anchor(self, print_time):
        # Anchor to the queued path at this time.  Trajectory steppers bypass
        # itersolve_generate_steps(), so that legacy solver's commanded_pos
        # can be stale after a homing halt or kinematic position change.
        if self.rebase_requires_hold:
            self._queue_terminal_hold()
            self.rebase_requires_hold = False
        print_time = self._clamp_rebase_time_to_committed_hold(print_time)
        pos_mm = self.ffi_lib.segfit_get_position(self.segfit, print_time)
        if not math.isfinite(print_time) or not math.isfinite(pos_mm):
            raise self.mcu.error(
                "Non-finite trajectory anchor for %s at print_time=%r:"
                " position=%r" % (self.name, print_time, pos_mm))
        # segfit samples commanded joint space. Convert it through the
        # stepper position offset so the wire anchor stays in physical MCU
        # step space across homing and SET_POSITION.
        pos_su = self.mcu_stepper.commanded_to_mcu_position_su(pos_mm)
        wire_pos_su = _signed_i32(pos_su)
        mcu_pos = self.mcu_stepper.get_mcu_position(pos_mm)
        if not (-2147483648 <= mcu_pos <= 2147483647):
            raise self.mcu.error(
                "Trajectory anchor for %s exceeds the signed 32-bit"
                " microstep range: %d" % (self.name, mcu_pos))
        position_offset_su = pos_su - pos_mm * self.su_per_mm
        acc = wire_pos_su << 32
        clock = self._send_rebase(print_time, pos_su, mcu_pos)
        self.ffi_lib.segfit_set_position_offset(self.segfit,
                                                position_offset_su)
        self.ffi_lib.segfit_set_anchor(self.segfit, print_time, acc)
        self.ffi_lib.segfit_set_anchor_position(self.segfit, pos_su)
        self.anchored = True
        self.need_rebase = False
        # Record the (re-)anchor point in the host intention twin.
        self.intentions.append((int(clock), int(clock), int(pos_su)))

    def flush(self, flush_time, step_gen_time):
        # In particular, do not re-anchor from a historical trapq window
        # after Klippy resumes from a host stall.  The MCU has already
        # completed its underrun ramp and rejects that stale clock.
        if getattr(self, 'recovery_hold', False):
            return
        sk = self.mcu_stepper.get_stepper_kinematics()
        if sk is None:
            return
        # This callback receives two horizons.  flush_time is the point up to
        # which already generated queue data may be committed; step_gen_time
        # is the (later) point to which kinematic motion must be generated.
        # A trajectory segment is itself the generated step data, so using
        # flush_time here can first transmit an anchor only after that anchor
        # is already due (most visibly on the retract after a homing drip).
        # Match the legacy itersolve/stepcompress path and generate through the
        # step-generation horizon.
        gen_time = step_gen_time
        scan_pre = self.ffi_lib.itersolve_get_gen_steps_pre_active(sk)
        scan_post = self.ffi_lib.itersolve_get_gen_steps_post_active(sk)
        # Homing/probing drip mode deliberately streams only a short prefix
        # of a move and may stop before its nominal trapq endpoint.  Keep the
        # qualified legacy activity probe for that special case: queuing a
        # terminal hold at the nominal endpoint would put it behind a trigger
        # that can halt the executor earlier.
        #
        # Outside drip mode, always use the explicit activity-window scan --
        # even when the kinematics has no pre/post-active margin.  A
        # trajectory stepper bypasses itersolve_generate_steps(), so the
        # legacy itersolve last_flush_time cursor never advances.  Using only
        # itersolve_check_active() would therefore keep a completed ordinary
        # move "active" forever and omit its terminal hold.  A following
        # synchronous command (notably M190/M109) can then let the finite MCU
        # queue drain into the emergency underrun ramp.
        if not scan_pre and not scan_post and self._in_drip_mode():
            return self._flush_standard_activity(sk, gen_time)
        return self._flush_scan_activity(sk, gen_time)

    def _in_drip_mode(self):
        printer = getattr(self.owner, 'printer', None)
        if printer is None:
            return False
        motion_queuing = printer.lookup_object('motion_queuing', None)
        if motion_queuing is None:
            return False
        return motion_queuing.check_drip_timing() is not None

    def _flush_standard_activity(self, sk, gen_time):
        # Homing drip mode can replace an interrupted trapq while future
        # rebase barriers are already queued; ending at the nominal trapq
        # boundary early would inject another hold into that ordered stream.
        active_time = self.ffi_lib.itersolve_check_active(sk, gen_time)
        if ((active_time or self.anchored)
                and not self.owner.is_mcu_synced(self.mcu)):
            raise self.mcu.error(
                "Machine-time discipline for %s is not converged; refusing"
                " trajectory Class-0 traffic" % (self.mcu.get_name(),))
        if active_time:
            # The legacy stepper-enable path learns about motion from
            # itersolve_generate_steps().  Trajectory fitting bypasses that
            # pulse generator, so announce the same activity boundary before
            # sending the first curve (and before a TMC init can collide with
            # live trajectory execution on a secondary MCU).
            self.mcu_stepper.note_active(active_time)
        if not self.anchored:
            if not active_time:
                return
            self._anchor(active_time)
        prev_acc = self._chained_acc()
        prev_time = self.ffi_lib.segfit_get_gen_time(self.segfit)
        n = (self.ffi_lib.segfit_generate(self.segfit, gen_time)
             if active_time else 0)
        if n < 0:
            raise self.mcu.error(
                "Trajectory for %s exceeds representable wire limits"
                % (self.name,))
        self._send_segs(n)
        self._record_intention(prev_acc, prev_time)
        prev_acc = self._chained_acc()
        prev_time = self.ffi_lib.segfit_get_gen_time(self.segfit)
        n = self.ffi_lib.segfit_finalize(self.segfit)
        if n < 0:
            raise self.mcu.error(
                "Trajectory for %s exceeds representable wire limits"
                % (self.name,))
        if n > 0:
            self._send_segs(n)
        self._record_intention(prev_acc, prev_time)
        if not active_time:
            self._queue_terminal_hold()
            self.anchored = False

    def _flush_scan_activity(self, sk, gen_time):
        # A nonzero kinematic scan window is real actuator motion outside the
        # nominal trapq interval (pressure-advance and input-shaper pre/post
        # roll). Fit its complete connected activity window rather than
        # converting the leading displacement into a rebase.
        prefetched_from = None
        prefetched_activity = False
        while True:
            activity_cursor = getattr(self, 'activity_cursor', 0.)
            from_time = (self.ffi_lib.segfit_get_gen_time(self.segfit)
                         if self.anchored else activity_cursor)
            if (prefetched_from is not None
                    and abs(from_time - prefetched_from) <= 1.e-12):
                has_activity = prefetched_activity
                prefetched_from = None
            else:
                has_activity = self.ffi_lib.segfit_check_activity(
                    self.segfit, from_time, gen_time)
            if ((has_activity or self.anchored)
                    and not self.owner.is_mcu_synced(self.mcu)):
                # Do not advance segfit or the persisted intention twin while
                # the firmware's Class-0 gate will reject these same segments.
                # Failing before send preserves one shared view of what ran.
                raise self.mcu.error(
                    "Machine-time discipline for %s is not converged;"
                    " refusing trajectory Class-0 traffic"
                    % (self.mcu.get_name(),))
            activity_start = (self.ffi_lib.segfit_get_activity_start(
                self.segfit) if has_activity else None)
            if activity_start is not None:
                self.mcu_stepper.note_active(activity_start)
            if not self.anchored:
                if not has_activity:
                    # Nothing through this generation horizon can move the
                    # joint.  Advancing the search cursor prevents a completed
                    # historical move from being selected after its trapq entry
                    # lingers for lookback diagnostics.
                    self.activity_cursor = max(activity_cursor, gen_time)
                    return
                # Anchor at the complete kinematic activity boundary, not
                # merely the nominal trapq move. Pressure advance and input
                # shaping use pre-active scan windows whose pulses are real
                # motion; skipping them would turn their initial displacement
                # into a position rebase. segfit_check_activity() also extends
                # the corresponding post-active tail below.
                self._anchor(activity_start)
            activity_end = (self.ffi_lib.segfit_get_activity_end(self.segfit)
                            if has_activity else from_time)
            fit_end = min(gen_time, activity_end)
            # Never sample beyond the connected activity window. The tail
            # sentinel is a held coordinate, not a new destination; fitting
            # past it previously manufactured a one-sample return-to-zero
            # burst.
            while True:
                prev_acc = self._chained_acc()
                prev_time = self.ffi_lib.segfit_get_gen_time(self.segfit)
                n = (self.ffi_lib.segfit_generate(self.segfit, fit_end)
                     if has_activity and fit_end > prev_time else 0)
                if n < 0:
                    raise self.mcu.error(
                        "Trajectory for %s exceeds representable wire limits"
                        % (self.name,))
                self._send_segs(n)
                self._record_intention(prev_acc, prev_time)
                if n < SEGFIT_BATCH_MAX:
                    break
                next_time = self.ffi_lib.segfit_get_gen_time(self.segfit)
                if next_time <= prev_time:
                    raise self.mcu.error(
                        "Trajectory fitter for %s made no progress while"
                        " draining a full segment batch" % (self.name,))
            # Do not retain a valid candidate across host flush callbacks. A
            # long constant-velocity phase may keep fitting until the 4.096s
            # wire duration cap; if it is only emitted then, its declared
            # start clock is already seconds in the past. Seal the prefix
            # generated for this step-generation horizon so every queued
            # segment is delivered while it is still future motion.
            prev_acc = self._chained_acc()
            prev_time = self.ffi_lib.segfit_get_gen_time(self.segfit)
            n = self.ffi_lib.segfit_finalize(self.segfit)
            if n < 0:
                raise self.mcu.error(
                    "Trajectory for %s exceeds representable wire limits"
                    % (self.name,))
            if n > 0:
                self._send_segs(n)
            self._record_intention(prev_acc, prev_time)
            window_done = (not has_activity
                           or fit_end >= activity_end - 1.e-12)
            if not window_done:
                return
            # Motion has ended.  Terminate every joint with an explicit
            # zero-velocity segment instead of relying on the final fitted
            # coefficient rounding to exactly zero.  Without this, one side
            # of a CoreXY pair can enter the MCU's emergency underrun ramp at
            # the end of an otherwise successful homing retract.
            # Never let that hold overlap a following disconnected activity
            # window.  At high M220 factors an extruder retract/unretract gap
            # can be shorter than the normal 1ms hold; fill only the known
            # idle span, in the correct local and machine clock domains.
            hold_until = gen_time
            next_cursor = max(activity_cursor, activity_end, fit_end)
            if next_cursor < gen_time - 1.e-12:
                has_next = self.ffi_lib.segfit_check_activity(
                    self.segfit, next_cursor, gen_time)
                prefetched_from = next_cursor
                prefetched_activity = has_next
                if has_next:
                    hold_until = min(
                        hold_until,
                        self.ffi_lib.segfit_get_activity_start(self.segfit))
            wire_clock = getattr(self, 'wire_clock', None)
            max_hold_ticks = (None if wire_clock is None else max(
                0, self._machine_clock(hold_until) - wire_clock))
            self._queue_terminal_hold(max_hold_ticks)
            # Every disconnected window is a fresh physical-position island.
            # In particular, relative extrusion must re-anchor from trapq E;
            # carrying one modulo accumulator across a whole print changes the
            # semantics of pressure-advance/retraction islands.
            self.anchored = False
            self.activity_cursor = max(activity_cursor, activity_end,
                                       fit_end)
            if fit_end >= gen_time - 1.e-12:
                return
            # A single host lookahead horizon can contain many disconnected
            # extrusion islands or shaped moves. Drain all of them before
            # motion_queuing finalizes old trapq entries; otherwise the next
            # callback may no longer have the pre-active context needed by
            # pressure advance or input shaping.

    def _queue_terminal_hold(self, max_machine_ticks=None):
        local_duration = self.terminal_hold_ticks
        machine_duration = self._machine_duration(local_duration)
        if (max_machine_ticks is not None
                and machine_duration > max_machine_ticks):
            available = int(max_machine_ticks)
            local_freq = self.mcu.seconds_to_clock(1.)
            machine_freq = self._machine_freq()
            local_duration = max(
                1, int(math.floor(available * local_freq / machine_freq)))
            machine_duration = self._machine_duration(local_duration)
            while machine_duration > available and local_duration > 1:
                local_duration -= 1
                machine_duration = self._machine_duration(local_duration)
            if available <= 0 or machine_duration > available:
                # An immediately adjacent rebase itself terminates the old
                # executor state. Preserve command ordering without emitting
                # an unrepresentable positive-duration hold.
                if self.wire_clock is not None:
                    self.rebase_min_clock = max(
                        self.rebase_min_clock, self.wire_clock)
                if getattr(self, 'execution_clock', None) is not None:
                    self.rebase_min_execution_clock = max(
                        getattr(self, 'rebase_min_execution_clock', 0),
                        self.execution_clock)
                return False
        self._send_local_hold(local_duration)
        self._wire_segment(1, machine_duration, 0, 0,
                           exec_duration=local_duration)
        # wire_clock is the exact machine-clock horizon after the hold.  A
        # following rebase must remain ordered after it even if both paths
        # are generated during the same host flush cycle.
        wire_clock = getattr(self, 'wire_clock', None)
        if wire_clock is not None:
            self.rebase_min_clock = max(self.rebase_min_clock, wire_clock)
        if getattr(self, 'execution_clock', None) is not None:
            self.rebase_min_execution_clock = max(
                getattr(self, 'rebase_min_execution_clock', 0),
                self.execution_clock)
        return True

    def _send_local_hold(self, duration):
        # traj_hold is the legacy machine-time command.  Fitted trajectories
        # are encoded wholly in the actuator MCU's local timer domain, so use
        # the explicit local form whenever firmware advertises it.  Falling
        # back is safe only for the primary MCU, where both domains coincide;
        # connect() rejects an older secondary-MCU firmware image.
        cmd = getattr(self, 'local_hold_cmd', None) or self.hold_cmd
        cmd.send([self.oid, duration])

    def _record_intention(self, prev_acc, prev_time):
        # Append (start_clock, end_clock, end_pos_subunits) for the span
        # just emitted, straight off the fitter's exact chained anchor.
        acc = self._chained_acc()
        if acc == prev_acc:
            return  # nothing emitted / anchor unchanged
        end_time = self.ffi_lib.segfit_get_gen_time(self.segfit)
        try:
            start_clock = int(self._machine_clock(prev_time))
            end_clock = int(self._machine_clock(end_time))
        except Exception:
            return
        self.intentions.append((start_clock, end_clock, int(acc >> 32)))

    def _chained_acc(self):
        wire_acc = getattr(self, 'wire_acc', None)
        if wire_acc is not None:
            return int(wire_acc)
        return int(self.ffi_lib.segfit_get_anchor(self.segfit))

    # ---- resume reconciliation (FD-0001 doc 08) ----
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

    def resume_reconcile(self, clock, pos_su, anchor_print_time):
        # Snapshot the board's segment executor at its authoritative held
        # accumulator and bring the host fitter + mcu-position offset back
        # into agreement.  The common future rebase is a recovery barrier,
        # not a durable idle anchor: it has no segment attached to keep its
        # start clock meaningful after an arbitrary operator inspection
        # delay.  Leave the stopped executor requiring a fresh rebase so the
        # first subsequent move receives an anchor that is future at the time
        # that move is actually queued.
        if self.rebase_cmd is None:
            return
        held_clock = int(clock)
        print_time = float(anchor_print_time)
        pos_su = int(pos_su)
        # A live board underrun does not change the established physical to
        # logical coordinate offset.  Convert the held accumulator through
        # that existing frame before replacing the host wire twin.
        held_commanded_su = self.mcu_stepper.mcu_to_commanded_position_su(
            pos_su)
        held_commanded_pos = held_commanded_su / self.su_per_mm
        mcu_pos = int(round(pos_su / SUBUNITS))
        wire_pos_su = _signed_i32(pos_su)
        clock = self._send_rebase(print_time, pos_su, mcu_pos)
        try:
            self.ffi_lib.segfit_set_position_offset(
                self.segfit,
                pos_su - held_commanded_pos * self.su_per_mm)
            self.ffi_lib.segfit_set_anchor(self.segfit, print_time,
                                           wire_pos_su << 32)
            self.ffi_lib.segfit_set_anchor_position(self.segfit, pos_su)
            self.anchored = True
            self.need_rebase = False
            # Ignore trapq history preceding the recovery boundary.  If the
            # interrupted move still extends beyond it, the normal activity
            # scan may fit that remaining suffix from the held position.
            self.activity_cursor = max(
                getattr(self, 'activity_cursor', 0.), print_time)
            self.note_rebase_needed(stopped=True)
        except Exception:
            # Fall back to a lazy re-anchor on the next motion.
            self.note_rebase_needed(stopped=True)
        self.intentions.append((clock, clock, pos_su))
        logging.info(
            "Trajectory recovery rebase for %s: held_clock=%d"
            " future_clock=%d pos=%d",
            self.name, held_clock, clock, pos_su)
        return held_commanded_pos

    def note_resume_reanchor(self, anchor_print_time=None):
        # Board RESET, homing retained: the board's volatile accumulator
        # is gone, but the host still knows where this joint was (its
        # last commanded position) and trusts the homing it had.  Re-anchor
        # at the host's current commanded position on the next motion and
        # continue.  Same mechanism for a relative axis (extruder re-prime)
        # and an absolute axis whose homing survived the reset.
        self.note_rebase_needed()
        if anchor_print_time is not None:
            self.activity_cursor = max(
                getattr(self, 'activity_cursor', 0.), anchor_print_time)

    # Retained name for callers/tests predating the homing-retained model.
    note_reprime = note_resume_reanchor

    def _send_segs(self, n):
        segs = self.ffi_lib.segfit_get_segs(self.segfit)
        for i in range(n):
            s = segs[i]
            if not s.duration:
                continue
            machine_duration = self._machine_duration(s.duration)
            if (not s.velocity and not s.accel and not s.jerk
                    and not s.snap and not s.crackle):
                self._send_local_hold(s.duration)
                self._wire_segment(1, machine_duration, 0, 0,
                                   exec_duration=s.duration)
                continue
            order = s.flags & TSEG_POLY_MASK
            if order == TSEG_POLY_QUINTIC:
                if self.quintic_cmd is None:
                    raise self.mcu.error(
                        "Firmware for %s lacks quintic trajectory support"
                        % (self.name,))
                base_flags = ((s.flags & ~TSEG_POLY_MASK)
                              | TSEG_LOCAL_TIME)
                self.quintic_cmd.send(
                    [self.oid, base_flags, s.duration, s.velocity, s.accel,
                     s.jerk, s.snap, s.crackle])
                self._wire_segment(
                    s.flags | TSEG_LOCAL_TIME, machine_duration,
                    s.velocity, s.accel, s.jerk, s.snap, s.crackle,
                    exec_duration=s.duration)
            elif not order:
                local_flags = s.flags | TSEG_LOCAL_TIME
                self.queue_cmd.send([self.oid, local_flags, s.duration,
                                     s.velocity, s.accel])
                self._wire_segment(local_flags, machine_duration,
                                   s.velocity, s.accel,
                                   exec_duration=s.duration)
            else:
                raise self.mcu.error(
                    "Unsupported fitted polynomial order 0x%x for %s"
                    % (order, self.name))

    def _wire_rebase(self, clock, pos_su, mcu_pos, execution_clock=None):
        self.wire_clock = int(clock)
        self.execution_clock = (self._execution_clock_for_record(clock)
                                if execution_clock is None
                                else int(execution_clock))
        self.wire_acc = int(pos_su) << 32
        wire_pos_su = _signed_i32(pos_su)
        self._record_wire({
            'event': 'rebase', 'start_clock': self.wire_clock,
            'end_clock': self.wire_clock,
            'execution_start_clock': self.execution_clock,
            'execution_end_clock': self.execution_clock,
            'position_su': int(wire_pos_su),
            'absolute_position_su': int(pos_su),
            'acc_q32': int(self.wire_acc),
            'mcu_position': int(mcu_pos),
        })

    def _wire_segment(self, flags, duration, velocity, accel,
                      jerk=0, snap=0, crackle=0, exec_duration=None):
        if (getattr(self, 'wire_clock', None) is None
                or getattr(self, 'wire_acc', None) is None):
            return
        start_clock = self.wire_clock
        if getattr(self, 'execution_clock', None) is None:
            self.execution_clock = self._execution_clock_for_record(
                start_clock)
        execution_start_clock = self.execution_clock
        start_acc = self.wire_acc
        start_abs_pos = self.wire_acc >> 32
        if exec_duration is None:
            exec_duration = duration
        self.wire_acc += py_end_delta_ho(
            exec_duration, velocity, accel, jerk, snap, crackle)
        self.wire_clock += int(duration)
        self.execution_clock += int(exec_duration)
        if velocity or accel or jerk or snap or crackle:
            self.motion_horizon_clock = self.wire_clock
        self._record_wire({
            'event': 'hold' if flags & 1 else 'segment',
            'start_clock': start_clock, 'end_clock': self.wire_clock,
            'duration': int(duration),
            'execution_duration': int(exec_duration), 'flags': int(flags),
            'execution_start_clock': int(execution_start_clock),
            'execution_end_clock': int(self.execution_clock),
            'velocity': int(velocity), 'accel': int(accel),
            'jerk': int(jerk), 'snap': int(snap), 'crackle': int(crackle),
            'start_position_su': _signed_i32(start_abs_pos),
            'end_position_su': _signed_i32(self.wire_acc >> 32),
            'absolute_start_position_su': int(start_abs_pos),
            'absolute_end_position_su': int(self.wire_acc >> 32),
            'start_acc_q32': int(start_acc),
            'end_acc_q32': int(self.wire_acc),
        })

    def _record_wire(self, fields):
        record = getattr(getattr(self, 'owner', None),
                         'record_wire_intention', None)
        if record is not None:
            record(self, fields)

    def queue_bezier_segment(self, duration, ctrl_su):
        # Emit one cubic (4 control points) or quintic (6 control points)
        # Bezier segment. ctrl_su are positions in sub-units spanning
        # 'duration' ticks; the chained encoding means ctrl_su[0] must be
        # the current anchor position (only the relative shape is sent).
        # Returns the exact Q32.32 end delta (mirrors the MCU accumulator)
        # so the caller can advance its anchor without any drift.
        duration = int(duration)
        order, c = bezier_to_wire(ctrl_su, duration)
        base_flags = TSEG_LOCAL_TIME
        machine_duration = self._machine_duration(duration)
        if order == TSEG_POLY_CUBIC:
            if self.cubic_cmd is None:
                raise self.mcu.error(
                    "Firmware for %s lacks higher-order trajectory support"
                    % (self.name,))
            self.cubic_cmd.send([self.oid, base_flags, duration,
                                 c['v'], c['a'], c['j']])
            self._wire_segment(base_flags | TSEG_POLY_CUBIC,
                               machine_duration, c['v'], c['a'], c['j'],
                               exec_duration=duration)
            return py_end_delta_ho(duration, c['v'], c['a'], c['j'])
        if self.quintic_cmd is None:
            raise self.mcu.error(
                "Firmware for %s lacks higher-order trajectory support"
                % (self.name,))
        self.quintic_cmd.send([self.oid, base_flags, duration,
                               c['v'], c['a'], c['j'], c['s'], c['c']])
        self._wire_segment(base_flags | TSEG_POLY_QUINTIC,
                           machine_duration, c['v'], c['a'], c['j'],
                           c['s'], c['c'], exec_duration=duration)
        return py_end_delta_ho(duration, c['v'], c['a'], c['j'],
                               c['s'], c['c'])


class TrajectoryQueuing:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.steppers = []
        self.timesync = None
        self.atlas_trace = None
        self.recovery_active = False
        self.recovery_trigger = None
        self.execution_grants = config.getboolean(
            'execution_grants', False)
        self.execution_grant_horizon = config.getfloat(
            'execution_grant_horizon', 1.5, minval=.5, maxval=5.)
        self.execution_grant_interval = config.getfloat(
            'execution_grant_interval', .250, minval=.050, maxval=1.)
        if self.execution_grant_interval * 2 >= self.execution_grant_horizon:
            raise config.error(
                "execution_grant_interval must be less than half of"
                " execution_grant_horizon")
        self.execution_group_id = config.getint(
            'execution_group_id', 1, minval=1, maxval=0xffffffff)
        epoch = secrets.randbits(64)
        self.execution_epoch_hi = epoch >> 32
        self.execution_epoch_lo = epoch & 0xffffffff
        self.group_members = {}
        self.group_sequence = 0
        self.group_pending = None
        self.group_next_proposal = 0.
        self.group_proposal_time = None
        self.group_committed_sequence = 0
        self.group_committed_until = None
        self.group_renewal_fault = None
        self.group_grant_ready = not self.execution_grants
        self.group_config_pending = None
        self.group_config_error = None
        self.recovery_grant_active = False
        self.group_timer = None
        # Normal G1 moves retain Klippy's coordinated Cartesian lookahead,
        # but trajectory steppers receive per-joint quintic intentions and
        # synthesize their own pulses on the MCU.  Quadratic remains an
        # explicit compatibility mode for firmware without higher order.
        self.g1_segment_order = config.getchoice(
            'g1_segment_order', {'quadratic': 0, 'quintic': 2}, 'quintic')
        self.printer.register_event_handler("klippy:connect",
                                            self._handle_connect)
        self.printer.register_event_handler("toolhead:check_move",
                                            self._handle_check_move)
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
        if self.execution_grants and eventtime is not None:
            self._execution_grant_valid(eventtime)
        return {'recovery_active': self.recovery_active,
                'recovery_trigger': self.recovery_trigger,
                'execution_grants': self.execution_grants,
                'execution_grant_ready': self.group_grant_ready,
                'execution_grant_fault': self.group_renewal_fault,
                'execution_recovery_grant': self.recovery_grant_active,
                'execution_config_pending':
                    self.group_config_pending is not None,
                'execution_config_error': self.group_config_error,
                'execution_group_id': self.execution_group_id,
                'execution_epoch': "%08x%08x" % (
                    self.execution_epoch_hi, self.execution_epoch_lo),
                'execution_sequence': self.group_committed_sequence,
                'execution_members': {
                    name: member.state
                    for name, member in self.group_members.items()},
                'trajectory_steppers': [ts.describe() for ts in self.steppers]}

    cmd_TRAJECTORY_STATUS_help = ("Report the state of every actuator on the"
                                  " trajectory-intention motion path")
    def cmd_TRAJECTORY_STATUS(self, gcmd):
        if not self.steppers:
            gcmd.respond_info("No steppers use 'motion_protocol: trajectory'")
            return
        lines = []
        if self.execution_grants:
            lines.append(
                "group=%u epoch=%08x%08x committed_sequence=%u ready=%d"
                " horizon=%.3fs interval=%.3fs"
                % (self.execution_group_id, self.execution_epoch_hi,
                   self.execution_epoch_lo, self.group_committed_sequence,
                   self.group_grant_ready, self.execution_grant_horizon,
                   self.execution_grant_interval))
        for ts in self.steppers:
            d = ts.describe()
            fw = ts.firmware_status()
            fw_text = ""
            if fw is not None:
                fw_text = (
                    " fw_flags=0x%02x queued=%d dropped=%d"
                    " horizon=%u fw_pos=%d"
                    % (fw['flags'], fw['queued'], fw['dropped'],
                       fw['horizon_clock'], fw['pos']))
                if 'pool_total' in fw:
                    fw_text += (
                        " pool_free=%d/%d slot_bytes=%d"
                        % (fw['pool_free'], fw['pool_total'],
                           fw['pool_slot_bytes']))
            lines.append(
                "%s: anchored=%d need_rebase=%d recovery_hold=%d"
                " higher_order=%d"
                " g1_order=%s pos=%d su (%.4f mm) su/mm=%.1f%s%s"
                % (d['name'], d['anchored'], d['need_rebase'],
                   d['recovery_hold'],
                   d['higher_order'], d['g1_segment_order'],
                   d['commanded_pos_su'],
                   d['commanded_pos_su'] / d['su_per_mm'], d['su_per_mm'],
                   " [homing volatile]" if d['homing_volatile'] else "",
                   fw_text))
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
        # Status and G-code are decimal millimeters while the held wire twin
        # retains fractional-microstep subunits.  Requiring one-subunit
        # equality makes a displayed P0 impossible to re-enter after normal
        # fitting residue.  Accept at most one physical microstep, then snap
        # to the authoritative held coordinate below.
        anchor_su = _snap_bezier_anchor(ctrl_su[0], cur)
        if anchor_su is None:
            raise gcmd.error("P0 (%.4f mm) must equal the current position"
                             " (%.4f mm) within one microstep - anchor the"
                             " move where the joint is"
                             % (pts_mm[0], cur / ts.su_per_mm))
        ctrl_su[0] = anchor_su
        self.printer.send_event(
            "trajectory_queuing:standalone_begin", ts.mcu)
        try:
            end_su = ts.bezier_move(duration, ctrl_su)
        finally:
            self.printer.send_event(
                "trajectory_queuing:standalone_end", ts.mcu)
        gcmd.respond_info(
            "BEZIER_MOVE %s: %d-point Bezier over %.3fs in %d wire"
            " segment(s), ended at %.4f mm."
            " Kinematic position is now stale - run SET_KINEMATIC_POSITION."
            % (name, len(pts_mm), duration, ts.last_bezier_segments,
               end_su / ts.su_per_mm))

    def register_stepper(self, mcu_stepper, config):
        ts = TrajectoryStepper(self, mcu_stepper, config)
        self.steppers.append(ts)
        mcu = mcu_stepper.get_mcu()
        if self.execution_grants and mcu.get_name() not in self.group_members:
            self.group_members[mcu.get_name()] = TrajectoryGroupMember(
                self, mcu)
        mcu.register_serial_response(
            lambda params, ts=ts: self._handle_underrun(ts, params),
            "traj_underrun oid=%c clock=%u pos=%i", mcu_stepper.get_oid())
        return ts

    def get_trajectory_steppers(self):
        return list(self.steppers)

    def get_machine_mcu(self):
        # FD-0001 defines machine time as the primary MCU's physical clock.
        # Segment coefficients and durations use this one domain on every
        # link; secondary firmware converts them once at ingest.
        return self.printer.lookup_object('mcu')

    def get_recovery_anchor_time(self):
        # Klipper print_time is shared across clock domains.  Choose the
        # latest live estimate and retain the normal Class-0 transmission
        # margin, so every per-board rebase names one common future instant.
        eventtime = self.printer.get_reactor().monotonic()
        estimates = [ts.mcu.estimated_print_time(eventtime)
                     for ts in self.steppers]
        return max(estimates) + BEZIER_QUEUE_MARGIN

    def complete_recovery_hold(self):
        for ts in self.steppers:
            ts.recovery_hold = False
        self.recovery_grant_active = False
        self.group_config_pending = None
        self.group_config_error = None
        self.recovery_active = False
        self.recovery_trigger = None

    def is_recovery_active(self):
        return self.recovery_active

    def is_mcu_synced(self, mcu):
        if self.timesync is None:
            return True
        return self.timesync.is_mcu_synced(mcu.get_name())

    def get_unsynced_mcus(self):
        # Recovery must not restart G-Code ingestion in the short interval
        # after transport reconnects but before the secondary machine-time
        # model is trustworthy again.  Return each participating trajectory
        # MCU once; the primary defines machine time and needs no discipline.
        machine_mcu = self.get_machine_mcu()
        return sorted(set(
            ts.mcu.get_name() for ts in self.steppers
            if ts.mcu is not machine_mcu and not self.is_mcu_synced(ts.mcu)))

    def _handle_check_move(self, move):
        # Fail before toolhead.move() commits this move to lookahead.  The
        # firmware Class-0 ingest gate remains authoritative, but reaching it
        # from a background flush turns an ordinary early G1 into a global
        # shutdown.  A relative trajectory joint participates only when its
        # extra axis moves.  A secondary kinematic joint is conservatively
        # treated as part of every Cartesian move because CoreXY/delta-style
        # coupling is not expressible as one axis_d index here.
        if getattr(self, 'recovery_active', False):
            raise self.printer.command_error(
                "HELIX trajectory recovery hold is active;"
                " use RESUME_MOTION to reconcile the all-MCU group")
        if (getattr(self, 'execution_grants', False)
                and not self._execution_grant_valid(
                    self.printer.get_reactor().monotonic())):
            raise self.printer.command_error(
                "HELIX execution group has no all-MCU grant;"
                " refusing move before lookahead")
        for ts in self.steppers:
            if ts.mcu is self.get_machine_mcu():
                continue
            participates = (move.axes_d[3] if ts.is_relative
                            else move.is_kinematic_move)
            if participates and not self.is_mcu_synced(ts.mcu):
                raise self.printer.command_error(
                    "Machine-time discipline for %s is not converged;"
                    " refusing move before lookahead (retry when"
                    " TIMESYNC_STATUS reports converged)"
                    % (ts.mcu.get_name(),))

    # Kinematics whose XY(Z) rails move together for ordinary motion, so
    # a paradigm split ACROSS rails is also a split coordination group.
    COUPLED_KINEMATICS = ('corexy', 'corexz', 'delta', 'deltesian',
                          'rotary_delta', 'winch', 'polar')

    def _validate_paradigm_groups(self):
        # FD-0001 doc 14: a coordination group must be single-paradigm —
        # steppers that execute one coordinated kinematic motion must be
        # either all trajectory (intent) or all legacy (firehose). Reject
        # the mixed topology at startup instead of producing a move with
        # two different failure behaviours.
        if not self.steppers:
            return
        traj_names = set(ts.name for ts in self.steppers)
        toolhead = self.printer.lookup_object('toolhead', None)
        if toolhead is None:
            return
        kin = toolhead.get_kinematics()
        rails = getattr(kin, 'rails', None) or []
        rail_kinds = []
        for rail in rails:
            names = [s.get_name() for s in rail.get_steppers()]
            in_traj = [n for n in names if n in traj_names]
            if in_traj and len(in_traj) != len(names):
                raise self.printer.config_error(
                    "trajectory_queuing: rail '%s' mixes trajectory and"
                    " legacy steppers (%s vs %s). A coordination group must"
                    " be single-paradigm (FD-0001 doc 14): give every"
                    " stepper of this rail motion_protocol: trajectory, or"
                    " none of them." % (names[0], ", ".join(in_traj),
                                        ", ".join(n for n in names
                                                  if n not in traj_names)))
            rail_kinds.append(bool(in_traj))
        kin_mod = type(kin).__module__.split('.')[-1]
        if kin_mod in self.COUPLED_KINEMATICS and len(set(rail_kinds)) > 1:
            raise self.printer.config_error(
                "trajectory_queuing: %s kinematics move their rails as one"
                " coordination group, but only some rails use"
                " motion_protocol: trajectory. A coordination group must be"
                " single-paradigm (FD-0001 doc 14): convert all of the"
                " kinematic rails, or none." % (kin_mod,))

    def _handle_connect(self):
        self._validate_paradigm_groups()
        self.timesync = self.printer.lookup_object('timesync', None)
        self.atlas_trace = self.printer.lookup_object('atlas_trace', None)
        for ts in self.steppers:
            ts.connect()
        if self.execution_grants:
            for member in self.group_members.values():
                member.connect()
                member.configure(
                    self.execution_group_id, self.execution_epoch_hi,
                    self.execution_epoch_lo)
            reactor = self.printer.get_reactor()
            self.group_timer = reactor.register_timer(
                self._grant_timer, reactor.monotonic() + .050)
        if self.steppers:
            mq = self.printer.lookup_object('motion_queuing')
            kin_flush_delay = TRAJECTORY_KIN_FLUSH_DELAY
            for ts in self.steppers:
                sk = ts.mcu_stepper.get_stepper_kinematics()
                kin_flush_delay = max(
                    kin_flush_delay,
                    ts.ffi_lib.itersolve_get_gen_steps_pre_active(sk),
                    ts.ffi_lib.itersolve_get_gen_steps_post_active(sk))
            mq.register_kin_flush_delay(kin_flush_delay)
            mq.register_flush_callback(self._flush)

    def _group_healthy(self):
        if self.group_renewal_fault is not None:
            return False
        if self.recovery_active and not self.recovery_grant_active:
            return False
        if self.group_config_pending is not None:
            return False
        for member in self.group_members.values():
            if member.is_paused() or not self.is_mcu_synced(member.mcu):
                return False
        return True

    def _begin_recovery_grant(self):
        # An expired firmware lease is deliberately closed forever.  Recovery
        # therefore starts a fresh epoch on every idle member instead of
        # attempting to extend the old, partially expired one.
        epoch = secrets.randbits(64)
        self.execution_epoch_hi = epoch >> 32
        self.execution_epoch_lo = epoch & 0xffffffff
        self.group_sequence = 0
        self.group_pending = None
        self.group_next_proposal = 0.
        self.group_proposal_time = None
        self.group_committed_sequence = 0
        self.group_committed_until = None
        self.group_renewal_fault = None
        self.group_grant_ready = False
        self.group_config_error = None
        self.recovery_grant_active = False
        self.group_config_pending = {'acked': set()}
        for member in self.group_members.values():
            member.configure(
                self.execution_group_id, self.execution_epoch_hi,
                self.execution_epoch_lo)

    def cancel_recovery_grant(self):
        # No normal motion is admitted while recovery_active remains set.
        # Stop proposing new leases as well; any already installed lease
        # expires harmlessly while the machine is idle.
        self.recovery_grant_active = False
        self.group_config_pending = None
        self.group_pending = None
        self.group_grant_ready = False

    def acquire_recovery_grant(self, timeout, info):
        if not self.execution_grants:
            return True
        if not self.recovery_active:
            return self._execution_grant_valid(
                self.printer.get_reactor().monotonic())
        if not self.group_members:
            info("resume DEFERRED: execution grants are enabled but the"
                 " all-MCU group has no members")
            return False
        self._begin_recovery_grant()
        reactor = self.printer.get_reactor()
        deadline = reactor.monotonic() + timeout
        info("establishing a fresh all-MCU execution epoch before rebase")
        while not self.group_grant_ready:
            if self.group_config_error is not None:
                error = self.group_config_error
                info("resume DEFERRED: %s rejected the recovery epoch"
                     " (reason=%u); motion remains held"
                     % (error['member'], error['reason']))
                self.cancel_recovery_grant()
                return False
            now = reactor.monotonic()
            if now >= deadline:
                pending = self.group_config_pending
                if pending is not None:
                    missing = sorted(
                        set(self.group_members) - pending['acked'])
                    detail = "epoch acknowledgement from %s" % (
                        ", ".join(missing),)
                elif self.group_pending is not None:
                    missing = sorted(
                        set(self.group_members)
                        - self.group_pending['acked'])
                    detail = "grant acknowledgement from %s" % (
                        ", ".join(missing),)
                else:
                    detail = "a coordinated grant proposal"
                info("resume DEFERRED: timed out waiting for %s;"
                     " motion remains held" % (detail,))
                self.cancel_recovery_grant()
                return False
            reactor.pause(min(deadline, now + .050))
        info("fresh all-MCU execution grant committed at sequence %u"
             % (self.group_committed_sequence,))
        return True

    def _group_motion_active(self, eventtime):
        try:
            machine_mcu = self.get_machine_mcu()
            current_clock = machine_mcu.print_time_to_clock(
                machine_mcu.estimated_print_time(eventtime))
            return any(
                ts.motion_horizon_clock is not None
                and ts.motion_horizon_clock > current_clock
                for ts in self.steppers)
        except Exception:
            # Failing closed here would prevent harmless idle reproposals on
            # reduced test implementations.  Live trajectory steppers always
            # publish a primary-machine-domain motion horizon.
            return False

    def _execution_grant_valid(self, eventtime):
        if not self.execution_grants:
            return True
        if not self.group_grant_ready or self.group_committed_until is None:
            return False
        current_time = max(
            member.mcu.estimated_print_time(eventtime)
            for member in self.group_members.values())
        if current_time < self.group_committed_until:
            return True
        self.group_grant_ready = False
        return False

    @staticmethod
    def _clock_is_after(new_clock, old_clock):
        delta = (int(new_clock) - int(old_clock)) & 0xffffffff
        return 0 < delta < 0x80000000

    def _advance_grant_past_member_clocks(self, grant_time):
        # Firmware rejects a new sequence unless its machine clock advances
        # modulo 32 bits.  A clock regression may settle after one MCU
        # accepted a proposal, so monotonic host print_time alone is
        # insufficient.  Raise the next proposal beyond every last accepted
        # machine clock reported for this epoch.  Each MCU derives its local
        # expiry from that clock with its onboard disciplined mapping.
        machine_mcu = self.get_machine_mcu()
        constraints = []
        for member in self.group_members.values():
            state = member.state
            if (state is None
                    or int(state.get('group_id', 0))
                    != self.execution_group_id
                    or int(state.get('epoch_hi', 0))
                    != self.execution_epoch_hi
                    or int(state.get('epoch_lo', 0))
                    != self.execution_epoch_lo
                    or not int(state.get('sequence', 0))):
                continue
            constraints.append(
                (machine_mcu, int(state.get('machine_clock', 0))))
        # One pass normally suffices.  Iterate because advancing print_time
        # for one clock domain can cross a 32-bit wrap in another.
        for _retry in range(4):
            advance = 0.
            for mcu, old_clock in constraints:
                new_clock = mcu.print_time_to_clock(grant_time) & 0xffffffff
                if self._clock_is_after(new_clock, old_clock):
                    continue
                one_second = (
                    mcu.print_time_to_clock(grant_time + 1.)
                    - mcu.print_time_to_clock(grant_time))
                frequency = abs(float(one_second))
                if frequency < 1.:
                    continue
                forward_ticks = (
                    (old_clock - new_clock) & 0xffffffff)
                advance = max(
                    advance, forward_ticks / frequency
                    + self.execution_grant_interval)
            if not advance:
                return grant_time
            grant_time += advance
        return grant_time

    def _grant_timer(self, eventtime):
        if not self.execution_grants:
            return self.printer.get_reactor().NEVER
        self._execution_grant_valid(eventtime)
        if not self._group_healthy():
            # Deliberately do not renew: every board already has the same
            # bounded stop contract. A missing member must not cause the
            # reachable subset to run farther ahead.
            self.group_grant_ready = False
            self.group_pending = None
            self.group_next_proposal = 0.
            return eventtime + self.execution_grant_interval
        if self.group_pending is None:
            if eventtime < self.group_next_proposal:
                return self.group_next_proposal
            self.group_sequence = (self.group_sequence + 1) & 0xffffffff
            if not self.group_sequence:
                self.group_sequence = 1
            # The primary MCU owns machine time.  A noisy secondary estimate
            # must never inflate the execution lease; firmware derives its
            # own local expiry from this primary-machine boundary.
            grant_time = (
                self.get_machine_mcu().estimated_print_time(eventtime)
                + self.execution_grant_horizon)
            # Startup clock fits can settle sharply backwards (notably on a
            # Wi-Fi MCU).  A subset of the group may already have accepted
            # the preceding proposal, so never let a fresh sequence move its
            # machine-time horizon backwards.  Keep the high-water mark for
            # every proposal, not only fully committed ones.
            if (self.group_proposal_time is not None
                    and grant_time <= self.group_proposal_time):
                grant_time = (self.group_proposal_time
                              + self.execution_grant_interval)
            grant_time = self._advance_grant_past_member_clocks(grant_time)
            self.group_proposal_time = grant_time
            machine_clock = self.get_machine_mcu().print_time_to_clock(
                grant_time) & 0xffffffff
            self.group_pending = {
                'sequence': self.group_sequence,
                'machine_clock': machine_clock,
                'grant_time': grant_time,
                'acked': set()}
        pending = self.group_pending
        for member in self.group_members.values():
            local_clock = member.mcu.print_time_to_clock(
                pending['grant_time'])
            member.grant(
                self.execution_group_id, self.execution_epoch_hi,
                self.execution_epoch_lo, pending['sequence'],
                pending['machine_clock'], local_clock)
        return eventtime + min(.100, self.execution_grant_interval)

    def _handle_group_state(self, member, state):
        if (int(state.get('group_id', 0)) != self.execution_group_id
                or int(state.get('epoch_hi', 0))
                != self.execution_epoch_hi
                or int(state.get('epoch_lo', 0))
                != self.execution_epoch_lo):
            return
        reject_reason = int(state.get('reject_reason', -1))
        config_pending = self.group_config_pending
        if config_pending is not None:
            if reject_reason != TGR_OK:
                self.group_config_error = {
                    'member': member.name, 'reason': reject_reason}
                return
            if (int(state.get('sequence', -1)) != 0
                    or not (int(state.get('flags', 0)) & TGF_CONFIGURED)
                    or int(state.get('flags', 0))
                    & (TGF_ARMED | TGF_EXPIRED)):
                return
            config_pending['acked'].add(member.name)
            if len(config_pending['acked']) != len(self.group_members):
                return
            self.group_config_pending = None
            self.recovery_grant_active = True
            reactor = self.printer.get_reactor()
            if self.group_timer is not None:
                reactor.update_timer(self.group_timer, reactor.NOW)
            return
        pending = self.group_pending
        if pending is None:
            return
        if reject_reason != TGR_OK:
            # A proposal may be accepted by one MCU while another MCU's
            # Class-0 gate is momentarily closed.  Retrying that same
            # future clock eventually turns it into a permanently stale
            # proposal.  Close host ingestion and create a new, later
            # sequence on the next timer pass.
            active = self._group_motion_active(
                self.printer.get_reactor().monotonic())
            if active:
                self.group_renewal_fault = {
                    'sequence': pending['sequence'],
                    'member': member.name,
                    'reason': reject_reason,
                }
                logging.error(
                    "HELIX execution grant %u rejected by %s (reason=%u)"
                    " during active motion; closing ingestion and allowing"
                    " the installed bounded leases to stop the group",
                    pending['sequence'], member.name, reject_reason)
            else:
                logging.warning(
                    "HELIX execution grant %u rejected by %s (reason=%u);"
                    " reproposing a fresh coordinated horizon while idle",
                    pending['sequence'], member.name, reject_reason)
            self.group_pending = None
            self.group_grant_ready = False
            reactor = self.printer.get_reactor()
            # Rate-limit idle reproposals to the normal renewal cadence.
            # Advancing a fresh proposal by one renewal interval every
            # 100ms response retry made its expiry race ahead of wall time;
            # after a long Wi-Fi qualification interval, a 64MHz MCU could
            # see the new local expiry more than half a 32-bit timer wrap
            # beyond its last accepted one and reject it as non-monotonic.
            self.group_next_proposal = (
                reactor.monotonic() + self.execution_grant_interval
                if not active else reactor.NEVER)
            return
        if (int(state.get('sequence', 0)) != pending['sequence']
                or int(state.get('machine_clock', 0))
                != pending['machine_clock']
                or not (int(state.get('flags', 0)) & TGF_ARMED)):
            return
        pending['acked'].add(member.name)
        if len(pending['acked']) != len(self.group_members):
            return
        self.group_committed_sequence = pending['sequence']
        self.group_committed_until = pending['grant_time']
        self.group_pending = None
        self.group_next_proposal = (
            self.printer.get_reactor().monotonic()
            + self.execution_grant_interval)
        self.group_grant_ready = True

    def _flush(self, flush_time, step_gen_time):
        for ts in self.steppers:
            ts.flush(flush_time, step_gen_time)

    def _handle_underrun(self, ts, params):
        # OIDs are MCU-local, so the response handler is bound to its exact
        # stepper at registration time.  Treat the stop as machine-wide:
        # another board may still have queued motion, but the host must not
        # extend any trajectory stream until their held positions agree at a
        # coordinated recovery boundary.
        ts.note_rebase_needed(stopped=True)
        first = not self.recovery_active
        if first:
            self.recovery_active = True
            self.recovery_grant_active = False
            self.group_config_pending = None
            self.group_config_error = None
            self.group_grant_ready = False
            self.group_pending = None
            self.recovery_trigger = {
                'mcu': ts.mcu.get_name(), 'joint': ts.name,
                'clock': int(params['clock']), 'pos': int(params['pos'])}
            for peer in self.steppers:
                peer.recovery_hold = True
        logging.warning(
            "Trajectory underrun on %s: clock=%d pos=%d; recovery hold %s",
            ts.name, params['clock'], params['pos'],
            "latched" if first else "already active")
        if first:
            self.printer.send_event(
                "trajectory_queuing:recovery_hold", self.recovery_trigger)

    def record_wire_intention(self, ts, fields):
        if self.atlas_trace is not None:
            self.atlas_trace.record_intention(ts.mcu, ts.name, ts.oid,
                                               fields)


def load_config(config):
    return TrajectoryQueuing(config)
