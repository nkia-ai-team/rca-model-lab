# Flash on-policy RCA rollout pilot

This folder owns no shared harness changes. `run.py` intercepts the existing
legacy `_post_chat` boundary while using the same replay restore, blinded seed,
MCP tools, aliasing, context truncation, submission and teardown. Each group gets
a unique Docker project; fresh output path is mandatory. Database restored once
per case group; MCP operations are reads and each episode uses a fresh MCP process.
Never run concurrently with mutable training weights; root orchestrator must
freeze and identify one exact policy for the complete group.

## Runnable pilot (ONLY after GPU0 serving restart is authorized)

From repository root:

```bash
.venv/bin/python outputs/flash-next-rl-pilot-20261014/rollout/run.py \
  --case case-f01-h-v3-48ec8e62 \
  --policy-version base-4c67bf686b7f7fd386bae6b07ab59e8ff1d5b897 \
  --group-id flash-grpo-base-g0001 --group-size 4 \
  --out outputs/flash-next-rl-pilot-20261014/rollout/groups/g0001
.venv/bin/python outputs/flash-next-rl-pilot-20261014/rollout/finalize.py \
  outputs/flash-next-rl-pilot-20261014/rollout/groups/g0001
```

Endpoint defaults to local KT tunnel http://127.0.0.1:18230/v1. Server must use
matching INT4 base and exact tokenizer/template. Finalizer uses GPT-6.1-sol Codex
mechanism judging with existing rubric v2; judge errors withhold whole group.
The selected expanded golden contract covers all 43 train cases; old v2 contract
missed 16. Validation6 and sealed12 are blocked by explicit train allowlist.

## Sampling and token probability contract

- Sample temperature1, top_p1, top_k-1, min_p0 and no repetition/frequency penalties.
  This is deliberately different from evaluation top_p.95/top_k64, to make raw
  model probabilities equal behavior probabilities for ordinary unconstrained tokens.
- vLLM returns exact prompt_token_ids and completion token_ids plus sampled-token
  logprobs. Full request/response saved. Reasoning/tool/stop token coverage checked
  against usage; never reconstruct IDs by tokenizing parsed messages.
- Actual GPU0 capability probe (2026-10-14) returned all 8 reasoning-token IDs and
  probabilities, 56 exact prompt IDs; `capability-probe.json` is raw evidence.
- Forced submit tool_choice is rewritten to auto for training sampling only;
  final-submit instruction and execution validation remain unchanged. Original
  choice is recorded. Finalizer rejects any accidentally constrained request as
  a backstop. No hard-case turns or max-turn trajectories are silently removed.
- Trainer must independently check vLLM/local old-policy logprob parity, compute
  frozen-reference logprobs and use exact sampled tokens (not teacher histories).
- `policy_version` is caller supplied immutable identity; current recorder cannot
  prove a serving adapter was not hot-swapped. Orchestrator must pin server weights.

## Rewards

Default follows the approved formula: joint success `1-.2*C`, valid partial
`.2*q+.1*q*m` with `q=recall*precision²`, mechanism `m∈{0,.5,1}`,
and known model-attributable missing submission `-.2`.
`C=.7*N/(N+Nref)+.3*T/(T+Tref)`. Frozen independent train-only successful base
medians come from `../reference-costs.json` (source hashes recorded by root).
`--efficiency-coefficient 0` selects the accuracy-only control while retaining
partial/failure reward definitions. No arbitrary normalization constants.

Every valid submission, including partial/wrong-root cases, receives actual
GPT-6.1-sol mechanism grading. Unknown stop reasons, transport/infrastructure
errors and unscored outcomes withhold the entire group. Known model failure
statuses are no_tool_call, max_turns, output_budget_exhausted and
forced_answer_rejected; these have no candidate mechanism and receive a
separate not_applicable_model_failure marker. No imputed mechanism credit.

The pure local reward formula is cross-tested against authoritative
`rca_lab.train.grpo.trajectory_rewards` across 48 outcome/cost/weight combinations.
Zero-variance groups have no relative policy learning signal; trainer should
record and skip zero-advantage policy updates instead of claiming improvement.

## Public data finding

`dataset-audit.json`: 445 OpenRCA2 complete replay captures with 445 golden target
contracts and metadata mechanisms. RCAEval has 239 case captures (not 240: extra
non-case directory), all with replay payload and metadata cause; 233 have golden
contracts, six are missing. Therefore these are usable replay environments, not
merely static SFT episodes. File presence is not proof every restore/tool works.

OpenRCA2 includes traces/logs/container metrics, with explicit missing sources
(database/changes/process/host/SNMP etc). RCAEval also lacks traces/Kubernetes.
Its mechanism labels often derive from injected fault type; those labels must be
observable from supplied telemetry before mechanism RL. Do not train rewards that
require unavailable evidence. Metadata cause is judge-only and must never enter
student prompt/tools. Existing runner sanitizes observed alarm seed.

Recommendation: start Polestar43 pilot for attribution and product relevance;
after it runs, add selected OpenRCA2 diversity with per-source sampling bounds and
mechanism observability checks, compare validation against the Polestar-only arm.
Do not auto-mix all public cases into first RL run or use sealed tests for mixing.

Validation: 3 reward/parity tests and 8 token/sampling-contract tests passed against actual vLLM probe; compilation
passed. No long RCA rollout or optimizer was launched by this component.

## Training-only forced submission decoding

The recorder now sends `tool_choice=auto` even when the harness asks the server to
force `submit_rca`. The original choice is preserved in each sample. The final
submit instruction, 60 investigation turns, rejection of non-submit actions in
forced-answer mode, result schema validation, and bounded repair behavior remain
unchanged. This removes constrained decoding only for RL collection, making the
behavior distribution match raw model probabilities. A wrong final action is a
model failure (-.2), and all its tokens remain in the trajectory. Difficult or
max-turn cases are not discarded merely because final submission was requested.
This differs from evaluation's server-forced tool grammar and is explicit in the
manifest. Unexpected other constrained decoding still fails closed. The old
finalizer constrained-request check remains as a backstop, not a rollout filter.
