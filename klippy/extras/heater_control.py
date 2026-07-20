# Host configuration and supervision for the MCU-autonomous heater loop.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import json, logging, math, os

from . import heater_profiles


GAIN_SHIFT = 20
ALPHA_ONE = 32768
OUTPUT_ONE = 65535
HC_STATE_NAMES = {
    0: 'disabled', 1: 'ready', 2: 'active', 3: 'autonomous',
    4: 'manual', 5: 'fault',
}
HC_FAULT_NAMES = {
    1: 'sensor range', 2: 'sample timeout', 4: 'maximum temperature',
    8: 'autonomous duration', 16: 'heating rate',
}


class MCUHeaterControl:
    def __init__(self, config, heater):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.heater = heater
        self.name = heater.short_name
        sensor = heater.sensor
        if not hasattr(sensor, 'mcu_adc') or not hasattr(sensor, 'adc_convert'):
            raise config.error(
                "control: helix_pid requires an ADC temperature sensor")
        self.mcu_adc = sensor.mcu_adc
        self.adc_convert = sensor.adc_convert
        self.mcu = self.mcu_adc.get_mcu()
        if heater.mcu_pwm.get_mcu() is not self.mcu:
            raise config.error(
                "control: helix_pid requires heater and sensor on one MCU")
        if heater.mcu_pwm.is_hardware_pwm():
            raise config.error(
                "control: helix_pid currently requires software PWM")
        heater.mcu_pwm.setup_max_duration(0.)

        # The merged DMA manager owns acquisition.  The local controller binds
        # to its pre-EWMA boxcar output; host telemetry remains independent.
        self.mcu_adc.setup_adc_stream(report_class=1)
        self.manager = self.mcu._helix_adc_stream_manager
        self.oid = self.mcu.create_oid()
        self.mcu.register_config_callback(self._build_config)

        self.period = sensor.get_report_time_delta()
        self.host_timeout = config.getfloat(
            'heater_control_host_timeout', 5., above=1.)
        self.autonomous_max_duration = config.getfloat(
            'heater_control_autonomous_max_duration', 3600., above=0.)
        self.sample_deadline = config.getfloat(
            'heater_control_sample_deadline', max(1., 3. * self.period),
            above=self.period)
        derivative_time = config.getfloat(
            'pid_derivative_filter', heater.get_smooth_time(), above=0.)
        self.derivative_alpha = 1. - math.exp(-self.period / derivative_time)
        verify = config.getsection('verify_heater %s' % (self.name,))
        self.verify_hysteresis = verify.getfloat(
            'hysteresis', 5., minval=0.)
        self.verify_max_error = verify.getfloat(
            'max_error', 120., minval=0.)
        self.verify_heating_gain = verify.getfloat(
            'heating_gain', 2., above=0.)
        default_gain_time = 60. if self.name == 'heater_bed' else 20.
        self.verify_gain_time = verify.getfloat(
            'check_gain_time', default_gain_time, minval=1.)

        self.base_pid = (
            config.getfloat('pid_Kp', minval=0.),
            config.getfloat('pid_Ki', minval=0.),
            config.getfloat('pid_Kd', minval=0.))
        self.kp, self.ki, self.kd = [value / 255.
                                     for value in self.base_pid]
        self.profile_manager = HeaterProfileManager(config, self)
        self.active_pid = self.base_pid
        self.active_profile_source = 'base'
        self.active_profile_raw_pid = self.base_pid
        self.active_profile_clamped = ()
        self._commands_ready = False
        self.set_target_cmd = self.set_profile_cmd = None
        self.manual_cmd = self.manual_guard_cmd = None
        self.ping_cmd = None
        self.query_cmd = self.timing_cmd = self.clear_cmd = None
        self.state = 1
        self.fault = 0
        self.fault_reported = False
        self.output = 0.
        self.samples = 0
        self.last_temp = 0.
        self.last_temp_valid = False
        self.last_sample_clock = 0
        self.last_run_clock = 0
        self.clock_frequency = 0
        self.loop_dt_count = 0
        self.loop_dt_mean = self.loop_dt_m2 = 0.
        self.loop_dt_min = self.loop_dt_max = None
        self.ping_timer = self.reactor.register_timer(self._ping_event)
        self.printer.register_event_handler('klippy:ready', self._handle_ready)
        self.printer.register_event_handler(
            'klippy:shutdown', self._handle_shutdown)

        gcode = self.printer.lookup_object('gcode')
        gcode.register_mux_command(
            'HEATER_CONTROL_STATUS', 'HEATER', self.name,
            self.cmd_HEATER_CONTROL_STATUS,
            desc=self.cmd_HEATER_CONTROL_STATUS_help)
        gcode.register_mux_command(
            'HEATER_CONTROL_CLEAR', 'HEATER', self.name,
            self.cmd_HEATER_CONTROL_CLEAR,
            desc=self.cmd_HEATER_CONTROL_CLEAR_help)
        self.profile_manager.register_commands(gcode)

    def _q20(self, value, option):
        result = int(value * (1 << GAIN_SHIFT) + .5)
        if not -0x80000000 <= result <= 0x7fffffff:
            raise self.printer.config_error(
                "%s is outside the MCU fixed-point range" % (option,))
        return result

    def _temp_to_adc(self, temp):
        adc_max = int(self.mcu.get_constant_float('ADC_MAX')
                      * self.manager.get_hardware_scale())
        value = self.adc_convert.calc_adc(temp)
        return max(0, min(adc_max, int(value * adc_max + .5)))

    def _target_parameters(self, temp):
        adc_max = (float(self.mcu.get_constant_float('ADC_MAX'))
                   * self.manager.get_hardware_scale())
        delta = min(.5, max(.05, .25 * (self.heater.max_temp
                                        - self.heater.min_temp)))
        lower = max(self.heater.min_temp, temp - delta)
        upper = min(self.heater.max_temp, temp + delta)
        if upper <= lower:
            raise self.printer.command_error(
                "Unable to linearize heater sensor at %.3fC" % (temp,))
        adc_lo = self.adc_convert.calc_adc(lower) * adc_max
        adc_hi = self.adc_convert.calc_adc(upper) * adc_max
        counts_per_degree = (adc_hi - adc_lo) / (upper - lower)
        if abs(counts_per_degree) < 1.e-6:
            raise self.printer.command_error(
                "Heater sensor slope is zero at %.3fC" % (temp,))
        slope_q16 = int((1000. / counts_per_degree) * (1 << 16))
        if not -0x80000000 <= slope_q16 <= 0x7fffffff:
            raise self.printer.command_error(
                "Heater sensor slope is outside the MCU fixed-point range")
        target_adc = self._temp_to_adc(temp)
        if not target_adc:
            raise self.printer.command_error(
                "Heater target maps to reserved ADC value zero")
        return target_adc, int(temp * 1000. + .5), slope_q16

    def _build_config(self):
        if self.mcu.get_constants().get('HEATER_CONTROL_V2') != 1:
            raise self.printer.config_error(
                "MCU '%s' lacks HEATER_CONTROL_V2 dynamic profiles" % (
                    self.mcu.get_name(),))
        stream_oid, subscription = self.manager.get_local_binding(self.mcu_adc)
        # Clock conversion is only valid after MCU identify/configuration.
        self.clock_frequency = self.mcu.seconds_to_clock(1.)
        pwm = self.heater.mcu_pwm
        # The MCU controller replaces the prompt host-refresh watchdog and
        # exclusively owns the already-configured software-PWM GPIO.
        cycle_ticks = self.mcu.seconds_to_clock(pwm.get_cycle_time())
        self.mcu.add_config_cmd(
            "config_heater_control oid=%d heater_pin=%s invert=%d"
            " cycle_ticks=%d" % (
                self.oid, pwm.get_pin(), pwm.get_invert(), cycle_ticks))

        adc_values = [self._temp_to_adc(t) for t in
                      (self.heater.min_temp, self.heater.max_temp)]
        min_adc, max_adc = min(adc_values), max(adc_values)
        max_temp_adc = self._temp_to_adc(self.heater.max_temp)
        invert_sense = int(adc_values[1] > adc_values[0])
        max_output = int(self.heater.max_power * OUTPUT_ONE + .5)
        kp_q20 = self._q20(self.kp, 'pid_Kp')
        ki_q20 = self._q20(self.ki * self.period, 'pid_Ki')
        kd_q20 = self._q20(self.kd / self.period, 'pid_Kd')
        alpha_q15 = max(1, min(ALPHA_ONE,
                               int(self.derivative_alpha * ALPHA_ONE + .5)))
        self.mcu.add_config_cmd(
            "heater_control_setup oid=%d min_adc=%d max_adc=%d"
            " max_temp_adc=%d invert_sense=%d sample_deadline=%d"
            " host_timeout=%d loop_period=%d autonomous_max_samples=%d"
            " max_output=%d"
            " kp_q20=%d ki_step_q20=%d kd_step_q20=%d d_alpha_q15=%d" % (
                self.oid, min_adc, max_adc, max_temp_adc, invert_sense,
                self.mcu.seconds_to_clock(self.sample_deadline),
                self.mcu.seconds_to_clock(self.host_timeout),
                self.mcu.seconds_to_clock(self.period),
                max(1, int(self.autonomous_max_duration / self.period + .5)),
                max_output, kp_q20, ki_q20, kd_q20, alpha_q15))
        max_error_mdeg_ms = int(self.verify_max_error * 1000000. + .5)
        if max_error_mdeg_ms > 0xffffffff:
            raise self.printer.config_error(
                "verify_heater max_error exceeds MCU representation")
        self.mcu.add_config_cmd(
            "heater_control_set_verify oid=%d period_ms=%d"
            " hysteresis_mdeg=%d max_error_mdeg_ms=%d"
            " heating_gain_mdeg=%d gain_samples=%d" % (
                self.oid, max(1, int(self.period * 1000. + .5)),
                int(self.verify_hysteresis * 1000. + .5),
                max(1, max_error_mdeg_ms),
                int(self.verify_heating_gain * 1000. + .5),
                max(1, int(self.verify_gain_time / self.period + .5))))
        self.mcu.add_config_cmd(
            "heater_control_bind oid=%d stream_oid=%d sub=%d" % (
                self.oid, stream_oid, subscription))

        cq = self.mcu.alloc_command_queue()
        self.set_target_cmd = self.mcu.lookup_command(
            "heater_control_set_target oid=%c target_adc=%u"
            " target_mdeg=%i slope_q16=%i", cq=cq)
        self.set_profile_cmd = self.mcu.lookup_command(
            "heater_control_set_profile oid=%c kp_q20=%i"
            " ki_step_q20=%i kd_step_q20=%i d_alpha_q15=%hu", cq=cq)
        self.manual_cmd = self.mcu.lookup_command(
            "heater_control_set_manual oid=%c output=%hu", cq=cq)
        self.manual_guard_cmd = self.mcu.lookup_command(
            "heater_control_set_manual_guard oid=%c guard_adc=%u"
            " guard_mdeg=%i slope_q16=%i ceiling_adc=%u", cq=cq)
        self.ping_cmd = self.mcu.lookup_command(
            "heater_control_ping oid=%c", cq=cq)
        self.clear_cmd = self.mcu.lookup_command(
            "heater_control_clear_fault oid=%c", cq=cq)
        state_fmt = ("heater_control_state oid=%c state=%c fault=%c adc=%u"
                     " target_adc=%u temp_mdeg=%i output=%hu samples=%u"
                     " last_sample=%u last_run=%u")
        self.query_cmd = self.mcu.lookup_query_command(
            "heater_control_query oid=%c", state_fmt, oid=self.oid, cq=cq)
        fault_fmt = ("heater_control_fault_event oid=%c state=%c fault=%c"
                     " adc=%u target_adc=%u temp_mdeg=%i output=%hu"
                     " samples=%u last_sample=%u last_run=%u")
        self.mcu.register_serial_response(
            self._handle_state, fault_fmt, self.oid)
        timing_fmt = ("heater_control_timing oid=%c count=%u min_us=%i"
                      " max_us=%i sum_lo=%u sum_hi=%i sumsq_lo=%u"
                      " sumsq_hi=%u period_ticks=%u")
        self.timing_cmd = self.mcu.lookup_query_command(
            "heater_control_query_timing oid=%c", timing_fmt,
            oid=self.oid, cq=cq)
        self._commands_ready = True
        logging.info(
            "MCU '%s' autonomous heater '%s': period=%.3fs host_timeout=%.3fs"
            " autonomous_max=%.1fs alpha=%.5f",
            self.mcu.get_name(), self.name, self.period, self.host_timeout,
            self.autonomous_max_duration, self.derivative_alpha)

    def _handle_ready(self):
        self.reactor.update_timer(self.ping_timer, self.reactor.NOW)
        self.set_target(self.heater.target_temp)

    def _handle_shutdown(self):
        self.reactor.update_timer(self.ping_timer, self.reactor.NEVER)

    def _ping_event(self, eventtime):
        if self.ping_cmd is not None and not self.printer.is_shutdown():
            # Liveness must precede synchronous observability queries.  A
            # delayed query response must not make a healthy controller enter
            # autonomous mode merely because telemetry is slow.
            self.ping_cmd.send([self.oid])
            self._handle_state(self.query_cmd.send([self.oid]))
            self._handle_timing(self.timing_cmd.send([self.oid]))
        return eventtime + 1.

    def _handle_state(self, params):
        self.state = params['state']
        self.fault = params['fault']
        self.output = params['output'] / float(OUTPUT_ONE)
        self.samples = params['samples']
        self.last_temp = params['temp_mdeg'] / 1000.
        target = self.heater.target_temp
        self.last_temp_valid = (
            self.state in (2, 3, 4) and target > 0.
            and abs(self.last_temp - target) <= 5.)
        self.last_sample_clock = params['last_sample']
        self.last_run_clock = params['last_run']
        self.heater.last_pwm_value = self.output
        if not self.fault:
            self.fault_reported = False
        elif not self.fault_reported and not self.printer.is_shutdown():
            self.fault_reported = True
            reasons = [name for bit, name in HC_FAULT_NAMES.items()
                       if self.fault & bit]
            self.printer.invoke_async_shutdown(
                "MCU heater '%s' fault: %s (0x%x)" % (
                    self.name, ', '.join(reasons) or 'unknown', self.fault))

    def _handle_timing(self, params):
        count = params['count']
        sum_value = params['sum_lo'] | ((params['sum_hi'] & 0xffffffff) << 32)
        if sum_value & (1 << 63):
            sum_value -= 1 << 64
        sumsq = params['sumsq_lo'] | (params['sumsq_hi'] << 32)
        mean_us = sum_value / float(count) if count else 0.
        variance_us = (sumsq / float(count) - mean_us * mean_us
                       if count else 0.)
        period = params['period_ticks'] / float(self.clock_frequency)
        self.loop_dt_count = count
        self.loop_dt_mean = period + mean_us * 1.e-6
        self.loop_dt_m2 = max(0., variance_us) * count * 1.e-12
        self.loop_dt_min = period + params['min_us'] * 1.e-6 if count else 0.
        self.loop_dt_max = period + params['max_us'] * 1.e-6 if count else 0.

    def set_target(self, temp):
        if self.set_target_cmd is None:
            return
        if temp <= 0.:
            self.set_target_cmd.send([self.oid, 0, 0, 0])
            return
        selection = self.profile_manager.select(temp)
        gains = selection['gains']
        self._set_profile(gains)
        self.active_profile_source = selection['source']
        self.active_profile_raw_pid = tuple(
            selection['raw_gains'][name]
            for name in heater_profiles.GAIN_NAMES)
        self.active_profile_clamped = tuple(selection['clamped_gains'])
        target_adc, target_mdeg, slope_q16 = self._target_parameters(temp)
        self.set_target_cmd.send(
            [self.oid, target_adc, target_mdeg, slope_q16])

    def _set_profile(self, gains):
        requested = tuple(float(gains[name])
                          for name in heater_profiles.GAIN_NAMES)
        if requested == self.active_pid or self.set_profile_cmd is None:
            self.active_pid = requested
            return
        kp, ki, kd = [value / 255. for value in requested]
        alpha_q15 = max(1, min(ALPHA_ONE,
                               int(self.derivative_alpha * ALPHA_ONE + .5)))
        self.set_profile_cmd.send([
            self.oid, self._q20(kp, 'pid_Kp'),
            self._q20(ki * self.period, 'pid_Ki'),
            self._q20(kd / self.period, 'pid_Kd'), alpha_q15])
        self.active_pid = requested

    def set_manual_output(self, value):
        if self.manual_cmd is None:
            return
        output = max(0, min(OUTPUT_ONE, int(value * OUTPUT_ONE + .5)))
        self.manual_cmd.send([self.oid, output])
        self.output = output / float(OUTPUT_ONE)
        self.heater.last_pwm_value = self.output

    def set_manual_guard(self, temp, ceiling=None):
        if self.manual_guard_cmd is None:
            return
        if temp <= 0.:
            self.manual_guard_cmd.send([self.oid, 0, 0, 0, 0])
            return
        target_adc, target_mdeg, slope_q16 = self._target_parameters(temp)
        if ceiling is None:
            ceiling = self.heater.max_temp
        ceiling_adc = self._temp_to_adc(ceiling)
        self.manual_guard_cmd.send(
            [self.oid, target_adc, target_mdeg, slope_q16, ceiling_adc])

    def query(self):
        if self.query_cmd is not None:
            self._handle_state(self.query_cmd.send([self.oid]))
            self._handle_timing(self.timing_cmd.send([self.oid]))
        return self.get_status()

    def get_status(self):
        variance = (self.loop_dt_m2 / self.loop_dt_count
                    if self.loop_dt_count else 0.)
        return {
            'state': HC_STATE_NAMES.get(self.state, 'unknown'),
            'fault': self.fault,
            'power': self.output,
            'samples': self.samples,
            # Firmware uses a target-local thermistor tangent, not the full
            # nonlinear host sensor model. Never present a stale or distant
            # tangent estimate as an independently measured temperature.
            'mcu_temperature': (self.last_temp
                                if self.last_temp_valid else None),
            'mcu_temperature_estimate': self.last_temp,
            'mcu_temperature_valid': self.last_temp_valid,
            'last_sample_clock': self.last_sample_clock,
            'loop_clock': self.last_run_clock,
            'loop_clock_frequency': self.clock_frequency,
            'loop_clock_source': 'mcu',
            'loop_dt_count': self.loop_dt_count,
            'loop_dt_mean': self.loop_dt_mean,
            'loop_dt_stddev': math.sqrt(max(0., variance)),
            'loop_dt_min': self.loop_dt_min or 0.,
            'loop_dt_max': self.loop_dt_max or 0.,
            'host_configured': self._commands_ready,
            'pid_gains': dict(zip(heater_profiles.GAIN_NAMES,
                                  self.active_pid)),
            'pid_profile_raw_gains': dict(zip(
                heater_profiles.GAIN_NAMES, self.active_profile_raw_pid)),
            'pid_profile_bounded': bool(self.active_profile_clamped),
            'pid_profile_clamped_gains': list(
                self.active_profile_clamped),
            'pid_profile_source': self.active_profile_source,
            'pid_profile_model': self.profile_manager.model.kind,
            'pid_profile_generation': self.profile_manager.store.data[
                'generation'],
        }

    cmd_HEATER_CONTROL_STATUS_help = "Report the MCU heater controller state"
    def cmd_HEATER_CONTROL_STATUS(self, gcmd):
        status = self.query()
        estimate = ("%.3f" % status['mcu_temperature']
                    if status['mcu_temperature_valid'] else
                    "n/a (local tangent %.3f)" % (
                        status['mcu_temperature_estimate'],))
        gcmd.respond_info(
            "%s: state=%s fault=0x%x power=%.4f samples=%d temp=%s "
            "profile=%s bounded=%s" % (
                self.name, status['state'], status['fault'], status['power'],
                status['samples'], estimate, status['pid_profile_source'],
                (','.join(status['pid_profile_clamped_gains'])
                 if status['pid_profile_bounded'] else 'no')))

    cmd_HEATER_CONTROL_CLEAR_help = "Clear a latched MCU heater fault"
    def cmd_HEATER_CONTROL_CLEAR(self, gcmd):
        if self.heater.target_temp:
            raise gcmd.error("Set the heater target to zero before clearing")
        if self.clear_cmd is not None:
            self.clear_cmd.send([self.oid])
        status = self.query()
        if status['fault']:
            raise gcmd.error(
                "MCU heater fault remains latched (0x%x)" % status['fault'])
        gcmd.respond_info("%s MCU heater fault cleared" % (self.name,))


