#!/usr/bin/env python3
"""Compare HELIX MCU pulses with Klipper's original V1 step generator.

The same trapq is sent down two independent paths:
  * original itersolve -> stepcompress, expanded into the pulses an MCU sees;
  * segfit -> the exact traj_solve_step() compiled from src/traj_stepper.c.

This is deliberately an edge-by-edge comparison, not merely an endpoint
check.  It covers an accel/cruise/decel homing-style move, its early trigger
prefix, and a reverse retract from a non-zero physical/rebased position.
"""
import ctypes
import os
import subprocess
import sys
import tempfile
import random

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'klippy'))
sys.path.insert(0, os.path.join(ROOT, 'klippy', 'extras'))

import chelper
import trajectory_queuing

MCU_FREQ = 12_000_000.
STEP_DIST = .00125
SU_PER_MM = 65536. / STEP_DIST
START_TIME = 1.
MAX_EDGE_ERROR_US = 550.


def build_mcu_solver():
    out = os.path.join(tempfile.gettempdir(), 'helix_traj_pulse_solver.so')
    src = os.path.join(ROOT, 'test', 'trajectory_v1_pulse_solver.c')
    cmd = [os.environ.get('CC', 'cc'), '-shared', '-fPIC',
           '-ffunction-sections', '-fdata-sections',
           '-Wl,--gc-sections', '-I' + ROOT,
           '-I' + os.path.join(ROOT, 'out'),
           '-I' + os.path.join(ROOT, 'out', 'board-generic'),
           '-I' + os.path.join(ROOT, 'src'), '-o', out, src]
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE,
                   stderr=subprocess.PIPE)
    solver = ctypes.CDLL(out, mode=os.RTLD_LAZY)
    solver.helix_test_solve_step.argtypes = [
        ctypes.c_uint32, ctypes.c_int32, ctypes.c_int32,
        ctypes.c_uint32, ctypes.c_int64, ctypes.c_int32,
        ctypes.POINTER(ctypes.c_uint32)]
    solver.helix_test_solve_step.restype = ctypes.c_int
    solver.helix_test_target16.argtypes = [
        ctypes.c_int64, ctypes.c_int32, ctypes.c_int32]
    solver.helix_test_target16.restype = ctypes.c_int64
    solver.helix_test_is_pure_cruise.argtypes = [
        ctypes.c_uint8, ctypes.c_int32, ctypes.c_int32,
        ctypes.c_int32, ctypes.c_int32]
    solver.helix_test_is_pure_cruise.restype = ctypes.c_int
    solver.helix_test_divide_residual.argtypes = [
        ctypes.c_int64, ctypes.c_int32, ctypes.c_uint32]
    solver.helix_test_divide_residual.restype = ctypes.c_int64
    solver.helix_test_smul_shr16_s32.argtypes = [
        ctypes.c_int32, ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_int32)]
    solver.helix_test_smul_shr16_s32.restype = ctypes.c_int
    solver.helix_test_solve_step_ho.argtypes = [
        ctypes.c_uint32, ctypes.c_int32, ctypes.c_int32,
        ctypes.c_int32, ctypes.c_int32, ctypes.c_int32,
        ctypes.c_uint32, ctypes.c_int64, ctypes.c_int32,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32)]
    solver.helix_test_solve_step_ho.restype = ctypes.c_int
    return solver


