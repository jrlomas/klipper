# The model tier abstraction (FD-0002 §2, §7; Milestone C).
#
# dev target != deploy target. We develop the model layer on NVIDIA
# (CUDA) / AMD (ROCm) GPUs because they are fast and present, but Atlas
# deploys on a Raspberry Pi 5 + Hailo-10H with a hard ~8 GB budget and a
# model-compilation step. So the runtime lives behind a ModelBackend
# interface (cuda / rocm / hailo / cpu / stub), and a DeployProfile guard
# holds development honest to the deploy budget even on a 16 GB dev card.
# The deterministic floor never depends on any of this.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

from .profile import (DeployProfile, DEPLOY, DEV, BudgetError,
                      ModelPinError, estimate_memory_mb)
from .backend import (ModelBackend, StubBackend, LlamaCppBackend,
                      HailoBackend, Completion, select_backend)
from . import prompts
from .assistant import answer_question, interpret_incident, propose_config_edit

__all__ = [
    "DeployProfile", "DEPLOY", "DEV", "BudgetError", "ModelPinError",
    "estimate_memory_mb", "ModelBackend", "StubBackend", "LlamaCppBackend",
    "HailoBackend", "Completion", "select_backend", "prompts",
    "answer_question", "interpret_incident", "propose_config_edit",
]
