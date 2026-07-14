#!/usr/bin/env python3
"""Direct path-fidelity checks for the HELIX quadratic segment fitter.

Feed representative Cartesian trapq paths through the production segfit C
implementation and evaluate the quantized wire polynomials against the same
kinematics callback sampled by Klipper.  This covers a straight trapezoid, a
G2/G3-style arc chord stream, and a finite-junction-speed corner.
"""

import math
import os
import sys

KDIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..",
                    "klippy")
sys.path.insert(0, KDIR)

MCU_FREQ = 12_000_000.
SAMPLE_TIME = .001
SAMPLE_TICKS = round(MCU_FREQ * SAMPLE_TIME)
# A representative CoreXY XY microstep distance.  Keeping the requested
# speeds inside the signed wire coefficient range is part of a valid fit.
STEP_DIST = .00625
SU_PER_MM = 65536. / STEP_DIST
TOLERANCE_SU = 32768.
Q32 = 2. ** 32


def append_profile(lib, trapq, print_time, start, direction, distance,
                   start_v, cruise_v, end_v, accel):
    accel_t = (cruise_v - start_v) / accel
    decel_t = (cruise_v - end_v) / accel
    ramp_distance = ((start_v + cruise_v) * .5 * accel_t
                     + (end_v + cruise_v) * .5 * decel_t)
    cruise_t = (distance - ramp_distance) / cruise_v
    assert accel_t >= 0. and decel_t >= 0. and cruise_t >= 0.
    lib.trapq_append(trapq, print_time, accel_t, cruise_t, decel_t,
                     start[0], start[1], start[2],
                     direction[0], direction[1], direction[2],
                     start_v, cruise_v, accel)
    return print_time + accel_t + cruise_t + decel_t


def append_arc(lib, trapq, print_time, radius=20., speed=20., chords=48):
    points = [(radius * math.cos(i * math.pi / (2. * chords)),
               radius * math.sin(i * math.pi / (2. * chords)), 0.)
              for i in range(chords + 1)]
    for start, end in zip(points[:-1], points[1:]):
        delta = tuple(b - a for a, b in zip(start, end))
        length = math.sqrt(sum(v * v for v in delta))
        direction = tuple(v / length for v in delta)
        duration = length / speed
        lib.trapq_append(trapq, print_time, 0., duration, 0.,
                         start[0], start[1], start[2],
                         direction[0], direction[1], direction[2],
                         speed, speed, 0.)
        print_time += duration
    return print_time


def collect_segments(lib, sf, end_time):
    emitted = []
    while lib.segfit_get_gen_time(sf) < end_time - SAMPLE_TIME * .5:
        before = lib.segfit_get_gen_time(sf)
        count = lib.segfit_generate(sf, end_time)
        assert count >= 0
        data = lib.segfit_get_segs(sf)
        emitted.extend((data[i].duration, data[i].velocity, data[i].accel)
                       for i in range(count))
        if lib.segfit_get_gen_time(sf) <= before:
            break
    count = lib.segfit_finalize(sf)
    assert count >= 0
    data = lib.segfit_get_segs(sf)
    emitted.extend((data[i].duration, data[i].velocity, data[i].accel)
                   for i in range(count))
    return emitted


