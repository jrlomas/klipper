# Tracking of PWM controlled heaters and their temperature control
#
# Copyright (C) 2016-2025  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import math, os, logging, threading


######################################################################
# Heater
######################################################################

KELVIN_TO_CELSIUS = -273.15
MAX_HEAT_TIME = 3.0
AMBIENT_TEMP = 25.
PID_PARAM_BASE = 255.
MAX_MAINTHREAD_TIME = 5.0
QUELL_STALE_TIME = 7.0
MIN_PWM_CHANGE_RATIO = 0.05

class Heater:
    def __init__(self, config, sensor):
        self.printer = config.get_printer()
        self.name = config.get_name()
        self.short_name = short_name = self.name.split()[-1]
        # Setup sensor
        self.sensor = sensor
        self.min_temp = config.getfloat('min_temp', minval=KELVIN_TO_CELSIUS)
        self.max_temp = config.getfloat('max_temp', above=self.min_temp)
        self.sensor.setup_minmax(self.min_temp, self.max_temp)
        self.sensor.setup_callback(self.temperature_callback)
        self.pwm_delay = self.sensor.get_report_time_delta()
        # Setup temperature checks
        self.min_extrude_temp = config.getfloat(
            'min_extrude_temp', 170.,
            minval=self.min_temp, maxval=self.max_temp)
        is_fileoutput = (self.printer.get_start_args().get('debugoutput')
                         is not None)
        self.can_extrude = self.min_extrude_temp <= 0. or is_fileoutput
        self.max_power = config.getfloat('max_power', 1., above=0., maxval=1.)
        self.min_pwm_change = self.max_power * MIN_PWM_CHANGE_RATIO
        self.smooth_time = config.getfloat('smooth_time', 1., above=0.)
        self.inv_smooth_time = 1. / self.smooth_time
        self.verify_mainthread_time = -999.
        self.lock = threading.Lock()
        self.last_temp = self.smoothed_temp = self.target_temp = 0.
        self.last_temp_time = 0.
        # pwm caching
        self.next_pwm_time = 0.
        self.last_pwm_value = 0.
        self.autonomous_hold = None
        # Setup control algorithm sub-class
        algos = {'watermark': ControlBangBang, 'pid': ControlPID,
                 'helix_pid': ControlHelixPID,
                 'helix_mpc': ControlHelixMPC}
        algo = config.getchoice('control', algos)
        self.control = algo(self, config)
        # Setup output heater pin
        heater_pin = config.get('heater_pin')
        ppins = self.printer.lookup_object('pins')
        self.mcu_pwm = ppins.setup_pin('pwm', heater_pin)
        pwm_cycle_time = config.getfloat('pwm_cycle_time', 0.100, above=0.,
                                         maxval=self.pwm_delay)
        self.mcu_pwm.setup_cycle_time(pwm_cycle_time)
        self.mcu_pwm.setup_max_duration(MAX_HEAT_TIME)
        self.mcu_heater_control = None
        if getattr(self.control, 'is_mcu_control', False):
            from . import heater_control
            self.mcu_heater_control = heater_control.MCUHeaterControl(
                config, self)
            self.control.attach_controller(self.mcu_heater_control)
        # Load additional modules
        self.printer.load_object(config, "verify_heater %s" % (short_name,))
        self.printer.load_object(config, "pid_calibrate")
        gcode = self.printer.lookup_object("gcode")
        gcode.register_mux_command("SET_HEATER_TEMPERATURE", "HEATER",
                                   short_name, self.cmd_SET_HEATER_TEMPERATURE,
                                   desc=self.cmd_SET_HEATER_TEMPERATURE_help)
        self.printer.register_event_handler("klippy:shutdown",
                                            self._handle_shutdown)
    def set_pwm(self, read_time, value):
        if (self.mcu_heater_control is not None
                and not getattr(self.control, 'is_mcu_control', False)):
            # Host-side calibration/test controllers request duty, but the
            # MCU still owns the physical PWM and its independent cutoffs.
            self.mcu_heater_control.set_manual_output(value)
            return
        if self.autonomous_hold is not None:
            # An engaged/expired MCU holder owns the physical pin until an
            # explicit release.  In addition, discard historical PID output
            # produced while the host catches up on buffered ADC reports
            # after a stall.  Sending those old timestamps would turn a
            # successful autonomous hold into an MCU "Timer too close"
            # shutdown as soon as Klippy resumed.
            if self.autonomous_hold.blocks_host_pwm():
                self.next_pwm_time = 0.
                self.last_pwm_value = 0.
                return
            pwm_time = read_time + self.pwm_delay
            mcu = self.mcu_pwm.get_mcu()
            eventtime = self.printer.get_reactor().monotonic()
            min_lead = .25 * mcu.min_schedule_time()
            if pwm_time < mcu.estimated_print_time(eventtime) + min_lead:
                self.next_pwm_time = 0.
                self.last_pwm_value = 0.
                return
        if self.target_temp <= 0. or read_time > self.verify_mainthread_time:
            value = 0.
        if ((read_time < self.next_pwm_time or not self.last_pwm_value)
            and abs(value - self.last_pwm_value) < self.min_pwm_change):
            # No significant change in value - can suppress update
            return
        pwm_time = read_time + self.pwm_delay
        self.next_pwm_time = (pwm_time + MAX_HEAT_TIME
                              - (3. * self.pwm_delay + 0.001))
        self.last_pwm_value = value
        self.mcu_pwm.set_pwm(pwm_time, value)
        #logging.debug("%s: pwm=%.3f@%.3f (from %.3f@%.3f [%.3f])",
        #              self.name, value, pwm_time,
        #              self.last_temp, self.last_temp_time, self.target_temp)
    def temperature_callback(self, read_time, temp):
        with self.lock:
            time_diff = read_time - self.last_temp_time
            self.last_temp = temp
            self.last_temp_time = read_time
            self.control.temperature_update(read_time, temp, self.target_temp)
            temp_diff = temp - self.smoothed_temp
            adj_time = min(time_diff * self.inv_smooth_time, 1.)
            self.smoothed_temp += temp_diff * adj_time
            self.can_extrude = (self.smoothed_temp >= self.min_extrude_temp)
        #logging.debug("temp: %.3f %f = %f", read_time, temp)
    def _handle_shutdown(self):
        self.verify_mainthread_time = -999.
    def setup_autonomous_hold(self, hold):
        if self.mcu_heater_control is not None:
            raise self.printer.config_error(
                "failure_policy: hold is incompatible with control:"
                " helix_pid or helix_mpc; these already own autonomous"
                " control")
        self.autonomous_hold = hold
        # The bounded holder replaces the legacy host-refresh watchdog.
        self.mcu_pwm.setup_max_duration(0.)
    # External commands
    def get_name(self):
        return self.name
    def get_pwm_delay(self):
        return self.pwm_delay
    def get_max_power(self):
        return self.max_power
    def get_smooth_time(self):
        return self.smooth_time
    def set_temp(self, degrees):
        if degrees and (degrees < self.min_temp or degrees > self.max_temp):
            raise self.printer.command_error(
                "Requested temperature (%.1f) out of range (%.1f:%.1f)"
                % (degrees, self.min_temp, self.max_temp))
        with self.lock:
            self.target_temp = degrees
            target_changed = getattr(self.control, 'target_changed', None)
            if target_changed is not None:
                target_changed(degrees)
            elif self.mcu_heater_control is not None:
                ceiling = getattr(self.control, 'manual_ceiling', None)
                self.mcu_heater_control.set_manual_guard(degrees, ceiling)
    def get_temp(self, eventtime):
        est_print_time = self.mcu_pwm.get_mcu().estimated_print_time(eventtime)
        quell_time = est_print_time - QUELL_STALE_TIME
        with self.lock:
            if self.last_temp_time < quell_time:
                return 0., self.target_temp
            return self.smoothed_temp, self.target_temp
    def check_busy(self, eventtime):
        with self.lock:
            return self.control.check_busy(
                eventtime, self.smoothed_temp, self.target_temp)
    def set_control(self, control):
        with self.lock:
            old_control = self.control
            self.control = control
            self.target_temp = 0.
            deactivate = getattr(old_control, 'deactivate', None)
            if deactivate is not None:
                deactivate()
            activate = getattr(control, 'activate', None)
            if activate is not None:
                activate(0.)
        return old_control
    def alter_target(self, target_temp):
        if target_temp:
            target_temp = max(self.min_temp, min(self.max_temp, target_temp))
        self.target_temp = target_temp
        target_changed = getattr(self.control, 'target_changed', None)
        if target_changed is not None:
            target_changed(target_temp)
        elif self.mcu_heater_control is not None:
            ceiling = getattr(self.control, 'manual_ceiling', None)
            self.mcu_heater_control.set_manual_guard(target_temp, ceiling)
    def stats(self, eventtime):
        est_print_time = self.mcu_pwm.get_mcu().estimated_print_time(eventtime)
        if not self.printer.is_shutdown():
            self.verify_mainthread_time = est_print_time + MAX_MAINTHREAD_TIME
        with self.lock:
            target_temp = self.target_temp
            last_temp = self.last_temp
            last_pwm_value = self.last_pwm_value
        is_active = target_temp or last_temp > 50.
        return is_active, '%s: target=%.0f temp=%.1f pwm=%.3f' % (
            self.short_name, target_temp, last_temp, last_pwm_value)
    def get_status(self, eventtime):
        with self.lock:
            target_temp = self.target_temp
            smoothed_temp = self.smoothed_temp
            last_pwm_value = self.last_pwm_value
        status = {'temperature': round(smoothed_temp, 2),
                  'target': target_temp, 'power': last_pwm_value}
        if self.mcu_heater_control is not None:
            status['mcu_control'] = self.mcu_heater_control.get_status()
        else:
            control_status = getattr(self.control, 'get_status', None)
            if control_status is not None:
                status['control_stats'] = control_status()
        return status
    cmd_SET_HEATER_TEMPERATURE_help = "Sets a heater temperature"
    def cmd_SET_HEATER_TEMPERATURE(self, gcmd):
        temp = gcmd.get_float('TARGET', 0.)
        pheaters = self.printer.lookup_object('heaters')
        pheaters.set_temperature(self, temp)


