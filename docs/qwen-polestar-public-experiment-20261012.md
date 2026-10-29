# Qwen Polestar/public-data experiment — 2026-10-12

## Authorized sequence

1. Evaluate Qwen3.8-27B-FP8 base and Sonnet 5.5 on the same existing sealed set.
2. Audit expanded /data/eval-cases inventory and identify eligible Polestar training captures.
3. Freeze Polestar training data before Polestar-only training.
4. Evaluate Polestar-only model; subsequently compare a public-data augmented variant.

## Fixed first evaluation

- Cases: the 12 exact captures in outputs/kt-v2bs94-sealed-20261008/plan.json; two repeats each, 24 per model.
- Contract: configs/eval/sealed-family-mcp.yaml.
- Harness: legacy; binary SHA256 0cf4e00c2312c2bd5fb2c87b3e4818cc8336443b6eb83aabe1fd7a4c26b154b3.
- Maximum investigation turns 40; prompt budget 40,000 tokens; tool result limit 6,000 characters; output budget 4,096 tokens.
- Qwen thinking enabled, MTP4. Sonnet exact API model and reasoning transport must be recorded; unavailable models must not be silently substituted.
- Root score from deterministic contract; root-plus-mechanism from Codex gpt-6.1-sol rubric v2, missing valid judgments only.
- Model-specific request controls and serving differences must be recorded, not represented as identical.
- This set was evaluated historically; it is a fixed held-out comparison set, not newly untouched test data.

## Data audit acceptance boundaries

- Inventory captures, canonical scenario/source identities, capture times, observation artifacts, golden answer coverage, and existing teacher trajectory provenance.
- Exclude sealed and validation source-equivalent recaptures from training, not only exact matching case IDs.
- Separate new captures from genuinely new scenario families; repeated captures are not independent scenario types.
- Do not regenerate or blanket-rejudge trusted existing training data during inventory review.
- Current Muse mixed Opus12 evaluation is not the final test for this experiment.

## Interpreting the later training comparison

Use the same frozen Polestar corpus in both training variants. A public-data continuation after Polestar measures the effect of that continuation and its extra compute; a fresh joint model changes the curriculum too. Record this choice explicitly before training. A compute-matched Polestar-only continuation is needed to isolate public-data content from additional training steps. Neither future training stage is launched by this document.


## Expanded Polestar training authorization — 2026-10-12

The user authorized preparing the 17 new captures with the existing Polestar golden/teacher-data workflow, then starting Qwen Polestar-only training. Existing 27 captures / 52 recovered episodes are reused verbatim. The extended split is `configs/teacher/polestar-expanded-20261012.yaml`: train 44, validation 6, sealed 12, excluded 1. Public datasets are excluded from this first training run.

`configs/teacher/polestar-expanded-merge-20261012.yaml` controls the merge through `scripts/mcp_prepare_polestar_expanded.py`. Source completion hashes and existing judged-readiness metadata are validated without rejudging trusted old episodes. The merge refuses missing cases, duplicate episodes, non-training cases, changed protected partitions, and incomplete source artifacts. No six-turn selection cap is applied; all source messages and supervision flags are preserved.

New golden contracts, new teacher generation, and Qwen/KT GPU 1 training preparation proceed in parallel. Training must wait for complete new data and the merged dataset hash. KT GPU 0 remains reserved for the existing Muse evaluation. Artifact preparation is not evidence that training has started; consult the run state and actual process for status.

The prior evaluation paragraph above describes the superseded first comparison. The current user correction for Opus/Sonnet is legacy, 60 turns, no soft prompt budget, 4096 output tokens; Qwen uses 32768 output tokens to address reasoning-only exhaustion. See active evaluation plan files for runtime provenance.


### Live implementation and handoff

- Authoritative new-only golden: `configs/eval/polestar-new17-20261012.yaml` (17 typed cases). Root-family provenance is retained at `outputs/polestar-new17-20261012/golden-provenance.json`; mechanism references remain the capture metadata, supplied only to grading.
- Generation: `configs/teacher/polestar-new17-generation-20261012.yaml`, `scripts/mcp_generate_polestar_new.py`, tmux `codex-actions-new17-20261012`. Two concurrent cases, target two accepted episodes each, at most five attempts. Codex `gpt-6-astra` selects structured actions through `codex://local`; the existing student loop executes the real MCP calls and records the exact observations. This uses the student **runner** as transport for the teacher model, not Qwen-generated supervision. Mechanism judge remains Codex `gpt-6.1-sol` rubric v2. Native CLI MCP discovery did not expose configured RCA servers here; that aborted cohort is retained separately and excluded.
- New accepted runs use the existing rationalization/export workflow with an explicit `runner: student` source format. Existing 52 episodes are neither rewritten nor rejudged. Target corpus: 52 existing + 34 new = 86 episodes across 44 captures, conditional on acceptance.
- Temporary server-capacity failures remain incomplete and are retried by the durable resume supervisor (`codex-new17-resume-20261012`); they never become accepted labels. Original first two capture restore collisions are preserved, with subsequent attempts reusing the existing isolated connections. Genuine unresolved failures remain visible as `needs_repair`.
- Training template: `configs/sft/qwen38-27b-polestar44-20261012.yaml`. Fresh base LoRA r16/alpha32; full eligible turns, case balancing, one epoch, lr 1e-5, reasoning/action token weight 1. Qwen default thinking is xhigh. Native end tokens are supervised. The recipe covers both full and linear attention projections plus MLP (496 matched modules); vision and MTP weights are not LoRA targets.
- Qwen uses the fine-grained FP8 format, distinct from Muse compressed-tensors. The loader verifies pinned checkpoint/tokenizer hashes and loads BF16 compute weights with `FineGrainedFP8Config(dequantize=True)`, per the [Transformers source/docs](https://github.com/huggingface/transformers/blob/main/docs/source/en/experts_interface.md). Actual KT H200 GPU1 probe passed: finite forward loss, 496 LoRA gradient tensors; a real existing 9-turn episode rendered successfully (max 30,811 tokens). This tiny backward probe does not establish full-length training throughput or memory usage.
- Training runtime is isolated at `/home/work/rca-training-20261012/venv`; inference runtime and GPU0 are unchanged. Models remain under `/home/work/models`. The current runtime uses reference PyTorch linear-attention/causal-convolution kernels; throughput has not yet been measured.
- `scripts/mcp_launch_qwen_polestar.py` binds the completed dataset hash/counts, waits for the successful runtime probe and free GPU1, then launches a detached remote trainer with W&B. The trainer fails before optimization if preprocessing would discard any turns by length. No public data is included. `outputs/qwen-polestar-20261012/state.json` is a preparation snapshot; live remote state after launch is `/home/work/rca-training-20261012/training-state.json`.
