# Calibration of heater PID settings
#
# Copyright (C) 2016-2018  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import csv, math, logging, os
from . import heaters

class PIDCalibrate:
    def __init__(self, config):
        self.printer = config.get_printer()
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command('PID_CALIBRATE', self.cmd_PID_CALIBRATE,
                               desc=self.cmd_PID_CALIBRATE_help)
        gcode.register_command(
            'HELIX_HEATER_SINE_TEST', self.cmd_HELIX_HEATER_SINE_TEST,
            desc=self.cmd_HELIX_HEATER_SINE_TEST_help)
    cmd_PID_CALIBRATE_help = "Run PID calibration test"
    def cmd_PID_CALIBRATE(self, gcmd):
        heater_name = gcmd.get('HEATER')
        target = gcmd.get_float('TARGET')
        write_file = gcmd.get_int('WRITE_FILE', 0)
        save_base = gcmd.get_int('SAVE_BASE', 1, minval=0, maxval=1)
        pheaters = self.printer.lookup_object('heaters')
        try:
            heater = pheaters.lookup_heater(heater_name)
        except self.printer.config_error as e:
            raise gcmd.error(str(e))
        self.printer.lookup_object('toolhead').get_last_move_time()
        mcu_control = getattr(heater, 'mcu_heater_control', None)
        default_method = 'ADAPTIVE' if mcu_control is not None else 'LEGACY'
        method = gcmd.get('METHOD', default_method).strip().upper()
        rule = gcmd.get('RULE', 'ZN').strip().upper()
        if rule not in ('ZN', 'TL'):
            raise gcmd.error('RULE must be ZN or TL')
        if method == 'ADAPTIVE':
            tolerance = gcmd.get_float('TOLERANCE', .02, above=0.)
            calibrate = ControlAdaptiveAutoTune(
                heater, target, tolerance, rule)
        elif method == 'LEGACY':
            calibrate = ControlAutoTune(heater, target, rule)
        else:
            raise gcmd.error('METHOD must be ADAPTIVE or LEGACY')
        old_control = heater.set_control(calibrate)
        try:
            pheaters.set_temperature(heater, target, True)
        except self.printer.command_error as e:
            heater.set_control(old_control)
            raise
        heater.set_control(old_control)
        if write_file:
            calibrate.write_file('/tmp/heattest.txt')
        if calibrate.check_busy(0., 0., 0.):
            raise gcmd.error("pid_calibrate interrupted")
        # Log and report results
        Kp, Ki, Kd = calibrate.calc_final_pid()
        logging.info("Autotune: final: Kp=%f Ki=%f Kd=%f", Kp, Ki, Kd)
        store_run = gcmd.get_int(
            'STORE', 1 if mcu_control is not None else 0,
            minval=0, maxval=1)
        stored = None
        if store_run and mcu_control is not None:
            try:
                stored = mcu_control.profile_manager.record_tune(
                    target, (Kp, Ki, Kd), calibrate,
                    'relay_%s_%s' % (method.lower(), rule.lower()))
            except (OSError, ValueError) as exc:
                raise gcmd.error('Unable to store PID characterization: %s'
                                 % (exc,))
        save_message = (
            "The SAVE_CONFIG command will update the printer config file\n"
            "with these parameters and restart the printer."
            if save_base else
            "Base printer.cfg gains were left unchanged; validate stored "
            "runs before scheduling them.")
        gcmd.respond_info(
            "PID parameters: pid_Kp=%.3f pid_Ki=%.3f pid_Kd=%.3f\n"
            "%s"
            "%s" % (
                Kp, Ki, Kd,
                ("Stored candidate run: %s\n" % stored['id']
                 if stored is not None else ''), save_message))
        # Store results for SAVE_CONFIG
        if not save_base:
            return
        cfgname = heater.get_name()
        configfile = self.printer.lookup_object('configfile')
        control_name = ('helix_pid'
                        if isinstance(old_control, heaters.ControlHelixPID)
                        else 'pid')
        configfile.set(cfgname, 'control', control_name)
        configfile.set(cfgname, 'pid_Kp', "%.3f" % (Kp,))
        configfile.set(cfgname, 'pid_Ki', "%.3f" % (Ki,))
        configfile.set(cfgname, 'pid_Kd', "%.3f" % (Kd,))

    cmd_HELIX_HEATER_SINE_TEST_help = (
        "Measure installed heater-chain response to guarded PWM sine")
    def cmd_HELIX_HEATER_SINE_TEST(self, gcmd):
        heater_name = gcmd.get('HEATER')
        pheaters = self.printer.lookup_object('heaters')
        try:
            heater = pheaters.lookup_heater(heater_name)
        except self.printer.config_error as exc:
            raise gcmd.error(str(exc))
        mcu_control = getattr(heater, 'mcu_heater_control', None)
        if mcu_control is None:
            raise gcmd.error(
                'HELIX_HEATER_SINE_TEST requires control: helix_pid')
        center = gcmd.get_float(
            'CENTER', minval=heater.min_temp, maxval=heater.max_temp)
        ceiling = gcmd.get_float(
            'CEILING', above=center, maxval=heater.max_temp)
        period = gcmd.get_float('PERIOD', 60., minval=10.)
        cycles = gcmd.get_int('CYCLES', 4, minval=2, maxval=100)
        warmup = gcmd.get_int('WARMUP_CYCLES', 2, minval=0, maxval=20)
        self.printer.lookup_object('toolhead').get_last_move_time()

        # Establish the operating point under the ordinary controller before
        # changing one variable: the open-loop PWM perturbation.
        pheaters.set_temperature(heater, center, True)
        bias_raw = gcmd.get('BIAS', 'AUTO').strip().upper()
        if bias_raw == 'AUTO':
            bias = heater.last_pwm_value
        else:
            try:
                bias = float(bias_raw)
            except ValueError:
                raise gcmd.error('BIAS must be AUTO or a numeric duty')
        max_power = heater.get_max_power()
        if bias <= 0. or bias >= max_power:
            pheaters.set_temperature(heater, 0.)
            raise gcmd.error(
                'Sine bias %.6f leaves no bidirectional duty margin' % bias)
        default_amplitude = .25 * min(bias, max_power - bias)
        amplitude = gcmd.get_float(
            'AMPLITUDE', default_amplitude, above=0.)
        if bias - amplitude < 0. or bias + amplitude > max_power:
            pheaters.set_temperature(heater, 0.)
            raise gcmd.error(
                'BIAS +/- AMPLITUDE must stay within 0..max_power')

        test = ControlHeaterSine(
            heater, center, ceiling, bias, amplitude, period, cycles, warmup)
        old_control = heater.set_control(test)
        try:
            pheaters.set_temperature(heater, center, True)
        finally:
            heater.set_control(old_control)
            heater.set_temp(0.)
        if not test.completed or self.printer.is_shutdown():
            raise gcmd.error('heater sine test interrupted')
        try:
            metrics = thermal_sine_metrics(test.samples, period, amplitude)
        except ValueError as exc:
            raise gcmd.error('Unable to analyze heater sine test: %s' % exc)
        filename = None
        if gcmd.get_int('WRITE_FILE', 1, minval=0, maxval=1):
            safe_name = heater_name.replace(' ', '_').replace('/', '_')
            filename = '/tmp/helix-heater-sine-%s.csv' % safe_name
            test.write_file(filename)
        gcmd.respond_info(
            '%s thermal-chain sine: samples=%d gain=%.6f C/duty '
            'phase=%.3fdeg drift=%+.6fC/min residual=%.6fC '
            'raw_residual=%.6fC SINAD=%.3fdB raw_SINAD=%.3fdB '
            'effective_control_bits=%.3f%s' % (
                heater_name, metrics['samples'], metrics['gain_c_per_duty'],
                metrics['phase_deg'], metrics['drift_c_per_min'],
                metrics['residual_rms_c'], metrics['raw_residual_rms_c'],
                metrics['sinad_db'], metrics['raw_sinad_db'],
                metrics['effective_control_bits'],
                '' if filename is None else '\nRaw capture: %s' % filename))

