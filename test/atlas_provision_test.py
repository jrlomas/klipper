#!/usr/bin/env python3
# Standalone unit test for the Atlas A6 provisioning floor (FD-0002 §5).
# Covers board-entry validation, USB/CAN detection with honest ambiguity,
# the Custom escape hatch, and the deterministic build+flash planner
# (including the "never flash on UNCONFIRMED / ambiguous" guards).
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                ".."))

from atlas.provision import (BoardCatalogError, CUSTOM_BOARD,  # noqa: E402
                             build_plan, detect_boards, load_board,
                             load_boards, match_usb, parse_lsusb)
from atlas.provision.detect import DetectedBoard  # noqa: E402

OCTOPUS = {
    "id": "btt-octopus-f446", "name": "BTT Octopus (F446)",
    "mcu": "stm32f446", "flash_method": "dfu", "usb_ids": ["0483:df11"],
    "kconfig": {"CONFIG_MACH_STM32F446": "y",
                "CONFIG_STM32_FLASH_START_8000": "y", "CONFIG_USB": "y"},
}
SPIDER = {
    "id": "fysetc-spider-f446", "name": "Fysetc Spider (F446)",
    "mcu": "stm32f446", "flash_method": "dfu", "usb_ids": ["0483:df11"],
    "kconfig": {"CONFIG_MACH_STM32F446": "y",
                "CONFIG_STM32_FLASH_START_8000": "y"},
}
EBB = {
    "id": "btt-ebb36-g0b1", "name": "BTT EBB36 CAN toolhead",
    "mcu": "stm32g0b1", "flash_method": "katapult-can", "canbus": True,
    "constrained": False,
    "kconfig": {"CONFIG_MACH_STM32G0B1": "y",
                "CONFIG_STM32_FLASH_START_8000": "y", "CONFIG_CANBUS": "y"},
}


def test_board_validation_ok():
    b = load_board(OCTOPUS)
    assert b.mcu == "stm32f446" and b.flash_method == "dfu"
    assert b.flash_signature() == "stm32f446/dfu"
    print("PASS: a well-formed board validates")


def test_board_validation_rejects():
    bad = [
        ({"id": "x", "name": "n", "mcu": "m"}, "missing flash_method"),
        ({"id": "x", "name": "n", "mcu": "m", "flash_method": "wat"},
         "bad flash method"),
        ({"id": "x", "name": "n", "mcu": "m", "flash_method": "dfu",
          "usb_ids": ["nothex"]}, "bad usb id"),
        ({"id": "x", "name": "n", "mcu": "m", "flash_method": "dfu",
          "usb_ids": ["0483:df11:extra"]}, "malformed usb id"),
    ]
    for data, why in bad:
        try:
            load_board(data)
        except BoardCatalogError:
            continue
        raise AssertionError("expected BoardCatalogError for %s" % why)
    print("PASS: malformed boards rejected (%d cases)" % len(bad))


def test_duplicate_board_ids():
    try:
        load_boards([OCTOPUS, dict(OCTOPUS)])
    except BoardCatalogError:
        print("PASS: duplicate board ids rejected")
        return
    raise AssertionError("expected BoardCatalogError")


def test_parse_lsusb():
    text = ("Bus 001 Device 004: ID 0483:df11 STMicroelectronics "
            "STM Device in DFU Mode\n"
            "Bus 001 Device 002: ID 1d6b:0002 Linux Foundation 2.0 hub\n")
    devs = parse_lsusb(text)
    ids = {(d["vid"], d["pid"]) for d in devs}
    assert ("0483", "df11") in ids
    assert any("DFU" in d["desc"] for d in devs)
    print("PASS: lsusb output parsed to vid:pid + description")


def test_usb_match_ambiguous_dfu():
    # Two F446 boards share the STM32 DFU id -> ambiguous, must not resolve.
    catalog = load_boards([OCTOPUS, SPIDER, EBB])
    devs = parse_lsusb("Bus 001 Device 004: ID 0483:df11 STM DFU")
    matches = match_usb(devs, catalog)
    assert len(matches) == 1
    m = matches[0]
    assert m.ambiguous is True
    assert m.resolved is None                 # refuses to guess
    assert {b.id for b in m.candidates} == {"btt-octopus-f446",
                                            "fysetc-spider-f446"}
    print("PASS: shared DFU id detected as ambiguous, does not auto-resolve")


