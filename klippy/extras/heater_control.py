# Host configuration and supervision for the MCU-autonomous heater loop.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging, math


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

        self.kp = config.getfloat('pid_Kp', minval=0.) / 255.
        self.ki = config.getfloat('pid_Ki', minval=0.) / 255.
        self.kd = config.getfloat('pid_Kd', minval=0.) / 255.
        self._commands_ready = False
        self.set_target_cmd = self.manual_cmd = self.manual_guard_cmd = None
        self.ping_cmd = None
        self.query_cmd = self.timing_cmd = self.clear_cmd = None
        self.state = 1
        self.fault = 0
        self.fault_reported = False
        self.output = 0.
        self.samples = 0
        self.last_temp = 0.
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

    def _q20(self, value, option):
        result = int(value * (1 << GAIN_SHIFT) + .5)
        if not -0x80000000 <= result <= 0x7fffffff:
            raise self.printer.config_error(
                "%s is outside the MCU fixed-point range" % (option,))
        return result

    def _temp_to_adc(self, temp):
        adc_max = int(self.mcu.get_constant_float('ADC_MAX'))
        value = self.adc_convert.calc_adc(temp)
        return max(0, min(adc_max, int(value * adc_max + .5)))

    def _target_parameters(self, temp):
        adc_max = float(self.mcu.get_constant_float('ADC_MAX'))
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
        if self.mcu.get_constants().get('HEATER_CONTROL_V1') != 1:
            raise self.printer.config_error(
                "MCU '%s' lacks HEATER_CONTROL_V1" % (self.mcu.get_name(),))
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
        self.manual_cmd = self.mcu.lookup_command(
            "heater_control_set_manual oid=%c output=%hu", cq=cq)
        self.manual_guard_cmd = self.mcu.lookup_command(
            "heater_control_set_manual_guard oid=%c guard_adc=%u"
            " guard_mdeg=%i slope_q16=%i", cq=cq)
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
        target_adc, target_mdeg, slope_q16 = self._target_parameters(temp)
        self.set_target_cmd.send(
            [self.oid, target_adc, target_mdeg, slope_q16])

    def set_manual_output(self, value):
        if self.manual_cmd is None:
            return
        output = max(0, min(OUTPUT_ONE, int(value * OUTPUT_ONE + .5)))
        self.manual_cmd.send([self.oid, output])
        self.output = output / float(OUTPUT_ONE)
        self.heater.last_pwm_value = self.output

    def set_manual_guard(self, temp):
        if self.manual_guard_cmd is None:
            return
        if temp <= 0.:
            self.manual_guard_cmd.send([self.oid, 0, 0, 0])
            return
        target_adc, target_mdeg, slope_q16 = self._target_parameters(temp)
        self.manual_guard_cmd.send(
            [self.oid, target_adc, target_mdeg, slope_q16])

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
            'mcu_temperature': self.last_temp,
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
        }

    cmd_HEATER_CONTROL_STATUS_help = "Report the MCU heater controller state"
    def cmd_HEATER_CONTROL_STATUS(self, gcmd):
        status = self.query()
        gcmd.respond_info(
            "%s: state=%s fault=0x%x power=%.4f samples=%d temp=%.3f" % (
                self.name, status['state'], status['fault'], status['power'],
                status['samples'], status['mcu_temperature']))

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
