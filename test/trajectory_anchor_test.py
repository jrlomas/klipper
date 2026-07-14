#!/usr/bin/env python3
"""Regression for trajectory anchors after a nonzero trapq restart."""

import os
import sys

KDIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..",
                    "klippy")
sys.path.insert(0, KDIR)

MCU_FREQ = 12_000_000.
SU_PER_MM = 52_428_800.
SAMPLE_TIME = .001


def collect_segments(lib, sf, end_time):
    emitted = []
    n = lib.segfit_generate(sf, end_time)
    segs = lib.segfit_get_segs(sf)
    emitted.extend((segs[i].duration, segs[i].velocity, segs[i].accel)
                   for i in range(n))
    n = lib.segfit_finalize(sf)
    segs = lib.segfit_get_segs(sf)
    emitted.extend((segs[i].duration, segs[i].velocity, segs[i].accel)
                   for i in range(n))
    return emitted


def main():
    import chelper
    ffi, lib = chelper.get_ffi()

    trapq = ffi.gc(lib.trapq_alloc(), lib.trapq_free)
    sk = ffi.gc(lib.cartesian_stepper_alloc(b'z'), lib.free)
    lib.itersolve_set_trapq(sk, trapq, .00125)
    # Model the first retract after a homing trigger.  The legacy iterative
    # solver remains at zero because trajectory steppers do not run it, while
    # the replacement trapq begins at the real 0.166667 mm trigger position.
    lib.itersolve_set_position(sk, 0., 0., 0.)
    start = 0.166667
    lib.trapq_append(trapq, 10., 0., .5, 0., 0., 0., start,
                     0., 0., 1., 3., 3., 0.)

    sf = ffi.gc(lib.segfit_alloc(), lib.segfit_free)
    lib.segfit_setup(sf, sk, MCU_FREQ, SU_PER_MM, 32768., SAMPLE_TIME)
    queued = lib.segfit_get_position(sf, 10.)
    stale = lib.itersolve_get_commanded_pos(sk)
    assert abs(stale) < 1e-12
    assert abs(queued - start) < 1e-12, (queued, start)

    # Model the post-homing physical MCU offset as well: the replacement
    # trapq is in logical joint coordinates, while the board must continue in
    # physical step space without a fitted discontinuity.
    physical_offset = .028
    anchor_su = round((queued + physical_offset) * SU_PER_MM)
    lib.segfit_set_position_offset(sf, physical_offset * SU_PER_MM)
    lib.segfit_set_anchor(sf, 10., anchor_su << 32)
    emitted = collect_segments(lib, sf, 10.100)
    assert emitted
    # The stream must not synthesize the old ~167 mm/s catch-up segment across
    # a false zero/pre-roll anchor.
    max_v_mm_s = max(abs(v) / 65536. * MCU_FREQ / SU_PER_MM
                     for _, v, _ in emitted)
    assert max_v_mm_s < 4., max_v_mm_s
    print("PASS: nonzero trapq anchor ignores stale commanded_pos"
          " (max fitted velocity %.3f mm/s)" % max_v_mm_s)

    # An X-only CoreXY move must produce the same bounded motion profile for
    # both A (X+Y) and B (X-Y) joints.  Give them different physical offsets
    # to prove the offset changes only the anchor, not the fitted velocity.
    corexy_tq = ffi.gc(lib.trapq_alloc(), lib.trapq_free)
    lib.trapq_append(corexy_tq, 20., .5, 0., 0., 2., 3., 0.,
                     1., 0., 0., 3., 3., 0.)
    profiles = []
    for kind, offset in ((b'+', .031), (b'-', -.017)):
        corexy_sk = ffi.gc(lib.corexy_stepper_alloc(kind), lib.free)
        lib.itersolve_set_trapq(corexy_sk, corexy_tq, .00125)
        corexy_sf = ffi.gc(lib.segfit_alloc(), lib.segfit_free)
        lib.segfit_setup(corexy_sf, corexy_sk, MCU_FREQ, SU_PER_MM,
                         32768., SAMPLE_TIME)
        logical = lib.segfit_get_position(corexy_sf, 20.)
        lib.segfit_set_position_offset(corexy_sf, offset * SU_PER_MM)
        physical = round((logical + offset) * SU_PER_MM)
        lib.segfit_set_anchor(corexy_sf, 20., physical << 32)
        profiles.append(collect_segments(lib, corexy_sf, 20.100))
    assert len(profiles[0]) == len(profiles[1])
    for aseg, bseg in zip(*profiles):
        assert aseg[0] == bseg[0]
        assert abs(aseg[1] - bseg[1]) <= 1, (aseg, bseg)
        assert abs(aseg[2] - bseg[2]) <= 1, (aseg, bseg)
    assert profiles[0]
    assert max(abs(v) for _, v, _ in profiles[0]) < 2**31 - 1
    print("PASS: CoreXY X homing emits matching bounded A/B profiles")

    # Reproduce a long sensorless-homing cruise while the host advances its
    # generation horizon in 250ms increments.  Each callback must finalize
    # and send that prefix; retaining it until the 4.096s protocol cap would
    # make the segment start clock several seconds stale.
    stream_tq = ffi.gc(lib.trapq_alloc(), lib.trapq_free)
    lib.trapq_append(stream_tq, 30., 0., 8., 0., 0., 0., 0.,
                     1., 0., 0., 20., 20., 0.)
    stream_sk = ffi.gc(lib.corexy_stepper_alloc(b'+'), lib.free)
    lib.itersolve_set_trapq(stream_sk, stream_tq, .00625)
    stream_sf = ffi.gc(lib.segfit_alloc(), lib.segfit_free)
    stream_su_per_mm = 65536. / .00625
    lib.segfit_setup(stream_sf, stream_sk, MCU_FREQ, stream_su_per_mm,
                     32768., SAMPLE_TIME)
    lib.segfit_set_anchor(stream_sf, 30., 0)
    streamed = []
    for index in range(1, 21):
        horizon = 30. + index * .250
        streamed.extend(collect_segments(lib, stream_sf, horizon))
        assert abs(lib.segfit_get_gen_time(stream_sf) - horizon) < 2.e-6
    assert streamed
    assert max(duration for duration, _, _ in streamed) <= .251 * MCU_FREQ
    velocities = [velocity for _, velocity, _ in streamed]
    assert all(abs(velocity - 1145325) <= 128
               for velocity in velocities), velocities
    print("PASS: incremental homing flush seals every <=251ms prefix")
    return 0


if __name__ == '__main__':
    sys.exit(main())
