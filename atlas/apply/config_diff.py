# A pragmatic Klipper printer.cfg parser + differ for the apply layer.
#
# Not a full Klipper config engine — just enough to turn "before" and
# "after" config text into a list of section/key Changes the risk
# classifier reasons about. Handles [section] headers, `key: value` and
# `key = value`, inline comments (# and ;), and multi-line values (the
# indented gcode blocks of gcode_macros).
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import re
from dataclasses import dataclass

_RE_SECTION = re.compile(r"^\[(?P<name>[^\]]+)\]\s*$")
_RE_KEY = re.compile(r"^(?P<indent>\s*)(?P<key>[^:=#;\s][^:=]*?)\s*[:=]\s*"
                     r"(?P<val>.*)$")


def _strip_comment(line: str) -> str:
    # Klipper treats # and ; as comment starts. Keep it simple: cut at the
    # first of either. (Good enough for diffing config semantics.)
    for c in ("#", ";"):
        i = line.find(c)
        if i != -1:
            line = line[:i]
    return line.rstrip()


def parse_config(text: str) -> dict:
    """Parse config text into {section: {key: value}}.

    Multi-line values (indented continuations under a key, e.g. a
    gcode_macro body) are folded into the value joined by newlines.
    """
    sections: dict = {}
    section = None
    cur_key = None
    key_indent = 0
    for raw in text.splitlines():
        line = _strip_comment(raw)
        if not line.strip():
            continue
        m = _RE_SECTION.match(line.strip())
        if m:
            section = m.group("name").strip()
            sections.setdefault(section, {})
            cur_key = None
            continue
        if section is None:
            continue
        m = _RE_KEY.match(line)
        indent = len(line) - len(line.lstrip())
        if m and not (cur_key is not None and indent > key_indent):
            cur_key = m.group("key").strip()
            key_indent = indent
            sections[section][cur_key] = m.group("val").strip()
        elif cur_key is not None and indent > key_indent:
            # continuation line of a multi-line value
            sections[section][cur_key] = (
                sections[section][cur_key] + "\n" + line.strip()).strip()
    return sections


@dataclass
class Change:
    section: str
    key: str            # "" for a whole-section add/remove marker
    op: str             # 'add' | 'remove' | 'change'
    old: str = ""
    new: str = ""

    @property
    def section_type(self) -> str:
        """The section kind, dropping any instance name.

        'tmc2209 stepper_x' -> 'tmc2209'; 'gcode_macro START' ->
        'gcode_macro'; 'heater_bed' -> 'heater_bed'.
        """
        return self.section.split()[0] if self.section else ""


def diff_configs(before: str, after: str) -> list:
    """Return the list of Changes turning `before` into `after`."""
    a, b = parse_config(before), parse_config(after)
    changes = []
    for section in sorted(set(a) | set(b)):
        akeys, bkeys = a.get(section, {}), b.get(section, {})
        for key in sorted(set(akeys) | set(bkeys)):
            av, bv = akeys.get(key), bkeys.get(key)
            if av is None and bv is not None:
                changes.append(Change(section, key, "add", "", bv))
            elif av is not None and bv is None:
                changes.append(Change(section, key, "remove", av, ""))
            elif av != bv:
                changes.append(Change(section, key, "change", av, bv))
    return changes