def build_deadline_math():
    out = os.path.join(tempfile.gettempdir(), 'helix_traj_deadline_math.so')
    src = os.path.join(ROOT, 'test', 'trajq_deadline_math.c')
    cmd = [os.environ.get('CC', 'cc'), '-shared', '-fPIC',
           '-ffunction-sections', '-fdata-sections',
           '-Wl,--gc-sections', '-I' + ROOT,
           '-I' + os.path.join(ROOT, 'out'),
           '-I' + os.path.join(ROOT, 'out', 'board-generic'),
           '-I' + os.path.join(ROOT, 'src'), '-o', out, src]
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE,
                   stderr=subprocess.PIPE)
    math = ctypes.CDLL(out, mode=os.RTLD_LAZY)
    math.helix_test_sdiv64_120.argtypes = [ctypes.c_int64]
    math.helix_test_sdiv64_120.restype = ctypes.c_int64
    math.helix_test_sdiv64_24_to_s32.argtypes = [ctypes.c_int64]
    math.helix_test_sdiv64_24_to_s32.restype = ctypes.c_int32
    math.helix_test_smul_shr_deadline.argtypes = [
        ctypes.c_int64, ctypes.c_uint32, ctypes.c_uint32]
    math.helix_test_smul_shr_deadline.restype = ctypes.c_int64
    math.helix_test_scale_i32_deadline.argtypes = [
        ctypes.c_int32, ctypes.c_uint32]
    math.helix_test_scale_i32_deadline.restype = ctypes.c_int64
    return math


def signed_i64(value):
    value &= (1 << 64) - 1
    return value - (1 << 64) if value & (1 << 63) else value


def append_move(lib, tq, start_z, distance, speed, accel):
    accel_t = speed / accel
    accel_d = .5 * accel * accel_t * accel_t
    cruise_d = abs(distance) - 2. * accel_d
    assert cruise_d >= 0.
    cruise_t = cruise_d / speed
    direction = 1. if distance > 0. else -1.
    lib.trapq_append(tq, START_TIME, accel_t, cruise_t, accel_t,
                     0., 0., start_z, 0., 0., direction,
                     0., speed, accel)
    return START_TIME + 2. * accel_t + cruise_t


def make_path(start_z, distance, speed, accel):
    ffi, lib = chelper.get_ffi()
    tq = ffi.gc(lib.trapq_alloc(), lib.trapq_free)
    sk = ffi.gc(lib.cartesian_stepper_alloc(b'z'), lib.free)
    lib.itersolve_set_trapq(sk, tq, STEP_DIST)
    lib.itersolve_set_position(sk, 0., 0., start_z)
    end_time = append_move(lib, tq, start_z, distance, speed, accel)
    return ffi, lib, tq, sk, end_time


