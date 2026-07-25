#!/usr/bin/env python3
"""Compile a restricted portable Python module into target-native code."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from helix.compiler.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
