#!/usr/bin/env python3
"""Run the computation-only HELIX trajectory solver benchmark on an MCU."""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helix_flash import FlashError, Link, Proto


STATUS = {
    0: "PASS",
    1: "BAD_ARGS",
    2: "SETUP",
    3: "SOLVER",
    4: "SPATIAL",
    5: "DEADLINE",
}


def comma_ints(value):
    try:
        values = [int(item.strip()) for item in value.split(',')]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if not values or any(item <= 0 for item in values):
        raise argparse.ArgumentTypeError("values must be positive integers")
    return values


def main():
    parser = argparse.ArgumentParser(
        description="measure on-MCU quintic crossing synthesis without I/O")
    parser.add_argument("--device", required=True,
                        help="MCU serial device (for example /dev/ttyACM0)")
    parser.add_argument("--baud", type=int, default=250000)
    parser.add_argument("--rates", type=comma_ints,
                        default=comma_ints("20000,40000,80000,160000,320000"),
                        help="comma-separated requested step rates per axis")
    parser.add_argument("--axes", type=comma_ints,
                        default=comma_ints("1,2,4,8"),
                        help="comma-separated virtual axis counts (1-8)")
    parser.add_argument("--captured-scales", type=comma_ints,
                        help="probe the captured EBB quintic at power-of-two"
                             " time-compression scales instead of the"
                             " synthetic rate sweep")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    link = Link(device=args.device, baud=args.baud)
    failed = False
    try:
        proto = Proto(link, verbose=args.verbose)
        dictionary = proto.fetch_dictionary()
        config = dictionary.get("config", {})
        clock = int(config.get("CLOCK_FREQ", 0))
        print("firmware=%s clock=%d" %
              (dictionary.get("version", "?"), clock))
        if args.captured_scales:
            print("scale status pulses max_ticks min_interval reserve_pct"
                  " max_error_eighths")
            for scale in args.captured_scales:
                proto.send("run_captured_quintic_probe", scale)
                values = proto.wait_response(
                    "captured_quintic_probe_result", 10.0)
                (got_scale, status, pulses, elapsed, interval,
                 error) = values
                if got_scale != scale:
                    raise FlashError(
                        "captured probe response does not match request")
                reserve = (100.0 * (interval - elapsed) / interval
                           if interval else 0.0)
                error_eighths = 8.0 * error / (1 << 32)
                label = STATUS.get(status, "UNKNOWN_%d" % status)
                print("%d %s %d %d %d %.2f %.6f" %
                      (scale, label, pulses, elapsed, interval,
                       reserve, error_eighths))
                failed |= status != 0
            return 1 if failed else 0

        print("rate axes status pulses max_ticks min_interval reserve_pct"
              " max_error_eighths")
        for rate in args.rates:
            for axes in args.axes:
                proto.send("run_traj_benchmark", rate, axes)
                values = proto.wait_response("traj_benchmark_result", 10.0)
                (got_rate, got_axes, status, pulses, elapsed, interval,
                 error) = values
                if got_rate != rate or got_axes != axes:
                    raise FlashError(
                        "benchmark response does not match request")
                reserve = (100.0 * (interval - elapsed) / interval
                           if interval else 0.0)
                error_eighths = 8.0 * error / (1 << 32)
                label = STATUS.get(status, "UNKNOWN_%d" % status)
                print("%d %d %s %d %d %d %.2f %.6f" %
                      (rate, axes, label, pulses, elapsed, interval,
                       reserve, error_eighths))
                failed |= status != 0
    finally:
        link.close()
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (FlashError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        sys.exit(2)