class HeaterProfileManager:
    """Host policy and G-Code interface for one MCU-controlled heater."""
    def __init__(self, config, controller):
        self.printer = config.get_printer()
        self.controller = controller
        self.heater = controller.heater
        self.name = controller.name
        config_dir = os.path.dirname(os.path.abspath(
            self.printer.get_start_args()['config_file']))
        path = config.get('heater_pid_profile_path',
                          'helix_heater_profiles.json')
        if not os.path.isabs(path):
            path = os.path.join(config_dir, path)
        store = self.printer.lookup_object('heater_profile_store', None)
        if store is None:
            try:
                store = heater_profiles.HeaterProfileStore(path)
            except ValueError as exc:
                raise config.error(str(exc))
            self.printer.add_object('heater_profile_store', store)
            store.path = os.path.abspath(path)
        elif store.path != os.path.abspath(path):
            raise config.error(
                'All helix_pid heaters must use one heater_pid_profile_path')
        self.store = store
        self.enabled = config.getboolean('heater_pid_gain_schedule', True)
        self.min_ratio = config.getfloat(
            'heater_pid_gain_min_ratio', .25, above=0.)
        self.max_ratio = config.getfloat(
            'heater_pid_gain_max_ratio', 4., above=self.min_ratio)
        self.context_sensor_name = config.get(
            'heater_pid_context_sensor', None)
        self.model = None
        self._rebuild()

    def _rebuild(self):
        self.model = heater_profiles.HeaterGainModel(
            self.store.runs(self.name), self.controller.base_pid,
            (self.min_ratio, self.max_ratio))

    def register_commands(self, gcode):
        commands = [
            ('HELIX_PID_PROFILE_STATUS', self.cmd_STATUS,
             'Show stored Helix PID characterization runs'),
            ('HELIX_PID_PROFILE_COEFFICIENTS', self.cmd_COEFFICIENTS,
             'Show the fitted Helix PID gain model'),
            ('HELIX_PID_PROFILE_VALIDATE', self.cmd_VALIDATE,
             'Validate or reject a stored Helix PID run'),
            ('HELIX_PID_PROFILE_CLEAR', self.cmd_CLEAR,
             'Clear stored Helix PID characterization data'),
            ('HELIX_PID_PROFILE_RETRAIN', self.cmd_RETRAIN,
             'Run PID calibration at an ascending target series'),
        ]
        for command, callback, desc in commands:
            gcode.register_mux_command(
                command, 'HEATER', self.name, callback, desc=desc)

    def _context_temp(self):
        if not self.context_sensor_name:
            return None
        sensor = self.printer.lookup_object(self.context_sensor_name, None)
        if sensor is None:
            return None
        status = sensor.get_status(self.printer.get_reactor().monotonic())
        value = status.get('temperature')
        return None if value is None else float(value)

    def select(self, target):
        if not self.enabled:
            return {'gains': dict(zip(heater_profiles.GAIN_NAMES,
                                      self.controller.base_pid)),
                    'source': 'disabled', 'model': self.model.kind}
        return self.model.select(target, self._context_temp())

    def record_tune(self, target, gains, calibrate, method='relay_zn'):
        samples = getattr(calibrate, 'temp_samples', [])
        peaks = getattr(calibrate, 'peaks', [])
        record = {
            'target': float(target),
            'context_temp': self._context_temp(),
            'gains': dict(zip(heater_profiles.GAIN_NAMES,
                              [float(value) for value in gains])),
            'method': method,
            'firmware': self.printer.get_start_args().get(
                'software_version', 'unknown'),
            'evidence': {
                'sample_count': len(samples),
                'peak_count': len(peaks),
                'peaks': [[float(temp), float(stamp)]
                          for temp, stamp in peaks],
                'pwm_transitions': len(getattr(
                    calibrate, 'pwm_samples', [])),
                'start_temp': float(samples[0][1]) if samples else None,
                'minimum_temp': min([sample[1] for sample in samples])
                                if samples else None,
                'maximum_temp': max([sample[1] for sample in samples])
                                if samples else None,
                'duration': (float(samples[-1][0] - samples[0][0])
                             if len(samples) > 1 else 0.),
                'relay_powers': [float(value) for value in getattr(
                    calibrate, 'powers', [])],
                'relay_biases': [float(value) for value in getattr(
                    calibrate, 'biases', [])],
                'relay_deltas': [float(value) for value in getattr(
                    calibrate, 'deltas', [])],
                'relay_cycles': list(getattr(
                    calibrate, 'cycle_metrics', [])),
                'ultimate': getattr(calibrate, 'ultimate', None),
            },
        }
        result = self.store.add_run(self.name, record)
        self._rebuild()
        return result

    def _require_confirmation(self, gcmd, expected='YES'):
        if gcmd.get('CONFIRM', '').strip().upper() != expected:
            raise gcmd.error('Destructive operation requires CONFIRM=%s'
                             % (expected,))

    def _run_lines(self, limit=None):
        records = self.store.runs(self.name)
        if limit is not None:
            records = records[-limit:]
        lines = []
        for run in records:
            gains = run.get('gains', {})
            lines.append(
                '%s status=%s target=%.2fC context=%s method=%s '
                'Kp=%.3f Ki=%.3f Kd=%.3f samples=%d peaks=%d' % (
                    run.get('id', '?'), run.get('status', 'candidate'),
                    run.get('target', 0.),
                    ('n/a' if run.get('context_temp') is None else
                     '%.2fC' % run['context_temp']),
                    run.get('method', 'unknown'), gains.get('kp', 0.),
                    gains.get('ki', 0.), gains.get('kd', 0.),
                    run.get('evidence', {}).get('sample_count', 0),
                    run.get('evidence', {}).get('peak_count', 0)))
        return lines

    def cmd_STATUS(self, gcmd):
        limit = gcmd.get_int('RUNS', 20, minval=1, maxval=200)
        records = self.store.runs(self.name)
        counts = {state: len([run for run in records
                              if run.get('status') == state])
                  for state in ('candidate', 'validated', 'rejected')}
        lines = [
            '%s PID profiles: generation=%d model=%s runs=%d '
            'candidate=%d validated=%d rejected=%d active=%s' % (
                self.name, self.store.data['generation'], self.model.kind,
                len(records), counts['candidate'], counts['validated'],
                counts['rejected'], self.controller.active_profile_source)]
        if self.controller.active_profile_clamped:
            lines.append(
                'Active selection bounded gains=%s raw=%s applied=%s' % (
                    ','.join(self.controller.active_profile_clamped),
                    json.dumps(dict(zip(
                        heater_profiles.GAIN_NAMES,
                        self.controller.active_profile_raw_pid)),
                        sort_keys=True),
                    json.dumps(dict(zip(
                        heater_profiles.GAIN_NAMES,
                        self.controller.active_pid)), sort_keys=True)))
        lines.extend(self._run_lines(limit) or ['No stored runs'])
        gcmd.respond_info('\n'.join(lines))

    def cmd_COEFFICIENTS(self, gcmd):
        gcmd.respond_info('%s PID gain model:\n%s' % (
            self.name, json.dumps(self.model.status(), sort_keys=True,
                                  indent=2)))

    def cmd_VALIDATE(self, gcmd):
        run_id = gcmd.get('RUN')
        status = gcmd.get('STATUS', 'VALIDATED').strip().lower()
        if status not in ('validated', 'rejected'):
            raise gcmd.error('STATUS must be VALIDATED or REJECTED')
        self._require_confirmation(gcmd)
        try:
            record = self.store.set_status(self.name, run_id, status)
        except KeyError:
            raise gcmd.error("Unknown heater run '%s'" % (run_id,))
        except (OSError, ValueError) as exc:
            raise gcmd.error('Unable to update PID characterization: %s'
                             % (exc,))
        self._rebuild()
        gcmd.respond_info('%s run %s marked %s; model=%s' % (
            self.name, record['id'], status, self.model.kind))

    def cmd_CLEAR(self, gcmd):
        self._require_confirmation(gcmd)
        if self.heater.target_temp:
            raise gcmd.error('Set the heater target to zero before clearing')
        try:
            changed = self.store.clear(self.name)
        except (OSError, ValueError) as exc:
            raise gcmd.error('Unable to clear PID characterization: %s'
                             % (exc,))
        self._rebuild()
        gcmd.respond_info('%s PID characterization data %s' % (
            self.name, 'cleared' if changed else 'was already empty'))

    def cmd_RETRAIN(self, gcmd):
        if self.heater.target_temp:
            raise gcmd.error('Set the heater target to zero before retraining')
        targets_raw = gcmd.get('TARGETS')
        try:
            targets = [float(item.strip())
                       for item in targets_raw.split(',') if item.strip()]
        except ValueError:
            raise gcmd.error('TARGETS must be comma-separated temperatures')
        if not targets or targets != sorted(set(targets)):
            raise gcmd.error('TARGETS must be unique and ascending')
        if any(target < self.heater.min_temp
               or target > self.heater.max_temp for target in targets):
            raise gcmd.error('A retrain target is outside the heater range')
        replace = gcmd.get_int('REPLACE', 0, minval=0, maxval=1)
        if replace:
            self._require_confirmation(gcmd)
        before = set(run.get('id') for run in self.store.runs(self.name))
        gcode = self.printer.lookup_object('gcode')
        for pos, target in enumerate(targets):
            gcmd.respond_info('Retraining %s at %.2fC (%d/%d)' % (
                self.name, target, pos + 1, len(targets)))
            gcode.run_script_from_command(
                'PID_CALIBRATE HEATER=%s TARGET=%.3f STORE=1 SAVE_BASE=0' % (
                    self.name, target))
        after = self.store.runs(self.name)
        new_ids = [run.get('id') for run in after
                   if run.get('id') not in before]
        if len(new_ids) != len(targets):
            raise gcmd.error('Retrain did not produce one run per target; '
                             'existing data was preserved')
        if replace:
            try:
                self.store.remove_except(self.name, new_ids)
            except (OSError, ValueError) as exc:
                raise gcmd.error(
                    'Retrain succeeded, but old data could not be removed: %s'
                    % (exc,))
        self._rebuild()
        gcmd.respond_info(
            '%s retrain complete: %d candidate runs; validate them before '
            'the model may schedule their gains' % (self.name, len(new_ids)))
