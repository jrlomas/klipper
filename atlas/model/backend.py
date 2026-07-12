# ModelBackend abstraction (FD-0002 §2, §7; Milestone C prep).
#
# One interface, several implementations: stub (for contract tests, no
# weights), llama.cpp (the deployable path — CUDA + ROCm + CPU builds,
# GGUF/Q4_K_M), and Hailo (the deploy target). The interface is fixed
# now so dropping Qwen3-4B onto the Hailo later is a plug-in, not an
# integration. Real inference lands in Milestone C; the availability
# probes and the profile enforcement are real today.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

from dataclasses import dataclass, field

from .profile import DEPLOY, DeployProfile


@dataclass
class Completion:
    text: str
    tool_calls: list = field(default_factory=list)  # [{name, arguments}]
    backend: str = ""
    stub: bool = False


class ModelBackend:
    """Interface every backend implements. generate() is the one contract."""

    name = "base"
    accelerator = "none"

    def available(self) -> bool:                     # pragma: no cover
        raise NotImplementedError

    def generate(self, prompt, schema=None, tools=None,
                 max_tokens=512) -> Completion:      # pragma: no cover
        raise NotImplementedError


class StubBackend(ModelBackend):
    """Deterministic, weight-free backend for contracts and eval plumbing.

    Returns scripted responses keyed by a substring of the prompt (or a
    default), and records calls. Lets the whole draft->validate->apply and
    eval pipeline be tested with no model present (FD-0002 §8 tier 2).
    """

    name = "stub"
    accelerator = "none"

    def __init__(self, responses=None, default="", tool_calls=None):
        self.responses = responses or {}
        self.default = default
        self.tool_calls = tool_calls or []
        self.calls = []

    def available(self) -> bool:
        return True

    def generate(self, prompt, schema=None, tools=None,
                 max_tokens=512) -> Completion:
        self.calls.append({"prompt": prompt, "schema": schema,
                           "tools": tools})
        text = self.default
        for needle, resp in self.responses.items():
            if needle in prompt:
                text = resp
                break
        return Completion(text=text, tool_calls=list(self.tool_calls),
                          backend=self.name, stub=True)


class LlamaCppBackend(ModelBackend):
    """The deployable path: llama.cpp with a GGUF model.

    Availability is probed (the python binding or a built binary); actual
    generation lands in Milestone C. accelerator distinguishes the CUDA /
    ROCm / CPU builds behind one interface.
    """

    name = "llama.cpp"

    def __init__(self, model_path=None, accelerator="cpu",
                 profile: DeployProfile = DEPLOY, params_b=4.0,
                 quant="Q4_K_M"):
        self.model_path = model_path
        self.accelerator = accelerator
        self.profile = profile
        self.params_b = params_b
        self.quant = quant

    def available(self) -> bool:
        try:
            import llama_cpp  # noqa: F401
            return True
        except ImportError:
            return False

    def enforce_budget(self) -> int:
        # A backend on the deployable path must fit the profile budget.
        return self.profile.check(self.profile.model, self.params_b,
                                  self.quant)

    def generate(self, prompt, schema=None, tools=None,
                 max_tokens=512) -> Completion:      # pragma: no cover
        raise NotImplementedError(
            "llama.cpp inference lands in Milestone C; the interface and "
            "budget guard are in place now.")


class HailoBackend(ModelBackend):
    """The deploy target: Hailo-10H (AI HAT+ 2), compiled Qwen3-4B."""

    name = "hailo"
    accelerator = "hailo"

    def __init__(self, profile: DeployProfile = DEPLOY):
        self.profile = profile

    def available(self) -> bool:
        try:
            import hailo_platform  # noqa: F401
            return True
        except ImportError:
            return False

    def generate(self, prompt, schema=None, tools=None,
                 max_tokens=512) -> Completion:      # pragma: no cover
        raise NotImplementedError(
            "Hailo inference lands at the Milestone C deploy step; see the "
            "Atlas bring-up plan.")


# Preference order: the real accelerators first, then CPU, then the stub
# (which is always available, so selection never fails).
def select_backend(profile: DeployProfile = DEPLOY, prefer=None,
                   candidates=None) -> ModelBackend:
    """Pick the first available backend in preference order.

    Falls back to StubBackend so contract/eval code always gets a usable
    backend even with no model installed.
    """
    if candidates is None:
        candidates = [
            HailoBackend(profile),
            LlamaCppBackend(accelerator="cuda", profile=profile),
            LlamaCppBackend(accelerator="rocm", profile=profile),
            LlamaCppBackend(accelerator="cpu", profile=profile),
            StubBackend(),
        ]
    order = prefer or [c.name + ":" + c.accelerator for c in candidates]
    for want in order:
        for c in candidates:
            tag = c.name + ":" + c.accelerator
            if (want in (c.name, tag)) and c.available():
                return c
    for c in candidates:
        if c.available():
            return c
    return StubBackend()
