#!/usr/bin/env python3
# Assemble the first-class bootloader "one build, one flash" combined
# image (RFC 0001 doc 11).
#
# Produces ONE flashable artifact from two inputs:
#   * the bootloader .bin, which owns the reset vector at flash base
#     (0x08000000), and
#   * the application .bin, linked at the bootloader's app-region offset
#     (CONFIG_FLASH_APPLICATION_ADDRESS).
#
# Layout of the emitted image (flashed once at 0x08000000; every later
# update is in-band over the protocol):
#
#   [ 0                    .. app_off )   bootloader  (pad 0xFF to app_off)
#   [ app_off              .. app_end )   application image
#   [ app_end              .. info_off)   erased gap  (0xFF)
#   [ info_off             .. info_off+16) validity record {magic,size,crc,0}
#
# The validity record is exactly the boot_info_record the bootloader
# reads at boot (src/boot_app/boot_flash.h): magic 0x50414F42 ("BOAP"),
# the application byte length, and the CRC-32 (IEEE 802.3 reflected —
# intentproto::crc32) over the application bytes. Stamping it here means
# the very first boot of a freshly programmed board finds a valid app
# and jumps straight to it, without an in-band update first.
#
# Offsets are the same per-target geometry as boot_flash.c / boot_flash.c
# so the assembly can never drift from what the on-chip bootloader
# checks.

import argparse
import struct
import zlib

BOOT_INFO_MAGIC = 0x50414F42  # "BOAP", matches boot_flash.h

FLASH_BASE = 0x08000000

# Per-target geometry (mirror of boot_flash.c). All addresses absolute.
GEOM = {
    "stm32f072": {
        "app_base": 0x08004000,   # 16 KB bootloader
        "info_addr": 0x0801F800,  # last 2 KB page of 128 KB
        "info_size": 0x800,
        "boot_budget": 0x4000,    # 16 KB
    },
    "stm32f4": {
        "app_base": 0x08008000,   # 32 KB bootloader (sectors 0-1)
        "info_addr": 0x08060000,  # sector 7 (128 KB)
        "info_size": 0x20000,
        "boot_budget": 0x8000,    # 32 KB
    },
}


def build(target, boot_bin, app_bin):
    g = GEOM[target]
    app_off = g["app_base"] - FLASH_BASE
    info_off = g["info_addr"] - FLASH_BASE
    app_size = info_off - app_off  # image region, excludes info page

    if len(boot_bin) > g["boot_budget"]:
        raise SystemExit(
            "bootloader %d bytes exceeds %s budget %d"
            % (len(boot_bin), target, g["boot_budget"]))
    if len(boot_bin) > app_off:
        raise SystemExit(
            "bootloader %d bytes overruns app base offset %d"
            % (len(boot_bin), app_off))
    if len(app_bin) > app_size:
        raise SystemExit(
            "application %d bytes exceeds app region %d"
            % (len(app_bin), app_size))

    # CRC-32 over the exact application bytes the bootloader will read.
    crc = zlib.crc32(app_bin) & 0xFFFFFFFF
    record = struct.pack("<IIII", BOOT_INFO_MAGIC, len(app_bin), crc, 0)

    # Assemble: bootloader, pad to app_off, app, pad to info_off, record.
    img = bytearray()
    img += boot_bin
    img += b"\xff" * (app_off - len(img))
    img += app_bin
    img += b"\xff" * (info_off - len(img))
    img += record
    return bytes(img), {
        "app_off": app_off, "app_size": len(app_bin), "app_crc": crc,
        "info_off": info_off, "total": len(img),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("target", choices=sorted(GEOM))
    ap.add_argument("bootloader_bin")
    ap.add_argument("application_bin")
    ap.add_argument("-o", "--output", required=True)
    args = ap.parse_args()

    with open(args.bootloader_bin, "rb") as f:
        boot_bin = f.read()
    with open(args.application_bin, "rb") as f:
        app_bin = f.read()

    img, info = build(args.target, boot_bin, app_bin)
    with open(args.output, "wb") as f:
        f.write(img)

    print("combined image: %s (%s)" % (args.output, args.target))
    print("  bootloader : %6d bytes @ 0x%08x" % (len(boot_bin), FLASH_BASE))
    print("  application: %6d bytes @ 0x%08x (crc32=0x%08x)"
          % (info["app_size"], FLASH_BASE + info["app_off"], info["app_crc"]))
    print("  validity   :     16 bytes @ 0x%08x (magic=0x%08x)"
          % (FLASH_BASE + info["info_off"], BOOT_INFO_MAGIC))
    print("  total      : %6d bytes" % info["total"])


if __name__ == "__main__":
    main()
