#!/usr/bin/env python3
import pathlib
import subprocess
import tempfile

root = pathlib.Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory() as tmp:
    output = pathlib.Path(tmp) / "acq_capture_test"
    subprocess.run([
        "cc", "-std=gnu11", "-Wall", "-Wextra", "-Werror",
        "-I", str(root), "-I", str(root / "src"),
        str(root / "test/acq_capture_test.c"),
        str(root / "src/generic/acq_capture.c"), "-o", str(output),
    ], check=True)
    subprocess.run([str(output)], check=True)
