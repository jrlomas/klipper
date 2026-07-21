# Physical cross-MCU synchronization-line commissioning test
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import math


TRIGGER_SOURCE_TRIGGERED = 1 << 1
RATE_SHIFT = 24
U32_MASK = (1 << 32) - 1


def _mean(values):
    return sum(values) / float(len(values))


def _pstdev(values):
    mean = _mean(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values)
                     / float(len(values)))


def _signed32(value):
    value &= U32_MASK
    return value - (1 << 32) if value & (1 << 31) else value


def predict_local_clock(machine_clock, mapping):
    delta = _signed32(machine_clock - mapping['machine_ref'])
    scaled = delta * mapping['rate']
    # Match the firmware's Q8.24 rounding rule exactly.
    return (mapping['local_ref']
            + ((scaled + (1 << (RATE_SHIFT - 1))) >> RATE_SHIFT)) & U32_MASK


def fit_affine_residuals(samples):
    """Fit secondary ticks against primary ticks and return residual ticks."""
    if len(samples) < 2:
        return 0., 0., [0.] * len(samples)
    x0, y0 = samples[0]
    xs = [float(x - x0) for x, _ in samples]
    ys = [float(y - y0) for _, y in samples]
    xmean = _mean(xs)
    ymean = _mean(ys)
    denom = sum((x - xmean) ** 2 for x in xs)
    slope = (sum((x - xmean) * (y - ymean)
                 for x, y in zip(xs, ys)) / denom) if denom else 0.
    intercept = ymean - slope * xmean
    residuals = [y - (intercept + slope * x)
                 for x, y in zip(xs, ys)]
    return slope, intercept + y0 - slope * x0, residuals


class MachineTimeSyncLine:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        ppins = self.printer.lookup_object('pins')
        self.source = ppins.setup_pin('digital_out', config.get('source_pin'))
        self.source.setup_max_duration(2.)
        self.source.setup_start_value(0., 0.)
        self.capture = ppins.setup_pin('endstop', config.get('capture_pin'))
        self.source_mcu = self.source.get_mcu()
        self.capture_mcu = self.capture.get_mcu()
        if self.source_mcu is self.capture_mcu:
            raise config.error("sync-line pins must be on different MCUs")
        self.timesync = self.printer.load_object(config, 'timesync')
        self.default_samples = config.getint(
            'samples', 20, minval=2, maxval=200)
        self._calibration = None
        self.lead_time = config.getfloat(
            'lead_time', .100, above=.020, maxval=1.)
        self.settle_time = config.getfloat(
            'settle_time', .025, above=.005, maxval=.500)
        self.printer.register_event_handler('klippy:ready', self._handle_ready)
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command(
            'SYNC_LINE_TEST', self.cmd_SYNC_LINE_TEST,
            desc=self.cmd_SYNC_LINE_TEST_help)

    def _handle_ready(self):
        if not self.capture.has_edge_observer():
            raise self.printer.config_error(
                "capture MCU '%s' firmware lacks trigger_source_observe"
                % (self.capture_mcu.get_name(),))
        if not self.source.has_digital_timing():
            raise self.printer.config_error(
                "source MCU '%s' firmware lacks digital_out_query"
                % (self.source_mcu.get_name(),))

    def _pause_until(self, waketime):
        while self.reactor.monotonic() < waketime:
            self.reactor.pause(waketime)

    def _set_source(self, value, lead_time):
        eventtime = self.reactor.monotonic()
        print_time = self.source_mcu.estimated_print_time(
            eventtime + lead_time)
        self.source.set_digital(print_time, value)
        self._pause_until(eventtime + lead_time + self.settle_time)
        states = self.source.query_digital_timing()
        if not states:
            raise self.printer.command_error("source edge timing unavailable")
        return states[0][1]

    def get_calibration(self):
        return self._calibration

    cmd_SYNC_LINE_TEST_help = (
        "Measure a physical primary-to-secondary sync line")
    def cmd_SYNC_LINE_TEST(self, gcmd):
        samples = gcmd.get_int(
            'SAMPLES', self.default_samples, minval=2, maxval=200)
        capture_name = self.capture_mcu.get_name()
        mapping = self.timesync.get_mcu_mapping(capture_name)
        if mapping is None:
            raise gcmd.error("MCU '%s' is not machine-time disciplined"
                             % (capture_name,))
        if not mapping['converged']:
            raise gcmd.error("MCU '%s' machine time is not converged"
                             % (capture_name,))
        # Establish the known idle level before arming the first rising edge.
        self._set_source(0, self.lead_time)
        pairs = []
        map_errors_us = []
        rows = []
        try:
            for index in range(samples):
                eventtime = self.reactor.monotonic()
                arm_time = self.capture_mcu.estimated_print_time(
                    eventtime + .010)
                self.capture.edge_observe_start(arm_time, capture=True)
                source_state = self._set_source(1, self.lead_time)
                capture_state = self.capture.edge_observe_query()
                if not capture_state['flags'] & TRIGGER_SOURCE_TRIGGERED:
                    raise gcmd.error("sync-line sample %d missed PB8 edge"
                                     % (index + 1,))
                if source_state.get('dropped'):
                    raise gcmd.error("sync-line sample %d source edge dropped"
                                     % (index + 1,))
                mapping = self.timesync.get_mcu_mapping(capture_name)
                primary_clock = source_state['actual']
                local_clock = capture_state['clock']
                predicted = predict_local_clock(primary_clock, mapping)
                error_ticks = _signed32(local_clock - predicted)
                error_us = error_ticks / mapping['mcu_freq'] * 1.e6
                primary64 = self.source_mcu.clock32_to_clock64(primary_clock)
                local64 = capture_state['clock64']
                pairs.append((primary64, local64))
                map_errors_us.append(error_us)
                rows.append((index + 1, primary_clock, local_clock,
                             error_ticks, error_us,
                             source_state.get('late', 0)))
                self.capture.edge_observe_disarm()
                self._set_source(0, self.lead_time)
        finally:
            self.capture.edge_observe_disarm()

        slope, intercept, residual_ticks = fit_affine_residuals(pairs)
        self._calibration = {
            'slope': slope, 'intercept': intercept,
            'primary_mcu': self.source_mcu,
            'secondary_mcu': self.capture_mcu,
            'secondary_freq': mapping['mcu_freq'],
        }
        residual_us = [r / mapping['mcu_freq'] * 1.e6
                       for r in residual_ticks]
        map_mean = _mean(map_errors_us)
        map_sigma = _pstdev(map_errors_us)
        residual_sigma = _pstdev(residual_us)
        residual_peak = max(abs(r) for r in residual_us)
        expected_slope = (mapping['mcu_freq']
                          / self.source_mcu.get_constant_float('CLOCK_FREQ'))
        ppm = (slope / expected_slope - 1.) * 1.e6
        gcmd.respond_info(
            "sync-line %s->%s samples=%d\n"
            "physical-fit residual sigma=%.4fus peak=%.4fus slope=%+.2fppm\n"
            "USB-map error mean=%+.4fus sigma=%.4fus range=%+.4f..%+.4fus\n"
            "sample,primary_actual,secondary_capture,map_error_ticks,"
            "map_error_us,source_late_ticks\n%s"
            % (self.source_mcu.get_name(), capture_name, samples,
               residual_sigma, residual_peak, ppm,
               map_mean, map_sigma, min(map_errors_us), max(map_errors_us),
               "\n".join("%d,%d,%d,%d,%+.6f,%d" % row
                          for row in rows)))


def load_config(config):
    return MachineTimeSyncLine(config)
