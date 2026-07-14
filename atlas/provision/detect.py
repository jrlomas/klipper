# A6 board detection — recognise a connected board from a scan.
#
# Detection is deterministic and, crucially, honest about ambiguity: the
# STM32 system DFU id (0483:df11) is shared by every STM32 in bootloader
# mode, so a USB match narrows to a *family*, not a board.  match_usb
# returns candidates with an `ambiguous` flag so the provisioning flow
# asks the user instead of silently flashing the wrong image.
#
# The scan itself (running lsusb / querying CAN) is I/O the daemon does;
# these functions take the *parsed* scan so they are pure and testable.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import re
from dataclasses import dataclass, field

_RE_LSUSB = re.compile(
    r"ID\s+(?P<vid>[0-9a-fA-F]{4}):(?P<pid>[0-9a-fA-F]{4})"
    r"(?:\s+(?P<desc>.*))?$")

KLIPPER_USB_ID = "1d50:614e"
_RE_MCU_PRODUCT = re.compile(
    r"\b(rp[0-9]+|stm32[a-z0-9]+|lpc[0-9]+[a-z0-9]*)\b", re.I)


@dataclass
class DetectedBoard:
    interface: str            # 'usb' | 'dfu' | 'katapult-can' | 'serial'
    identifier: str           # 'vid:pid' | canbus uuid | device path
    candidates: list = field(default_factory=list)  # BoardEntry list
    ambiguous: bool = False   # more than one catalog board could match
    desc: str = ""

    @property
    def resolved(self):
        """The confidently unique board, or None when confirmation is due."""
        if self.ambiguous:
            return None
        return self.candidates[0] if len(self.candidates) == 1 else None


def parse_lsusb(text: str) -> list:
    """Parse `lsusb` output into [{vid, pid, desc}] (lowercased ids)."""
    out = []
    for line in text.splitlines():
        m = _RE_LSUSB.search(line)
        if m:
            out.append({"vid": m.group("vid").lower(),
                        "pid": m.group("pid").lower(),
                        "desc": (m.group("desc") or "").strip()})
    return out


def _normalise_mcu(name: str) -> str:
    """Map Klipper's USB product spelling onto the catalog MCU family."""
    name = str(name or "").strip().lower()
    if name.startswith("stm32") and name.endswith("xx"):
        name = name[:-2]
    return name


def _product_mcu(desc: str) -> str:
    matches = _RE_MCU_PRODUCT.findall(str(desc or ""))
    return _normalise_mcu(matches[-1]) if matches else ""


def _match_running_klipper(dev, catalog):
    """Return an honest MCU-family match for a running Klipper USB peer.

    1d50:614e and the USB product identify firmware and MCU family, not a
    physical PCB.  Even a one-entry catalog result therefore remains
    ambiguous: an unlisted/custom board may use the same MCU and image.
    """
    mcu = _product_mcu(dev.get("product") or dev.get("desc"))
    if not mcu:
        return None
    candidates = [board for board in catalog
                  if _normalise_mcu(board.mcu) == mcu]
    if not candidates:
        return None
    return DetectedBoard(
        interface="klipper-usb",
        identifier=dev.get("path") or KLIPPER_USB_ID,
        candidates=candidates, ambiguous=True,
        desc=dev.get("desc") or dev.get("product", ""))


def match_usb(usb_devices, catalog) -> list:
    """Match parsed USB devices against the catalog by vid:pid.

    Returns a DetectedBoard per matched device.  A device whose id maps
    to more than one catalog board is flagged ambiguous (e.g. the shared
    STM32 DFU id) so the caller confirms rather than guesses.
    """
    by_id = {}
    for board in catalog:
        for uid in board.usb_ids:
            by_id.setdefault(uid.lower(), []).append(board)
    results = []
    for dev in usb_devices:
        uid = "%s:%s" % (dev["vid"], dev["pid"])
        if uid == KLIPPER_USB_ID:
            running = _match_running_klipper(dev, catalog)
            if running is not None:
                results.append(running)
            continue
        cands = list(by_id.get(uid, []))
        if not cands:
            continue
        iface = "dfu" if uid == "0483:df11" else "usb"
        results.append(DetectedBoard(
            interface=iface, identifier=uid, candidates=cands,
            ambiguous=len(cands) > 1, desc=dev.get("desc", "")))
    return results


def detect_boards(scan: dict, catalog) -> list:
    """Recognise boards from a scan dict.

    scan keys (all optional):
      lsusb        - raw `lsusb` text (USB + DFU devices)
      serial_devices - [{path, vid, pid, product/desc}, ...] from udev.  This
                       is preferred for a running Klipper USB peer because the
                       stable serial path can be carried into a flash plan.
      canbus_uuids - [uuid, ...] from a CAN query (Katapult/Klipper)
    Returns a combined list of DetectedBoard.
    """
    results = []
    if scan.get("lsusb"):
        usb_devices = parse_lsusb(scan["lsusb"])
        if scan.get("serial_devices"):
            # The richer udev record below represents these same peers and
            # includes their stable path.  Do not emit a second generic-ID
            # result from lsusb.
            usb_devices = [dev for dev in usb_devices
                           if "%s:%s" % (dev["vid"], dev["pid"])
                           != KLIPPER_USB_ID]
        results.extend(match_usb(usb_devices, catalog))
    if scan.get("serial_devices"):
        results.extend(match_usb(scan["serial_devices"], catalog))
    for uuid in scan.get("canbus_uuids", []):
        can_boards = [b for b in catalog if b.canbus]
        results.append(DetectedBoard(
            interface="katapult-can", identifier=uuid,
            candidates=can_boards, ambiguous=len(can_boards) != 1))
    return results
