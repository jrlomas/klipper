#!/usr/bin/env python3
import os
import subprocess
import tempfile


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_acq_ring():
    output = os.path.join(tempfile.gettempdir(), "acq_ring_test")
    subprocess.check_call([
        "gcc", "-std=gnu11", "-Wall", "-Wextra", "-Werror",
        "-I", ROOT, "-I", ROOT + "/src",
        os.path.join(ROOT, "test", "acq_ring_test.c"),
        os.path.join(ROOT, "src", "generic", "acq_ring.c"),
        "-o", output,
    ])
    subprocess.check_call([output])


if __name__ == "__main__":
    test_acq_ring()
