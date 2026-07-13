# Atlas Pinned-Model Evaluation

Status: **Workstation CPU preflight passed on 2026-07-13; GPU authorship and
Pi 5 + Hailo-10H validation remain pending.** This is evidence for the local
model path, not a claim that the deploy hardware is validated.

## Artifact and budget

- Model: official
  [`Qwen/Qwen3-4B-GGUF`](https://huggingface.co/Qwen/Qwen3-4B-GGUF),
  `Qwen3-4B-Q4_K_M.gguf`
- File size: 2,497,280,256 bytes
- SHA-256: `7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5`
- Runtime: local llama.cpp `llama-completion`, CPU transport, offline mode
- Deploy-profile estimate: 2,836 MB; ceiling: 6,144 MB — **PASS**

The hash and size matched the model publisher's LFS metadata before the run.
The backend kept prompts in mode-private temporary files, used Qwen's native
chat framing with bounded non-thinking mode, and constrained tool output with
a JSON schema.

## Results

Command:

```console
$ python3 scripts/atlas_llm_eval.py /path/to/Qwen3-4B-Q4_K_M.gguf \
    --cli /path/to/llama-completion
```

| Metric | Result |
| --- | ---: |
| Structured config-edit correctness | 4 / 4 (100%) |
| Deterministic diagnosis | 2 / 2 (100%) |
| Deterministic safety classification | 3 / 3 (100%) |
| Overall | 9 / 9 (100%) |

The separate real-model smoke decoded and interpreted a timer fault, proposed
`max_accel: 2000` through `propose_config_edit`, and sent the resulting diff
through the deterministic apply gate. It was classified `CONSEQUENTIAL` and
applied with the reversible path, as designed.

## Provenance and remaining validation

This run is labelled **workstation CPU preflight; GPU/Hailo validation
pending**. The workstation exposes AMD and NVIDIA adapters, but the available
llama.cpp build could not initialize a supported GPU runtime and the NVIDIA
driver was unavailable to `nvidia-smi`; no GPU result is claimed. The same
labelled suite must still run on the development GPU and, last, on the Pi 5 +
Hailo-10H target with memory and latency recorded.
