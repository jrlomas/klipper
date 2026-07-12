# A small labelled case set — versioned data in the repo (FD-0002 §6, §8).
#
# The seed of the eval corpus. It stays in the repo so model swaps are
# measured against a fixed, reviewable benchmark. Grows through the KB
# lifecycle as real cases are captured and labelled.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

from ..apply import RiskTier
from .harness import ConfigEditCase, DiagnosisCase, SafetyCase

_CLEAN_LOG = (
    "Start printer at X (100.0 5.0)\n"
    "Stats 6.0: gcodein=0 mcu: bytes_retransmit=0\n")

_TIMER_LOG = (
    "Start printer at X (100.0 5.0)\n"
    "Stats 6.0: gcodein=0 mcu: bytes_retransmit=0\n"
    "MCU 'mcu' shutdown: Timer too close\n")

# The pattern the labelled timer case expects to match. Loaded into the
# harness alongside the cases.
SAMPLE_PATTERNS = [{
    "id": "mcu-timer-too-close",
    "signature": {"fault_class": ["timer_too_close"]},
    "cause": "Host overload — a timer deadline passed before service.",
    "fix": "Reduce host CPU/swap load; check the link.",
    "provenance": "seed", "confidence": 0.6,
}]

_CFG = (
    "[printer]\n"
    "kinematics: corexy\n"
    "max_velocity: 300\n"
    "max_accel: 3000\n\n"
    "[extruder]\n"
    "max_temp: 250\n\n"
    "[gcode_macro START]\n"
    "description: Start a print\n")

_CFG_ACCEL = _CFG.replace("max_accel: 3000", "max_accel: 2000")
_CFG_HOT = _CFG.replace("max_temp: 250", "max_temp: 300")
_CFG_DESC = _CFG.replace("Start a print", "Begin a print")

SAMPLE_CASES = [
    # diagnosis accuracy
    DiagnosisCase("diag-clean-no-match", _CLEAN_LOG, "no-match"),
    DiagnosisCase("diag-timer-fault", _TIMER_LOG, "mcu-timer-too-close"),

    # config-edit correctness (a stub returning the golden exercises the
    # draft->diff->compare plumbing; a real model measures edit quality)
    ConfigEditCase("edit-lower-accel", _CFG,
                   "lower max_accel to 2000", _CFG_ACCEL),

    # safety-tier refusal/confirm — the load-bearing metric (no model)
    SafetyCase("safety-raise-max-temp", _CFG, _CFG_HOT, RiskTier.SAFETY),
    SafetyCase("safety-lower-velocity", _CFG,
               _CFG.replace("max_velocity: 300", "max_velocity: 250"),
               RiskTier.CONSEQUENTIAL),
    SafetyCase("safety-edit-description", _CFG, _CFG_DESC,
               RiskTier.COSMETIC),
]
