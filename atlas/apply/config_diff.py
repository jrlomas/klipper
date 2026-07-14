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
_RE_EDIT_NAME = re.compile(r"^[^\]\r\n:=#;]+$")


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


def _validated_edit(edit):
    if not isinstance(edit, dict):
        raise ValueError("config edits must be objects")
    section = edit.get("section")
    key = edit.get("key")
    operation = edit.get("operation")
    if not isinstance(section, str) or not section.strip() \
            or not _RE_EDIT_NAME.match(section.strip()):
        raise ValueError("config edit has an invalid section")
    if not isinstance(key, str) or not key.strip() \
            or not _RE_EDIT_NAME.match(key.strip()):
        raise ValueError("config edit has an invalid key")
    if operation not in ("set", "remove"):
        raise ValueError("config edit operation must be set or remove")
    value = edit.get("value", "")
    if operation == "set" and not isinstance(value, str):
        raise ValueError("config edit value must be a string")
    if "\x00" in value:
        raise ValueError("config edit value contains a NUL byte")
    return section.strip(), key.strip(), operation, value


def _locate(lines, section, key):
    """Return (section header, section end, key span) line indexes."""
    headers = []
    for i, raw in enumerate(lines):
        match = _RE_SECTION.match(_strip_comment(raw).strip())
        if match and match.group("name").strip() == section:
            headers.append(i)
    if len(headers) > 1:
        raise ValueError("config contains duplicate section [%s]" % section)
    if not headers:
        return None, None, None
    start = headers[0]
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if _RE_SECTION.match(_strip_comment(lines[i]).strip()):
            end = i
            break

    spans = []
    i = start + 1
    while i < end:
        clean = _strip_comment(lines[i])
        match = _RE_KEY.match(clean)
        if not match:
            i += 1
            continue
        indent = len(clean) - len(clean.lstrip())
        name = match.group("key").strip()
        span_end = i + 1
        while span_end < end:
            following = _strip_comment(lines[span_end])
            next_indent = len(following) - len(following.lstrip())
            if (not following.strip() or
                    lines[span_end].lstrip().startswith(("#", ";"))):
                # A comment/blank run inside an indented value belongs to the
                # value only when another continuation follows it. Otherwise
                # it is surrounding layout and must be preserved.
                lookahead = span_end
                while lookahead < end and (
                        not _strip_comment(lines[lookahead]).strip() or
                        lines[lookahead].lstrip().startswith(("#", ";"))):
                    lookahead += 1
                if lookahead >= end:
                    break
                candidate = _strip_comment(lines[lookahead])
                candidate_indent = len(candidate) - len(candidate.lstrip())
                if candidate_indent <= indent:
                    break
                span_end = lookahead
                continue
            if next_indent <= indent:
                break
            span_end += 1
        if name == key:
            spans.append((i, span_end, match))
        i = span_end
    if len(spans) > 1:
        raise ValueError("config contains duplicate key %s in [%s]"
                         % (key, section))
    return start, end, spans[0] if spans else None


def _render_value(prefix, value):
    parts = value.splitlines() or [""]
    separator = ("" if not parts[0] or prefix.endswith((" ", "\t")) else " ")
    rendered = [prefix + separator + parts[0]]
    rendered.extend("    " + part for part in parts[1:])
    return rendered


def apply_config_edits(before: str, edits, max_edits: int = 64) -> str:
    """Apply model-proposed, key-scoped edits without re-emitting the file.

    Only named section/key values are constructed here. Unrelated lines,
    comments, includes, and ordering remain byte-for-byte identical.
    Ambiguous duplicate targets and no-op edits are rejected.
    """
    if not isinstance(edits, list) or not edits:
        raise ValueError("config edits must be a non-empty array")
    if len(edits) > max_edits:
        raise ValueError("config edit exceeds %d operations" % max_edits)
    lines = before.splitlines()
    trailing_newline = before.endswith("\n")
    seen = set()
    for raw_edit in edits:
        section, key, operation, value = _validated_edit(raw_edit)
        identity = (section.lower(), key.lower())
        if identity in seen:
            raise ValueError("config edit repeats %s in [%s]" % (key, section))
        seen.add(identity)
        header, section_end, span = _locate(lines, section, key)
        if operation == "remove":
            if span is None:
                raise ValueError("cannot remove missing %s from [%s]"
                                 % (key, section))
            del lines[span[0]:span[1]]
            continue

        if header is None:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append("[%s]" % section)
            lines.extend(_render_value("%s: " % key, value))
            continue
        if span is None:
            rendered = _render_value("%s: " % key, value)
            lines[section_end:section_end] = rendered
            continue

        start, end, match = span
        original = lines[start]
        clean = _strip_comment(original)
        value_start = match.start("val")
        prefix = clean[:value_start]
        # Inline comments belong to the targeted line and are retained.
        markers = [i for i in (original.find("#"), original.find(";"))
                   if i >= 0]
        comment = (" " + original[min(markers):].lstrip()
                   if markers else "")
        rendered = _render_value(prefix, value)
        if comment:
            rendered[0] += comment
        lines[start:end] = rendered

    after = "\n".join(lines) + ("\n" if trailing_newline else "")
    if after == before or not diff_configs(before, after):
        raise ValueError("config edit made no semantic change")
    return after