######################################################################
# Bang-bang control algo
######################################################################

class ControlBangBang:
    def __init__(self, heater, config):
        self.heater = heater
        self.heater_max_power = heater.get_max_power()
        self.max_delta = config.getfloat('max_delta', 2.0, above=0.)
        self.heating = False
    def temperature_update(self, read_time, temp, target_temp):
        if self.heating and temp >= target_temp+self.max_delta:
            self.heating = False
        elif not self.heating and temp <= target_temp-self.max_delta:
            self.heating = True
        if self.heating:
            self.heater.set_pwm(read_time, self.heater_max_power)
        else:
            self.heater.set_pwm(read_time, 0.)
    def check_busy(self, eventtime, smoothed_temp, target_temp):
        return smoothed_temp < target_temp-self.max_delta


######################################################################
# Proportional Integral Derivative (PID) control algo
######################################################################

PID_SETTLE_DELTA = 1.
PID_SETTLE_SLOPE = .1

class ControlPID:
    def __init__(self, heater, config):
        gains = (config.getfloat('pid_Kp'), config.getfloat('pid_Ki'),
                 config.getfloat('pid_Kd'))
        self._init_gains(heater, gains)

    @classmethod
    def from_gains(cls, heater, gains):
        control = cls.__new__(cls)
        control._init_gains(heater, gains)
        return control

    def _init_gains(self, heater, gains):
        self.heater = heater
        self.heater_max_power = heater.get_max_power()
        self.Kp, self.Ki, self.Kd = [gain / PID_PARAM_BASE
                                     for gain in gains]
        self.min_deriv_time = heater.get_smooth_time()
        self.temp_integ_max = 0.
        if self.Ki:
            self.temp_integ_max = self.heater_max_power / self.Ki
        self.prev_temp = AMBIENT_TEMP
        self.prev_temp_time = 0.
        self.prev_temp_deriv = 0.
        self.loop_samples = 0
        self.loop_clock = 0.
        self.loop_dt_count = 0
        self.loop_dt_mean = self.loop_dt_m2 = 0.
        self.loop_dt_min = self.loop_dt_max = None
        self.prev_temp_integ = 0.
    def temperature_update(self, read_time, temp, target_temp):
        self.loop_samples += 1
        loop_clock = self.heater.printer.get_reactor().monotonic()
        if self.loop_clock:
            dt = loop_clock - self.loop_clock
            self.loop_dt_count += 1
            delta = dt - self.loop_dt_mean
            self.loop_dt_mean += delta / self.loop_dt_count
            self.loop_dt_m2 += delta * (dt - self.loop_dt_mean)
            self.loop_dt_min = (dt if self.loop_dt_min is None
                                else min(self.loop_dt_min, dt))
            self.loop_dt_max = (dt if self.loop_dt_max is None
                                else max(self.loop_dt_max, dt))
        self.loop_clock = loop_clock
        time_diff = read_time - self.prev_temp_time
        # Calculate change of temperature
        temp_diff = temp - self.prev_temp
        if time_diff >= self.min_deriv_time:
            temp_deriv = temp_diff / time_diff
        else:
            temp_deriv = (self.prev_temp_deriv * (self.min_deriv_time-time_diff)
                          + temp_diff) / self.min_deriv_time
        # Calculate accumulated temperature "error"
        temp_err = target_temp - temp
        temp_integ = self.prev_temp_integ + temp_err * time_diff
        temp_integ = max(0., min(self.temp_integ_max, temp_integ))
        # Calculate output
        co = self.Kp*temp_err + self.Ki*temp_integ - self.Kd*temp_deriv
        #logging.debug("pid: %f@%.3f -> diff=%f deriv=%f err=%f integ=%f co=%d",
        #    temp, read_time, temp_diff, temp_deriv, temp_err, temp_integ, co)
        bounded_co = max(0., min(self.heater_max_power, co))
        self.heater.set_pwm(read_time, bounded_co)
        # Store state for next measurement
        self.prev_temp = temp
        self.prev_temp_time = read_time
        self.prev_temp_deriv = temp_deriv
        if co == bounded_co:
            self.prev_temp_integ = temp_integ
    def check_busy(self, eventtime, smoothed_temp, target_temp):
        temp_diff = target_temp - smoothed_temp
        return (abs(temp_diff) > PID_SETTLE_DELTA
                or abs(self.prev_temp_deriv) > PID_SETTLE_SLOPE)
    def get_status(self):
        variance = (self.loop_dt_m2 / self.loop_dt_count
                    if self.loop_dt_count else 0.)
        return {
            'state': 'host',
            'samples': self.loop_samples,
            'loop_clock': self.loop_clock,
            'loop_clock_frequency': 1.,
            'loop_clock_source': 'host',
            'loop_dt_count': self.loop_dt_count,
            'loop_dt_mean': self.loop_dt_mean,
            'loop_dt_stddev': math.sqrt(max(0., variance)),
            'loop_dt_min': self.loop_dt_min or 0.,
            'loop_dt_max': self.loop_dt_max or 0.,
        }


