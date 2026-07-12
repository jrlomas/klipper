# Deploy profile + budget guard (FD-0002 §2; decision 2026-07-12 #6).
#
# The deploy target is a Hailo-10H with ~8 GB, running Qwen3-4B Q4_K_M.
# A dev GPU (the AMD Radeon here is ~16 GB) will happily load a 14B model
# — which is exactly the temptation this guard removes. `--profile deploy`
# refuses any model past the pinned Qwen3-4B / ~6 GB ceiling, so the eval
# harness always re-tests against what actually ships. The pin itself is
# versioned data in the repo; this module is the enforcement.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

from dataclasses import dataclass


class BudgetError(RuntimeError):
    """A model would exceed the profile's memory ceiling."""


class ModelPinError(RuntimeError):
    """A model differs from the profile's pinned model in strict mode."""


# Approximate bytes-per-weight by quantization (weights only). Q4_K_M is
# ~4.5 bits/weight; used to keep a 4B model inside the Hailo budget.
_BYTES_PER_WEIGHT = {
    "F16": 2.0, "Q8_0": 1.06, "Q6_K": 0.82, "Q5_K_M": 0.7,
    "Q4_K_M": 0.56, "Q4_0": 0.5, "Q3_K_M": 0.43, "Q2_K": 0.33,
}


def estimate_memory_mb(params_b: float, quant: str = "Q4_K_M",
                       context: int = 8192) -> int:
    """Rough resident-memory estimate (MB) for a GGUF model + KV cache.

    Weights = params * bytes/weight; plus a coarse KV-cache/overhead term
    scaled by context. Deliberately conservative — this gates a budget,
    so over-estimating is the safe direction.
    """
    bpw = _BYTES_PER_WEIGHT.get(quant.upper(), 0.56)
    weights_mb = params_b * 1e9 * bpw / (1024 * 1024)
    kv_mb = context / 8192 * 400 * max(params_b / 4.0, 1.0)  # coarse
    overhead_mb = 300
    return int(weights_mb + kv_mb + overhead_mb)


@dataclass
class DeployProfile:
    name: str
    model: str                 # pinned model family+size, e.g. "Qwen3-4B"
    quant: str                 # pinned quantization, e.g. "Q4_K_M"
    context: int               # pinned context window
    mem_ceiling_mb: int        # hard resident-memory ceiling
    strict: bool               # True: also pin the exact model+quant

    def check(self, model: str, params_b: float, quant: str,
              context: int = None) -> int:
        """Validate a candidate model against the profile.

        Returns the estimated memory (MB) on success; raises BudgetError
        if it exceeds the ceiling, or ModelPinError in strict mode if the
        model/quant differs from the pin.
        """
        context = self.context if context is None else context
        if self.strict:
            if model != self.model:
                raise ModelPinError(
                    "%s: model %r is not the pinned %r"
                    % (self.name, model, self.model))
            if quant.upper() != self.quant.upper():
                raise ModelPinError(
                    "%s: quant %r is not the pinned %r"
                    % (self.name, quant, self.quant))
        est = estimate_memory_mb(params_b, quant, context)
        if est > self.mem_ceiling_mb:
            raise BudgetError(
                "%s: ~%d MB exceeds the %d MB ceiling (model %s %s, "
                "ctx %d)" % (self.name, est, self.mem_ceiling_mb, model,
                             quant, context))
        return est


# The canonical deploy profile — what actually ships on the Hailo-10H.
# ~6 GB leaves headroom under the 8 GB board budget for context + ASR.
DEPLOY = DeployProfile(
    name="deploy", model="Qwen3-4B", quant="Q4_K_M", context=8192,
    mem_ceiling_mb=6144, strict=True)

# A looser dev profile for experimentation on a bigger card. It does NOT
# pin the model, but the eval harness must still report against DEPLOY, so
# a passing dev run is never mistaken for a passing deploy run.
DEV = DeployProfile(
    name="dev", model="Qwen3-4B", quant="Q4_K_M", context=8192,
    mem_ceiling_mb=15000, strict=False)
