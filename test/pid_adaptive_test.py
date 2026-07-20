#!/usr/bin/env python3
"""Deterministic plant simulation for the adaptive relay autotune."""

import math, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'klippy'))

from extras import pid_calibrate


class FakeHeater:
    def __init__(self):
        self.power = 0.
        self.target = 0.

    def get_max_power(self):
        return 1.

    def get_pwm_delay(self):
        return 0.

    def get_smooth_time(self):
        return 1.

    def set_pwm(self, read_time, value):
        self.power = value

    def alter_target(self, target):
        self.target = target


def simulate(rule='ZN'):
    heater = FakeHeater()
    tune = pid_calibrate.ControlAdaptiveAutoTune(
        heater, 100., tolerance=.01, rule=rule)
    temp, ambient = 25., 25.
    dt, tau, full_power_rate = .1, 25., 15.
    for step in range(30000):
        stamp = step * dt
        tune.temperature_update(stamp, temp, heater.target)
        # First-order thermal plant.  Full-power equilibrium is 400C, so the
        # operating point requires about 20% duty and exposes relay bias.
        temp += dt * (heater.power * full_power_rate
                      - (temp - ambient) / tau)
        if tune.done:
            break
    assert tune.done and not tune.errored
    gains = tune.calc_final_pid()
    assert all(math.isfinite(value) and value > 0. for value in gains)
    assert len(tune.peaks) >= 8
    assert tune.powers[-1] < .70
    assert max(tune.powers[-4:]) - min(tune.powers[-4:]) <= .011
    return gains


def simulate_symmetric(rule='TL', initial_bias=.15, dt=.1):
    heater = FakeHeater()
    tune = pid_calibrate.ControlSymmetricAutoTune(
        heater, 100., initial_bias, .08, tolerance=.01, rule=rule)
    temp, ambient = 100., 25.
    tau, full_power_rate = 25., 15.
    for step in range(30000):
        stamp = step * dt
        tune.temperature_update(stamp, temp, heater.target)
        temp += dt * (heater.power * full_power_rate
                      - (temp - ambient) / tau)
        if tune.done:
            break
    assert tune.done and not tune.errored
    gains = tune.calc_final_pid()
    assert all(math.isfinite(value) and value > 0. for value in gains)
    assert len(tune.peaks) >= 8
    # This plant's exact holding bias is (100-25)/(25*15) == 0.2.
    assert abs(tune.ultimate['bias'] - .2) < .04
    assert abs(tune.ultimate['amplitude']
               - tune.TARGET_AMPLITUDE) < tune.AMPLITUDE_TOLERANCE
    assert max(tune.biases[-4:]) - min(tune.biases[-4:]) <= .011
    assert max(tune.deltas[-4:]) - min(tune.deltas[-4:]) <= .011
    assert all(delta < bias < 1. - delta
               for bias, delta in zip(tune.biases, tune.deltas))
    return gains


def test_adaptive_convergence():
    zn = simulate('ZN')
    tl = simulate('TL')
    assert tl[0] < zn[0] and tl[1] < zn[1]


def test_symmetric_convergence_and_wrong_initial_bias():
    low = simulate_symmetric(initial_bias=.15)
    # The physical G0B1 path currently reports at roughly 300 ms cadence.
    high = simulate_symmetric(initial_bias=.25, dt=.3)
    assert all(value > 0. for value in low + high)


def test_host_comparison_controller_uses_identical_gains():
    heater = FakeHeater()
    gains = (17.947990989340383, 2.03725, 45.583426633920496)
    control = pid_calibrate.heaters.ControlPID.from_gains(heater, gains)
    assert abs(control.Kp * 255. - gains[0]) < 1.e-12
    assert abs(control.Ki * 255. - gains[1]) < 1.e-12
    assert abs(control.Kd * 255. - gains[2]) < 1.e-12


def test_thermal_sine_fit_absorbs_gain_and_phase():
    period = 20.
    amplitude, phase = 2.5, math.radians(-37.)
    samples = []
    for pos in range(800):
        stamp = pos * .1
        temp = (100. + amplitude * math.sin(
            2. * math.pi * stamp / period + phase)
                + .02 * math.sin(4. * math.pi * stamp / period))
        samples.append((stamp, temp, .2 + .05 * math.sin(
            2. * math.pi * stamp / period), stamp >= 20.))
    result = pid_calibrate.thermal_sine_metrics(samples, period, .05)
    assert abs(result['amplitude_c'] - amplitude) < .001
    assert abs(result['phase_deg'] + 37.) < .02
    assert abs(result['gain_c_per_duty'] - 50.) < .02
    assert .013 < result['residual_rms_c'] < .015


def test_thermal_sine_fit_separates_operating_point_drift():
    period = 20.
    amplitude, phase, drift = 2.5, math.radians(-37.), .0125
    samples = []
    for pos in range(800):
        stamp = pos * .1
        temp = (100. + drift * stamp + amplitude * math.sin(
            2. * math.pi * stamp / period + phase)
                + .02 * math.sin(4. * math.pi * stamp / period))
        samples.append((stamp, temp, .2 + .05 * math.sin(
            2. * math.pi * stamp / period), stamp >= 20.))
    result = pid_calibrate.thermal_sine_metrics(samples, period, .05)
    assert abs(result['amplitude_c'] - amplitude) < .001
    assert abs(result['phase_deg'] + 37.) < .02
    assert abs(result['drift_c_per_s'] - drift) < .0002
    assert .013 < result['residual_rms_c'] < .015
    assert result['raw_residual_rms_c'] > .20
    assert result['sinad_db'] > result['raw_sinad_db'] + 20.


def test_thermal_sine_controller_finishes_off():
    heater = FakeHeater()
    control = pid_calibrate.ControlHeaterSine(
        heater, 100., 110., .2, .05, 10., 2, 1)
    for pos in range(305):
        stamp = pos * .1
        control.temperature_update(stamp, 100., 100.)
    assert control.done and control.completed
    assert heater.power == 0. and heater.target == 0.


def main():
    test_adaptive_convergence()
    test_symmetric_convergence_and_wrong_initial_bias()
    test_host_comparison_controller_uses_identical_gains()
    test_thermal_sine_fit_absorbs_gain_and_phase()
    test_thermal_sine_fit_separates_operating_point_drift()
    test_thermal_sine_controller_finishes_off()
    print('PASS: adaptive relay power converges and tuning rules are finite')


if __name__ == '__main__':
    main()