######################################################################
# Host-executed predictive control (physical qualification)
######################################################################

class ControlPredictive:
    """Floating-point reference for guarded physical MPC qualification."""
    control_kind = 'predictive'

    @classmethod
    def from_model(cls, heater, period, model, tuning, ambient,
                   selection=None):
        control = cls.__new__(cls)
        control.heater = heater
        control.period = period
        control.gain = model['gain']
        control.tau = model['tau']
        control.delay = model['delay']
        control.horizon = tuning['horizon']
        control.effort = tuning['effort_penalty']
        control.integral_gain = tuning['integral_gain']
        control.observer_time = tuning['observer_time']
        control.output_slew_rate = tuning['output_slew_rate']
        control.control_band = tuning['control_band']
        control.ambient = ambient
        control.selection = selection
        control.model_status = dict(model)
        control.model_status.update({
            'horizon': control.horizon,
            'effort_penalty': control.effort,
            'integral_gain': control.integral_gain,
            'observer_time': control.observer_time,
            'output_slew_rate': control.output_slew_rate,
            'control_band': control.control_band,
        })
        control.max_output = heater.get_max_power()
        control.retention = math.exp(-control.horizon / control.tau)
        response_horizon = max(period, control.horizon - control.delay)
        control.response = control.gain * (
            1. - math.exp(-response_horizon / control.tau))
        control.observer_alpha = 1. - math.exp(
            -period / control.observer_time)
        control.max_step = control.output_slew_rate * period
        control.filtered = None
        control.bias = control.output = 0.
        control.rebase_output = False
        control.approach_active = True
        control.approach_blend = 1.
        control.last_target = 0.
        control.prev_temp = AMBIENT_TEMP
        control.prev_temp_time = 0.
        control.prev_temp_deriv = 0.
        control.loop_samples = 0
        control.loop_clock = 0.
        control.loop_dt_count = 0
        control.loop_dt_mean = control.loop_dt_m2 = 0.
        control.loop_dt_min = control.loop_dt_max = None
        return control

    def _record_timing(self):
        self.loop_samples += 1
        loop_clock = self.heater.printer.get_reactor().monotonic()
        if self.loop_clock:
            dt = loop_clock - self.loop_clock
            self.loop_dt_count += 1
            delta = dt - self.loop_dt_mean
            self.loop_dt_mean += delta / self.loop_dt_count
            self.loop_dt_m2 += delta * (dt - self.loop_dt_mean)
            self.loop_dt_min = (dt if self.loop_dt_min is None
                                else min(self.loop_dt_min, dt))
            self.loop_dt_max = (dt if self.loop_dt_max is None
                                else max(self.loop_dt_max, dt))
        self.loop_clock = loop_clock

    def temperature_update(self, read_time, temp, target_temp):
        self._record_timing()
        if self.prev_temp_time:
            time_diff = read_time - self.prev_temp_time
            if time_diff > 0.:
                self.prev_temp_deriv = (temp - self.prev_temp) / time_diff
        self.prev_temp = temp
        self.prev_temp_time = read_time
        if target_temp != self.last_target:
            self.last_target = target_temp
            self.approach_active = True
            self.approach_blend = 1.
            self.filtered = None
            self.bias = 0.
            self.rebase_output = False
        error = target_temp - temp
        if not target_temp:
            self.output = self.bias = 0.
            self.filtered = None
            self.rebase_output = False
            self.approach_active = True
            self.approach_blend = 1.
        elif abs(error) >= 2. * self.control_band:
            self.approach_active = True
            self.approach_blend = 1.
            desired = self.max_output if error > 0. else 0.
            low = max(0., self.output - self.max_step)
            high = min(self.max_output, self.output + self.max_step)
            self.output = max(low, min(high, desired))
            self.filtered = None
            self.bias = 0.
            # Entering the predictive band is an ordinary control transition,
            # not a model reconfiguration.  Carrying the full-power approach
            # output into the model as a bias makes that bias unwind only at
            # the integral rate and defeats predictive braking.  The explicit
            # output slew bound already makes this handoff continuous.
            self.rebase_output = False
        else:
            # Continuously cross-fade over [band, 2*band].  At the outer edge
            # approach owns the requested duty; at the inner edge prediction
            # owns it.  The final slew clamp remains independently binding.
            blend = max(0., min(
                1., (abs(error) - self.control_band) / self.control_band))
            self.approach_blend = blend
            self.approach_active = bool(blend)
            desired = self.max_output if error > 0. else 0.
            if self.filtered is None:
                self.filtered = temp
            else:
                self.filtered += self.observer_alpha * (
                    temp - self.filtered)
            free_temp = (self.ambient + self.retention
                         * (self.filtered - self.ambient))
            residual = target_temp - free_temp
            response_sq = self.response * self.response
            effort_sq = self.effort * self.effort
            model_output = ((self.response * residual
                             + effort_sq * self.output)
                            / (response_sq + effort_sq))
            if self.rebase_output:
                self.bias = self.output - model_output
                self.rebase_output = False
            filtered_error = target_temp - self.filtered
            bias_candidate = max(-self.max_output, min(
                self.max_output,
                self.bias + self.integral_gain * self.period
                * filtered_error))
            low = max(0., self.output - self.max_step)
            high = min(self.max_output, self.output + self.max_step)
            candidate = (blend * desired
                         + (1. - blend) * (model_output + bias_candidate))
            if ((low <= candidate <= high)
                    or (candidate > high and filtered_error < 0.)
                    or (candidate < low and filtered_error > 0.)):
                self.bias = bias_candidate
            blended_output = (blend * desired
                              + (1. - blend) * (model_output + self.bias))
            self.output = max(low, min(high, blended_output))
        self.heater.set_pwm(read_time, self.output)

    def check_busy(self, eventtime, smoothed_temp, target_temp):
        return (abs(target_temp - smoothed_temp) > PID_SETTLE_DELTA
                or abs(self.prev_temp_deriv) > PID_SETTLE_SLOPE)

    def deactivate(self):
        self.heater.set_pwm(self.prev_temp_time, 0.)

    def get_status(self):
        variance = (self.loop_dt_m2 / self.loop_dt_count
                    if self.loop_dt_count else 0.)
        return {
            'state': 'host',
            'samples': self.loop_samples,
            'loop_clock': self.loop_clock,
            'loop_clock_frequency': 1.,
            'loop_clock_source': 'host',
            'loop_dt_count': self.loop_dt_count,
            'loop_dt_mean': self.loop_dt_mean,
            'loop_dt_stddev': math.sqrt(max(0., variance)),
            'loop_dt_min': self.loop_dt_min or 0.,
            'loop_dt_max': self.loop_dt_max or 0.,
            'host_predictive_output': self.output,
            'host_predictive_bias': self.bias,
            'host_predictive_filtered_temperature': self.filtered,
            'host_predictive_ambient': self.ambient,
            'host_predictive_model': dict(self.model_status),
            'host_predictive_approach_active': self.approach_active,
            'host_predictive_approach_blend': self.approach_blend,
        }