def test_can_detection():
    catalog = load_boards([OCTOPUS, EBB])
    scan = {"canbus_uuids": ["1122334455aa"]}
    res = detect_boards(scan, catalog)
    assert len(res) == 1 and res[0].interface == "katapult-can"
    assert res[0].candidates[0].id == "btt-ebb36-g0b1"
    print("PASS: CAN uuid detection maps to the CAN board")


def test_custom_hatch_always_present():
    plan = build_plan(CUSTOM_BOARD)
    assert plan.method == "custom"
    assert any("menuconfig" in s for s in plan.steps)
    assert plan.warnings  # tells the user they're on their own
    print("PASS: Custom escape hatch yields a manual plan")


def test_plan_dfu_address_from_kconfig():
    plan = build_plan(load_board(OCTOPUS))
    flash = [s for s in plan.steps if s.startswith("dfu-util")]
    assert flash and "0x08008000:leave" in flash[0]
    assert any("make" == s for s in plan.steps)
    assert plan.needs_confirmation is True    # flashing is irreversible
    print("PASS: DFU plan derives 0x08008000 load address from Kconfig")


def test_plan_unconfirmed_guard():
    board = load_board({
        "id": "risky", "name": "Risky", "mcu": "stm32f407",
        "flash_method": "dfu",
        "kconfig": {"CONFIG_STM32_FLASH_START_8000": "UNCONFIRMED"}})
    plan = build_plan(board)
    assert any("UNCONFIRMED" in w for w in plan.warnings)
    print("PASS: UNCONFIRMED kconfig raises a brick-risk warning")


def test_plan_katapult_can_needs_uuid():
    board = load_board(EBB)
    # No target -> plan must warn the uuid is unknown, use a placeholder.
    plan = build_plan(board)
    assert any("uuid" in w.lower() for w in plan.warnings)
    # With a target uuid -> the flash step carries it.
    target = DetectedBoard(interface="katapult-can",
                           identifier="1122334455aa", candidates=[board])
    plan2 = build_plan(board, target=target)
    assert any("1122334455aa" in s for s in plan2.steps)
    print("PASS: katapult-can plan demands a uuid, uses the detected one")


def test_ambiguous_target_blocks_flash():
    catalog = load_boards([OCTOPUS, SPIDER])
    target = DetectedBoard(interface="dfu", identifier="0483:df11",
                           candidates=catalog, ambiguous=True)
    plan = build_plan(load_board(OCTOPUS), target=target)
    assert any("ambiguous" in w.lower() for w in plan.warnings)
    print("PASS: an ambiguous detected target adds a confirm-first warning")


def test_on_disk_catalog_optional():
    # The shipped boards/*.yaml catalog loads when PyYAML is present; the
    # Custom hatch is always appended. Skipped cleanly without PyYAML.
    try:
        import yaml  # noqa: F401
    except ImportError:
        print("SKIP: PyYAML not installed; on-disk catalog test skipped")
        return
    from atlas.provision import builtin_catalog
    cat = builtin_catalog()
    ids = {b.id for b in cat}
    assert "custom" in ids                    # hatch always present
    assert len(cat) >= 2                       # at least one real board + custom
    for b in cat:
        assert b.id and b.mcu is not None
    print("PASS: on-disk board catalog loads (%d entries incl. custom)"
          % len(cat))


def main():
    test_board_validation_ok()
    test_board_validation_rejects()
    test_duplicate_board_ids()
    test_parse_lsusb()
    test_usb_match_ambiguous_dfu()
    test_can_detection()
    test_custom_hatch_always_present()
    test_plan_dfu_address_from_kconfig()
    test_plan_unconfirmed_guard()
    test_plan_katapult_can_needs_uuid()
    test_ambiguous_target_blocks_flash()
    test_on_disk_catalog_optional()
    print("ALL PASS")


if __name__ == "__main__":
    main()