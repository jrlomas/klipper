#!/usr/bin/env python3
"""Capture a repeatable heater step through Moonraker.

The script always requests zero target on exit.  It records the same public
heater status for host and MCU controllers so qualification plots do not rely
on private implementation details.
"""

import argparse
import csv
import json
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:7125")
    parser.add_argument("--heater", required=True)
    parser.add_argument("--target", required=True, type=float)
    parser.add_argument("--output", required=True)
    parser.add_argument("--interval", type=float, default=.25)
    parser.add_argument("--settle-band", type=float, default=1.)
    parser.add_argument("--settle-seconds", type=float, default=60.)
    parser.add_argument("--max-seconds", type=float, default=900.)
    args = parser.parse_args()

    first = query_heater(args.url, args.heater)
    if first["target"] or first["power"]:
        raise RuntimeError("heater is not idle at qualification start")
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
            row = {
                "elapsed_s": now - started,
                "temperature_c": state["temperature"],
                "target_c": state["target"],
                "power": state["power"],
                "controller_state": control.get("state", "host"),
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
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print("ERROR: %s" % (error,), file=sys.stderr)
        sys.exit(1)
