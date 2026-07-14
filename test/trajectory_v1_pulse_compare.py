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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'klippy'))
sys.path.insert(0, os.path.join(ROOT, 'klippy', 'extras'))

import chelper
import trajectory_queuing

MCU_FREQ = 12_000_000.
STEP_DIST = .00125
SU_PER_MM = 65536. / STEP_DIST
START_TIME = 1.


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
    return solver


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
    lib.segfit_set_anchor(sf, START_TIME, anchor_su << 32)
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
        boundary = ((2 * mpos + 1) << 47 if direction > 0
                    else (2 * mpos - 1) << 47)
        target16 = (boundary - acc) >> 16
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
    expected_end = round((start_z + distance) / STEP_DIST)
    if trigger_after is None:
        assert end_mpos == expected_end, (name, end_mpos, expected_end)
    print("PASS: %-15s %4d pulses, mean %.2fus, max %.2fus"
          % (name, len(helix), mean_us, max_us))


def main():
    solver = build_mcu_solver()
    compare_case(solver, 'homing profile', 0., 5., 20., 300.)
    compare_case(solver, 'trigger prefix', 0., 5., 20., 300., .035)
    compare_case(solver, 'reverse retract', .180, -3., 10., 300.)
    return 0


if __name__ == '__main__':
    sys.exit(main())
