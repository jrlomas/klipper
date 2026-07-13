#!/usr/bin/env python3
# Provisioning/fleet execution acceptance tests.

import json
import os
import pathlib
import sys
import tempfile
import subprocess

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))

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
        pub = pathlib.Path(__file__).resolve().parent.parent / "keys" / "helix_dev_signing.pub"
        assert verify_detached(str(image), str(pub)) is True
        image.write_bytes(b"tampered")
        assert verify_detached(str(image), str(pub)) is False
        print("PASS: real Ed25519 verification accepts signed and rejects tampered")


def test_executor_uses_argv_signed_gate_and_private_audit():
    with tempfile.TemporaryDirectory() as tmp:
        target = DetectedBoard("dfu", "0483:df11", [BOARD])
        plan = build_plan(BOARD, target, klipper_dir=tmp)
        image = pathlib.Path(tmp) / "signed.bin"
        image.write_bytes(b"signed")
        commands = []
        executor = ProvisionExecutor(
            pathlib.Path(tmp) / "audit.json",
            runner=lambda argv, cwd: commands.append((argv, cwd)))
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
        executor = ProvisionExecutor(pathlib.Path(tmp) / "audit.json",
                                     runner=lambda argv, cwd: None,
                                     verifier=lambda path: True)
        report = check_board(
            "host", 0x00010001,
            BoardState("mcu", "old", 0x00010000, True))
        commands = remediate_board(
            report, executor, plan, image, confirmed=True)
        assert commands[-1][0] == "dfu-util"
        print("PASS: fleet repair is the same confirmed signed provisioning job")


def main():
    test_real_ed25519_verifier_fails_closed()
    test_executor_uses_argv_signed_gate_and_private_audit()
    test_hard_blockers_cannot_be_confirmed_away()
    test_fleet_remediation_reuses_signed_executor()
    print("ALL PASS")


if __name__ == "__main__":
    main()
