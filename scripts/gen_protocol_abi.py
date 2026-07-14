#!/usr/bin/env python3
"""Generate the firmware protocol-hash advertisement from intentproto."""

import argparse
import importlib.util
import os


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ABI_PATH = os.path.join(ROOT, "atlas", "fleet", "abi.py")


def _load_abi():
    spec = importlib.util.spec_from_file_location("_atlas_fleet_abi", ABI_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    abi = _load_abi()
    rendered = abi.abi_header(abi.host_protocol_hash())
    output = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(output), exist_ok=True)
    temporary = output + ".tmp"
    with open(temporary, "w", encoding="utf-8") as fh:
        fh.write(rendered)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temporary, output)


if __name__ == "__main__":
    main()
