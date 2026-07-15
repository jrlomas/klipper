#!/usr/bin/env python3
# Workstation acceptance tests for the PWM/DAC scalar trajectory emitter.

import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                "../klippy"))

from extras.trajectory_pwm import (SUBUNITS, TrajectoryPWM,  # noqa: E402
                                   plan_value_trajectory)


class FakeCommand:
    def __init__(self):
        self.sent = []

    def send(self, args):
        self.sent.append(list(args))


class FakeMCU:
    def __init__(self, frequency=1000000):
        self.frequency = frequency

    def seconds_to_clock(self, seconds):
        return int(round(seconds * self.frequency))

    def print_time_to_clock(self, print_time):
        return self.seconds_to_clock(print_time)


class FakePrinter:
    def __init__(self, machine_mcu=None):
        self.machine_mcu = machine_mcu

    def lookup_object(self, name, default=None):
        if name == 'mcu' and self.machine_mcu is not None:
            return self.machine_mcu
        return default

    @staticmethod
    def command_error(message):
        return RuntimeError(message)


def test_pure_plan_corrects_quantization_and_lands_close():
    plan = plan_value_trajectory(
        .025, lambda t: .2 + .6 * (t / .025) ** 2, 1000000, .001)
    assert plan['start_pos'] == round(.2 * SUBUNITS)
    assert plan['end_pos'] == round(.8 * SUBUNITS)
    assert len(plan['segments']) == 25
    assert abs(plan['end_error_su']) < .01
    assert all(duration == 1000 and accel == 0
               for duration, _velocity, accel in plan['segments'])
    print("PASS: sampled value plan corrects fixed-point error at every span")


def make_pwm():
    pwm = TrajectoryPWM.__new__(TrajectoryPWM)
    pwm.mcu = FakeMCU(64000000)
    pwm.printer = FakePrinter(FakeMCU(12000000))
    pwm.oid = 7
    pwm.sample_time = .001
    pwm.need_rebase = True
    pwm.last_plan = None
    pwm.rebase_cmd = FakeCommand()
    pwm.queue_cmd = FakeCommand()
    pwm.hold_cmd = FakeCommand()
    return pwm


def test_feed_preflights_then_rebases_queues_and_holds():
    pwm = make_pwm()
    plan = pwm.feed_value_trajectory(
        2., .003, lambda t: .25 + t * 100.)
    assert pwm.rebase_cmd.sent == [[7, 24000000, round(.25 * SUBUNITS)]]
    assert len(pwm.queue_cmd.sent) == 3
    assert all(cmd[2] == 12000 for cmd in pwm.queue_cmd.sent)
    assert pwm.hold_cmd.sent == [[7, 12000]]
    assert pwm.need_rebase is False and pwm.last_plan is plan
    print("PASS: feed emits its rebase, spans, and hold in the shared"
          " primary-MCU machine-clock domain")


def test_bad_callback_emits_nothing():
    pwm = make_pwm()
    try:
        pwm.feed_value_trajectory(2., .003, lambda _t: math.nan)
    except RuntimeError as exc:
        assert "non-finite" in str(exc)
    else:
        raise AssertionError("non-finite trajectory was accepted")
    assert not pwm.rebase_cmd.sent and not pwm.queue_cmd.sent
    assert not pwm.hold_cmd.sent
    print("PASS: invalid scalar functions fail before any MCU command")


def test_oversized_batch_emits_nothing():
    pwm = make_pwm()
    try:
        pwm.feed_value_trajectory(2., 1., lambda _t: .5)
    except RuntimeError as exc:
        assert "256-segment" in str(exc)
    else:
        raise AssertionError("unbounded trajectory batch was accepted")
    assert not pwm.rebase_cmd.sent and not pwm.queue_cmd.sent
    print("PASS: scalar feed is bounded before it can overrun the move pool")


def main():
    test_pure_plan_corrects_quantization_and_lands_close()
    test_feed_preflights_then_rebases_queues_and_holds()
    test_bad_callback_emits_nothing()
    test_oversized_batch_emits_nothing()
    print("ALL PASS")


if __name__ == "__main__":
    main()