def fit_axis(ffi, lib, trapq, axis, start_time, end_time):
    sk = ffi.gc(lib.cartesian_stepper_alloc(axis), lib.free)
    lib.itersolve_set_trapq(sk, trapq, STEP_DIST)
    sf = ffi.gc(lib.segfit_alloc(), lib.segfit_free)
    lib.segfit_setup(sf, sk, MCU_FREQ, SU_PER_MM, TOLERANCE_SU,
                     SAMPLE_TIME)
    start_su = round(lib.segfit_get_position(sf, start_time) * SU_PER_MM)
    lib.segfit_set_anchor(sf, start_time, start_su << 32)
    lib.segfit_set_anchor_position(sf, start_su)
    segments = collect_segments(lib, sf, end_time)
    assert segments
    fitted_end = lib.segfit_get_gen_time(sf)
    assert abs(fitted_end - end_time) <= SAMPLE_TIME + 1.e-9, (
        fitted_end, end_time)

    acc = start_su << 32
    elapsed = 0
    worst = 0.
    samples = 0
    for duration, velocity, accel in segments:
        offsets = range(SAMPLE_TICKS, duration + 1, SAMPLE_TICKS)
        offsets = list(offsets)
        if not offsets or offsets[-1] != duration:
            offsets.append(duration)
        for ticks in offsets:
            got = (acc / Q32 + velocity / 65536. * ticks
                   + .5 * accel / Q32 * ticks * ticks)
            when = start_time + (elapsed + ticks) / MCU_FREQ
            want = lib.segfit_get_position(sf, when) * SU_PER_MM
            worst = max(worst, abs(got - want))
            samples += 1
        acc += int(lib.segfit_end_delta_ho(
            duration, velocity, accel, 0, 0, 0))
        elapsed += duration

    assert worst <= TOLERANCE_SU + 1., (axis, worst, TOLERANCE_SU)
    # At an exact trapq boundary the queue's inactive sentinel owns that
    # timestamp.  Sample the representable instant immediately before it to
    # obtain the completed move endpoint instead of the sentinel coordinate.
    target_time = math.nextafter(end_time, start_time)
    target = lib.segfit_get_position(sf, target_time) * SU_PER_MM
    endpoint_error = abs(acc / Q32 - target)
    assert endpoint_error <= TOLERANCE_SU + 1., (
        endpoint_error, acc / Q32, target, len(segments), elapsed)
    return len(segments), samples, worst, endpoint_error


def check_path(name, build_path, axes):
    import chelper
    ffi, lib = chelper.get_ffi()
    trapq = ffi.gc(lib.trapq_alloc(), lib.trapq_free)
    start_time = 10.
    built = build_path(lib, trapq, start_time)
    if isinstance(built, tuple):
        end_time, spans = built
    else:
        end_time, spans = built, {}
    results = [fit_axis(ffi, lib, trapq, axis,
                        spans.get(axis, (start_time, end_time))[0],
                        spans.get(axis, (start_time, end_time))[1])
               for axis in axes]
    print("PASS: %s: %d axes, %d wire segments, %d samples, "
          "worst %.2f su (tol %.2f), endpoint %.2f su"
          % (name, len(axes), sum(r[0] for r in results),
             sum(r[1] for r in results), max(r[2] for r in results),
             TOLERANCE_SU, max(r[3] for r in results)))


def straight(lib, trapq, start_time):
    return append_profile(lib, trapq, start_time, (0., 0., 0.),
                          (1., 0., 0.), 40., 0., 30., 0., 500.)


def arc(lib, trapq, start_time):
    return append_arc(lib, trapq, start_time)


def corner(lib, trapq, start_time):
    # Two perpendicular moves retain a finite 5 mm/s junction velocity,
    # exercising the lookahead-corner shape presented to the joint fitters.
    end = append_profile(lib, trapq, start_time, (0., 0., 0.),
                         (1., 0., 0.), 20., 0., 20., 5., 600.)
    final = append_profile(lib, trapq, end, (20., 0., 0.),
                           (0., 1., 0.), 20., 5., 20., 0., 600.)
    # Each Cartesian joint is active on one side of the corner; production
    # emits a terminal hold for X before Y's active interval begins.
    return final, {b'x': (start_time, end), b'y': (end, final)}


def main():
    check_path("straight trapezoid", straight, (b'x',))
    check_path("48-chord quarter arc", arc, (b'x', b'y'))
    check_path("finite-junction-speed corner", corner, (b'x', b'y'))
    print("segfit_fidelity_test: all paths within motion_tolerance")
    return 0


if __name__ == "__main__":
    sys.exit(main())
