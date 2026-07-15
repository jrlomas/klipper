#!/usr/bin/env python3
"""Include-aware, bounded Atlas config-grounding tests."""

import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from atlas.config_context import read_config_tree  # noqa: E402
from atlas.model.assistant import config_excerpt  # noqa: E402


def test_led_question_gets_active_hardware_and_effect_with_sources():
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "printer.cfg").write_text(
            "[include toolhead.cfg]\n"
            "[printer]\nkinematics: corexy\n"
            "# [neopixel displayStatus]\n# pin: display:PA0\n")
        (root / "toolhead.cfg").write_text(
            "[neopixel board_neopixel]\n"
            "pin: ebb36:PD3\nchain_count: 3\n\n"
            "[led_effect panel_idle]\n"
            "leds:\n    neopixel:board_neopixel\n"
            "layers:\n    breathing 10 1 top (.0,.88,.71)\n")
        tree = read_config_tree(str(root / "printer.cfg"), 1024 * 1024)
        excerpt = config_excerpt(
            tree, "Where are the effects for the toolhead neopixels?")
        assert "# Atlas source: toolhead.cfg" in excerpt
        assert "[neopixel board_neopixel]" in excerpt
        assert "[led_effect panel_idle]" in excerpt
        assert "ebb36:PD3" in excerpt
        assert "displayStatus" not in excerpt
        assert "_TOOLHEAD_PARK_PAUSE_CANCEL" not in excerpt
        print("PASS: LED questions retrieve active hardware and effects "
              "with source attribution")


def test_include_tree_is_bounded_and_cannot_escape_config_root():
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        outside = root.parent / (root.name + "-outside.cfg")
        outside.write_text("password=hunter2\n")
        try:
            (root / "printer.cfg").write_text(
                "[include ../%s]\n[include child.cfg]\n" % outside.name)
            (root / "child.cfg").write_text("[include printer.cfg]\n[fan]\npin: PA0\n")
            tree = read_config_tree(str(root / "printer.cfg"), 1024)
            assert "hunter2" not in tree
            assert tree.count("# Atlas source: printer.cfg") == 1
            assert tree.count("# Atlas source: child.cfg") == 1
        finally:
            outside.unlink()
        print("PASS: include traversal is root-confined, cycle-safe, and "
              "bounded")


def main():
    test_led_question_gets_active_hardware_and_effect_with_sources()
    test_include_tree_is_bounded_and_cannot_escape_config_root()
    print("ALL PASS")


if __name__ == "__main__":
    main()
