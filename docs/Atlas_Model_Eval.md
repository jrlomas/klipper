# Atlas Pinned-Model Evaluation

Status: **Workstation CPU preflight and CUDA/ROCm GPU smoke tests passed on
2026-07-13; Pi 5 + Hailo-10H validation remains pending.** This is evidence
for the local model path, not a claim that the deploy hardware is validated.

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
    --cli /path/to/llama-completion --accelerator cuda
```

| Metric | Result |
| --- | ---: |
| CUDA structured config-edit correctness | 4 / 4 (100%) |
| ROCm structured config-edit correctness | 4 / 4 (100%) |
| Deterministic diagnosis | 2 / 2 (100%) |
| Deterministic safety classification | 3 / 3 (100%) |
| CUDA overall | 9 / 9 (100%) |
| ROCm overall | 9 / 9 (100%) |

The separate real-model smoke decoded and interpreted a timer fault, proposed
`max_accel: 2000` through `propose_config_edit`, and sent the resulting diff
through the deterministic apply gate. It was classified `CONSEQUENTIAL` and
applied with the reversible path, as designed.

On the host-visible CUDA and ROCm sessions, the same versioned nine-case
harness passed `9 / 9` on both adapters. Each run includes the four
real-model, grammar-constrained `propose_config_edit` cases; the two diagnosis
and three safety cases are deterministic invariants. Both runs are labelled
**authored on GPU; Hailo validation pending**.

## Always-on assistant integration

The production-shaped workstation seam was also exercised on 2026-07-13:

1. `atlas serve` started with the pinned GGUF, explicit
   `llama-completion`, a temporary `klippy.log`, read-only `printer.cfg`, and
   a mode-private assistant Unix socket.
2. `atlas assistant ask` traversed CLI → socket → daemon → llama.cpp and
   returned a grounded explanation of the observed `Timer too close` shutdown
   in about 25 seconds on CPU.
3. `atlas assistant propose` requested only
   `extruder.max_temp: 280 → 270`. Qwen returned exactly that one-key edit in
   about 29 seconds; the non-LLM classifier labelled it `SAFETY`, selected
   `confirm`, issued an expiring proposal token, and reported `applied: false`.
4. The original config SHA-256 remained
   `91728f4c8ca605d6fd2461e2a5eb93c3e3013ef62b1187e886a9bfd500f34ff8`,
   proving the workstation preview did not mutate it.

The same API is wired through authenticated Moonraker endpoints and the
Mainsail companion panel. Mainsail's 46 unit tests, lint, formatting check,
production build, and distribution zip passed with the assistant UI. The
panel keeps conversation locally, sends at most eight prior messages, shows
the deterministic risk result, and offers no apply control at this stage.
The daemon memory seam was separately staged: it created a `0600` atomic
`memory.json`, recorded the observed diagnosis once, mirrored the monitor
baseline, and made both available to deterministic RAG retrieval.

## Provenance and remaining validation

This run is labelled **workstation CPU preflight; Hailo validation pending**.
The workstation compiler, runtime, and short real-model inference path were
proved independently:

- CUDA 12.0 built and linked `llama-completion` for the RTX 2080's explicit
  `sm_75` target, including CUDA runtime and cuBLAS/cuBLASLt.
- ROCm 7.2.4 built and linked the same target for the AMD card's explicit
  `gfx1200` target, including HIP, hipBLAS/rocBLAS, and the HSA runtime.
- A host-visible run found `/dev/nvidia0`, `/dev/kfd`, and the DRM render
  nodes. The NVIDIA runtime identified the RTX 2080 Super (8 GB, `sm_75`);
  ROCm identified the Radeon `gfx1200` (16 GB).
- The pinned Qwen3-4B Q4_K_M model offloaded all 37 layers on each adapter.
  A 1,024-token-context, 23-token decode measured 104.85 tok/s on CUDA and
  73.14 tok/s on ROCm. These are smoke-test measurements, not a benchmark.

The earlier no-device result came from the restricted tool execution context,
which hides GPU character devices; it was not a driver or toolchain fault.
The labelled suite is now recorded on both workstation adapters. It must run
last on the Pi 5 + Hailo-10H target with memory and latency recorded. Live
config mutation/reload/undo also remains a board-rig acceptance item; the
workstation result proves drafting and the safety preview, not real-machine
application.
