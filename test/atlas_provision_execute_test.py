#!/usr/bin/env python3
# Provisioning/fleet execution acceptance tests.

import json
import os
import pathlib
import sys
import tempfile
import subprocess

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from atlas.fleet import BoardState, check_board, remediate_board  # noqa: E402
from atlas.provision import (DetectedBoard, ProvisionBlocked,  # noqa: E402
                             ProvisionExecutor, build_plan, load_board,
                             verify_detached)


BOARD = load_board({
    "id": "board", "name": "Board", "mcu": "stm32f446",
    "flash_method": "dfu", "usb_ids": ["0483:df11"],
    "kconfig": {"CONFIG_MACH_STM32F446": "y",
                "CONFIG_STM32_FLASH_START_8000": "y"},
})
PICO = load_board({
    "id": "pico", "name": "Pico", "mcu": "rp2040",
    "flash_method": "rp2040-usb", "usb_ids": ["2e8a:0003"],
    "kconfig": {"CONFIG_MACH_RP2040": "y", "CONFIG_USB": "y",
                "CONFIG_USBSERIAL": "y"},
})
EBB_USB = load_board({
    "id": "ebb-usb", "name": "EBB USB", "mcu": "stm32g0b1",
    "flash_method": "katapult-usb", "usb_ids": ["1d50:6177"],
    "kconfig": {"CONFIG_MACH_STM32G0B1": "y",
                "CONFIG_STM32_FLASH_START_2000": "y",
                "CONFIG_USB": "y", "CONFIG_USBSERIAL": "y"},
})


def test_real_ed25519_verifier_fails_closed():
    with tempfile.TemporaryDirectory() as tmp:
        image = pathlib.Path(tmp) / "image.bin"
        signature = pathlib.Path(str(image) + ".sig")
        image.write_bytes(b"firmware")
        subprocess.run([
            sys.executable, str(pathlib.Path(__file__).resolve().parent.parent /
                                "scripts" / "sign_image.py"),
            "blob", str(image), "--key",
            str(pathlib.Path(__file__).resolve().parent.parent /
                "keys" / "helix_dev_signing.key"), "-o", str(signature),
        ], check=True, capture_output=True)
        pub = (pathlib.Path(__file__).resolve().parent.parent / "keys"
               / "helix_dev_signing.pub")
        assert verify_detached(str(image), str(pub)) is True
        image.write_bytes(b"tampered")
        assert verify_detached(str(image), str(pub)) is False
        print("PASS: real Ed25519 verification accepts signed and rejects "
              "tampered")


def test_executor_uses_argv_signed_gate_and_private_audit():
    with tempfile.TemporaryDirectory() as tmp:
        target = DetectedBoard("dfu", "0483:df11", [BOARD])
        plan = build_plan(BOARD, target, klipper_dir=tmp)
        image = pathlib.Path(tmp) / "signed.bin"
        image.write_bytes(b"signed")
        commands = []
        def run(argv, cwd):
            commands.append((argv, cwd))
            if (argv[0] == "make" and "olddefconfig" not in argv
                    and "clean" not in argv):
                output = pathlib.Path(cwd) / "out" / "klipper.bin"
                output.parent.mkdir(exist_ok=True)
                output.write_bytes(image.read_bytes())
        executor = ProvisionExecutor(
            pathlib.Path(tmp) / "audit.json",
            runner=run)
        try:
            executor.execute(plan, image, confirmed=True)
        except ProvisionBlocked as exc:
            assert "verification" in str(exc)
        else:
            raise AssertionError("unsigned/unverified image was flashed")
        executor.verifier = lambda path: True
        done = executor.execute(plan, image, confirmed=True)
        assert done == [entry[0] for entry in commands]
        assert all(isinstance(arg, list) for arg, _ in commands)
        assert commands[-1][0][0] == "dfu-util"
        assert commands[-1][0][-1] == str(image.resolve())
        assert commands[0][0][-1] == "clean"
        assert all(any(arg.startswith("KCONFIG_CONFIG=") for arg in cmd)
                   for cmd, _ in commands[:3])
        audit = pathlib.Path(tmp) / "audit.json"
        assert (audit.stat().st_mode & 0o777) == 0o600
        assert json.loads(audit.read_text())[0]["status"] == "complete"
        print("PASS: provision job uses argv, signed gate, and private audit")


def test_hard_blockers_cannot_be_confirmed_away():
    with tempfile.TemporaryDirectory() as tmp:
        risky = load_board({
            "id": "risky", "name": "Risky", "mcu": "stm32f4",
            "flash_method": "dfu",
            "kconfig": {"CONFIG_STM32_FLASH_START_8000": "UNCONFIRMED"}})
        plan = build_plan(risky, klipper_dir=tmp)
        image = pathlib.Path(tmp) / "signed.bin"
        image.write_bytes(b"x")
        executor = ProvisionExecutor(pathlib.Path(tmp) / "audit.json",
                                     runner=lambda argv, cwd: None,
                                     verifier=lambda path: True)
        try:
            executor.execute(plan, image, confirmed=True)
        except ProvisionBlocked as exc:
            assert "UNCONFIRMED" in str(exc)
        else:
            raise AssertionError("UNCONFIRMED board was executable")
        print("PASS: ambiguous/unconfirmed blockers cannot be confirmed away")


