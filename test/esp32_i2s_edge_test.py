#!/usr/bin/env python3
"""Regression for sampled I2S STEP pulse visibility.

The ESP32 trajectory scheduler may discover that both a STEP and its UNSTEP
are overdue while preparing one future I2S word.  Both transitions must not
be consumed before that word is serialized or the external shift register
sees no pulse at all.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "traj_stepper.c"


def test_i2s_scheduler_stops_after_each_output_edge():
    source = SOURCE.read_text()
    start = source.index("traj_stepper_i2s_advance(uint32_t sample_clock)")
    end = source.index("#endif", start)
    implementation = source[start:end]

    edge_detection = (
        "s->wake_kind == WK_STEP"
        in implementation
        and "s->wake_kind == WK_UNSTEP" in implementation
    )
    assert edge_detection, "I2S feeder does not identify STEP output edges"

    run = implementation.index("traj_stepper_run_event(s)")
    stop = implementation.index("if (output_edge)", run)
    assert stop > run, "I2S feeder does not stop after committing an edge"


def test_i2s_registry_is_cleared_on_config_restart():
    source = SOURCE.read_text()
    start = source.index("traj_stepper_shutdown(void)")
    end = source.index("DECL_SHUTDOWN(traj_stepper_shutdown)", start)
    implementation = source[start:end]
    assert "i2s_traj_stepper_count = 0" in implementation
    assert "i2s_traj_overrun = 0" in implementation


def test_i2s_edge_diagnostics_report_raw_and_serialized_edges():
    shift = (ROOT / "src" / "esp32" / "i2s_shift.c").read_text()
    assert "monitor_step_toggles++" in shift
    assert "step_toggles=%u registry_count=%u" in shift
    assert "I2S_FIFO_LENGTH * 3 / 4" in shift
    assert "refill_budget_cycles=%u deadline_misses=%u" in shift
    source = SOURCE.read_text()
    assert "traj_stepper_i2s_registry_count(void)" in source


if __name__ == "__main__":
    test_i2s_scheduler_stops_after_each_output_edge()
    test_i2s_registry_is_cleared_on_config_restart()
    test_i2s_edge_diagnostics_report_raw_and_serialized_edges()
    print("PASS: I2S feeder preserves every serialized STEP edge"
          " across config restart")
