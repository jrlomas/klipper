# ModelBackend abstraction (FD-0002 §2, §7; Milestone C prep).
#
# One interface, several implementations: stub (for contract tests, no
# weights), llama.cpp (the deployable path — CUDA + ROCm + CPU builds,
# GGUF/Q4_K_M), and Hailo (the deploy target). The interface is fixed
# now so dropping Qwen3-4B onto the Hailo later is a plug-in, not an
# integration. The llama.cpp binding and non-interactive binary transports
# are real today; deploy-model quality and Hailo execution land with the
# pinned Milestone C weights and target hardware.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field

from .profile import DEPLOY, DeployProfile


def _clean_model_text(text):
    """Remove transport/reasoning wrappers from user-visible output."""
    text = (text or "").strip()
    marker = "[end of text]"
    if text.endswith(marker):
        text = text[:-len(marker)].rstrip()
    if text.startswith("<think>") and "</think>" in text:
        text = text.split("</think>", 1)[1].lstrip()
    return text


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

    def generate(self, prompt, schema=None, tools=None, max_tokens=512,
                 system=None) -> Completion:         # pragma: no cover
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

    def generate(self, prompt, schema=None, tools=None, max_tokens=512,
                 system=None) -> Completion:
        self.calls.append({"prompt": prompt, "schema": schema,
                           "tools": tools, "system": system})
        text = self.default
        for needle, resp in self.responses.items():
            if needle in prompt:
                text = resp
                break
        return Completion(text=text, tool_calls=list(self.tool_calls),
                          backend=self.name, stub=True)