######################################################################
# MCU-executed PID control
######################################################################

class ControlHelixPID:
    is_mcu_control = True
    control_kind = 'pid'
    def __init__(self, heater, config):
        self.heater = heater
        # Consume and validate the standard PID fields here.  The helper
        # converts them to fixed-period MCU coefficients after the sensor and
        # PWM objects are available.
        config.getfloat('pid_Kp')
        config.getfloat('pid_Ki')
        config.getfloat('pid_Kd')
        self.controller = None
        self.prev_temp = AMBIENT_TEMP
        self.prev_time = 0.
        self.temp_deriv = 0.
    def attach_controller(self, controller):
        self.controller = controller
    def target_changed(self, target_temp):
        if self.controller is not None:
            self.controller.set_target(target_temp)
    def deactivate(self):
        if self.controller is not None:
            self.controller.set_target(0.)
            self.controller.set_manual_guard(0.)
            self.controller.set_manual_output(0.)
    def activate(self, target_temp):
        if self.controller is not None:
            self.controller.set_manual_guard(0.)
            self.controller.set_target(target_temp)
    def temperature_update(self, read_time, temp, target_temp):
        if self.prev_time:
            dt = read_time - self.prev_time
            if dt > 0.:
                self.temp_deriv = (temp - self.prev_temp) / dt
        self.prev_temp = temp
        self.prev_time = read_time
    def check_busy(self, eventtime, smoothed_temp, target_temp):
        return (abs(target_temp - smoothed_temp) > PID_SETTLE_DELTA
                or abs(self.temp_deriv) > PID_SETTLE_SLOPE)


