#!/usr/bin/env python3
"""Capture a repeatable heater step through Moonraker.

The script always requests zero target on exit.  It records the same public
heater status for host and MCU controllers so qualification plots do not rely
on private implementation details.
"""

import argparse
import csv
import json
import math
import sys
import time
import urllib.parse
import urllib.request


def request_json(base_url, path, method="GET"):
    request = urllib.request.Request(base_url + path, method=method)
    with urllib.request.urlopen(request, timeout=5.) as response:
        return json.load(response)


def send_gcode(base_url, script):
    path = "/printer/gcode/script?" + urllib.parse.urlencode({"script": script})
    result = request_json(base_url, path, method="POST")
    if "error" in result:
        raise RuntimeError(result["error"])


def query_heater(base_url, heater):
    encoded = urllib.parse.quote(heater, safe="")
    result = request_json(
        base_url, "/printer/objects/query?webhooks&" + encoded)
    status = result["result"]["status"]
    if status["webhooks"]["state"] != "ready":
        raise RuntimeError(status["webhooks"]["state_message"])
    return status[heater]


def target_command(heater, target):
    if heater == "heater_bed":
        return "M140 S%.3f" % (target,)
    if heater == "extruder":
        return "M104 S%.3f" % (target,)
    return "SET_HEATER_TEMPERATURE HEATER=%s TARGET=%.3f" % (
        heater, target)


