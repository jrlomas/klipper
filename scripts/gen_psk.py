#!/usr/bin/env python3
# Generate a pre-shared key for intentproto datagram authentication
# (docs/Protocol_v2.md) and print the exact configuration for both ends.
#
# The key is printable ASCII so the same bytes can live in a Kconfig
# string (board side, baked at build time) and in a host psk_file (read
# as raw bytes) with no encoding ambiguity.
#
# Usage:
#   scripts/gen_psk.py ~/printer_data/config/toolhead.psk
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import argparse
import os
import secrets
import stat
import sys


def main():
    p = argparse.ArgumentParser(
        description="generate an intentproto datagram PSK")
    p.add_argument("psk_file", help="host-side key file to create")
    p.add_argument("--bytes", type=int, default=32, dest="nbytes",
                   help="key strength in random bytes (default 32; the"
                        " printable key is longer)")
    p.add_argument("--force", action="store_true",
                   help="overwrite an existing key file")
    args = p.parse_args()

    path = os.path.expanduser(args.psk_file)
    if os.path.exists(path) and not args.force:
        sys.stderr.write("refusing to overwrite %s (use --force)\n" % path)
        return 1

    # URL-safe base64 of N random bytes: printable, no quoting issues in
    # Kconfig strings or config files.
    key = secrets.token_urlsafe(args.nbytes)

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                 stat.S_IRUSR | stat.S_IWUSR)  # 0600
    with os.fdopen(fd, "w") as f:
        f.write(key + "\n")

    print("wrote %s (mode 0600, %d chars)\n" % (path, len(key)))
    print("Host side — klippy printer config:")
    print("    [intentproto_transport myboard]")
    print("    psk_file: %s\n" % (args.psk_file,))
    print("  (or the standalone bridge: udp_bridge.py --psk-file %s)\n"
          % (args.psk_file,))
    print("Board side — bake the SAME string into the firmware build:")
    print("    W5500 Ethernet:  make menuconfig ->")
    print('        CONFIG_W5500_PSK="%s"' % (key,))
    print("    ESP32 WiFi: set the equivalent PSK option in its build")
    print("        configuration to the same string.\n")
    print("Treat the key like a password: per board, never committed to")
    print("a repository, rotated by re-running this tool and reflashing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
