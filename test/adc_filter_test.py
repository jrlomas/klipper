#!/usr/bin/env python3
import os
import pathlib
import random
import subprocess
import sys
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from klippy.extras.adc_stream_model import FilterModel, run_interleaved


def test_c_reference_vectors():
    output = os.path.join(tempfile.gettempdir(), "adc_filter_test")
    subprocess.run([
        "cc", "-std=gnu11", "-Wall", "-Wextra", "-Werror",
        "-I", str(ROOT), "-I", str(ROOT / "src"),
        str(ROOT / "test" / "adc_filter_test.c"),
        str(ROOT / "src" / "generic" / "adc_filter.c"),
        "-o", output,
    ], check=True)
    result = subprocess.run(
        [output], check=True, capture_output=True, text=True)
    assert result.stdout.startswith("PASS:")


def independent_reference(samples, input_div, osr, shift, report_div):
    accepted = [(i, value) for i, value in enumerate(samples)
                if i % input_div == 0]
    outputs = []
    for offset in range(0, len(accepted) - osr + 1, osr):
        group = accepted[offset:offset + osr]
        total = sum(value for _, value in group)
        value = ((total + (1 << (shift - 1))) >> shift
                 if shift else total)
        outputs.append((group[-1][0], min(value, 0xffffffff)))
    reports = []
    for offset in range(0, len(outputs) - report_div + 1, report_div):
        group = outputs[offset:offset + report_div]
        reports.append({
            "count": report_div,
            "minimum": min(v for _, v in group),
            "maximum": max(v for _, v in group),
            "sum": sum(v for _, v in group),
            "first_scan": group[0][0],
            "last_scan": group[-1][0],
            "flags": 0,
        })
    return reports


def test_randomized_reference_model():
    rng = random.Random(0xADC2026)
    for _ in range(500):
        count = rng.randrange(1, 800)
        samples = [rng.randrange(4096) for _ in range(count)]
        input_div = rng.randrange(1, 9)
        osr = rng.randrange(1, 33)
        shift = rng.randrange(0, 9)
        report_div = rng.randrange(1, 17)
        model = FilterModel(input_div, osr, shift, report_div)
        actual = []
        for index, sample in enumerate(samples):
            result = model.push(sample, index)
            if result is not None:
                actual.append(result)
        expected = independent_reference(
            samples, input_div, osr, shift, report_div)
        assert actual == expected


def test_interleaved_channels_and_discontinuity():
    scans = [(i, 1000 - i) for i in range(32)]
    reports = run_interleaved(scans, [
        {"channel": 0, "filter": {
            "input_div": 1, "osr": 4, "shift": 2, "report_div": 2}},
        {"channel": 1, "filter": {
            "input_div": 2, "osr": 2, "shift": 1, "report_div": 2}},
    ])
    assert len(reports[0]) == 4
    assert len(reports[1]) == 4
    model = FilterModel(osr=2, shift=1)
    model.reset(discontinuity=True)
    assert model.push(1, 0) is None
    assert model.push(3, 1)["flags"] == 1


def test_window_average_and_ewma_reference():
    model = FilterModel(osr=4, window_divisor=4, alpha_q15=16384)
    reports = []
    for index, sample in enumerate([100] * 4 + [200] * 4 + [100] * 4):
        result = model.push(sample, index)
        if result is not None:
            reports.append(result["sum"])
    assert reports == [100, 150, 125]
    model.reset(discontinuity=True)
    result = None
    for index in range(4):
        result = model.push(50, index)
    assert result["sum"] == 50 and result["flags"] == 1


if __name__ == "__main__":
    test_c_reference_vectors()
    test_randomized_reference_model()
    test_interleaved_channels_and_discontinuity()
    test_window_average_and_ewma_reference()
    print("PASS: deterministic randomized ADC filter reference")