class ControlHelixMPC(ControlHelixPID):
    """Host facade for the MCU predictive thermal controller."""
    control_kind = 'predictive'

    def __init__(self, heater, config):
        # Retain explicit PID gains as the qualification/fallback baseline,
        # but do not use them in the predictive MCU loop.
        super().__init__(heater, config)
        config.getfloat('thermal_model_gain', above=0.)
        config.getfloat('thermal_model_tau', above=0.)

    def temperature_update(self, read_time, temp, target_temp):
        super().temperature_update(read_time, temp, target_temp)
        if self.controller is not None:
            self.controller.observe_temperature(temp, target_temp)


######################################################################
# Sensor and heater lookup
######################################################################

class PrinterHeaters:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.sensor_factories = {}
        self.heaters = {}
        self.gcode_id_to_sensor = {}
        self.available_heaters = []
        self.available_sensors = []
        self.available_monitors = []
        self.has_started = self.have_load_sensors = False
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        self.printer.register_event_handler("gcode:request_restart",
                                            self.turn_off_all_heaters)
        # Register commands
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command("TURN_OFF_HEATERS", self.cmd_TURN_OFF_HEATERS,
                               desc=self.cmd_TURN_OFF_HEATERS_help)
        gcode.register_command("M105", self.cmd_M105, when_not_ready=True)
    def load_config(self, config):
        self.have_load_sensors = True
        # Load default temperature sensors
        pconfig = self.printer.lookup_object('configfile')
        dir_name = os.path.dirname(__file__)
        filename = os.path.join(dir_name, 'temperature_sensors.cfg')
        try:
            dconfig = pconfig.read_config(filename)
        except Exception:
            logging.exception("Unable to load temperature_sensors.cfg")
            raise config.error("Cannot load config '%s'" % (filename,))
        for c in dconfig.get_prefix_sections(''):
            self.printer.load_object(dconfig, c.get_name())
    def add_sensor_factory(self, sensor_type, sensor_factory):
        self.sensor_factories[sensor_type] = sensor_factory
    def setup_heater(self, config, gcode_id=None):
        heater_name = config.get_name().split()[-1]
        if heater_name in self.heaters:
            raise config.error("Heater %s already registered" % (heater_name,))
        # Setup sensor
        sensor = self.setup_sensor(config)
        # Create heater
        self.heaters[heater_name] = heater = Heater(config, sensor)
        self.register_sensor(config, heater, gcode_id)
        self.available_heaters.append(config.get_name())
        return heater
    def get_all_heaters(self):
        return self.available_heaters
    def lookup_heater(self, heater_name):
        if heater_name not in self.heaters:
            raise self.printer.config_error(
                "Unknown heater '%s'" % (heater_name,))
        return self.heaters[heater_name]
    def setup_sensor(self, config):
        if not self.have_load_sensors:
            self.load_config(config)
        sensor_type = config.get('sensor_type')
        if sensor_type not in self.sensor_factories:
            raise self.printer.config_error(
                "Unknown temperature sensor '%s'" % (sensor_type,))
        return self.sensor_factories[sensor_type](config)
    def register_sensor(self, config, psensor, gcode_id=None):
        sensor_name = config.get_name()
        self.available_sensors.append(sensor_name)
        gcode = self.printer.lookup_object('gcode')
        gcode.register_mux_command('TEMPERATURE_WAIT', "SENSOR", sensor_name,
                                   self.cmd_TEMPERATURE_WAIT,
                                   desc=self.cmd_TEMPERATURE_WAIT_help)
        if gcode_id is None:
            gcode_id = config.get('gcode_id', None)
            if gcode_id is None:
                return
        if gcode_id in self.gcode_id_to_sensor:
            raise self.printer.config_error(
                "G-Code sensor id %s already registered" % (gcode_id,))
        self.gcode_id_to_sensor[gcode_id] = psensor
    def register_monitor(self, config):
        self.available_monitors.append(config.get_name())
    def get_status(self, eventtime):
        return {'available_heaters': self.available_heaters,
                'available_sensors': self.available_sensors,
                'available_monitors': self.available_monitors}
    def turn_off_all_heaters(self, print_time=0.):
        for heater in self.heaters.values():
            heater.set_temp(0.)
    cmd_TURN_OFF_HEATERS_help = "Turn off all heaters"
    def cmd_TURN_OFF_HEATERS(self, gcmd):
        self.turn_off_all_heaters()
    # G-Code M105 temperature reporting
    def _handle_ready(self):
        self.has_started = True
    def _get_temp(self, eventtime):
        # Tn:XXX /YYY B:XXX /YYY
        out = []
        if self.has_started:
            for gcode_id, sensor in sorted(self.gcode_id_to_sensor.items()):
                cur, target = sensor.get_temp(eventtime)
                out.append("%s:%.1f /%.1f" % (gcode_id, cur, target))
        if not out:
            return "T:0"
        return " ".join(out)
    def cmd_M105(self, gcmd):
        # Get Extruder Temperature
        reactor = self.printer.get_reactor()
        msg = self._get_temp(reactor.monotonic())
        did_ack = gcmd.ack(msg)
        if not did_ack:
            gcmd.respond_raw(msg)
    def _wait_for_temperature(self, heater):
        # Helper to wait on heater.check_busy() and report M105 temperatures
        if self.printer.get_start_args().get('debugoutput') is not None:
            return
        toolhead = self.printer.lookup_object("toolhead")
        gcode = self.printer.lookup_object("gcode")
        reactor = self.printer.get_reactor()
        eventtime = reactor.monotonic()
        while not self.printer.is_shutdown() and heater.check_busy(eventtime):
            print_time = toolhead.get_last_move_time()
            gcode.respond_raw(self._get_temp(eventtime))
            eventtime = reactor.pause(eventtime + 1.)
    def set_temperature(self, heater, temp, wait=False):
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.register_lookahead_callback((lambda pt: None))
        heater.set_temp(temp)
        if wait and temp:
            self._wait_for_temperature(heater)
    cmd_TEMPERATURE_WAIT_help = "Wait for a temperature on a sensor"
    def cmd_TEMPERATURE_WAIT(self, gcmd):
        sensor_name = gcmd.get('SENSOR')
        min_temp = gcmd.get_float('MINIMUM', float('-inf'))
        max_temp = gcmd.get_float('MAXIMUM', float('inf'), above=min_temp)
        if min_temp == float('-inf') and max_temp == float('inf'):
            raise gcmd.error(
                "Error on 'TEMPERATURE_WAIT': missing MINIMUM or MAXIMUM.")
        if self.printer.get_start_args().get('debugoutput') is not None:
            return
        if sensor_name in self.heaters:
            sensor = self.heaters[sensor_name]
        else:
            sensor = self.printer.lookup_object(sensor_name)
        toolhead = self.printer.lookup_object("toolhead")
        reactor = self.printer.get_reactor()
        eventtime = reactor.monotonic()
        while not self.printer.is_shutdown():
            temp, target = sensor.get_temp(eventtime)
            if temp >= min_temp and temp <= max_temp:
                return
            print_time = toolhead.get_last_move_time()
            gcmd.respond_raw(self._get_temp(eventtime))
            eventtime = reactor.pause(eventtime + 1.)

def load_config(config):
    return PrinterHeaters(config)
