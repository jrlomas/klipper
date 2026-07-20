#!/usr/bin/env python3
import pathlib
import subprocess
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_heater_control_math():
    output = pathlib.Path(tempfile.gettempdir()) / "heater_control_math_test"
    subprocess.run([
        "cc", "-std=gnu11", "-Wall", "-Wextra", "-Werror",
        "-I", str(ROOT), "-I", str(ROOT / "src"),
        str(ROOT / "test" / "heater_control_math_test.c"),
        str(ROOT / "src" / "generic" / "heater_control_math.c"),
        "-o", str(output),
    ], check=True)
    subprocess.run([str(output)], check=True)


if __name__ == "__main__":
    test_heater_control_math()
