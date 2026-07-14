#!/usr/bin/env python3
# Standalone unit test for the Atlas apply layer (FD-0002 §7).
# The risk classifier is the safety gate — a deterministic, non-LLM
# decision — so it gets the most coverage: safety changes always confirm,
# consequential auto-apply with undo, cosmetic auto-apply, and the most
# conservative tier wins across a changeset. Also checks the config
# differ and the draft->validate->apply->undo pipeline.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                ".."))

from atlas.apply import (ApplyPipeline, Change, Proposal,  # noqa: E402
                         apply_config_edits,
                         RiskTier, classify_change, classify_changeset,
                         decision_for, diff_configs, parse_config)

CFG_A = """\
[printer]
kinematics: corexy
max_velocity: 300
max_accel: 3000

[extruder]
max_temp: 250
rotation_distance: 22.6
control: pid

[gcode_macro START_PRINT]
description: Start a print
gcode:
  G28
  G1 Z5 F3000

[display]
menu_root: __main
"""


# -- config diff ---------------------------------------------------------

def test_parse_config():
    cfg = parse_config(CFG_A)
    assert cfg["extruder"]["max_temp"] == "250"
    assert cfg["printer"]["kinematics"] == "corexy"
    # multi-line gcode body folded into one value
    assert "G28" in cfg["gcode_macro START_PRINT"]["gcode"]
    assert "G1 Z5 F3000" in cfg["gcode_macro START_PRINT"]["gcode"]
    print("PASS: config parses sections, keys, and multi-line values")


def test_diff_detects_changes():
    after = CFG_A.replace("max_temp: 250", "max_temp: 300")
    changes = diff_configs(CFG_A, after)
    assert len(changes) == 1
    c = changes[0]
    assert c.section == "extruder" and c.key == "max_temp"
    assert c.op == "change" and c.old == "250" and c.new == "300"
    print("PASS: differ detects a single changed key with old/new")


def test_diff_add_remove():
    after = CFG_A + "\n[fan]\npin: PA8\n"
    changes = diff_configs(CFG_A, after)
    assert any(c.section == "fan" and c.op == "add" for c in changes)
    print("PASS: differ detects an added section/key")


# -- classifier: the safety gate -----------------------------------------

def test_safety_by_key():
    for section, key in [("extruder", "max_temp"), ("extruder", "control"),
                         ("stepper_x", "rotation_distance"),
                         ("printer", "kinematics"),
                         ("stepper_x", "position_endstop"),
                         ("stepper_x", "microsteps")]:
        c = Change(section, key, "change", "1", "2")
        assert classify_change(c) == RiskTier.SAFETY, (section, key)
    print("PASS: thermal/kinematics/endstop keys classify as SAFETY")


def test_safety_by_section():
    # Any key in a driver-current or probe section is safety-affecting.
    for section in ["tmc2209 stepper_x", "heater_bed", "probe", "bltouch"]:
        c = Change(section, "some_key", "change", "1", "2")
        assert classify_change(c) == RiskTier.SAFETY, section
    print("PASS: driver/heater/probe sections are wholly SAFETY")


def test_cosmetic():
    assert classify_change(
        Change("gcode_macro START_PRINT", "description", "change", "a", "b")
    ) == RiskTier.COSMETIC
    assert classify_change(
        Change("display", "menu_root", "change", "a", "b")
    ) == RiskTier.CONSEQUENTIAL   # display non-cosmetic key -> consequential
    assert classify_change(
        Change("display_glyph logo", "text", "change", "a", "b")
    ) == RiskTier.COSMETIC
    print("PASS: descriptions/labels are COSMETIC; display logic isn't")


def test_confirmation_by_default():
    # Executable macro bodies and unknown plugin semantics require explicit
    # confirmation; only allowlisted reversible values are consequential.
    assert classify_change(
        Change("gcode_macro X", "gcode", "change", "G28", "G28 X")
    ) == RiskTier.SAFETY
    assert classify_change(
        Change("some_new_module", "mystery_option", "add", "", "1")
    ) == RiskTier.SAFETY
    assert classify_change(
        Change("printer", "max_velocity", "change", "300", "250")
    ) == RiskTier.CONSEQUENTIAL
    print("PASS: executable/unknown semantics confirm; allowlist is reversible")


def test_targeted_config_editor_preserves_unrelated_text():
    before = ("# top\n[printer]\nmax_velocity: 300 # retained\n"
              "# adjacent comment\nmax_accel: 3000\n\n"
              "[gcode_macro X]\ngcode:\n  G28\n  M400\n")
    after = apply_config_edits(before, [{
        "section": "printer", "key": "max_velocity",
        "operation": "set", "value": "250"}])
    assert "max_velocity: 250 # retained" in after
    assert "# adjacent comment\nmax_accel: 3000" in after
    assert "gcode:\n  G28\n  M400" in after
    assert len(diff_configs(before, after)) == 1
    print("PASS: targeted editor changes one key and preserves file context")


def test_targeted_config_editor_rejects_ambiguous_or_noop_edits():
    for edits in ([{"section": "printer", "key": "max_velocity",
                    "operation": "set", "value": "300"}],
                  [{"section": "missing", "key": "x",
                    "operation": "remove"}]):
        try:
            apply_config_edits(CFG_A, edits)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid targeted edit was accepted")
    print("PASS: targeted editor rejects no-op and missing removals")


