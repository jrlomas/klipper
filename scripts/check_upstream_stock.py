#!/usr/bin/env python3
# HELIX upstream-tracking guard.
#
# Enforces the "envelope/sidecar" architecture (docs/Upstream_Tracking.md):
#   1. STOCK guard: a small set of upstream-Klipper files that carry the v1
#      wire protocol MUST stay byte-identical to the recorded upstream
#      baseline, so future Klipper releases merge into HELIX cleanly. HELIX
#      never adds protocol features by editing them; the novelty lives in the
#      additive intentproto v2 envelope.
#   2. QUARANTINE guard: the intentproto v1-reimplementation translation units
#      (proto/dict/host) must never be linked into an APPLICATION firmware
#      image; only the bootloader (the sanctioned exception) may link them.
#
# Runs fully offline against the hashes in scripts/upstream_stock.manifest.
# On a legitimate upstream merge that changes a stock file, re-run with
# --update to record the new baseline (that is the ONLY sanctioned way the
# hashes move).
#
# Exit status: 0 = clean, 1 = a guard failed, 2 = usage/IO error.

import argparse
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
MANIFEST = os.path.join(HERE, "upstream_stock.manifest")


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest():
    with open(MANIFEST) as f:
        return json.load(f)


def check_stock(man):
    """Every stock file must match its recorded hash. Returns list of errors."""
    errors = []
    for entry in man["stock_files"]:
        path = os.path.join(REPO, entry["path"])
        if not os.path.exists(path):
            errors.append("missing stock file: %s" % entry["path"])
            continue
        got = sha256_of(path)
        if got != entry["sha256"]:
            errors.append(
                "STOCK FILE DIVERGED from upstream baseline: %s\n"
                "    recorded %s\n    actual   %s\n"
                "    This file must stay byte-identical to upstream Klipper.\n"
                "    If this change came from a real upstream merge, re-run\n"
                "    `scripts/check_upstream_stock.py --update` as part of it."
                % (entry["path"], entry["sha256"], got))
    return errors


def check_quarantine(man):
    """No application build file may reference proto/dict/host. Errors list."""
    q = man["app_quarantine"]
    forbidden = q["forbidden"]
    errors = []
    for rel in q["app_build_files"]:
        path = os.path.join(REPO, rel)
        if not os.path.exists(path):
            errors.append("missing app build file: %s" % rel)
            continue
        with open(path) as f:
            text = f.read()
        for tok in forbidden:
            # Match the token as a path component / object name, not as a
            # substring of a longer word (e.g. avoid matching "protocol").
            for lineno, line in enumerate(text.splitlines(), 1):
                if _references(line, tok):
                    errors.append(
                        "QUARANTINE VIOLATION: %s:%d links '%s'\n"
                        "    The intentproto v1-reimplementation TUs "
                        "(proto/dict/host) must never enter application\n"
                        "    firmware — only the bootloader may. Line: %s"
                        % (rel, lineno, tok, line.strip()))
    return errors


def _references(line, tok):
    # tok is like "proto.cpp" or "proto.o". Treat '.' literally and require a
    # non-alphanumeric boundary before the stem so "protocol" never matches.
    stem = tok.split(".")[0]        # proto / dict / host
    ext = tok[len(stem):]           # .cpp / .o
    idx = 0
    while True:
        idx = line.find(tok, idx)
        if idx < 0:
            return False
        before = line[idx - 1] if idx > 0 else " "
        after_i = idx + len(tok)
        after = line[after_i] if after_i < len(line) else " "
        if not (before.isalnum() or before == "_") and \
           not (after.isalnum() or after == "_"):
            return True
        idx = after_i
    # (unreachable)


def do_update(man):
    changed = []
    for entry in man["stock_files"]:
        path = os.path.join(REPO, entry["path"])
        got = sha256_of(path)
        if got != entry["sha256"]:
            changed.append((entry["path"], entry["sha256"], got))
            entry["sha256"] = got
    if not changed:
        print("baseline already current; nothing to update")
        return 0
    with open(MANIFEST, "w") as f:
        json.dump(man, f, indent=2)
        f.write("\n")
    print("updated baseline for %d file(s):" % len(changed))
    for p, old, new in changed:
        print("  %s\n    %s -> %s" % (p, old, new))
    print("\nCommit this manifest change AS PART OF the upstream merge that "
          "caused it,\nand explain the upstream delta in the commit message.")
    return 0


def main():
    ap = argparse.ArgumentParser(description="HELIX upstream-tracking guard")
    ap.add_argument("--update", action="store_true",
                    help="record the current stock-file hashes as the new "
                         "baseline (use only inside a real upstream merge)")
    args = ap.parse_args()

    try:
        man = load_manifest()
    except (IOError, ValueError) as e:
        sys.stderr.write("cannot read manifest %s: %s\n" % (MANIFEST, e))
        return 2

    if args.update:
        return do_update(man)

    errors = check_stock(man) + check_quarantine(man)
    if errors:
        sys.stderr.write("HELIX upstream guard FAILED:\n\n")
        for e in errors:
            sys.stderr.write("  * " + e + "\n")
        sys.stderr.write(
            "\nSee docs/Upstream_Tracking.md for why these invariants hold.\n")
        return 1

    n_stock = len(man["stock_files"])
    n_app = len(man["app_quarantine"]["app_build_files"])
    print("HELIX upstream guard OK: %d stock file(s) match the upstream "
          "baseline; %d application build file(s) quarantine-clean."
          % (n_stock, n_app))
    return 0


if __name__ == "__main__":
    sys.exit(main())
