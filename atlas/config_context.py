"""Bounded, read-only traversal of Klipper's local include tree."""

import glob
import os
import re


_INCLUDE = re.compile(r"^\s*\[include\s+([^\]]+)\]\s*$",
                      re.IGNORECASE)


def read_config_tree(root_path, max_bytes, max_files=64, max_depth=8):
    """Return source-labelled config text rooted inside one config dir."""
    root_path = os.path.realpath(os.path.abspath(os.path.expanduser(
        root_path)))
    root_dir = os.path.dirname(root_path)
    seen = set()
    chunks = []
    total = 0

    def inside(path):
        try:
            return os.path.commonpath((root_dir, path)) == root_dir
        except ValueError:
            return False

    def visit(path, depth):
        nonlocal total
        path = os.path.realpath(path)
        if depth > max_depth or path in seen or len(seen) >= max_files:
            return
        if not inside(path) or not os.path.isfile(path):
            return
        size = os.path.getsize(path)
        if size > max_bytes - total:
            return
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        seen.add(path)
        total += size
        relative = os.path.relpath(path, root_dir).replace(os.sep, "/")
        chunks.append("# Atlas source: %s\n%s" % (relative, text))
        for line in text.splitlines():
            match = _INCLUDE.match(line)
            if match is None:
                continue
            pattern = match.group(1).strip()
            if not pattern or os.path.isabs(pattern):
                continue
            candidate = os.path.realpath(os.path.join(root_dir, pattern))
            if not inside(candidate):
                continue
            for included in sorted(glob.glob(candidate)):
                visit(included, depth + 1)

    visit(root_path, 0)
    return "\n".join(chunks)
