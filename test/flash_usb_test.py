#!/usr/bin/env python3
# Regression test for application-to-DFU USB enumeration races.

import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from scripts import flash_usb  # noqa: E402


def test_serial_dfu_waits_for_rom_enumeration():
    calls = []
    saved = {
        name: getattr(flash_usb, name) for name in (
            "translate_serial_to_tty", "translate_serial_to_usb_path",
            "enter_bootloader", "wait_path", "detect_canboot",
            "call_dfuutil")
    }
    try:
        flash_usb.translate_serial_to_tty = (
            lambda device: ("/dev/ttyACM0", "/dev/serial/by-path/test"))
        flash_usb.translate_serial_to_usb_path = (
            lambda device: ("1-7", "/sys/devices/test/1-7:1.0"))
        flash_usb.enter_bootloader = lambda device: None
        flash_usb.wait_path = lambda path: path
        flash_usb.detect_canboot = lambda path: False
        flash_usb.call_dfuutil = (
            lambda flags, image, sudo: calls.append((flags, image, sudo)))
        flash_usb.flash_dfuutil(
            "/dev/serial/by-id/test", "/release/klipper.bin",
            ["-R", "-a", "0", "-s", "0x8002000:leave"], sudo=False)
    finally:
        for name, value in saved.items():
            setattr(flash_usb, name, value)
    flags, image, sudo = calls[0]
    assert flags[:3] == ["-w", "-p", "1-7"]
    assert image == "/release/klipper.bin" and sudo is False
    print("PASS: serial-to-DFU flashing waits for ROM enumeration")


def main():
    test_serial_dfu_waits_for_rom_enumeration()
    print("ALL PASS")


if __name__ == "__main__":
    main()
