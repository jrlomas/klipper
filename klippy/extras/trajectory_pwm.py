# Trajectory PWM/DAC actuator: sampled non-stepper motion backend
#
# Configures the MCU-side sampled PWM/DAC trajectory backend
# (src/traj_pwm.c, FD-0001 doc 04) for a non-stepper actuator whose
# "position" is an output level - laser power, spindle speed, a hobby
# servo or an analog/PWM-driven axis.  The MCU samples the segment
# polynomial q(dt) = q0 + v*dt + 1/2*a*dt^2 at a fixed loop rate and
# writes the mapped duty cycle; this module owns the config command,
# the position anchor (trajectory_rebase) and the segment/hold command
# surface.
#
# Two host interfaces are provided:
#   * the direct path - rebase() the anchor and queue_segment() wire
#     spans by hand; and
#   * the fitter path - feed_value_trajectory() takes a piecewise-
#     linear value trajectory (print_time, value) and runs it through
#     the SAME C segfit fitter the stepper path uses (a private trapq
#     + kinematics stand in for the joint), so wire quantization and
#     chained-position exactness are shared, not reimplemented.
# The same entry point also accepts a scalar callback; that path preflights
# and bounds the complete sampled trajectory before touching the MCU.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging, math

SUBUNITS = 65536.
DEFAULT_SAMPLE_TIME = 0.001
DEFAULT_CYCLE_TIME = 0.100
DEFAULT_UNDERRUN_DECEL = 5000.  # native units/s^2
MAX_SEGMENT_DURATION = 1 << 26
MAX_VALUE_SEGMENTS = 256


def _round_away(value):
    return (int(math.floor(value + .5)) if value >= 0.
            else -int(math.floor(-value + .5)))


def plan_value_trajectory(duration, value_at, frequency,
                          sample_time=DEFAULT_SAMPLE_TIME):
    """Preflight a scalar trajectory into quadratic-protocol segments.

    ``value_at`` is called with offsets in seconds from zero through
    ``duration``. The emitted spans linearly interpolate those samples.
    Every span starts from the exact Q32.32 accumulator produced by the
    prior quantized span, so rounding error is corrected instead of
    accumulating unchecked. No MCU command is sent by this pure helper.
    """
    duration = float(duration)
    frequency = float(frequency)
    sample_time = float(sample_time)
    if duration <= 0. or frequency <= 0. or sample_time <= 0.:
        raise ValueError("duration, frequency, and sample_time must be positive")
    span_count = max(1, int(math.ceil(duration / sample_time)))
    span_count = min(span_count, max(1, int(math.floor(duration * frequency))))
    if span_count > MAX_VALUE_SEGMENTS:
        raise ValueError("value trajectory exceeds the 256-segment batch limit")
    offsets = [duration * i / span_count for i in range(span_count + 1)]
    ticks = [_round_away(offset * frequency) for offset in offsets]
    values = []
    for offset in offsets:
        value = float(value_at(offset))
        if not math.isfinite(value):
            raise ValueError("value trajectory returned a non-finite value")
        pos = _round_away(value * SUBUNITS)
        if pos < -2147483648 or pos > 2147483647:
            raise ValueError("value trajectory exceeds the signed wire range")
        values.append(pos)

    acc = values[0] << 32
    segments = []
    for index in range(1, len(values)):
        span_ticks = ticks[index] - ticks[index - 1]
        if span_ticks <= 0 or span_ticks >= MAX_SEGMENT_DURATION:
            raise ValueError("value trajectory produced an invalid segment duration")
        target = values[index] << 32
        velocity = _round_away((target - acc) / float(span_ticks << 16))
        if velocity < -2147483648 or velocity > 2147483647:
            raise ValueError("value trajectory exceeds the velocity wire range")
        acc += (velocity * span_ticks) << 16
        segments.append((span_ticks, velocity, 0))
    return {
        'start_pos': values[0],
        'end_pos': values[-1],
        'end_acc': acc,
        'end_error_su': (acc - (values[-1] << 32)) / float(1 << 32),
        'segments': segments,
    }


# Default fit tolerance for a VALUE channel, in sub-units: 1/256 of a
# native unit (~0.4% of full scale at full_scale=1.0) - comfortably
# below one duty LSB of a typical 8-bit-or-better PWM resolution.
DEFAULT_TOLERANCE_SU = SUBUNITS / 256.