def test_fleet_remediation_reuses_signed_executor():
    with tempfile.TemporaryDirectory() as tmp:
        target = DetectedBoard("dfu", "0483:df11", [BOARD])
        plan = build_plan(BOARD, target, klipper_dir=tmp)
        image = pathlib.Path(tmp) / "signed.bin"
        image.write_bytes(b"x")
        def run(argv, cwd):
            if (argv[0] == "make" and "olddefconfig" not in argv
                    and "clean" not in argv):
                output = pathlib.Path(cwd) / "out" / "klipper.bin"
                output.parent.mkdir(exist_ok=True)
                output.write_bytes(image.read_bytes())
        executor = ProvisionExecutor(pathlib.Path(tmp) / "audit.json",
                                     runner=run,
                                     verifier=lambda path: True)
        report = check_board(
            "host", 0x00010001,
            BoardState("mcu", "old", 0x00010000, True))
        commands = remediate_board(
            report, executor, plan, image, confirmed=True)
        assert commands[-1][0] == "dfu-util"
    print("PASS: fleet repair is the same confirmed signed "
          "provisioning job")


def test_mismatched_build_never_reaches_flash():
    with tempfile.TemporaryDirectory() as tmp:
        target = DetectedBoard("dfu", "0483:df11", [BOARD])
        plan = build_plan(BOARD, target, klipper_dir=tmp)
        image = pathlib.Path(tmp) / "signed.bin"
        image.write_bytes(b"signed release")
        commands = []
        def run(argv, cwd):
            commands.append(argv)
            if (argv[0] == "make" and "olddefconfig" not in argv
                    and "clean" not in argv):
                output = pathlib.Path(cwd) / "out" / "klipper.bin"
                output.parent.mkdir(exist_ok=True)
                output.write_bytes(b"different build")
        executor = ProvisionExecutor(pathlib.Path(tmp) / "audit.json",
                                     runner=run,
                                     verifier=lambda path: True)
        try:
            executor.execute(plan, image, confirmed=True)
        except ProvisionBlocked as exc:
            assert "does not match" in str(exc)
        else:
            raise AssertionError("mismatched build reached the flash command")
        assert not any(command[0] == "dfu-util" for command in commands)
        print("PASS: byte mismatch between build and signed image blocks flash")


def test_signed_release_inside_build_tree_is_blocked_before_clean():
    with tempfile.TemporaryDirectory() as tmp:
        target = DetectedBoard("dfu", "0483:df11", [BOARD])
        plan = build_plan(BOARD, target, klipper_dir=tmp)
        image = pathlib.Path(tmp) / "out" / "release.bin"
        image.parent.mkdir()
        image.write_bytes(b"signed")
        commands = []
        executor = ProvisionExecutor(pathlib.Path(tmp) / "audit.json",
                                     runner=lambda argv, cwd:
                                     commands.append(argv),
                                     verifier=lambda path: True)
        try:
            executor.execute(plan, image, confirmed=True)
        except ProvisionBlocked as exc:
            assert "outside the build output" in str(exc)
        else:
            raise AssertionError("release inside build tree reached clean")
        assert commands == []
        print("PASS: clean cannot delete or replace the verified release")


def test_rp2040_flash_command_names_verified_image_directly():
    path = "/dev/serial/by-id/usb-Klipper_rp2040_test-if00"
    target = DetectedBoard("klipper-usb", path, [PICO])
    plan = build_plan(PICO, target)
    image = "/verified/release/klipper.uf2"
    command = ProvisionExecutor._flash_command(plan, image)
    assert command == ["python3", "scripts/flash_usb.py", "-t", "rp2040",
                       "-d", path, "--no-sudo", image]
    assert "FLASH_FILE=" not in " ".join(command)
    print("PASS: RP2040 flasher receives the exact verified image path")


def test_katapult_usb_flash_command_names_image_and_offset():
    path = "/dev/serial/by-id/usb-Klipper_stm32g0b1xx_test-if00"
    target = DetectedBoard("klipper-usb", path, [EBB_USB])
    plan = build_plan(EBB_USB, target)
    image = "/verified/release/klipper.bin"
    command = ProvisionExecutor._flash_command(plan, image)
    assert command == ["python3", "scripts/flash_usb.py", "-t",
                       "stm32g0b1", "-d", path, "-s", "134225920",
                       "--no-sudo", image]
    print("PASS: Katapult USB flasher receives exact image and 8 KiB offset")


def test_flash_address_ignores_non_address_kconfig_symbols():
    board = load_board({
        "id": "ebb-usb-chipboot", "name": "EBB USB", "mcu": "stm32g0b1",
        "flash_method": "katapult-usb", "usb_ids": ["1d50:6177"],
        "kconfig": {
            # olddefconfig emits this selector before the actual hex offset.
            "CONFIG_STM32_FLASH_START_CHIPBOOT_16K": "n",
            "CONFIG_STM32_FLASH_START_2000": "y",
            "CONFIG_USB": "y",
        },
    })
    target = DetectedBoard("klipper-usb", "/dev/ttyACM0", [board])
    plan = build_plan(board, target, klipper_dir="/tmp/klipper")
    command = ProvisionExecutor._flash_command(
        plan, "/verified/release/klipper.bin")
    offset = command.index("-s")
    assert command[offset:offset + 2] == ["-s", str(0x08002000)]
    print("PASS: non-address STM32 flash selectors cannot corrupt offset")


def main():
    test_real_ed25519_verifier_fails_closed()
    test_executor_uses_argv_signed_gate_and_private_audit()
    test_hard_blockers_cannot_be_confirmed_away()
    test_fleet_remediation_reuses_signed_executor()
    test_mismatched_build_never_reaches_flash()
    test_signed_release_inside_build_tree_is_blocked_before_clean()
    test_rp2040_flash_command_names_verified_image_directly()
    test_katapult_usb_flash_command_names_image_and_offset()
    test_flash_address_ignores_non_address_kconfig_symbols()
    print("ALL PASS")


if __name__ == "__main__":
    main()
