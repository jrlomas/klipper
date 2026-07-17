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


def test_katapult_reenumeration_resolves_current_interface():
    calls = []
    saved = {
        name: getattr(flash_usb, name) for name in (
            "translate_serial_to_tty", "translate_serial_to_usb_path",
            "enter_bootloader", "wait_path", "detect_canboot",
            "find_usb_tty", "call_flashcan")
    }
    try:
        flash_usb.translate_serial_to_tty = (
            lambda device: ("/dev/ttyACM1", "/dev/serial/by-path/app-1.1"))
        flash_usb.translate_serial_to_usb_path = (
            lambda device: ("1-4.3", "/sys/devices/1-4.3/1-4.3:1.1"))
        flash_usb.enter_bootloader = lambda device: None
        flash_usb.wait_path = lambda path: path
        flash_usb.detect_canboot = lambda path: True
        flash_usb.find_usb_tty = lambda path: "/dev/ttyACM2"
        flash_usb.call_flashcan = (
            lambda device, image: calls.append((device, image)))
        flash_usb.flash_dfuutil(
            "/dev/serial/by-id/bridge", "/release/bridge.bin",
            ["-R", "-a", "0", "-s", "0x8002000:leave"], sudo=False)
    finally:
        for name, value in saved.items():
            setattr(flash_usb, name, value)
    assert calls == [("/dev/ttyACM2", "/release/bridge.bin")]
    print("PASS: Katapult flashing resolves the re-enumerated CDC interface")


def test_find_usb_tty_scans_all_current_interfaces():
    usbdir = "/sys/devices/usb1/1-4/1-4.3"
    ttydir = os.path.join(usbdir, "1-4.3:1.0", "tty")
    saved_listdir = flash_usb.os.listdir
    saved_exists = flash_usb.os.path.exists
    try:
        flash_usb.os.listdir = lambda path: {
            usbdir: ["1-4.3:1.0", "1-4.3:1.1"],
            ttydir: ["ttyACM2"],
        }.get(path, [])
        flash_usb.os.path.exists = lambda path: path == "/dev/ttyACM2"
        found = flash_usb.find_usb_tty(
            os.path.join(usbdir, "1-4.3:1.1"), timeout=0.0)
    finally:
        flash_usb.os.listdir = saved_listdir
        flash_usb.os.path.exists = saved_exists
    assert found == "/dev/ttyACM2"
    print("PASS: current Katapult CDC interface is found below the USB device")


def main():
    test_serial_dfu_waits_for_rom_enumeration()
    test_katapult_reenumeration_resolves_current_interface()
    test_find_usb_tty_scans_all_current_interfaces()
    print("ALL PASS")


if __name__ == "__main__":
    main()