def legacy_pulses(start_z, distance, speed, accel):
    ffi, lib, tq, sk, end_time = make_path(
        start_z, distance, speed, accel)
    ssm = ffi.gc(lib.steppersyncmgr_alloc(), lib.steppersyncmgr_free)
    ss = lib.steppersyncmgr_alloc_steppersync(ssm)
    se = lib.steppersync_alloc_syncemitter(ss, b'v1-compare', 1)
    sc = lib.syncemitter_get_stepcompress(se)
    # Same 25us compression tolerance used by MCU_stepper.
    lib.stepcompress_fill(sc, 0, round(MCU_FREQ * .000025), 1, 2)
    lib.steppersync_set_time(ss, 0., MCU_FREQ)
    lib.syncemitter_set_stepper_kinematics(se, sk)
    start_mcu_pos = round(start_z / STEP_DIST)
    assert not lib.stepcompress_set_last_position(sc, 0, start_mcu_pos)
    assert not lib.itersolve_generate_steps(sk, sc, end_time)
    assert not lib.stepcompress_flush(
        sc, round(end_time * MCU_FREQ) + 1000)
    records = ffi.new('struct pull_history_steps[]', 10000)
    count = lib.stepcompress_extract_old(sc, records, 10000, 0, 2**63)
    pulses = []
    for i in range(count):
        rec = records[i]
        direction = 1 if rec.step_count > 0 else -1
        for j in range(abs(rec.step_count)):
            clock = (rec.first_clock + j * rec.interval
                     + rec.add * j * (j - 1) // 2)
            pulses.append((clock, direction))
    pulses.sort()
    return pulses, end_time


def fitted_segments(start_z, distance, speed, accel, end_time):
    ffi, lib, tq, sk, _ = make_path(start_z, distance, speed, accel)
    sf = ffi.gc(lib.segfit_alloc(), lib.segfit_free)
    lib.segfit_setup(sf, sk, MCU_FREQ, SU_PER_MM, 32768., .001)
    anchor_su = round(start_z / STEP_DIST * 65536.)
    lib.segfit_set_anchor(sf, START_TIME, signed_i64(anchor_su << 32))
    lib.segfit_set_anchor_position(sf, anchor_su)
    segments = []
    while lib.segfit_get_gen_time(sf) < end_time - 1.e-9:
        count = lib.segfit_generate(sf, end_time)
        data = lib.segfit_get_segs(sf)
        segments.extend((data[i].duration, data[i].velocity, data[i].accel)
                        for i in range(count))
        if not count:
            break
    count = lib.segfit_finalize(sf)
    data = lib.segfit_get_segs(sf)
    segments.extend((data[i].duration, data[i].velocity, data[i].accel)
                    for i in range(count))
    return anchor_su, segments


def fitted_quintic_segments(start_z, distance, speed, accel, end_time):
    ffi, lib, tq, sk, _ = make_path(start_z, distance, speed, accel)
    sf = ffi.gc(lib.segfit_alloc(), lib.segfit_free)
    lib.segfit_setup(sf, sk, MCU_FREQ, SU_PER_MM, 32768., .001)
    lib.segfit_set_order(sf, 2)
    anchor_su = round(start_z / STEP_DIST * 65536.)
    lib.segfit_set_anchor(sf, START_TIME, signed_i64(anchor_su << 32))
    lib.segfit_set_anchor_position(sf, anchor_su)
    segments = []
    while lib.segfit_get_gen_time(sf) < end_time - 1.e-9:
        count = lib.segfit_generate(sf, end_time)
        data = lib.segfit_get_segs(sf)
        segments.extend((data[i].duration, data[i].velocity, data[i].accel,
                         data[i].jerk, data[i].snap, data[i].crackle)
                        for i in range(count))
        if not count:
            break
    count = lib.segfit_finalize(sf)
    data = lib.segfit_get_segs(sf)
    segments.extend((data[i].duration, data[i].velocity, data[i].accel,
                     data[i].jerk, data[i].snap, data[i].crackle)
                    for i in range(count))
    return anchor_su, segments


def helix_pulses(solver, start_z, distance, speed, accel, end_time):
    anchor_su, segments = fitted_segments(
        start_z, distance, speed, accel, end_time)
    acc = anchor_su << 32
    mpos = round(start_z / STEP_DIST)
    start_clock = round(START_TIME * MCU_FREQ)
    pulses = []
    for duration, velocity, acceleration in segments:
        vend = velocity + ((acceleration * duration) >> 16)
        direction = (1 if velocity > 0 else -1 if velocity < 0
                     else 1 if acceleration > 0 else -1 if acceleration < 0
                     else 1 if vend > 0 else -1)
        target16 = solver.helix_test_target16(
            signed_i64(acc), mpos, direction)
        t_prev = 0
        while True:
            step_t = ctypes.c_uint32()
            result = solver.helix_test_solve_step(
                duration, velocity, acceleration, t_prev, target16,
                direction, ctypes.byref(step_t))
            if not result:
                break
            t_prev = step_t.value
            if result == 2:             # zero-speed poll, not a pulse
                continue
            pulses.append((start_clock + step_t.value, direction))
            mpos += direction
            target16 += direction * (1 << 32)
        acc += trajectory_queuing.py_end_delta_ho(
            duration, velocity, acceleration)
        start_clock += duration
    return pulses, mpos


def helix_quintic_pulses(solver, start_z, distance, speed, accel, end_time):
    anchor_su, segments = fitted_quintic_segments(
        start_z, distance, speed, accel, end_time)
    acc = anchor_su << 32
    mpos = round(start_z / STEP_DIST)
    start_clock = round(START_TIME * MCU_FREQ)
    pulses = []
    prior_interval = 0
    prior_dir = 0
    for duration, velocity, acceleration, jerk, snap, crackle in segments:
        end_delta = trajectory_queuing.py_end_delta_ho(
            duration, velocity, acceleration, jerk, snap, crackle)
        vend = velocity + ((acceleration * duration) >> 16)
        direction = (1 if end_delta > 0 else -1 if end_delta < 0
                     else 1 if velocity > 0 else -1 if velocity < 0
                     else 1 if vend >= 0 else -1)
        if direction != prior_dir:
            prior_interval = 0
        target16 = solver.helix_test_target16(
            signed_i64(acc), mpos, direction)
        first_guess = ((abs(target16) * prior_interval) >> 32
                       if prior_interval else 0)
        if prior_interval and not first_guess:
            first_guess = 1
        t_prev = 0
        last_step_t = 0
        while True:
            step_t = ctypes.c_uint32()
            result = solver.helix_test_solve_step_ho(
                duration, velocity, acceleration, jerk, snap, crackle,
                t_prev, target16, direction, prior_interval, last_step_t,
                first_guess, ctypes.byref(step_t))
            if not result:
                break
            if result == 2:
                t_prev = step_t.value
                continue
            assert step_t.value > t_prev, (
                duration, t_prev, step_t.value, target16)
            pulses.append((start_clock + step_t.value, direction))
            mpos += direction
            target16 += direction * (1 << 32)
            if last_step_t:
                prior_interval = step_t.value - last_step_t
            last_step_t = step_t.value
            t_prev = step_t.value
        acc = signed_i64(acc + end_delta)
        start_clock += duration
        prior_dir = direction
    return pulses, mpos


def check_phase_boundaries(solver):
    # The low trajectory phase wraps every 65536 microsteps while the physical
    # counter remains unwrapped.  At an exact step center the next threshold
    # must always remain one half-step away on either side of every wrap.
    for mpos in (32767, 32768, 32769, -32767, -32768, -32769, 200000):
        center_acc = signed_i64((mpos * 65536) << 32)
        assert solver.helix_test_target16(center_acc, mpos, 1) == 1 << 31
        assert solver.helix_test_target16(center_acc, mpos, -1) == -(1 << 31)
        offset_acc = signed_i64((mpos * 65536 + 12345) << 32)
        assert solver.helix_test_target16(offset_acc, mpos, 1) == (
            (32768 - 12345) << 16)
        assert solver.helix_test_target16(offset_acc, mpos, -1) == (
            (-32768 - 12345) << 16)
    print("PASS: MCU half-step thresholds remain local across phase wrap")


def check_quintic_cruise_fastpath(solver):
    quintic = 2 << 6
    assert solver.helix_test_is_pure_cruise(quintic, 0, 0, 0, 0)
    assert not solver.helix_test_is_pure_cruise(quintic, 1, 0, 0, 0)
    assert not solver.helix_test_is_pure_cruise(quintic, 0, 1, 0, 0)
    assert not solver.helix_test_is_pure_cruise(quintic, 0, 0, 1, 0)
    assert not solver.helix_test_is_pure_cruise(quintic, 0, 0, 0, 1)
    print("PASS: degenerate quintic cruises use the division-light solver")


def check_bounded_residual_division(solver):
    vectors = [
        (0, 1, 100), (1, 1, 100), (-1, 1, 100),
        ((1 << 32) - 1, 77024, 1856000),
        (1 << 32, 77024, 1856000),
        ((1 << 32) + 123456789, -77024, 1856000),
        ((1 << 33) + 7, 2_000_000_000, 0xffffffff),
        ((1 << 63) - 1, 1, 0xffffffff),
        (-(1 << 63), -0x80000000, 0xffffffff),
    ]
    rng = random.Random(0x48454c4958)
    for _ in range(2000):
        residual = rng.randrange(-(1 << 63), 1 << 63)
        velocity = rng.randrange(-(1 << 31), 1 << 31)
        if not velocity:
            velocity = 1
        limit = rng.randrange(1, 1 << 32)
        vectors.append((residual, velocity, limit))
    for residual, velocity, limit in vectors:
        magnitude = abs(residual) // abs(velocity)
        magnitude = min(magnitude, limit)
        # Newton correction is -residual/velocity, truncated toward zero.
        negative = (residual < 0) == (velocity < 0)
        expected = -magnitude if negative else magnitude
        got = solver.helix_test_divide_residual(
            residual, velocity, limit)
        assert got == expected, (residual, velocity, limit, got, expected)
    print("PASS: bounded 64/32 Newton quotient matches exact division")


def check_shifted_s32_multiply(solver):
    rng = random.Random(0x53484946543136)
    vectors = [(0, 0), (1, 0xffffffff), (-1, 0xffffffff),
               ((1 << 31) - 1, 0xffffffff), (-(1 << 31), 0xffffffff)]
    vectors.extend((rng.randrange(-(1 << 31), 1 << 31),
                    rng.randrange(0, 1 << 32)) for _ in range(4000))
    for value, ticks in vectors:
        magnitude = (abs(value) * ticks) >> 16
        expected = -magnitude if value < 0 else magnitude
        result = ctypes.c_int32()
        fits = -(1 << 31) <= expected < (1 << 31)
        got_fits = bool(solver.helix_test_smul_shr16_s32(
            value, ticks, ctypes.byref(result)))
        assert got_fits == fits, (value, ticks, got_fits, fits)
        if fits:
            assert result.value == expected, (
                value, ticks, result.value, expected)
    print("PASS: compact shifted multiply preserves exact signed results")


def check_deadline_constant_division(math):
    rng = random.Random(0x5155494e544943)
    vectors120 = [0, 1, -1, (1 << 63) - 1, -(1 << 63)]
    vectors120.extend(
        rng.randrange(-(1 << 63), 1 << 63) for _ in range(4000))
    for value in vectors120:
        magnitude = abs(value) // 120
        expected = -magnitude if value < 0 else magnitude
        got = math.helix_test_sdiv64_120(value)
        assert got == expected, ('/120', value, got, expected)
    limit = (1 << 31) - 1
    vectors24 = [0, 1, -1, limit * 24, -limit * 24]
    vectors24.extend(
        rng.randrange(-limit * 24, limit * 24 + 1) for _ in range(4000))
    for value in vectors24:
        magnitude = abs(value) // 24
        expected = -magnitude if value < 0 else magnitude
        got = math.helix_test_sdiv64_24_to_s32(value)
        assert got == expected, ('/24', value, got, expected)
    print("PASS: deadline constant quotients match exact signed division")


def check_deadline_multiply(math):
    rng = random.Random(0x4d554c5449504c59)
    vectors = [(0, 0), (1, 0xffffffff), (-1, 0xffffffff),
               ((1 << 31) - 1, 0xffffffff), (-(1 << 31), 0xffffffff)]
    vectors.extend((rng.randrange(-(1 << 31), 1 << 31),
                    rng.randrange(0, 1 << 32)) for _ in range(4000))
    for value, ticks in vectors:
        for shift in (0, 16):
            magnitude = (abs(value) * ticks) >> shift
            expected = -magnitude if value < 0 else magnitude
            got = math.helix_test_smul_shr_deadline(value, ticks, shift)
            assert got == expected, (value, ticks, shift, got, expected)
    print("PASS: deadline single-multiply path preserves fixed-point math")
    for value, _ in vectors:
        for factor in (4, 5, 12, 20, 24, 60, 120):
            got = math.helix_test_scale_i32_deadline(value, factor)
            assert got == value * factor, (value, factor, got)
    print("PASS: native coefficient scaling preserves signed products")


def compare_case(solver, name, start_z, distance, speed, accel,
                 trigger_after=None):
    legacy, end_time = legacy_pulses(start_z, distance, speed, accel)
    helix, end_mpos = helix_pulses(
        solver, start_z, distance, speed, accel, end_time)
    if trigger_after is not None:
        cutoff = round((START_TIME + trigger_after) * MCU_FREQ)
        legacy = [p for p in legacy if p[0] <= cutoff]
        helix = [p for p in helix if p[0] <= cutoff]
    assert len(helix) == len(legacy), (name, len(legacy), len(helix))
    assert [p[1] for p in helix] == [p[1] for p in legacy]
    errors = [hp[0] - vp[0] for hp, vp in zip(helix, legacy)]
    max_us = max(map(abs, errors)) / MCU_FREQ * 1.e6 if errors else 0.
    mean_us = (sum(map(abs, errors)) / len(errors) / MCU_FREQ * 1.e6
               if errors else 0.)
    # Half-microstep spatial fitting tolerance permits larger time offsets
    # near zero velocity; the aggregate edge stream must remain close to V1.
    assert mean_us < 75., (name, mean_us)
    assert max_us < MAX_EDGE_ERROR_US, (name, mean_us, max_us)
    expected_end = round((start_z + distance) / STEP_DIST)
    if trigger_after is None:
        assert end_mpos == expected_end, (name, end_mpos, expected_end)
    print("PASS: %-15s %4d pulses, mean %.2fus, max %.2fus"
          % (name, len(helix), mean_us, max_us))


def compare_quintic_case(solver, name, start_z, distance, speed, accel):
    legacy, end_time = legacy_pulses(start_z, distance, speed, accel)
    helix, end_mpos = helix_quintic_pulses(
        solver, start_z, distance, speed, accel, end_time)
    assert len(helix) == len(legacy), (name, len(legacy), len(helix))
    assert [p[1] for p in helix] == [p[1] for p in legacy]
    errors = [hp[0] - vp[0] for hp, vp in zip(helix, legacy)]
    mean_us = (sum(map(abs, errors)) / len(errors) / MCU_FREQ * 1.e6
               if errors else 0.)
    max_us = max(map(abs, errors)) / MCU_FREQ * 1.e6 if errors else 0.
    # The fitted quintic is allowed half a microstep of spatial error versus
    # itersolve.  At nonzero velocity this timing bound is substantially
    # tighter; near the zero-speed endpoints the mean is the useful measure.
    assert mean_us < 75., (name, mean_us, max_us)
    assert max_us < MAX_EDGE_ERROR_US, (name, mean_us, max_us)
    expected_end = round((start_z + distance) / STEP_DIST)
    assert end_mpos == expected_end, (name, end_mpos, expected_end)
    print("PASS: %-15s %4d quintic pulses, mean %.2fus, max %.2fus"
          % (name, len(helix), mean_us, max_us))


def main():
    solver = build_mcu_solver()
    math = build_deadline_math()
    check_phase_boundaries(solver)
    check_quintic_cruise_fastpath(solver)
    check_bounded_residual_division(solver)
    check_shifted_s32_multiply(solver)
    check_deadline_constant_division(math)
    check_deadline_multiply(math)
    compare_case(solver, 'homing profile', 0., 5., 20., 300.)
    compare_case(solver, 'trigger prefix', 0., 5., 20., 300., .035)
    compare_case(solver, 'reverse retract', .180, -3., 10., 300.)
    compare_case(solver, 'phase wrap', 39., 4., 20., 300.)
    compare_quintic_case(solver, 'q homing', 0., 5., 20., 300.)
    compare_quintic_case(solver, 'q reverse', .180, -3., 10., 300.)
    compare_quintic_case(solver, 'q phase wrap', 39., 4., 20., 300.)
    compare_quintic_case(solver, 'q short', 1.25, .3, 5., 300.)
    compare_quintic_case(solver, 'q fast EBB', 0., 5., 24., 1000.)
    return 0


if __name__ == '__main__':
    sys.exit(main())
