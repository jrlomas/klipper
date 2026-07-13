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

## Provenance and remaining validation

This run is labelled **workstation CPU preflight; GPU/Hailo validation
pending**. The workstation exposes AMD and NVIDIA adapters, but the available
llama.cpp build could not initialize a supported GPU runtime and the NVIDIA
driver was unavailable to `nvidia-smi`; no GPU result is claimed. The same
labelled suite must still run on the development GPU and, last, on the Pi 5 +
Hailo-10H target with memory and latency recorded.
Live config mutation/reload/undo also remains a board-rig acceptance item; the
workstation result proves drafting and the safety preview, not real-machine
application.