class ValueTrajectoryFitter:
    """Fit a piecewise-linear VALUE trajectory into wire segments.

    Feeds a (print_time, value) polyline through the C segfit sampler -
    the same fitter/quantizer the stepper path uses - by standing up a
    private trapq + single-axis kinematics as the "joint".  The fitter
    maintains the exact Q32.32 chained anchor, so segments emitted here
    integrate on the MCU to the same sub-unit positions the host
    computed.  Pure chelper: no printer or MCU objects, so it is unit
    testable standalone (see test/traj_pwm_fitter_test.py).
    """
    def __init__(self, mcu_freq, tolerance_su=DEFAULT_TOLERANCE_SU,
                 sample_time=DEFAULT_SAMPLE_TIME):
        import chelper
        ffi_main, self.ffi_lib = chelper.get_ffi()
        self.tq = ffi_main.gc(self.ffi_lib.trapq_alloc(),
                              self.ffi_lib.trapq_free)
        # A plain cartesian 'x' solver: position(t) = the move's x - i.e.
        # the value itself.  The fitter only uses its trapq walk +
        # calc_position callback.
        self.sk = ffi_main.gc(self.ffi_lib.cartesian_stepper_alloc(b'x'),
                              self.ffi_lib.free)
        # step_dist is irrelevant for pure sampling (no step generation
        # runs on this solver); 1.0 keeps the kinematics well-formed.
        self.ffi_lib.itersolve_set_trapq(self.sk, self.tq, 1.)
        self.segfit = ffi_main.gc(self.ffi_lib.segfit_alloc(),
                                  self.ffi_lib.segfit_free)
        # su per native value unit is simply SUBUNITS (value 1.0 = 2^16).
        self.ffi_lib.segfit_setup(self.segfit, self.sk, mcu_freq,
                                  SUBUNITS, tolerance_su, sample_time)
        self.anchored = False
        self.end_time = 0.
        self.end_value = 0.

    def anchor(self, print_time, value):
        # Anchor the chained stream at (print_time, value).  Returns the
        # integer sub-unit position for the caller's rebase command.
        pos_su = int(round(value * SUBUNITS))
        self.ffi_lib.segfit_set_anchor(self.segfit, print_time,
                                       pos_su << 32)
        self.anchored = True
        self.end_time = print_time
        self.end_value = value
        return pos_su

    def position_su(self):
        # Exact integer sub-unit position of the chained anchor.
        return int(self.ffi_lib.segfit_get_anchor(self.segfit)) >> 32

    def feed(self, knots, emit):
        """Fit the polyline 'knots' ([(print_time, value), ...], strictly
        increasing times, knots[0] == the current anchor point) and call
        emit(duration, velocity, accel) for each wire segment.  Finalizes
        the tail span so the value lands on the last knot, and returns
        the exact end position in sub-units."""
        if not self.anchored:
            raise ValueError("value trajectory fed before anchor()")
        # Append each linear span as a cruise move on the private trapq.
        for (t0, v0), (t1, v1) in zip(knots[:-1], knots[1:]):
            dt = t1 - t0
            if dt <= 0.:
                raise ValueError("value trajectory times must be"
                                 " strictly increasing")
            dv = v1 - v0
            if dv >= 0.:
                axis_r, rate = 1., dv / dt
            else:
                axis_r, rate = -1., -dv / dt
            self.ffi_lib.trapq_append(self.tq, t0, 0., dt, 0.,
                                      v0, 0., 0., axis_r, 0., 0.,
                                      rate, rate, 0.)
        t_end = knots[-1][0]
        n = self.ffi_lib.segfit_generate(self.segfit, t_end)
        if n < 0:
            raise ValueError("value trajectory fit overflow")
        self._drain(n, emit)
        n = self.ffi_lib.segfit_finalize(self.segfit)
        if n < 0:
            raise ValueError("value trajectory fit overflow")
        self._drain(n, emit)
        self.ffi_lib.trapq_finalize_moves(self.tq, t_end + 1., t_end + 1.)
        self.end_time = t_end
        self.end_value = knots[-1][1]
        return self.position_su()

    def _drain(self, n, emit):
        segs = self.ffi_lib.segfit_get_segs(self.segfit)
        for i in range(n):
            s = segs[i]
            if s.duration:
                emit(s.duration, s.velocity, s.accel)


def subunit_to_duty(pos_su, scale, max_value):
    # Pure sub-unit position -> duty mapping, mirroring
    # traj_pwm_duty() in src/traj_pwm.c: duty = pos_su * max_value /
    # scale, clamped to [0, max_value].  Negative positions map to 0.
    # Kept a module-level pure function so it is unit testable without
    # a printer or MCU (see test/traj_pwm_map_test.py).
    if pos_su <= 0 or scale <= 0:
        return 0
    duty = (int(pos_su) * int(max_value)) // int(scale)
    if duty >= max_value:
        return int(max_value)
    return int(duty)