TUNE_PID_DELTA = 5.0

def _pid_from_ultimate(Ku, Tu, rule):
    if rule == 'TL':
        # Tyreus-Luyben is deliberately less aggressive than classic ZN.
        Kp = Ku / 2.2 * heaters.PID_PARAM_BASE
        Ti = 2.2 * Tu
        Td = Tu / 6.3
    else:
        Kp = 0.6 * Ku * heaters.PID_PARAM_BASE
        Ti = 0.5 * Tu
        Td = 0.125 * Tu
    return Kp, Kp / Ti, Kp * Td


class ControlAutoTune:
    def __init__(self, heater, target, rule='ZN'):
        self.heater = heater
        self.heater_max_power = heater.get_max_power()
        self.calibrate_temp = target
        self.rule = rule
        # Heating control
        self.heating = False
        self.peak = 0.
        self.peak_time = 0.
        # Peak recording
        self.peaks = []
        # Sample recording
        self.last_pwm = 0.
        self.pwm_samples = []
        self.temp_samples = []
    # Heater control
    def set_pwm(self, read_time, value):
        if value != self.last_pwm:
            self.pwm_samples.append(
                (read_time + self.heater.get_pwm_delay(), value))
            self.last_pwm = value
        self.heater.set_pwm(read_time, value)
    def temperature_update(self, read_time, temp, target_temp):
        self.temp_samples.append((read_time, temp))
        # Check if the temperature has crossed the target and
        # enable/disable the heater if so.
        if self.heating and temp >= target_temp:
            self.heating = False
            self.check_peaks()
            self.heater.alter_target(self.calibrate_temp - TUNE_PID_DELTA)
        elif not self.heating and temp <= target_temp:
            self.heating = True
            self.check_peaks()
            self.heater.alter_target(self.calibrate_temp)
        # Check if this temperature is a peak and record it if so
        if self.heating:
            self.set_pwm(read_time, self.heater_max_power)
            if temp < self.peak:
                self.peak = temp
                self.peak_time = read_time
        else:
            self.set_pwm(read_time, 0.)
            if temp > self.peak:
                self.peak = temp
                self.peak_time = read_time
    def check_busy(self, eventtime, smoothed_temp, target_temp):
        if self.heating or len(self.peaks) < 12:
            return True
        return False
    # Analysis
    def check_peaks(self):
        self.peaks.append((self.peak, self.peak_time))
        if self.heating:
            self.peak = 9999999.
        else:
            self.peak = -9999999.
        if len(self.peaks) < 4:
            return
        self.calc_pid(len(self.peaks)-1)
    def calc_pid(self, pos):
        temp_diff = self.peaks[pos][0] - self.peaks[pos-1][0]
        time_diff = self.peaks[pos][1] - self.peaks[pos-2][1]
        # Use Astrom-Hagglund method to estimate Ku and Tu
        amplitude = .5 * abs(temp_diff)
        Ku = 4. * self.heater_max_power / (math.pi * amplitude)
        Tu = time_diff
        Kp, Ki, Kd = _pid_from_ultimate(Ku, Tu, self.rule)
        logging.info("Autotune: raw=%f/%f Ku=%f Tu=%f  Kp=%f Ki=%f Kd=%f",
                     temp_diff, self.heater_max_power, Ku, Tu, Kp, Ki, Kd)
        return Kp, Ki, Kd
    def calc_final_pid(self):
        cycle_times = [(self.peaks[pos][1] - self.peaks[pos-2][1], pos)
                       for pos in range(4, len(self.peaks))]
        midpoint_pos = sorted(cycle_times)[len(cycle_times)//2][1]
        return self.calc_pid(midpoint_pos)
    # Offline analysis helper
    def write_file(self, filename):
        pwm = ["pwm: %.3f %.3f" % (time, value)
               for time, value in self.pwm_samples]
        out = ["%.3f %.3f" % (time, temp) for time, temp in self.temp_samples]
        f = open(filename, "w")
        f.write('\n'.join(pwm + out))
        f.close()


ADAPTIVE_DELTA = 5.0
ADAPTIVE_SAMPLES = 3
ADAPTIVE_MAX_PEAKS = 60


def _solve_thermal_fit(matrix, vector):
    size = len(vector)
    rows = [list(matrix[pos]) + [vector[pos]] for pos in range(size)]
    for col in range(size):
        pivot = max(range(col, size), key=lambda row: abs(rows[row][col]))
        if abs(rows[pivot][col]) < 1.e-18:
            raise ValueError('thermal sine fit is singular')
        rows[col], rows[pivot] = rows[pivot], rows[col]
        divisor = rows[col][col]
        rows[col] = [value / divisor for value in rows[col]]
        for row in range(size):
            if row == col:
                continue
            factor = rows[row][col]
            rows[row] = [rows[row][idx] - factor * rows[col][idx]
                         for idx in range(size + 1)]
    return [rows[row][size] for row in range(size)]


def _least_squares(rows, values):
    size = len(rows[0])
    matrix = [[sum(row[i] * row[j] for row in rows)
               for j in range(size)] for i in range(size)]
    vector = [sum(row[i] * value for row, value in zip(rows, values))
              for i in range(size)]
    return _solve_thermal_fit(matrix, vector)


def thermal_sine_metrics(samples, period, commanded_amplitude):
    measured = [sample for sample in samples if sample[3]]
    if len(measured) < 8:
        raise ValueError('insufficient thermal sine samples')
    omega = 2. * math.pi / period
    origin = measured[0][0]
    raw_rows = [(1., math.sin(omega * sample[0]),
                 math.cos(omega * sample[0])) for sample in measured]
    values = [sample[1] for sample in measured]
    raw_offset, raw_sine, raw_cosine = _least_squares(raw_rows, values)
    raw_fitted = [sum(coef * value for coef, value in zip(
                  (raw_offset, raw_sine, raw_cosine), row))
                  for row in raw_rows]
    raw_residuals = [value - fit
                     for value, fit in zip(values, raw_fitted)]

    # Open-loop thermal experiments commonly retain a slow operating-point
    # drift even after warm-up.  Publish that drift separately instead of
    # folding it into the periodic distortion/noise floor.  Centering elapsed
    # time at the measurement-window origin also conditions the normal matrix.
    rows = [(1., sample[0] - origin, math.sin(omega * sample[0]),
             math.cos(omega * sample[0])) for sample in measured]
    offset, drift, sine, cosine = _least_squares(rows, values)
    fitted = [sum(coef * value for coef, value in zip(
              (offset, drift, sine, cosine), row)) for row in rows]
    residuals = [sample[1] - fit
                 for sample, fit in zip(measured, fitted)]
    amplitude = math.hypot(sine, cosine)
    signal_rms = amplitude / math.sqrt(2.)
    residual_rms = math.sqrt(
        sum(value * value for value in residuals) / len(residuals))
    raw_amplitude = math.hypot(raw_sine, raw_cosine)
    raw_signal_rms = raw_amplitude / math.sqrt(2.)
    raw_residual_rms = math.sqrt(
        sum(value * value for value in raw_residuals) / len(raw_residuals))
    if signal_rms <= 0.:
        raise ValueError('no thermal response at the commanded frequency')
    sinad_db = (20. * math.log10(signal_rms / residual_rms)
                if residual_rms else float('inf'))
    raw_sinad_db = (20. * math.log10(raw_signal_rms / raw_residual_rms)
                    if raw_residual_rms else float('inf'))
    phase = math.degrees(math.atan2(cosine, sine))
    return {
        'samples': len(measured), 'offset_c': offset,
        'amplitude_c': amplitude,
        'gain_c_per_duty': amplitude / commanded_amplitude,
        'phase_deg': phase, 'drift_c_per_s': drift,
        'drift_c_per_min': 60. * drift,
        'residual_rms_c': residual_rms,
        'raw_residual_rms_c': raw_residual_rms,
        'sinad_db': sinad_db,
        'raw_sinad_db': raw_sinad_db,
        'effective_control_bits': ((sinad_db - 1.76) / 6.02
                                   if math.isfinite(sinad_db)
                                   else float('inf')),
    }


class ControlHeaterSine:
    """Guarded open-loop PWM sine for installed thermal-chain measurement."""
    def __init__(self, heater, center, ceiling, bias, amplitude, period,
                 cycles, warmup_cycles):
        self.heater = heater
        self.center = center
        self.manual_ceiling = ceiling
        self.bias = bias
        self.amplitude = amplitude
        self.period = period
        self.cycles = cycles
        self.warmup_cycles = warmup_cycles
        self.duration = (cycles + warmup_cycles) * period
        self.started = None
        self.done = self.completed = False
        self.samples = []

    def deactivate(self):
        self.heater.set_pwm(0., 0.)
        self.done = True

    def temperature_update(self, read_time, temp, target_temp):
        if self.done:
            return
        if self.started is None:
            self.started = read_time
        elapsed = read_time - self.started
        if elapsed >= self.duration:
            self.heater.set_pwm(read_time, 0.)
            self.heater.alter_target(0.)
            self.done = self.completed = True
            return
        phase = 2. * math.pi * elapsed / self.period
        output = self.bias + self.amplitude * math.sin(phase)
        measured = elapsed >= self.warmup_cycles * self.period
        self.samples.append((elapsed, temp, output, measured))
        self.heater.set_pwm(read_time, output)

    def check_busy(self, eventtime, smoothed_temp, target_temp):
        return not self.done

    def write_file(self, filename):
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        flags |= getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(filename, flags, 0o600)
        with os.fdopen(fd, 'w', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(('elapsed_s', 'temperature_c', 'commanded_power',
                             'measurement_window'))
            writer.writerows(self.samples)


class ControlAdaptiveAutoTune:
    """Power-balanced relay identification derived from Kalico's method.

    Unlike the legacy full-power relay, this converges the on-state power so
    high and low excursions are centered on the requested operating point.
    That reduces bias in Ku/Tu when a heater has large excess power.
    """
    def __init__(self, heater, target, tolerance=.02, rule='ZN'):
        self.heater = heater
        self.heater_max_power = heater.get_max_power()
        self.calibrate_temp = target
        self.temp_high = target + .5 * ADAPTIVE_DELTA
        self.temp_low = target - .5 * ADAPTIVE_DELTA
        self.tolerance = tolerance
        self.rule = rule
        self.heating = False
        self.started = self.crossed = self.done = self.errored = False
        self.peak = target
        self.peak_times = []
        self.peaks = []
        self.powers = [self.heater_max_power]
        self.switch_times = []
        self.last_pwm = 0.
        self.pwm_samples = []
        self.temp_samples = []
        self.ultimate = None

    def set_pwm(self, read_time, value):
        if value != self.last_pwm:
            self.pwm_samples.append(
                (read_time + self.heater.get_pwm_delay(), value))
            self.last_pwm = value
        self.heater.set_pwm(read_time, value)

    def _finish(self, read_time, error=None):
        self.set_pwm(read_time, 0.)
        self.heater.alter_target(0.)
        self.done = True
        self.heating = False
        if error is not None:
            self.errored = True
            logging.warning('Adaptive PID autotune: %s', error)

    def _track_peak(self, read_time, temp):
        if temp == self.peak:
            self.peak_times.append(read_time)
        elif temp > self.calibrate_temp and temp > self.peak:
            self.peak, self.peak_times = temp, [read_time]
        elif temp < self.calibrate_temp and temp < self.peak:
            self.peak, self.peak_times = temp, [read_time]

    def _store_peak(self):
        if not self.peak_times:
            return
        stamp = sum(self.peak_times) / len(self.peak_times)
        self.peaks.append((self.peak, stamp))
        self.peak = self.calibrate_temp
        self.peak_times = []

    def _power_tolerance(self):
        if len(self.powers) < ADAPTIVE_SAMPLES + 1:
            return None
        recent = self.powers[-(ADAPTIVE_SAMPLES + 1):]
        return max(recent) - min(recent)

    def _complete_cycle(self):
        if len(self.peaks) < 2:
            return
        low = min(self.peaks[-2][0], self.peaks[-1][0])
        high = max(self.peaks[-2][0], self.peaks[-1][0])
        span = high - low
        if span <= 1.e-9:
            return
        power = self.powers[-1]
        asymmetry = .5 * (low + high) - self.calibrate_temp
        tolerance = self._power_tolerance()
        logging.info(
            'Adaptive autotune: sample=%d pwm=%.6f asymmetry=%.6f '
            'tolerance=%s', len(self.powers), power, asymmetry,
            'n/a' if tolerance is None else '%.6f' % tolerance)
        if tolerance is not None and tolerance <= self.tolerance:
            return True
        # Relay output alternates between zero and power.  Move its midpoint
        # until the measured extrema are centered on the requested target.
        next_power = 2. * power * (self.calibrate_temp - low) / span
        self.powers.append(max(.01, min(self.heater_max_power, next_power)))
        return False

    def temperature_update(self, read_time, temp, target_temp):
        self.temp_samples.append((read_time, temp))
        if self.done:
            return
        if not self.started:
            if temp >= self.temp_low:
                self._finish(read_time,
                             'temperature is too high to start calibration')
                return
            self.started = True
            self.heating = True
            self.heater.alter_target(self.temp_high)
        if len(self.peaks) > ADAPTIVE_MAX_PEAKS:
            self._finish(read_time, 'calibration did not converge')
            return
        if temp > self.calibrate_temp:
            self.crossed = True
        if self.crossed:
            if temp > self.temp_high or temp < self.temp_low:
                self._track_peak(read_time, temp)
            if self.peak > self.temp_high and temp < self.calibrate_temp:
                self._store_peak()
            elif self.peak < self.temp_low and temp > self.calibrate_temp:
                self._store_peak()
                if self._complete_cycle():
                    self._finish(read_time)
                    return
        if self.heating and temp >= self.temp_high:
            self.heating = False
            self.switch_times.append(read_time)
            self.heater.alter_target(self.temp_low)
        elif not self.heating and temp <= self.temp_low:
            self.heating = True
            self.switch_times.append(read_time)
            self.heater.alter_target(self.temp_high)
        self.set_pwm(read_time, self.powers[-1] if self.heating else 0.)

    def check_busy(self, eventtime, smoothed_temp, target_temp):
        if eventtime == smoothed_temp == target_temp == 0.:
            return self.errored
        return not self.done

    def _ultimate_constants(self):
        if len(self.peaks) < 7:
            raise ValueError('insufficient adaptive relay peaks')
        recent = self.peaks[-7:]
        amplitudes = [.5 * abs(recent[pos][0] - recent[pos - 1][0])
                      for pos in range(1, len(recent))]
        amplitude = sum(amplitudes[-2 * ADAPTIVE_SAMPLES:]) / (
            2. * ADAPTIVE_SAMPLES)
        periods = [recent[pos][1] - recent[pos - 2][1]
                   for pos in range(2, len(recent))]
        Tu = sum(periods[-ADAPTIVE_SAMPLES:]) / ADAPTIVE_SAMPLES
        power = sum(self.powers[-ADAPTIVE_SAMPLES:]) / ADAPTIVE_SAMPLES
        Ku = 4. * power / (math.pi * amplitude)
        self.ultimate = {'ku': Ku, 'tu': Tu, 'amplitude': amplitude,
                         'power': power}
        return Ku, Tu

    def calc_final_pid(self):
        Ku, Tu = self._ultimate_constants()
        Kp, Ki, Kd = _pid_from_ultimate(Ku, Tu, self.rule)
        logging.info(
            'Adaptive autotune: Ku=%f Tu=%f power=%f amplitude=%f '
            'rule=%s Kp=%f Ki=%f Kd=%f', Ku, Tu,
            self.ultimate['power'], self.ultimate['amplitude'], self.rule,
            Kp, Ki, Kd)
        return Kp, Ki, Kd

    def write_file(self, filename):
        out = ['time,temp'] + ['%.6f,%.6f' % sample
                               for sample in self.temp_samples]
        out += ['pwm_time,pwm'] + ['%.6f,%.6f' % sample
                                   for sample in self.pwm_samples]
        with open(filename, 'w') as stream:
            stream.write('\n'.join(out) + '\n')

def load_config(config):
    return PIDCalibrate(config)
