#!/usr/bin/env python3
"""Deterministic cross-plant simulation for Helix predictive heating."""

import collections, math


class PredictiveController:
    def __init__(self, dt, gain, tau, delay=0., horizon=None, effort=None,
                 integral_gain=.0005, observer_time=2., slew_rate=1.,
                 control_band=10.):
        self.dt, self.gain, self.tau = dt, gain, tau
        self.delay = delay
        self.horizon = (max(delay + dt, min(30., .25 * tau))
                        if horizon is None else horizon)
        self.effort = max(.1, .05 * gain) if effort is None else effort
        self.integral_gain = integral_gain
        self.observer_alpha = 1. - math.exp(-dt / observer_time)
        self.max_step = slew_rate * dt
        self.control_band = control_band
        self.retention = math.exp(-self.horizon / tau)
        response_horizon = max(dt, self.horizon - delay)
        self.response = gain * (1. - math.exp(-response_horizon / tau))
        self.filtered = None
        self.bias, self.output = 0., 0.
        self.rebase_output = False

    def update(self, temperature, target, ambient):
        target_error = target - temperature
        if abs(target_error) > self.control_band:
            desired = 1. if target_error > 0. else 0.
            self.output = max(
                max(0., self.output - self.max_step),
                min(min(1., self.output + self.max_step), desired))
            self.filtered = None
            self.bias = 0.
            self.rebase_output = bool(self.output)
            return self.output
        if self.filtered is None:
            self.filtered = temperature
        else:
            self.filtered += self.observer_alpha * (
                temperature - self.filtered)
        free = ambient + self.retention * (self.filtered - ambient)
        residual = target - free
        response2 = self.response ** 2
        effort2 = self.effort ** 2
        model_output = ((self.response * residual
                         + effort2 * self.output) / (response2 + effort2))
        if self.rebase_output:
            self.bias = self.output - model_output
            self.rebase_output = False
        error = target - self.filtered
        bias_candidate = max(-1., min(
            1., self.bias + self.integral_gain * self.dt * error))
        low = max(0., self.output - self.max_step)
        high = min(1., self.output + self.max_step)
        candidate = model_output + bias_candidate
        if ((low <= candidate <= high)
                or (candidate > high and error < 0.)
                or (candidate < low and error > 0.)):
            self.bias = bias_candidate
        self.output = max(low, min(high, model_output + self.bias))
        return self.output


def simulate(gain, tau, delay, target, disturbance=None):
    dt, ambient = .3, 25.
    controller = PredictiveController(dt, gain, tau, delay)
    queue = collections.deque(
        [0.] * max(1, int(round(delay / dt))), maxlen=max(
            1, int(round(delay / dt))))
    temperature = ambient
    samples = []
    duration = max(600., 8. * tau)
    for index in range(int(duration / dt)):
        stamp = index * dt
        # Repeatable 0.05C-quantized observation with a small deterministic
        # conversion pattern.
        observed = round((temperature + ((index % 5) - 2) * .005) / .05) * .05
        output = controller.update(observed, target, ambient)
        queue.append(output)
        applied = queue[0]
        load = 0. if disturbance is None else disturbance(stamp)
        temperature += dt * (
            (ambient - temperature) / tau + gain * applied / tau + load)
        samples.append((stamp, temperature, output))
    return samples


def qualify(name, samples, target, tau):
    settled = [sample for sample in samples if sample[0] >= 5. * tau]
    errors = [sample[1] - target for sample in settled]
    deltas = [abs(right[2] - left[2])
              for left, right in zip(settled, settled[1:])]
    mean = sum(errors) / len(errors)
    variance = sum((value - mean) ** 2 for value in errors) / len(errors)
    assert abs(mean) < .15, (name, 'bias', mean)
    assert math.sqrt(variance) < .2, (name, 'stddev', math.sqrt(variance))
    assert max(abs(value) for value in errors) < .6, (
        name, 'peak error', max(abs(value) for value in errors))
    assert math.sqrt(sum(value * value for value in deltas) / len(deltas)) < .02


def main():
    bed = simulate(90., 300., 2., 55.)
    qualify('bed', bed, 55., 300.)
    hotend = simulate(
        280., 20., .6, 210.,
        disturbance=lambda stamp: -.03 if 250. <= stamp < 300. else 0.)
    qualify('hotend', hotend, 210., 20.)
    print('PASS: predictive controller qualifies across bed and hotend plants')


if __name__ == '__main__':
    main()