class TrajectoryPWM:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]
        ppins = self.printer.lookup_object('pins')
        pin_params = ppins.lookup_pin(config.get('pin'), can_invert=False)
        self.mcu = pin_params['chip']
        self._pin = pin_params['pin']
        self.cycle_time = config.getfloat(
            'cycle_time', DEFAULT_CYCLE_TIME, above=0.)
        self.sample_time = config.getfloat(
            'motion_sample_time', DEFAULT_SAMPLE_TIME, above=0.)
        # full_scale: native position (in the caller's units) that maps
        # to 100% output.  scale (sub-units per full-scale duty) is what
        # the MCU divides the sampled sub-unit position by.
        self.full_scale = config.getfloat('full_scale', 1., above=0.)
        # shutdown_value: fraction of full scale driven on machine
        # shutdown (FD-0001 doc 04 stop table "output to configured
        # shutdown value").
        self.shutdown_frac = config.getfloat(
            'shutdown_value', 0., minval=0., maxval=1.)
        self.underrun_decel = config.getfloat(
            'motion_underrun_decel', DEFAULT_UNDERRUN_DECEL, above=0.)
        # Fit tolerance for feed_value_trajectory(), in sub-units.
        self.tolerance_su = config.getfloat(
            'motion_tolerance', DEFAULT_TOLERANCE_SU, above=0.)
        self._fitter = None
        self.oid = None
        self.max_value = 0
        self.scale = int(self.full_scale * SUBUNITS + .5)
        self.queue_cmd = self.hold_cmd = None
        self.rebase_cmd = self.get_pos_cmd = None
        self.need_rebase = True
        self.last_plan = None
        self.mcu.register_config_callback(self._build_config)

    def _build_config(self):
        self.oid = self.mcu.create_oid()
        self.mcu.request_move_queue_slot()
        # Route this actuator's underrun events now that the oid exists.
        self.mcu.register_response(self._handle_underrun, "traj_underrun",
                                   self.oid)
        self.max_value = int(self.mcu.get_constant_float("PWM_MAX"))
        cycle_ticks = self.mcu.seconds_to_clock(self.cycle_time)
        sample_ticks = max(1, self.mcu.seconds_to_clock(self.sample_time))
        shutdown_value = int(self.shutdown_frac * self.max_value + .5)
        freq = self.mcu.seconds_to_clock(1.)
        # underrun_decel wire units: sub-units/tick^2, 32 fractional bits
        decel_wire = int(self.underrun_decel * SUBUNITS
                         / (freq * freq) * 2.**32 + .5)
        self.mcu.add_config_cmd(
            "config_traj_pwm oid=%d pin=%s cycle_ticks=%d sample_ticks=%d"
            " scale=%d shutdown_value=%d max_value=%d underrun_decel=%d"
            % (self.oid, self._pin, cycle_ticks, sample_ticks, self.scale,
               shutdown_value, self.max_value, decel_wire))
        cmd_queue = self.mcu.alloc_command_queue()
        self.queue_cmd = self.mcu.lookup_command(
            "queue_traj_pwm_segment oid=%c flags=%c duration=%u"
            " velocity=%i accel=%i", cq=cmd_queue)
        self.hold_cmd = self.mcu.lookup_command(
            "traj_pwm_hold oid=%c duration=%u", cq=cmd_queue)
        self.rebase_cmd = self.mcu.lookup_command(
            "traj_pwm_rebase oid=%c clock=%u pos=%i", cq=cmd_queue)
        self.get_pos_cmd = self.mcu.lookup_query_command(
            "traj_pwm_get_position oid=%c",
            "traj_position oid=%c clock=%u pos=%i", oid=self.oid,
            cq=cmd_queue)

    # ---- position anchor / segment feed API ------------------------

    def rebase(self, print_time, pos_native):
        # Anchor the chained position stream at pos_native (caller's
        # units) as of print_time.  Required before any segment.
        pos_su = int(round(pos_native * SUBUNITS))
        clock = self.mcu.print_time_to_clock(print_time)
        self.rebase_cmd.send([self.oid, clock & 0xffffffff, pos_su])
        self.need_rebase = False
        # A manual re-anchor invalidates the fitter's chained stream; the
        # next feed_value_trajectory() re-anchors at its first knot.
        if getattr(self, '_fitter', None) is not None:
            self._fitter.anchored = False

    def queue_segment(self, duration_ticks, velocity, accel, flags=0):
        # Queue one constant-acceleration span.  velocity is Q16.16
        # sub-units/tick, accel is sub-units/tick^2 (32 fractional
        # bits) - the same wire encoding the segment fitter emits.
        self.queue_cmd.send([self.oid, flags, duration_ticks,
                             velocity, accel])

    def hold(self, duration_ticks):
        self.hold_cmd.send([self.oid, duration_ticks])

    def get_position(self):
        params = self.get_pos_cmd.send([self.oid])
        return params['pos'] / SUBUNITS

    def feed_value_trajectory(self, trajectory, duration=None, value_at=None,
                              sample_time=None):
        """Queue either a polyline or a bounded sampled scalar function.

        The preferred form is ``feed_value_trajectory(knots)`` where knots
        is a list of ``(print_time, value_native)`` pairs. For callers that
        naturally provide a function, the compatible form is
        ``feed_value_trajectory(print_time, duration, value_at)``.
        """
        if duration is None and value_at is None:
            return self._feed_value_knots(trajectory)
        if duration is None or value_at is None:
            raise self.printer.command_error(
                "value trajectory requires knots or print_time, duration,"
                " value_at")
        return self._feed_sampled_value_trajectory(
            trajectory, duration, value_at, sample_time)

    def _feed_sampled_value_trajectory(self, print_time, duration, value_at,
                                       sample_time=None):
        """Queue a time-indexed native-unit value function.

        The complete plan is evaluated and range-checked before rebase, so
        a bad callback cannot leave a partially emitted trajectory. The
        final hold is intentional: without it, a non-zero terminal slope
        would correctly look like an underrun to the shared segment core.
        """
        if sample_time is None:
            sample_time = self.sample_time
        frequency = self.mcu.seconds_to_clock(1.)
        try:
            plan = plan_value_trajectory(
                duration, value_at, frequency, sample_time)
        except (TypeError, ValueError) as exc:
            raise self.printer.command_error(
                "invalid trajectory_pwm value trajectory: %s" % (exc,))
        self.rebase(print_time, plan['start_pos'] / SUBUNITS)
        for span_ticks, velocity, accel in plan['segments']:
            self.queue_segment(span_ticks, velocity, accel)
        # A one-sample terminal hold drains to an intentional idle state.
        terminal_ticks = plan['segments'][-1][0]
        self.hold(terminal_ticks)
        self.last_plan = plan
        return plan

    def _feed_value_knots(self, knots):
        # Fit a piecewise-linear value trajectory (list of
        # (print_time, value_native), strictly increasing times) through
        # the C segfit fitter and ship the segments - the natural
        # interface for laser/spindle raster where power tracks a
        # commanded value trajectory.  Anchors (rebases) automatically at
        # knots[0] when unanchored or after an underrun; a continuing
        # call must start where the previous one ended.  Returns the
        # exact end value in native units.
        if self.queue_cmd is None:
            raise self.printer.command_error(
                "trajectory_pwm %s is not configured yet" % (self.name,))
        if len(knots) < 2:
            raise self.printer.command_error(
                "value trajectory needs at least 2 (time, value) knots")
        if self._fitter is None:
            self._fitter = ValueTrajectoryFitter(
                self.mcu.seconds_to_clock(1.), self.tolerance_su,
                self.sample_time)
        t0, v0 = knots[0]
        if self.need_rebase or not self._fitter.anchored:
            clock = self.mcu.print_time_to_clock(t0)
            pos_su = self._fitter.anchor(t0, v0)
            self.rebase_cmd.send([self.oid, clock & 0xffffffff, pos_su])
            self.need_rebase = False
        elif (abs(t0 - self._fitter.end_time) > self.sample_time
              or abs(v0 - self._fitter.end_value) * SUBUNITS
              > self.tolerance_su):
            raise self.printer.command_error(
                "value trajectory discontinuity: chunk starts at"
                " (%.6f, %.4f) but the stream ended at (%.6f, %.4f) -"
                " rebase first" % (t0, v0, self._fitter.end_time,
                                   self._fitter.end_value))
        def emit(duration, velocity, accel):
            if not velocity and not accel:
                self.hold_cmd.send([self.oid, duration])
            else:
                self.queue_cmd.send([self.oid, 0, duration,
                                     velocity, accel])
        try:
            end_su = self._fitter.feed(knots, emit)
        except ValueError as e:
            raise self.printer.command_error(str(e))
        return end_su / SUBUNITS

    def _handle_underrun(self, params):
        self.need_rebase = True
        logging.warning("Trajectory PWM underrun on %s: clock=%d pos=%d",
                        self.name, params['clock'], params['pos'])

    def get_status(self, eventtime):
        return {
            'name': self.name,
            'need_rebase': self.need_rebase,
            'last_segment_count': (len(self.last_plan['segments'])
                                   if self.last_plan else 0),
            'last_endpoint_error_su': (self.last_plan['end_error_su']
                                       if self.last_plan else None),
        }


def load_config_prefix(config):
    return TrajectoryPWM(config)
