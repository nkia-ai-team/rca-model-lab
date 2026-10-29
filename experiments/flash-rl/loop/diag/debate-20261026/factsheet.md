# Fact sheet — Flash RCA RL loop (2026-10-26)

All numbers below were measured in this project; paths are on the local machine under
/home/swlee/projects/rca-model-lab-mcp/outputs/flash-rl-loop-20261015/ (read-only access is fine: loop.py, config.json,
state.json, loop.log, diag/README-20261024.md, data-v5/README.md, eval/*/result.json).

## Task
- Root-cause analysis (RCA) agent for an observability product (Polestar). Trigger = an incident (bundle of alarms) fires;
  the model investigates a captured incident through ~23 read-only tools (rca-mcp: metrics, logs, traces, k8s events,
  DB sessions, topology, change events) for 30–60 turns, then submits root cause target(s), mechanism, evidence refs.
- Episodes: 20k–140k generated tokens, contexts up to ~90k tokens. Deployment target: Flash (Qwen3-Next-class MoE,
  INT4 W4A16, 48 layers, 512 routed experts/layer, MTP speculative decoding) on a GB10 128 GB box.
- User's stated goal: higher RCA accuracy (root + mechanism "joint"), better reasoning quality (grounded evidence, no
  fabrication, causal depth) and efficient reasoning (fewer turns/tokens), across all datasets incl. public ones.
- User principles: no few-shot / no diagnostic heuristics added to prompts ("skill comes from training, not prompt
  patching"); prompts only carry the task contract; no keyword rule-based judging of meaning; fix root causes, present
  options with trade-offs; evaluation prioritizes root+mechanism joint, compare only same-judge numbers.

## Data
- Polestar captures (testbed scenarios with injected faults, verified by subagents): train 70 / validation 10 + new13 /
  sealed 12. Golden root target ids built from each capture's own DB dump. Scenario metadata (title, cause,
  distinguishing_evidence) written by the scenario author; verification found frequent inaccuracies in descriptive fields
  (durations, log text, session states), graders read title + cause.
- Public sets converted into the same tool surface: OpenRCA 2.0 (o2ts 226 train / o2hs 118 / o2ot 21) and RCAEval
  (tt4 72 train, ob4 70 train), held-out partitions (public_compare / heldout_service) for evaluation (113 cases total).
- Training slot mix per update: until 10-26 1 Polestar + 2 public groups; from u64: 2 Polestar + 4 public.
- Rollout group statistics (4 samples per case, joint = root+mechanism correct):
  | source | joint rate | all-4-fail | all-4-pass |
  | ob4 (RCAEval) | 0.11–0.19 | 56–74% | 0% |
  | tt4 (RCAEval) | 0.07–0.36 | 27–80% | 0% |
  | OpenRCA2 | 0.27–0.38 | 47–58% | 6–24% |
  | Polestar | 0.57–0.66 | 14–20% | 27–45% |
  Flat groups are dropped (dynamic sampling) and redrawn; all-solved Polestar cases are re-rolled as masked/budget variants.

## Reward (per episode, judged by Claude Opus 5.5 CLI; GRPO group-relative advantage, group size 4, kl_beta 0)
- joint success ≥ .5: base + path/evidence terms, −.3·hallucination score, −.1 per major fabricated claim (max .3),
  +.1·causal depth (grounded failure_mode/proximate_cause/trigger links), small relative length penalty.
- partial: quality·(root .1 + mechanism .2·score); no submission −.2.
- Judge noise: running the same evaluation twice changes 1–3 of 20 validation runs; mechanism rubric v3.

## Training
- LoRA (dense rank 16 on attention/shared expert, rank 2 on every routed expert), 2-GPU layer split, prefix-sharing tree
  forward, importance-capped corrected GRPO, drift gate (truncated mass ≤1%).
- History: 42 releases with ONE optimizer step each (37 steps, 460 episodes total, lr cut 1e-4→2.5e-5 by two rollbacks later
  judged false alarms). From u51: lr 1e-4, one step per group (3 steps/update); from u64: 6 groups, one step per 2 groups.
  Cumulative optimizer steps at u62: 73.
- One update ≈ 4 h (rollout ~0.7 h, training 2.5–3.5 h); 6-group updates ≈ 8 h. GPU SM utilization during training ~28%.

## Results
- Milestone 39 (u47 vs start policy, same cases): public 113 joint 32 vs 34, sealed 11/24 vs 12/24, original12 16/24 vs
  16/24 — no difference (McNemar p≈1). Validation10 rose to 19–20/20 earlier but the start policy re-measured under the same
  rubric was already 19/20.
- u62 (36 steps after lr restore): validation10 16/20 (ref 19), public12 6/12 (ref 4), new13 22/26 (ref 22).
- Output-distribution shift policy vs base on the policy's own rollout tokens: ~0.006–0.011 nat/token at u50, u54, u62
  (|Δlogp| ≈ vLLM nondeterminism floor 0.08) although LoRA ‖ΔW‖ grew steadily. Expert-only vs dense-only LoRA ablation:
  0.0075 vs 0.0038 nat/token (both contribute).
- Fabrication: 25–50% of joint-correct answers contain a major unsupported claim; turns/tokens unchanged by training.

## Questions from the user for this debate
1. Is the training data OK?
2. If the long multi-turn credit assignment is the problem, would we have to judge every turn ("was this the right
   investigative direction?") — wouldn't that take far too long?
3. Is "raising general reasoning ability" actually our task?
