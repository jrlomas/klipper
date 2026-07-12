# A6 provisioning — board catalog, detection, and one-touch build+flash.
#
# "Pick a board, not a chip" (FD-0002 §5): a catalog entry carries the
# MCU, the flash method, a curated Kconfig fragment and default config,
# and the USB/CAN signatures that let Atlas recognise a connected board.
# A 'Custom' escape hatch always exists.  Detection is deterministic and
# honest — when a bootloader signature is ambiguous (many STM32 boards
# share the DFU id 0483:df11), it returns *candidates* and asks, rather
# than guessing.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

from .catalog import (BoardEntry, BoardCatalogError, CUSTOM_BOARD,
                      load_board, load_boards, load_board_catalog,
                      builtin_catalog)
from .detect import (DetectedBoard, parse_lsusb, match_usb, detect_boards)
from .plan import BuildFlashPlan, build_plan

__all__ = [
    "BoardEntry", "BoardCatalogError", "CUSTOM_BOARD",
    "load_board", "load_boards", "load_board_catalog", "builtin_catalog",
    "DetectedBoard", "parse_lsusb", "match_usb", "detect_boards",
    "BuildFlashPlan", "build_plan",
]
