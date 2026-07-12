#!/usr/bin/env python3
# Replicate klipper's compile_time_request build flow for CMake/ESP-IDF
#
# klipper's Makefile extracts the .compile_time_request section from
# every compiled object (DECL_COMMAND/DECL_CONSTANT/DECL_TASK/...
# strings - see src/ctr.h), concatenates them NUL->newline, and feeds
# the result to scripts/buildcommands.py, which emits both the data
# dictionary (klipper.dict) and the generated compile_time_request.c
# (command tables, embedded compressed dictionary, call lists).  This
# script performs exactly those steps from a CMake custom command;
# see src/esp32/main/CMakeLists.txt.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import argparse
import os
import subprocess
import sys


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--objcopy", required=True)
    p.add_argument("--objlist", required=True,
                   help="file containing ';'-separated object paths"
                   " (from $<TARGET_OBJECTS:...>)")
    p.add_argument("--out-c", required=True)
    p.add_argument("--dict", required=True)
    p.add_argument("--klipper-root", required=True)
    p.add_argument("--tools", default="",
                   help="';'-separated toolchain executables recorded"
                   " in the dictionary's build_versions")
    args = p.parse_args()

    with open(args.objlist) as f:
        objs = [o for o in f.read().replace("\n", ";").split(";") if o]

    # Extract and concatenate the .compile_time_request sections
    blob = b""
    for obj in objs:
        tmp = args.out_c + ".ctr.bin"
        ret = subprocess.run(
            [args.objcopy, "-j", ".compile_time_request", "-O", "binary",
             obj, tmp],
            capture_output=True)
        if ret.returncode:
            # An object with no requests is fine; a real failure is not
            if b"not found" not in ret.stderr and ret.stderr:
                sys.stderr.write(ret.stderr.decode(errors="replace"))
                return ret.returncode
            continue
        with open(tmp, "rb") as f:
            blob += f.read()
        os.unlink(tmp)
    # NUL -> newline with squeeze, as the Makefile's `tr -s '\0' '\n'`
    lines = [l for l in blob.split(b"\0") if l]
    txt = args.out_c + ".ctr.txt"
    with open(txt, "wb") as f:
        f.write(b"\n".join(lines) + b"\n")

    # buildcommands.py resolves ./klippy relative to the cwd
    cmd = [sys.executable,
           os.path.join(args.klipper_root, "scripts", "buildcommands.py"),
           "-d", args.dict, "-t", args.tools, txt, args.out_c]
    return subprocess.run(cmd, cwd=args.klipper_root).returncode


if __name__ == "__main__":
    sys.exit(main())