def summarize(rows, target, band, hold_seconds):
    powers = [float(row["power"]) for row in rows]
    temperatures = [float(row["temperature_c"]) for row in rows]
    elapsed = [float(row["elapsed_s"]) for row in rows]
    ready_start = None
    ready_at = None
    for index, (stamp, temperature) in enumerate(zip(elapsed, temperatures)):
        if abs(temperature - target) <= band:
            if ready_start is None:
                ready_start = stamp
            if stamp - ready_start >= hold_seconds:
                ready_at = ready_start
                break
        else:
            ready_start = None
    stable_rows = [
        (temperature, power) for stamp, temperature, power in
        zip(elapsed, temperatures, powers)
        if ready_at is not None and stamp >= ready_at]
    stable_temperatures = [item[0] for item in stable_rows]
    stable_powers = [item[1] for item in stable_rows]
    errors = [temperature - target for temperature in stable_temperatures]
    power_deltas = [right - left for left, right in
                    zip(stable_powers, stable_powers[1:])]

    def mean(values):
        return sum(values) / len(values) if values else None

    def rms(values):
        return math.sqrt(sum(value * value for value in values) / len(values)) \
            if values else None

    def stddev(values):
        average = mean(values)
        return rms([value - average for value in values]) \
            if average is not None else None

    first_crossing = next((stamp for stamp, temperature in
                           zip(elapsed, temperatures)
                           if temperature >= target), None)
    return {
        "target_c": target,
        "initial_temperature_c": temperatures[0],
        "ready_band_c": band,
        "ready_hold_s": hold_seconds,
        "time_to_print_s": ready_at,
        "time_to_first_crossing_s": first_crossing,
        "qualification_duration_s": elapsed[-1],
        "maximum_temperature_c": max(temperatures),
        "overshoot_c": max(0., max(temperatures) - target),
        "steady_samples": len(stable_rows),
        "steady_temperature_mean_c": mean(stable_temperatures),
        "steady_temperature_stddev_c": stddev(stable_temperatures),
        "steady_temperature_error_rms_c": rms(errors),
        "steady_temperature_peak_error_c": (
            max((abs(value) for value in errors), default=None)),
        "steady_power_mean": mean(stable_powers),
        "steady_power_stddev": stddev(stable_powers),
        "steady_power_delta_rms": rms(power_deltas),
        "fault_samples": sum(bool(int(row["fault"])) for row in rows),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:7125")
    parser.add_argument("--heater", required=True)
    parser.add_argument("--target", required=True, type=float)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--summary", help="JSON summary path (default: OUTPUT.json)")
    parser.add_argument("--interval", type=float, default=.25)
    parser.add_argument(
        "--max-initial-temperature", type=float,
        help="Refuse a warm-start capture above this temperature")
    parser.add_argument("--settle-band", type=float, default=1.)
    parser.add_argument("--settle-seconds", type=float, default=60.)
    parser.add_argument("--max-seconds", type=float, default=900.)
    args = parser.parse_args()

    first = query_heater(args.url, args.heater)
    if first["target"] or first["power"]:
        raise RuntimeError("heater is not idle at qualification start")
    if (args.max_initial_temperature is not None
            and first["temperature"] > args.max_initial_temperature):
        raise RuntimeError(
            "heater is too warm for qualification start "
            "(%.2fC > %.2fC)" % (
                first["temperature"], args.max_initial_temperature))
    started = time.monotonic()
    settled_since = None
    rows = []
    send_gcode(args.url, target_command(args.heater, args.target))
    try:
        while True:
            now = time.monotonic()
            state = query_heater(args.url, args.heater)
            control = state.get("mcu_control", {})
            if not control:
                control = state.get("control_stats", {})
            model = control.get("thermal_model", {})
            control_band = model.get("control_band", "")
            if control_band == "" and model.get("control_band_mdeg") != "":
                control_band = model["control_band_mdeg"] / 1000.
            row = {
                "elapsed_s": now - started,
                "heater_type": state.get(
                    "heater_type", control.get("heater_type", "")),
                "temperature_c": state["temperature"],
                "target_c": state["target"],
                "power": state["power"],
                "controller_state": control.get("state", "host"),
                "controller_algorithm": control.get("algorithm", ""),
                "control_mode": control.get("control_mode", "host"),
                "fault": control.get("fault", 0),
                "mcu_samples": control.get("samples", ""),
                "mcu_temperature_c": control.get("mcu_temperature", ""),
                "mcu_temperature_estimate_c": control.get(
                    "mcu_temperature_estimate", ""),
                "mcu_temperature_valid": control.get(
                    "mcu_temperature_valid", ""),
                "loop_clock": control.get("loop_clock", ""),
                "loop_clock_frequency": control.get(
                    "loop_clock_frequency", ""),
                "loop_clock_source": control.get("loop_clock_source", ""),
                "loop_dt_count": control.get("loop_dt_count", ""),
                "loop_dt_mean_s": control.get("loop_dt_mean", ""),
                "loop_dt_stddev_s": control.get("loop_dt_stddev", ""),
                "loop_dt_min_s": control.get("loop_dt_min", ""),
                "loop_dt_max_s": control.get("loop_dt_max", ""),
                "thermal_model_source": control.get(
                    "thermal_model_source", ""),
                "thermal_model_gain": model.get("gain", ""),
                "thermal_model_tau_s": model.get("tau", ""),
                "thermal_model_delay_s": model.get("delay", ""),
                "thermal_model_horizon_s": model.get("horizon", ""),
                "thermal_control_band_c": control_band,
                "host_predictive_output": control.get(
                    "host_predictive_output", ""),
                "host_predictive_bias": control.get(
                    "host_predictive_bias", ""),
                "host_predictive_filtered_temperature_c": control.get(
                    "host_predictive_filtered_temperature", ""),
                "host_predictive_ambient_c": control.get(
                    "host_predictive_ambient", ""),
                "host_predictive_approach_active": control.get(
                    "host_predictive_approach_active", ""),
                "host_predictive_approach_blend": control.get(
                    "host_predictive_approach_blend", ""),
            }
            rows.append(row)
            print("%.1fs %.2fC / %.1fC power=%.3f state=%s fault=%s" % (
                row["elapsed_s"], row["temperature_c"], row["target_c"],
                row["power"], row["controller_state"], row["fault"]),
                flush=True)
            if abs(state["temperature"] - args.target) <= args.settle_band:
                if settled_since is None:
                    settled_since = now
                elif now - settled_since >= args.settle_seconds:
                    break
            else:
                settled_since = None
            if now - started >= args.max_seconds:
                raise RuntimeError("qualification step timed out")
            time.sleep(args.interval)
    finally:
        send_gcode(args.url, target_command(args.heater, 0.))
        if rows:
            with open(args.output, "w", newline="") as output:
                writer = csv.DictWriter(output, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)
    summary = summarize(
        rows, args.target, args.settle_band, args.settle_seconds)
    summary.update({
        "heater": args.heater,
        "heater_type": rows[-1]["heater_type"],
        "controller_state": rows[-1]["controller_state"],
        "controller_algorithm": rows[-1]["controller_algorithm"],
        "control_mode": rows[-1]["control_mode"],
        "thermal_model_source": rows[-1]["thermal_model_source"],
        "thermal_model_gain": rows[-1]["thermal_model_gain"],
        "thermal_model_tau_s": rows[-1]["thermal_model_tau_s"],
        "thermal_model_delay_s": rows[-1]["thermal_model_delay_s"],
        "thermal_model_horizon_s": rows[-1]["thermal_model_horizon_s"],
        "thermal_control_band_c": rows[-1]["thermal_control_band_c"],
        "source_csv": args.output,
    })
    summary_path = args.summary or args.output + ".json"
    with open(summary_path, "w") as output:
        json.dump(summary, output, indent=2, sort_keys=True)
        output.write("\n")
    print("time-to-print=%.2fs overshoot=%.3fC steady-stddev=%.4fC" % (
        summary["time_to_print_s"], summary["overshoot_c"],
        summary["steady_temperature_stddev_c"]), flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print("ERROR: %s" % (error,), file=sys.stderr)
        sys.exit(1)
