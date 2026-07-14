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

    anchor_su = round(queued * SU_PER_MM)
    lib.segfit_set_anchor(sf, 10., anchor_su << 32)
    emitted = []
    n = lib.segfit_generate(sf, 10.100)
    segs = lib.segfit_get_segs(sf)
    emitted.extend((segs[i].duration, segs[i].velocity, segs[i].accel)
                   for i in range(n))
    n = lib.segfit_finalize(sf)
    segs = lib.segfit_get_segs(sf)
    emitted.extend((segs[i].duration, segs[i].velocity, segs[i].accel)
                   for i in range(n))
    assert emitted
    # The stream must not synthesize the old ~167 mm/s catch-up segment across
    # a false zero/pre-roll anchor.
    max_v_mm_s = max(abs(v) / 65536. * MCU_FREQ / SU_PER_MM
                     for _, v, _ in emitted)
    assert max_v_mm_s < 4., max_v_mm_s
    print("PASS: nonzero trapq anchor ignores stale commanded_pos"
          " (max fitted velocity %.3f mm/s)" % max_v_mm_s)
    return 0


if __name__ == '__main__':
    sys.exit(main())
