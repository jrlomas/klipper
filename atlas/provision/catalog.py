# A6 board catalog — "pick a board, not a chip" (FD-0002 §5).
#
# Each entry is versioned data in the repo (boards/*.yaml).  The schema
# core is plain-dict-in / validated-object-out so the deterministic floor
# tests need no PyYAML; YAML is only used to read the on-disk catalog.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
from dataclasses import dataclass, field

_FLASH_METHODS = {"dfu", "katapult-can", "katapult-usb", "sdcard",
                  "serial", "rp2040-usb", "custom"}


class BoardCatalogError(ValueError):
    """A board catalog entry is malformed."""


@dataclass
class BoardEntry:
    id: str
    name: str
    mcu: str                     # e.g. "stm32f072", "rp2040"
    flash_method: str            # one of _FLASH_METHODS
    kconfig: dict = field(default_factory=dict)   # CONFIG_* -> value
    usb_ids: list = field(default_factory=list)   # ["vid:pid", ...]
    canbus: bool = False
    config_snippet: str = ""     # curated printer.cfg [mcu] stanza
    pins: dict = field(default_factory=dict)      # alias -> pin
    constrained: bool = False    # F042/F072-class: features must fit
    notes: str = ""

    def flash_signature(self) -> str:
        return "%s/%s" % (self.mcu, self.flash_method)


# The Custom escape hatch — always available, never auto-selected.  It
# carries no Kconfig so the user supplies everything (menuconfig + config).
CUSTOM_BOARD = BoardEntry(
    id="custom", name="Custom (advanced)", mcu="", flash_method="custom",
    notes="Full escape hatch: choose the MCU and flash method by hand.")


def load_board(data: dict) -> BoardEntry:
    if not isinstance(data, dict):
        raise BoardCatalogError("board must be a mapping")
    for req in ("id", "name", "mcu", "flash_method"):
        if not data.get(req):
            raise BoardCatalogError("board missing required field %r" % req)
    method = data["flash_method"]
    if method not in _FLASH_METHODS:
        raise BoardCatalogError(
            "board %s: unknown flash_method %r (expected %s)"
            % (data["id"], method, ", ".join(sorted(_FLASH_METHODS))))
    usb_ids = data.get("usb_ids", [])
    for uid in usb_ids:
        if not _valid_usb_id(uid):
            raise BoardCatalogError(
                "board %s: malformed usb id %r (want 'vid:pid' hex)"
                % (data["id"], uid))
    kconfig = data.get("kconfig", {})
    if not isinstance(kconfig, dict):
        raise BoardCatalogError("board %s: kconfig must be a mapping"
                                % data["id"])
    return BoardEntry(
        id=data["id"], name=data["name"], mcu=data["mcu"],
        flash_method=method, kconfig=dict(kconfig),
        usb_ids=list(usb_ids), canbus=bool(data.get("canbus", False)),
        config_snippet=data.get("config_snippet", ""),
        pins=dict(data.get("pins", {})),
        constrained=bool(data.get("constrained", False)),
        notes=data.get("notes", ""))


def _valid_usb_id(uid: str) -> bool:
    parts = str(uid).lower().split(":")
    if len(parts) != 2:
        return False
    try:
        int(parts[0], 16)
        int(parts[1], 16)
    except ValueError:
        return False
    return len(parts[0]) == 4 and len(parts[1]) == 4


def load_boards(items) -> list:
    boards, seen = [], set()
    for item in items:
        b = load_board(item)
        if b.id in seen:
            raise BoardCatalogError("duplicate board id %r" % b.id)
        seen.add(b.id)
        boards.append(b)
    return boards


def load_board_catalog(path) -> list:
    """Load every boards/*.yaml entry; always append the Custom hatch."""
    boards = []
    if os.path.isdir(path):
        files = sorted(f for f in os.listdir(path)
                       if f.endswith((".yaml", ".yml")))
        if files:
            try:
                import yaml
            except ImportError as exc:  # pragma: no cover
                raise BoardCatalogError(
                    "loading YAML board files needs PyYAML "
                    "(pip install -r atlas/requirements.txt)") from exc
            items = []
            for name in files:
                with open(os.path.join(path, name)) as fh:
                    doc = yaml.safe_load(fh)
                if doc is None:
                    continue
                items.extend(doc if isinstance(doc, list) else [doc])
            boards = load_boards(items)
    return boards + [CUSTOM_BOARD]


def builtin_catalog() -> list:
    """The on-disk boards/ catalog next to this module (+ Custom)."""
    return load_board_catalog(os.path.join(os.path.dirname(__file__),
                                           "boards"))
