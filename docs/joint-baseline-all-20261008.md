# Existing-recipe joint SFT, all turns — 2026-10-08

User-approved question: does training on recovered/expanded data improve held-out
RCA performance over the fresh base? Blanket LLM rejudging remains stopped.

## Fixed data and objective

- Reuse immutable `outputs/joint-recovered-20261008/datasets/joint.jsonl` and its
  original source manifests, complete marker and disjoint split.
- 664 episodes, 374 cases, 12,131 supervised assistant turns before token filtering.
  The existing 746 unsupervised assistant turns remain excluded from loss, in context.
- Do not apply the new blanket evidence-judge decisions as an additional filter.
  The dataset retains its existing review provenance; this is not a claim of newly
  established observation-grounded approval for every turn.
- No per-episode turn cap. Existing per-turn runtime prefix reconstruction remains.
- Restore reasoning loss weight 1, case-balanced loss, legacy sampling. No new
  equal-source sampler or action-only loss experiment in this run.
- Fresh pinned base; existing v1q LoRA rank16/alpha32, attention+MLP targets,
  LR1e-5, BF16 compute, FP32 adapter/optimizer and gradient-flow guard.
- One complete epoch after token filtering (`max_steps: -1`). No fixed step cutoff.
- Full example limit49,152 and prompt limit40,000 tokens to fit serving. These are
  token-fit exclusions, not turn-count sampling; preprocessing records actual drops.

## Execution

Config: `configs/sft/joint-baseline-all-20261008.yaml`.
Template: `configs/sft/muse-glimmer-30b-mcp-joint-baseline-all-template.yaml`.
Service: `thinkfl-joint-baseline-all.service`.
State/logs: `outputs/joint-baseline-all-20261008/`.
GPU: KT GPU1; GPU0 serving untouched.

Local preflight verified source hashes, episode/case counts, train membership and
disjoint split partitions. Rendered trainer config passed Pydantic validation and
explicit assertions for no cap, full epoch, original loss and fresh initialization.
Remote preflight found GPU1 free and recorded launcher SHA before sync.

The existing continuation automatically verifies synced hashes/imports, starts
training, waits for final adapter, then compares fresh base and adapter on the same
36 non-sealed validation rollouts each. New mechanism judging uses GPT-6.1 Sol.
Training and evaluation completion must be established from logs/artifacts; launch
alone is not a model-quality result.
