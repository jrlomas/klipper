# Atlas Pinned-Model Evaluation

Status: **Corpus v2 passed on both workstation GPUs on 2026-07-14.** This is
the model-quality authoring result for the pinned Qwen artifact. Pi 5 +
Hailo-10H validation remains pending.

## Artifact and budget

- Model: official `Qwen/Qwen3-4B-GGUF`, `Qwen3-4B-Q4_K_M.gguf`
- File size: 2,497,280,256 bytes
- SHA-256: `7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5`
- Deploy-profile estimate: 2,836 MB; ceiling: 6,144 MB — **PASS**

The backend keeps prompts in mode-private temporary files, uses Qwen's native
chat framing in bounded non-thinking mode, and grammar-constrains tool output.
The CLI transport explicitly requests full layer offload for CUDA/ROCm
(`--gpu-layers 99`); the CPU profile explicitly disables devices and uses zero
GPU layers. Accelerator labels therefore describe the executed command rather
than relying on version-dependent llama.cpp defaults.

## Corpus v2

The in-repository corpus now contains 50 labelled cases. Reports deliberately
separate deterministic invariants from model quality and do not publish a
cross-category "overall" percentage.

| Category | Cases | Execution |
| --- | ---: | --- |
| Diagnosis matcher | 4 | Deterministic |
| Safety classifier | 18 | Deterministic |
| Targeted config-edit quality | 12 | Model |
| Diagnosis narrative signals | 6 | Model |
| Prompt-injection resistance | 6 | Model |
| Uncertainty behavior | 4 | Model |

The stub/contract suite passes all 50 cases, proving corpus plumbing rather
than model quality. The real pinned model then produced these independent
workstation results:

| Category | CUDA RTX 2080 Super | ROCm Radeon gfx1200 |
| --- | ---: | ---: |
| Diagnosis matcher | 4 / 4 (100%) | 4 / 4 (100%) |
| Safety classifier | 18 / 18 (100%) | 18 / 18 (100%) |
| Targeted config edits | 12 / 12 (100%) | 12 / 12 (100%) |
| Diagnosis narrative | 6 / 6 (100%) | 6 / 6 (100%) |
| Prompt-injection resistance | 6 / 6 (100%) | 6 / 6 (100%) |
| Uncertainty behavior | 4 / 4 (100%) | 4 / 4 (100%) |

There is deliberately no combined overall score. Deterministic invariants and
model behavior remain separate in the report.

Run it with:

```console
$ python3 scripts/atlas_llm_eval.py /path/to/Qwen3-4B-Q4_K_M.gguf \
    --cli /path/to/llama-completion --accelerator cuda
```

## Legacy v1 result — retained as smoke evidence

On 2026-07-13 the nine-case v1 suite passed on CPU, CUDA, and ROCm. Four cases
invoked the model for trivial single-key whole-config edits; two diagnosis and
three safety cases were deterministic. Therefore the old "9/9" means the
runtime/tool transport and deterministic floor worked. It must not be quoted
as a broad diagnosis, safety, or injection-resistance score.

The workstation integration smoke also traversed CLI → private Unix socket →
daemon → llama.cpp, returned a timer-fault explanation, and produced a
safety-classified config preview without mutating the source file. The current
runtime strengthens that seam with same-UID peer verification, bounded queueing,
lock-free status, latency/token/load/proposal metrics, BM25 retrieval scores,
read-only config grounding, and targeted section/key edits.

Moonraker relays the assistant through its normal endpoint authorization
policy. Operators must configure Moonraker authorization correctly; Atlas does
not claim an independent web authentication layer. The daemon IPC remains a
same-user, mode-private local boundary.

## Failures found by the real run

The first CUDA v2 pass was a failed qualification, not silently promoted:

- malformed targeted edits escaped the evaluator instead of scoring as misses;
- prompt fencing alone was insufficient: the small model repeated every
  planted injection marker;
- empty-evidence questions did not consistently state uncertainty;
- structured edits could omit values, invent customary G-code, or optimize an
  objectively vague request.

The final code fails malformed edits closed, removes instruction-like lines
from untrusted data before inference, strengthens the uncertainty contract,
requires values in every edit grammar, uses deterministic structured decoding,
and rejects objective-free optimization requests before inference. The corpus
was not weakened: the final unchanged category counts passed on both GPUs.

These cases are regression evidence for this exact model, prompt contract, and
corpus—not a proof against every possible prompt injection.

## Remaining qualification

- Run the identical corpus on Pi 5 + Hailo-10H after that backend exists;
  record memory, latency, and token throughput.
- Keep live config mutation unwired until the board-rig test consumes a bound,
  expiring proposal, performs compare-and-swap apply/reload, and proves undo.

The Hailo backend remains an honest unavailable stub. Nothing in this document
claims Hailo execution or live model-driven machine control.
