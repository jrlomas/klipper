#!/usr/bin/env python3
"""Extruder-specific fidelity checks for HELIX trajectory intentions.

Exercise the production extruder kinematics callback (including pressure
advance smoothing) through segfit, rather than approximating E motion with a
Cartesian axis.  This is the host-side gate for opting a toolhead extruder
into ``motion_protocol: trajectory``.
"""

import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "klippy"))

import chelper


# The normal per-actuator encoding proves its quantization in the executing
# EBB36 timer domain; absolute rebases remain scheduled in machine time.
MCU_FREQ = 64_000_000.
LOCAL_FREQ = 64_000_000.
RATE_SHIFT = 24
RATE_RAW = round(LOCAL_FREQ / MCU_FREQ * (1 << RATE_SHIFT))
# Live V0 / EBB36: 22.67895 mm rotation distance, 50:10 gear ratio,
# 200 full steps/rev, 16 microsteps.
STEP_DIST = 22.67895 / (5. * 200. * 16.)
SU_PER_MM = 65536. / STEP_DIST
TOLERANCE_SU = 32768.
SAMPLE_TIME = .001
SAMPLE_TICKS = round(MCU_FREQ * SAMPLE_TIME)
Q32 = 2. ** 32
START_TIME = 5.


def derivative_to_local(value, order):
    scaled = int(value)
    for _ in range(order):
        numerator = scaled * (1 << RATE_SHIFT)
        numerator += (-RATE_RAW // 2 if numerator < 0 else RATE_RAW // 2)
        scaled = -((-numerator) // RATE_RAW) if numerator < 0 \
            else numerator // RATE_RAW
    return scaled


def duration_to_local(duration):
    return (duration * RATE_RAW + (1 << (RATE_SHIFT - 1))) >> RATE_SHIFT


def append_profile(lib, trapq, print_time, start_e, distance, speed, accel,
                   pressure_advance_allowed):
    """Append the same signed E trapq shape PrinterExtruder.process_move uses."""
    direction = 1. if distance >= 0. else -1.
    speed *= direction
    accel *= direction
    accel_t = abs(speed / accel)
    ramp_distance = abs(speed) * accel_t
    assert abs(distance) >= ramp_distance
    cruise_t = (abs(distance) - ramp_distance) / abs(speed)
    lib.trapq_append(
        trapq, print_time, accel_t, cruise_t, accel_t,
        start_e, 0., 0., 1., float(pressure_advance_allowed), 0.,
        0., speed, accel)
    return print_time + 2. * accel_t + cruise_t


def build_path(distance, speed, accel, pressure_advance=0.,
               smooth_time=.040, pressure_advance_allowed=False,
               start_e=0.):
    ffi, lib = chelper.get_ffi()
    trapq = ffi.gc(lib.trapq_alloc(), lib.trapq_free)
    sk = ffi.gc(lib.extruder_stepper_alloc(), lib.extruder_stepper_free)
    lib.itersolve_set_trapq(sk, trapq, STEP_DIST)
    lib.itersolve_set_position(sk, start_e, 0., 0.)
    if pressure_advance:
        lib.extruder_set_pressure_advance(
            sk, 0., pressure_advance, smooth_time)
    end_time = append_profile(
        lib, trapq, START_TIME, start_e, distance, speed, accel,
        pressure_advance_allowed)
    return ffi, lib, trapq, sk, end_time


def collect_quintic(lib, sf, end_time):
    segments = []
    while lib.segfit_get_gen_time(sf) < end_time - 1.e-12:
        before = lib.segfit_get_gen_time(sf)
        count = lib.segfit_generate(sf, end_time)
        assert count >= 0
        data = lib.segfit_get_segs(sf)
        segments.extend((data[i].duration, data[i].velocity, data[i].accel,
                         data[i].jerk, data[i].snap, data[i].crackle,
                         data[i].flags) for i in range(count))
        if lib.segfit_get_gen_time(sf) <= before:
            break
    count = lib.segfit_finalize(sf)
    assert count >= 0
    data = lib.segfit_get_segs(sf)
    segments.extend((data[i].duration, data[i].velocity, data[i].accel,
                     data[i].jerk, data[i].snap, data[i].crackle,
                     data[i].flags) for i in range(count))
    return segments


def fit_path(distance, speed, accel, pressure_advance=0.,
             smooth_time=.040, pressure_advance_allowed=False,
             start_e=0.):
    ffi, lib, trapq, sk, nominal_end_time = build_path(
        distance, speed, accel, pressure_advance, smooth_time,
        pressure_advance_allowed, start_e)
    sf = ffi.gc(lib.segfit_alloc(), lib.segfit_free)
    lib.segfit_setup(sf, sk, MCU_FREQ, SU_PER_MM, TOLERANCE_SU, SAMPLE_TIME)
    lib.segfit_set_order(sf, 2)
    assert lib.segfit_check_activity(sf, 0., nominal_end_time + smooth_time)
    activity_start = lib.segfit_get_activity_start(sf)
    activity_end = lib.segfit_get_activity_end(sf)
    if pressure_advance:
        assert abs(activity_start - (START_TIME - smooth_time * .5)) < 1.e-12
        assert abs(activity_end
                   - (nominal_end_time + smooth_time * .5)) < 1.e-12
    anchor_mm = lib.segfit_get_position(sf, activity_start)
    anchor_su = round(anchor_mm * SU_PER_MM)
    lib.segfit_set_anchor(sf, activity_start, anchor_su << 32)
    lib.segfit_set_anchor_position(sf, anchor_su)
    segments = collect_quintic(lib, sf, activity_end)
    assert segments

    acc = anchor_su << 32
    elapsed = 0
    worst = 0.
    worst_local_endpoint = 0.
    worst_local_path = 0.
    signs = set()
    for duration, velocity, acceleration, jerk, snap, crackle, flags in segments:
        assert flags & (3 << 6) == 2 << 6
        end_velocity = (velocity
                        + (acceleration * duration >> 16)
                        + round(jerk / 2. ** 48 * duration ** 2 / 2.
                                * 65536.)
                        + round(snap / 2. ** 64 * duration ** 3 / 6.
                                * 65536.)
                        + round(crackle / 2. ** 80 * duration ** 4 / 24.
                                * 65536.))
        for value in (velocity, end_velocity):
            if value:
                signs.add(1 if value > 0 else -1)
        offsets = list(range(SAMPLE_TICKS, duration + 1, SAMPLE_TICKS))
        if not offsets or offsets[-1] != duration:
            offsets.append(duration)
        for ticks in offsets:
            got = (acc / Q32 + velocity / 65536. * ticks
                   + .5 * acceleration / Q32 * ticks ** 2
                   + jerk / 2. ** 48 * ticks ** 3 / 6.
                   + snap / 2. ** 64 * ticks ** 4 / 24.
                   + crackle / 2. ** 80 * ticks ** 5 / 120.)
            when = activity_start + (elapsed + ticks) / MCU_FREQ
            want = lib.segfit_get_position(sf, when) * SU_PER_MM
            worst = max(worst, abs(got - want))
        acc += int(lib.segfit_end_delta_ho(
            duration, velocity, acceleration, jerk, snap, crackle))
        local_duration = duration_to_local(duration)
        local_coeffs = [derivative_to_local(value, order)
                        for order, value in enumerate(
                            (velocity, acceleration, jerk, snap, crackle), 1)]
        wire_delta = int(lib.segfit_end_delta_ho(
            duration, velocity, acceleration, jerk, snap, crackle))
        local_delta = int(lib.segfit_end_delta_ho(
            local_duration, *local_coeffs))
        denominator = local_duration << 16
        residual = wire_delta - local_delta
        correction = (abs(residual) + denominator // 2) // denominator
        local_coeffs[0] += -correction if residual < 0 else correction
        local_delta = int(lib.segfit_end_delta_ho(
            local_duration, *local_coeffs))
        worst_local_endpoint = max(
            worst_local_endpoint, abs(local_delta - wire_delta) / Q32)
        for ticks in offsets:
            local_ticks = duration_to_local(ticks)
            wire_pos = (velocity / 65536. * ticks
                        + .5 * acceleration / Q32 * ticks ** 2
                        + jerk / 2. ** 48 * ticks ** 3 / 6.
                        + snap / 2. ** 64 * ticks ** 4 / 24.
                        + crackle / 2. ** 80 * ticks ** 5 / 120.)
            lv, la, lj, ls, lc = local_coeffs
            local_pos = (lv / 65536. * local_ticks
                          + .5 * la / Q32 * local_ticks ** 2
                          + lj / 2. ** 48 * local_ticks ** 3 / 6.
                          + ls / 2. ** 64 * local_ticks ** 4 / 24.
                          + lc / 2. ** 80 * local_ticks ** 5 / 120.)
            worst_local_path = max(
                worst_local_path, abs(local_pos - wire_pos))
        elapsed += duration

    assert worst <= TOLERANCE_SU + 1., worst
    endpoint_mm = acc / Q32 / SU_PER_MM
    source_end_mm = lib.segfit_get_position(
        sf, math.nextafter(activity_end, activity_start))
    endpoint_error_su = abs(endpoint_mm - source_end_mm) * SU_PER_MM
    assert endpoint_error_su <= TOLERANCE_SU + 1., endpoint_error_su
    assert worst_local_endpoint <= TOLERANCE_SU + 1., worst_local_endpoint
    assert worst_local_path <= TOLERANCE_SU + 1., worst_local_path
    return {
        'segments': len(segments),
        'worst_su': worst,
        'local_endpoint_su': worst_local_endpoint,
        'local_path_su': worst_local_path,
        'endpoint_error_su': endpoint_error_su,
        'endpoint_mm': endpoint_mm,
        'signs': signs,
        'lib': lib,
        # Keep the C objects backing segfit's borrowed pointers alive for
        # callers that compare the source kinematics after fitting.
        'ffi': ffi,
        'trapq': trapq,
        'sk': sk,
        'sf': sf,
        'activity_start': activity_start,
        'end_time': activity_end,
        'nominal_end_time': nominal_end_time,
    }


def test_forward_extrusion():
    result = fit_path(20., 20., 400.)
    assert abs(result['endpoint_mm'] - 20.) < STEP_DIST
    assert result['signs'] == {1}
    return result


def test_bounded_retraction_and_unretract():
    # The hardware qualification uses the same conservative 2mm retraction;
    # never pull molten filament far enough to enter the heatbreak.
    retract = fit_path(-2., 10., 300., start_e=20.)
    assert abs(retract['endpoint_mm'] - 18.) < STEP_DIST
    assert retract['signs'] == {-1}
    unretract = fit_path(2., 10., 300., start_e=18.)
    assert abs(unretract['endpoint_mm'] - 20.) < STEP_DIST
    assert unretract['signs'] == {1}
    return retract, unretract


def test_pressure_advance_is_in_the_fitted_path():
    nominal = fit_path(8., 16., 320., pressure_advance=0.,
                       pressure_advance_allowed=True)
    advanced = fit_path(8., 16., 320., pressure_advance=.030175,
                        smooth_time=.040, pressure_advance_allowed=True)
    # Pressure advance must materially change the sampled E trajectory during
    # acceleration, while the fitted endpoint returns to nominal after the
    # completed accel/cruise/decel profile.
    sample_time = START_TIME + .030
    nominal_pos = nominal['lib'].segfit_get_position(
        nominal['sf'], sample_time)
    advanced_pos = advanced['lib'].segfit_get_position(
        advanced['sf'], sample_time)
    assert abs(advanced_pos - nominal_pos) > STEP_DIST
    assert abs(advanced['endpoint_mm'] - nominal['endpoint_mm']) < STEP_DIST
    return advanced, abs(advanced_pos - nominal_pos)


def test_pressure_advance_activity_clips_to_generation_cursor():
    # Klippy can append a move after step generation has advanced into its
    # pressure-advance lookback window.  That historical part is no longer a
    # valid rebase deadline; stock itersolve clips it to last_flush_time.
    # HELIX must return the same forward-only boundary.
    ffi, lib, trapq, sk, end_time = build_path(
        8., 16., 320., pressure_advance=.030175, smooth_time=.040,
        pressure_advance_allowed=True)
    sf = ffi.gc(lib.segfit_alloc(), lib.segfit_free)
    lib.segfit_setup(sf, sk, MCU_FREQ, SU_PER_MM,
                     TOLERANCE_SU, SAMPLE_TIME)
    cursor = START_TIME - .005
    assert lib.segfit_check_activity(sf, cursor, end_time + .020)
    assert lib.segfit_get_activity_start(sf) == cursor
    assert lib.segfit_get_activity_end(sf) > cursor


def main():
    forward = test_forward_extrusion()
    print("PASS: forward extrusion -> %d quintic segments, worst %.2f su"
          % (forward['segments'], forward['worst_su']))
    retract, unretract = test_bounded_retraction_and_unretract()
    print("PASS: bounded 2mm retract/unretract -> %d/%d quintic segments"
          % (retract['segments'], unretract['segments']))
    advanced, pa_delta = test_pressure_advance_is_in_the_fitted_path()
    print("PASS: pressure advance is fitted (delta %.6fmm, %d segments)"
          % (pa_delta, advanced['segments']))
    test_pressure_advance_activity_clips_to_generation_cursor()
    print("PASS: late pressure-advance lookback clips to generation cursor")
    print("extruder_trajectory_test: all E-axis paths within motion_tolerance")
    return 0


if __name__ == '__main__':
    sys.exit(main())