class LlamaCppBackend(ModelBackend):
    """The deployable path: llama.cpp with a GGUF model.

    Availability is probed (the Python binding or llama-completion binary)
    and both paths generate today. accelerator distinguishes the CUDA /
    ROCm / CPU builds behind one interface.
    """

    name = "llama.cpp"

    def __init__(self, model_path=None, accelerator="cpu",
                 profile: DeployProfile = DEPLOY, params_b=4.0,
                 quant="Q4_K_M", n_ctx=8192, system=None, llama=None,
                 cli_path=None, cli_runner=None, timeout=300):
        self.model_path = model_path
        self.accelerator = accelerator
        self.profile = profile
        self.params_b = params_b
        self.quant = quant
        self.n_ctx = n_ctx
        self.system = system
        self._llama = llama            # injectable for tests
        self.cli_path = cli_path
        self.cli_runner = cli_runner or subprocess.run
        self.timeout = timeout

    @staticmethod
    def _binding_available() -> bool:
        try:
            import llama_cpp  # noqa: F401
            return True
        except ImportError:
            return False

    def _find_cli(self):
        candidate = (self.cli_path or shutil.which("llama-completion")
                     or shutil.which("llama-cli"))
        # Current llama.cpp reserves non-interactive, file-fed generation
        # for llama-completion. Accept a llama-cli path as a convenient
        # locator when the sibling completion binary is installed.
        if candidate and os.path.basename(candidate) == "llama-cli":
            completion = os.path.join(os.path.dirname(candidate),
                                      "llama-completion")
            if os.path.isfile(completion):
                candidate = completion
        if candidate and os.path.isfile(candidate) and os.access(candidate,
                                                                    os.X_OK):
            return os.path.abspath(candidate)
        return None

    def available(self) -> bool:
        if self._llama is not None:
            return True
        return self._binding_available() or self._find_cli() is not None

    def enforce_budget(self) -> int:
        # A backend on the deployable path must fit the profile budget.
        return self.profile.check(self.profile.model, self.params_b,
                                  self.quant)

    def _ensure_loaded(self):
        if self._llama is None:
            if not self.model_path:
                raise RuntimeError("LlamaCppBackend needs a model_path")
            from llama_cpp import Llama
            # n_gpu_layers=-1 offloads to CUDA/ROCm when the build supports
            # it; a CPU build ignores it. Kept behind the one interface.
            n_gpu = -1 if self.accelerator in ("cuda", "rocm") else 0
            self._llama = Llama(model_path=self.model_path, n_ctx=self.n_ctx,
                                n_gpu_layers=n_gpu, verbose=False)
        return self._llama

    def generate(self, prompt, schema=None, tools=None, max_tokens=512,
                 system=None) -> Completion:
        """Run a chat completion, mapping schema->JSON grammar and
        tools->tool-calling. Returns a normalized Completion.
        """
        if self._llama is None and not self._binding_available():
            return self._generate_cli(prompt, schema, tools, max_tokens,
                                      system)
        llama = self._ensure_loaded()
        messages = [{"role": "system",
                     "content": system or self.system
                     or "You are Atlas, a local 3D-printer companion."},
                    {"role": "user", "content": prompt}]
        kwargs = {"messages": messages, "max_tokens": max_tokens,
                  "temperature": 0.2}
        if schema is not None:
            kwargs["response_format"] = {"type": "json_object",
                                         "schema": schema}
        if tools is not None:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        out = llama.create_chat_completion(**kwargs)
        msg = out["choices"][0]["message"]
        text = _clean_model_text(msg.get("content"))
        calls = []
        for tc in (msg.get("tool_calls") or []):
            fn = tc.get("function", {})
            args = fn.get("arguments", "{}")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {"_raw": args}
            calls.append({"name": fn.get("name"), "arguments": args})
        return Completion(text=text, tool_calls=calls,
                          backend="%s:%s" % (self.name, self.accelerator))

    def _generate_cli(self, prompt, schema, tools, max_tokens, system):
        cli = self._find_cli()
        if cli is None:
            raise RuntimeError("llama.cpp CLI is unavailable")
        if not self.model_path:
            raise RuntimeError("LlamaCppBackend needs a model_path")
        system_text = (system or self.system
                       or "You are Atlas, a local 3D-printer companion.")
        tool_mode = tools is not None
        effective_schema = schema
        if tool_mode:
            tool_names = [t.get("function", {}).get("name") for t in tools]
            tool_names = [name for name in tool_names if name]
            system_text += (
                "\nAvailable tools (JSON):\n%s\nTo call a tool, output only "
                "a JSON object with keys name and arguments. Do not claim "
                "an action occurred unless you emit that object."
                % json.dumps(tools, sort_keys=True))
            # llama-completion has no native tool-call transport. Constrain
            # its output to the same normalized envelope used by the Python
            # binding so malformed prose cannot masquerade as a tool call.
            effective_schema = {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "enum": tool_names},
                    "arguments": {"type": "object"},
                },
                "required": ["name", "arguments"],
                "additionalProperties": False,
            }
            if len(tools) == 1:
                parameters = tools[0].get("function", {}).get("parameters")
                if isinstance(parameters, dict):
                    effective_schema["properties"]["arguments"] = parameters
        if "qwen" in os.path.basename(self.model_path).lower():
            # Qwen2/3's native ChatML framing materially improves instruction
            # and JSON adherence on the non-chat llama-completion transport.
            # Atlas uses the non-thinking mode for bounded local latency. For
            # grammar-constrained output, prefill Qwen's empty reasoning block
            # so the first generated token can satisfy the JSON grammar.
            user_text = prompt + "\n/no_think"
            assistant_prefix = ("<think>\n\n</think>\n\n"
                                if effective_schema else "")
            combined_prompt = (
                "<|im_start|>system\n%s<|im_end|>\n"
                "<|im_start|>user\n%s<|im_end|>\n"
                "<|im_start|>assistant\n%s" % (system_text, user_text,
                                                assistant_prefix))
        else:
            combined_prompt = "%s\n\nUser:\n%s\n\nAssistant:\n" % (
                system_text, prompt)
        with tempfile.TemporaryDirectory(prefix="atlas-llama-") as tmp:
            prompt_path = os.path.join(tmp, "prompt.txt")
            with open(prompt_path, "w", encoding="utf-8") as handle:
                handle.write(combined_prompt)
            os.chmod(prompt_path, 0o600)
            argv = [
                cli, "--offline", "--model", os.path.abspath(self.model_path),
                "--ctx-size", str(self.n_ctx), "--predict", str(max_tokens),
                "--temp", "0.2", "--simple-io", "--no-display-prompt",
                "--no-conversation", "--file", prompt_path,
            ]
            if self.accelerator == "cpu":
                argv.extend(["--device", "none", "--gpu-layers", "0"])
            if effective_schema is not None:
                argv.extend(["--json-schema", json.dumps(
                    effective_schema, sort_keys=True)])
            result = self.cli_runner(
                argv, check=True, capture_output=True, text=True,
                timeout=self.timeout)
        text = _clean_model_text(result.stdout)
        calls = []
        if tool_mode:
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict) and isinstance(parsed.get("name"), str):
                arguments = parsed.get("arguments", {})
                if not isinstance(arguments, dict):
                    arguments = {"_raw": arguments}
                calls.append({"name": parsed["name"],
                              "arguments": arguments})
                text = ""
        return Completion(text=text, tool_calls=calls,
                          backend="%s:%s" % (self.name, self.accelerator))


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
