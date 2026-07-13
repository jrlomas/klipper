# A6 build+flash planner — one-touch provisioning over the existing
# first-class bootloader (FD-0002 §5, FD-0001 doc 11).
#
# build_plan() turns a catalog board + a detected target into an explicit,
# reviewable list of steps: write the Kconfig fragment, build, and flash
# by the board's method.  It is a *plan* — it never runs anything itself,
# so the daemon (or a test) can show the exact commands, get consent, and
# only then execute.  This keeps a hard-to-reverse action (flashing)
# behind an inspectable, deterministic artifact.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import re
from dataclasses import dataclass, field

# CONFIG_STM32_FLASH_START_xxxx -> DFU load address 0x0800_xxxx.
_RE_FLASH_START = re.compile(r"CONFIG_STM32_FLASH_START_([0-9A-Fa-f]+)$")


@dataclass
class BuildFlashPlan:
    board_id: str
    method: str
    kconfig: dict = field(default_factory=dict)
    steps: list = field(default_factory=list)      # shell commands, in order
    warnings: list = field(default_factory=list)
    blockers: list = field(default_factory=list)
    needs_confirmation: bool = True                # flashing is irreversible
    klipper_dir: str = "~/klipper"
    config_out: str = ".config"
    target_identifier: str = ""

    def render(self) -> str:
        lines = ["# Atlas build+flash plan for %s (method: %s)"
                 % (self.board_id, self.method)]
        for w in self.warnings:
            lines.append("# WARNING: %s" % w)
        lines.extend(self.steps)
        return "\n".join(lines)


def _dfu_address(kconfig: dict) -> str:
    for key in kconfig:
        m = _RE_FLASH_START.match(key)
        if m:
            return "0x0800%s" % m.group(1).zfill(4)
    return "0x08000000"   # no offset symbol -> flash from base (no bootloader)


def _kconfig_lines(kconfig: dict) -> list:
    out = []
    for key, val in kconfig.items():
        if val in ("n", False, None):
            out.append("# %s is not set" % key)
        else:
            out.append("%s=%s" % (key, "y" if val in ("y", True) else val))
    return out


def build_plan(board, target=None, klipper_dir="~/klipper",
               config_out=".config") -> BuildFlashPlan:
    """Produce a reviewable build+flash plan for a board.

    `target` is an optional DetectedBoard (its identifier picks the
    concrete device/uuid/port); when absent the plan uses placeholders and
    flags that the target must be filled in.
    """
    plan = BuildFlashPlan(board_id=board.id, method=board.flash_method,
                          kconfig=dict(board.kconfig), klipper_dir=klipper_dir,
                          config_out=config_out,
                          target_identifier=(target.identifier
                                             if target is not None else ""))

    if board.flash_method == "custom":
        plan.warnings.append(
            "Custom board: no curated Kconfig. Run `make menuconfig` and "
            "flash by hand.")
        plan.blockers.append("custom boards require manual provisioning")
        plan.steps = ["cd %s" % klipper_dir, "make menuconfig", "make"]
        return plan

    # Flag any unconfirmed values the research/catalog left as placeholders
    # rather than silently building something that may brick the board.
    for key, val in board.kconfig.items():
        if str(val).upper() == "UNCONFIRMED":
            plan.warnings.append(
                "Kconfig %s is UNCONFIRMED for this board — verify before "
                "flashing (a wrong flash offset can brick it)." % key)
            plan.blockers.append("%s is UNCONFIRMED" % key)
            plan.needs_confirmation = True

    if board.constrained:
        plan.warnings.append(
            "Constrained board (%s): keep the build minimal; features that "
            "don't fit simply aren't built here (F042 policy)." % board.mcu)

    # 1. Write the Kconfig fragment.
    frag = "\n".join(_kconfig_lines(board.kconfig))
    plan.steps.append("cd %s" % klipper_dir)
    plan.steps.append("cat > %s <<'EOF'\n%s\nEOF" % (config_out, frag))
    plan.steps.append("make olddefconfig")
    plan.steps.append("make")

    # 2. Flash by method.
    ident = target.identifier if target is not None else None
    plan.steps.extend(_flash_steps(board, ident, plan))

    if target is not None and target.ambiguous:
        plan.warnings.append(
            "Detected target is ambiguous (%d candidates share this "
            "signature) — confirm the exact board before flashing."
            % len(target.candidates))
        plan.blockers.append("detected target is ambiguous")

    return plan


def _flash_steps(board, ident, plan) -> list:
    method = board.flash_method
    if method == "dfu":
        addr = _dfu_address(board.kconfig)
        dev = ident or "0483:df11"
        return ["dfu-util -d %s -a 0 -s %s:leave -D out/klipper.bin"
                % (dev, addr)]
    if method == "rp2040-usb":
        return ["# put the RP2040 in BOOTSEL, then:",
                "make flash FLASH_DEVICE=%s" % (ident or "2e8a:0003")]
    if method == "katapult-usb":
        return ["make flash FLASH_DEVICE=%s" % (ident or "<usb-id>")]
    if method == "katapult-can":
        uuid = ident or "<canbus-uuid>"
        if ident is None:
            plan.warnings.append("CAN target uuid unknown — run a canbus "
                                 "query first.")
            plan.blockers.append("CAN target uuid is unknown")
        return ["python3 lib/katapult/scripts/flash_can.py -i can0 -u %s "
                "-f out/klipper.bin" % uuid]
    if method == "serial":
        return ["make flash FLASH_DEVICE=%s" % (ident or "/dev/ttyUSB0")]
    if method == "sdcard":
        return ["# copy out/klipper.bin to the SD card as firmware.bin,",
                "# insert it, and power-cycle the board to flash.",
                "cp out/klipper.bin /media/sdcard/firmware.bin"]
    plan.warnings.append("Unknown flash method %r; flash by hand." % method)
    return ["# manual flash required"]
