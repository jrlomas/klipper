#!/usr/bin/env python3
"""Analyze and plot comparable Helix heater qualification CSV files."""

import argparse
import csv
import json
import math
import os
from pathlib import Path


def load_series(spec):
    label, filename = spec.split("=", 1)
    with open(filename, newline="") as source:
        rows = list(csv.DictReader(source))
    numeric = ("elapsed_s", "temperature_c", "target_c", "power")
    for row in rows:
        for key in numeric:
            row[key] = float(row[key])
    return label, rows


def mean(values):
    return sum(values) / len(values)


def stddev(values):
    average = mean(values)
    return math.sqrt(sum((value - average) ** 2 for value in values)
                     / len(values))


def metrics(rows, steady_seconds, band):
    target = rows[-1]["target_c"]
    end_time = rows[-1]["elapsed_s"]
    steady = [row for row in rows
              if row["elapsed_s"] >= end_time - steady_seconds]
    errors = [row["temperature_c"] - target for row in steady]
    powers = [row["power"] for row in steady]
    last_outside = max(
        (index for index, row in enumerate(rows)
         if abs(row["temperature_c"] - target) > band), default=-1)
    stable_index = min(last_outside + 1, len(rows) - 1)
    result = {
        "target_c": target,
        "initial_c": rows[0]["temperature_c"],
        "duration_s": end_time,
        "stable_band_c": band,
        "stable_entry_s": rows[stable_index]["elapsed_s"],
        "peak_c": max(row["temperature_c"] for row in rows),
        "overshoot_c": max(row["temperature_c"] for row in rows) - target,
        "steady_window_s": steady_seconds,
        "steady_mean_error_c": mean(errors),
        "steady_stddev_c": stddev(errors),
        "steady_peak_to_peak_c": max(errors) - min(errors),
        "steady_rmse_c": math.sqrt(mean([error * error
                                          for error in errors])),
        "steady_mean_power": mean(powers),
        "steady_power_stddev": stddev(powers),
    }
    for key in ("loop_dt_count", "loop_dt_mean_s", "loop_dt_stddev_s",
                "loop_dt_min_s", "loop_dt_max_s"):
        value = rows[-1].get(key, "")
        if value not in (None, ""):
            result[key] = float(value)
    return result


def plot(series, output, title):
    os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib-helix")
    import matplotlib.pyplot as plt

    figure, (temperature, power) = plt.subplots(
        2, 1, figsize=(9.0, 5.6), sharex=True,
        gridspec_kw={"height_ratios": [2, 1]})
    for label, rows in series:
        elapsed = [row["elapsed_s"] for row in rows]
        values = [row["temperature_c"] for row in rows]
        duties = [100. * row["power"] for row in rows]
        temperature.plot(elapsed, values, label=label, linewidth=1.35)
        power.plot(elapsed, duties, label=label, linewidth=1.0)
    target = series[0][1][-1]["target_c"]
    temperature.axhline(target, color="#333333", linestyle="--",
                        linewidth=.8, label="target")
    temperature.set_ylabel("Temperature (C)")
    temperature.set_title(title)
    temperature.grid(alpha=.25)
    temperature.legend()
    power.set_xlabel("Elapsed time (s)")
    power.set_ylabel("Duty (%)")
    power.set_ylim(-2., 102.)
    power.grid(alpha=.25)
    figure.tight_layout()
    figure.savefig(output, format="svg", metadata={"Creator": "Helix"})
    plt.close(figure)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--series", action="append", required=True,
                        help="label=csv (repeatable)")
    parser.add_argument("--metrics", required=True)
    parser.add_argument("--plot", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--steady-seconds", type=float, default=60.)
    parser.add_argument("--band", type=float, default=1.)
    args = parser.parse_args()
    series = [load_series(spec) for spec in args.series]
    results = {label: metrics(rows, args.steady_seconds, args.band)
               for label, rows in series}
    Path(args.metrics).write_text(json.dumps(results, indent=2) + "\n")
    plot(series, args.plot, args.title)


if __name__ == "__main__":
    main()
