# The deterministic risk classifier — the safety gate (FD-0002 §7).
#
# This is the load-bearing safety decision, and it is deliberately NOT the
# model: a plain, auditable rules table sets the tier from a config diff.
#   SAFETY        - thermal limits, endstop/probe, kinematics scale, driver
#                   current, heater/stepper pins: can damage the machine or
#                   a person. Always confirm; never auto-applied.
#   CONSEQUENTIAL - explicitly allowlisted speed/accel/service defaults:
#                   reversible. Auto-apply allowed with undo + audit.
#   COSMETIC      - display layout, labels, macro descriptions: auto-apply.
#
# Unknown config semantics are confirmation-by-default. Only explicitly
# allowlisted reversible or cosmetic changes may avoid confirmation.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

from enum import IntEnum


class RiskTier(IntEnum):
    COSMETIC = 0
    CONSEQUENTIAL = 1
    SAFETY = 2          # highest — most conservative wins in a changeset


# Sections that are safety-critical in their entirety: adding, removing, or
# editing any key here is safety-affecting.
_SAFETY_SECTIONS = frozenset({
    "heater_bed", "extruder", "extruder1", "extruder2", "extruder3",
    "heater_generic", "probe", "bltouch", "smart_effector", "safe_z_home",
    "verify_heater", "tmc2209", "tmc2208", "tmc2130", "tmc2660", "tmc5160",
    "tmc2240", "temperature_fan", "gcode_shell_command", "delayed_gcode",
    "output_pin", "pwm_tool", "servo", "static_digital_output",
    "static_pwm_output", "mcu", "multi_pin", "homing_override",
    "endstop_phase", "filament_switch_sensor", "filament_motion_sensor",
    "load_cell", "hx711s", "adxl345",
})

# Keys that are safety-affecting in *any* section.
_SAFETY_KEYS = frozenset({
    "kinematics",
    "max_temp", "min_temp", "max_power", "min_extrude_temp",
    "pwm_cycle_time", "control", "pid_kp", "pid_ki", "pid_kd",
    "run_current", "hold_current", "sense_resistor",
    "rotation_distance", "full_steps_per_rotation", "gear_ratio",
    "microsteps", "position_endstop", "position_min", "position_max",
    "homing_speed", "second_homing_speed", "homing_retract_dist",
    "endstop_pin", "heater_pin", "sensor_pin", "sensor_type",
    "step_pin", "dir_pin", "enable_pin", "z_offset", "x_offset",
    "y_offset", "pin", "gcode", "command", "script", "runout_gcode",
    "insert_gcode",
})

_CONSEQUENTIAL_KEYS = frozenset({
    "max_velocity", "max_accel", "max_accel_to_decel",
    "minimum_cruise_ratio", "square_corner_velocity", "max_z_velocity",
    "max_z_accel", "timeout", "path", "recover_velocity",
})

_CONSEQUENTIAL_SECTIONS = frozenset({
    "printer", "idle_timeout", "pause_resume", "respond", "save_variables",
    "virtual_sdcard", "exclude_object", "firmware_retraction",
})

# Sections whose non-logic keys are cosmetic.
_COSMETIC_SECTIONS = frozenset({
    "display", "display_data", "display_glyph", "display_template",
    "menu",
})

_COSMETIC_KEYS = frozenset({
    "description", "text", "glyphs", "icon", "color",
})


def classify_change(change) -> RiskTier:
    st = change.section_type.lower()
    key = (change.key or "").lower()

    if st in _SAFETY_SECTIONS:
        return RiskTier.SAFETY
    if key in _SAFETY_KEYS:
        return RiskTier.SAFETY

    if st == "gcode_macro":
        # Macro bodies can command arbitrary motion, heat, pins, and shell
        # bridges. Descriptions alone are cosmetic.
        return RiskTier.COSMETIC if key == "description" \
            else RiskTier.SAFETY
    if st in _COSMETIC_SECTIONS:
        return RiskTier.COSMETIC if key in _COSMETIC_KEYS \
            else RiskTier.CONSEQUENTIAL
    if key in _COSMETIC_KEYS:
        return RiskTier.COSMETIC
    if st in _CONSEQUENTIAL_SECTIONS and key in _CONSEQUENTIAL_KEYS:
        return RiskTier.CONSEQUENTIAL

    # Unrecognised plugin/section/key semantics require explicit confirmation.
    return RiskTier.SAFETY


def classify_changeset(changes) -> tuple:
    """Return (overall_tier, [(change, tier), ...]).

    The overall tier is the most conservative (highest) across all
    changes — one safety-affecting line makes the whole edit confirm.
    """
    per = [(c, classify_change(c)) for c in changes]
    overall = max((t for _, t in per), default=RiskTier.COSMETIC)
    return overall, per


# tier -> (action, needs_confirmation)
_DECISION = {
    RiskTier.SAFETY: ("confirm", True),
    RiskTier.CONSEQUENTIAL: ("auto-apply-with-undo", False),
    RiskTier.COSMETIC: ("auto-apply", False),
}


def decision_for(tier: RiskTier) -> tuple:
    """(action, needs_confirmation) for a tier."""
    return _DECISION[tier]