def test_targeted_editor_replaces_complete_multiline_value():
    before = ("[gcode_macro X]\ngcode:\n  G28\n  # inside body\n\n"
              "  M104 S200\n\n# outside body\n[display]\ntext: hi\n")
    after = apply_config_edits(before, [{
        "section": "gcode_macro X", "key": "gcode",
        "operation": "set", "value": "G28 X\nM400"}])
    assert "M104 S200" not in after
    assert "G28 X\n    M400" in after
    assert "# outside body\n[display]" in after
    changes = diff_configs(before, after)
    assert len(changes) == 1 and changes[0].key == "gcode"
    print("PASS: targeted editor replaces a complete commented multiline value")


def test_most_conservative_wins():
    changes = [
        Change("display", "menu_root", "change", "a", "b"),   # consequential
        Change("gcode_macro X", "description", "change", "a", "b"),  # cosmetic
        Change("extruder", "max_temp", "change", "250", "300"),  # SAFETY
    ]
    overall, per = classify_changeset(changes)
    assert overall == RiskTier.SAFETY
    assert len(per) == 3
    print("PASS: one safety-affecting line makes the whole changeset SAFETY")


def test_decision_mapping():
    assert decision_for(RiskTier.SAFETY) == ("confirm", True)
    assert decision_for(RiskTier.CONSEQUENTIAL)[1] is False
    assert decision_for(RiskTier.COSMETIC) == ("auto-apply", False)
    print("PASS: tiers map to the FD-0002 §7 actions")


# -- pipeline ------------------------------------------------------------

def test_pipeline_safety_requires_confirmation():
    after = CFG_A.replace("max_temp: 250", "max_temp: 300")
    pipe = ApplyPipeline()
    res = pipe.process(Proposal(CFG_A, after))
    assert res.tier == RiskTier.SAFETY
    assert res.needs_confirmation and not res.applied
    assert res.entry is None                 # nothing journaled yet
    # With explicit confirmation it applies and journals.
    res2 = pipe.process(Proposal(CFG_A, after), confirmed=True)
    assert res2.applied and res2.entry is not None
    print("PASS: safety edit blocks until confirmed, then journals")


def test_pipeline_cosmetic_auto_applies():
    after = CFG_A.replace("Start a print", "Start the print")
    pipe = ApplyPipeline()
    res = pipe.process(Proposal(CFG_A, after))
    assert res.tier == RiskTier.COSMETIC
    assert res.applied and not res.needs_confirmation
    print("PASS: cosmetic edit auto-applies without confirmation")


def test_pipeline_consequential_auto_with_undo():
    after = CFG_A.replace("max_velocity: 300", "max_velocity: 250")
    pipe = ApplyPipeline()
    res = pipe.process(Proposal(CFG_A, after))
    assert res.tier == RiskTier.CONSEQUENTIAL and res.applied
    # undo restores the original text
    restored = pipe.undo_last()
    assert restored == CFG_A
    assert pipe.journal.entries[-1].reverted
    print("PASS: consequential edit auto-applies and is undoable")


def test_pipeline_rejects_noop_and_invalid():
    pipe = ApplyPipeline()
    res = pipe.process(Proposal(CFG_A, CFG_A))   # identical
    assert not res.validation.ok and not res.applied
    print("PASS: a no-op proposal is rejected by validation")


def test_preview_never_applies_or_journals():
    after = CFG_A.replace("Start a print", "Preview wording")
    pipe = ApplyPipeline()
    result = pipe.preview(Proposal(CFG_A, after))
    assert result.validation.ok and result.tier == RiskTier.COSMETIC
    assert result.applied is False and result.entry is None
    assert pipe.journal.entries == []
    print("PASS: preview classifies without applying or journaling")


def test_journal_audit_trail():
    pipe = ApplyPipeline()
    a2 = CFG_A.replace("Start a print", "Begin print")     # cosmetic
    a3 = a2.replace("max_velocity: 300", "max_velocity: 280")  # conseq
    pipe.process(Proposal(CFG_A, a2))
    pipe.process(Proposal(a2, a3))
    assert len(pipe.journal.entries) == 2
    assert [e.tier for e in pipe.journal.entries] == [
        RiskTier.COSMETIC, RiskTier.CONSEQUENTIAL]
    print("PASS: every applied change is journaled (the audit trail)")


def main():
    test_parse_config()
    test_diff_detects_changes()
    test_diff_add_remove()
    test_safety_by_key()
    test_safety_by_section()
    test_cosmetic()
    test_confirmation_by_default()
    test_targeted_config_editor_preserves_unrelated_text()
    test_targeted_config_editor_rejects_ambiguous_or_noop_edits()
    test_targeted_editor_replaces_complete_multiline_value()
    test_most_conservative_wins()
    test_decision_mapping()
    test_pipeline_safety_requires_confirmation()
    test_pipeline_cosmetic_auto_applies()
    test_pipeline_consequential_auto_with_undo()
    test_pipeline_rejects_noop_and_invalid()
    test_preview_never_applies_or_journals()
    test_journal_audit_trail()
    print("ALL PASS")


if __name__ == "__main__":
    main()
