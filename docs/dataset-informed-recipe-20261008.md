> Superseded for execution: user authorized the existing-objective, all-turn baseline in [joint-baseline-all-20261008.md](joint-baseline-all-20261008.md). Blanket admission remains stopped; action-only loss and equal-source sampling are deferred.

> USER STOP: blanket per-turn LLM rejudging and its automatic training continuation are stopped. Service disabled. Do not resume this pipeline. Reuse valid existing judgments; scope any further checks to demonstrated data-transformation defects. The no-turn-cap instruction remains in force.

> Current directive: no per-episode turn cap. Active execution is `admitted-recipe-20261008-all-turns`; earlier capped launches below are historical.

# Dataset-informed RCA recipe, 2026-10-08

## Status and reason for replacement

The first joint run reused the existing recipe before profiling the four datasets.
It was deliberately stopped before an observed optimizer step. Its data, configuration,
and logs remain under `outputs/joint-training-20261008`; it must not be resumed as the
new recipe. GPU0 serving was left untouched.

All 664 recovered episodes / 374 cases were structurally profiled. TT4 contributes
126 episodes / 70 cases; OB4 125 / 69; OpenRCA2 361 / 208; Polestar 52 / 27.
Case weighting alone gives OpenRCA2 about 56% and Polestar about 7% of nominal mass.
This is not source balancing.

## Findings that change the recipe

The old six-turn selection retains 31–41% of eligible completions. Tool-first and
pre-submit retention are structural supervision metrics, not semantic evidence quality.
Unselected completions do **not** remove preceding observations from a selected turn's
runtime prefix. Policies at caps 6, 8, and 12 were compared in
`outputs/recipe-audit-20261008/profile.json`.

Nineteen previously rejected/held targets were exactly mapped into the joint data.
Thirteen were still supervised; remote exact tokenization confirmed all thirteen
survived the old length filter and sample6 selection. A further 32-turn GPT-6.1 Sol
audit yielded 18 approvals, 10 rejections, and 4 holds. In the distinct-case stratified
sample, actual final submissions received 0 approvals, 6 rejections, and 2 holds.
These diagnostic samples are not estimates of whole-dataset error rates.

The merged quarantine maps 33 targets: 27 newly disabled and 6 already disabled.
It changes only supervision flags and preserves original message order and context.
It is a candidate dataset, **not** an approved training dataset.

The previous rationalization judge sees a reasoning suffix and a tool call, without
the prior observations. Its approval cannot establish observation support. Historical
Claude mechanism caches also remain historical; they were not replaced by Sol
rationalization scores. New evidence judging uses Codex / GPT-6.1 Sol exclusively.

## First controlled recipe

| Component | Choice and rationale |
| --- | --- |
| Base | Existing pinned Muse Glimmer 30B FP8-block checkpoint and tokenizer; fresh adapter |
| Precision | Dequantized BF16 compute, FP32 adapters/optimizer, existing input-gradient guard |
| Mixture | One draw from each of TT4, OB4, OpenRCA2, Polestar per four-example accumulation group; cycle cases within source; unit loss weights |
| Targets | All eligible completions per episode; `turns_per_episode: 0`. No fixed-count turn sampling |
| Admission | Audit every candidate target against visible prior evidence; only exact approved targets receive loss; no replacement from unassessed turns |
| Loss | Action-first control, reasoning weight 0; actions still require admission |
| Context | Preserve original normalized rolling prefix; max prompt 40,000 tokens, full example 49,152 tokens |
| Optimizer | Existing LR 1e-5, cosine, warmup 5%, clip 1, batch 1, accumulation 4 |
| Adapter | Existing LoRA rank16, alpha32, dropout0.05, attention and MLP projections |
| Budget | One sampler epoch after actual token-length filtering; no minimum-50-step repetition of a small admitted subset |

This is a conservative control, not an optimal ratio or causal ablation claim. The user
explicitly excludes a per-episode turn cap; cap6/cap8 are not future experiment candidates.
Sampler epochs complete every case turn cycle at least once across all sources. Smaller
sources/cases may repeat to preserve balance; manifests report unique and repeated draws.

## Execution and acceptance

1. Freeze source mapping, candidate identities and exact evidence hashes.
2. Reuse only matching model/rubric/item verdicts; judge missing candidates with Sol.
3. Preserve pending states on transport/schema/quota failures. Holds/rejections are
   resolved negative decisions, never approvals.
4. Export approved-only supervision, verify all supervised target bindings, source
   coverage and train-split isolation before syncing to GPU1.
5. Write actual token-length drops, selected indices and source sampling mass before
   loading the base model; run the existing gradient-flow guard before optimization.
6. Train and compare fresh base/adapter on the existing non-sealed validation plan.
   Training loss alone is not RCA model-development success.

Full-dataset token lengths and final model quality are not yet established by the
structural profile or the 32-turn audit. Execution status belongs in the durable
controller state, not in a premature success claim here.

## Evidence

- `outputs/recipe-audit-20261008/profile-summary.md`
- `outputs/recipe-audit-20261008/profile-quarantine-selection.json`
- `outputs/recipe-audit-20261008/evidence-notes.md`
- `outputs/recipe-audit-20261008/evidence-review-decisions.json`
- `outputs/recipe-audit-20261008/quarantined-merged/manifest.json`
- `configs/sft/muse-glimmer-30b-mcp-admitted-template.yaml`

Earlier gradient failures and data/harness limitations remain documented in
`docs/history-lessons-20261007.md` and `docs/base-model-contract-20261007.md`.

## Live launch record

On 2026-10-08, `thinkfl-admitted-recipe.service` was started with four Sol judge
workers. Candidate preparation produced 3,981 targets (TT4 756, OB4 750,
OpenRCA2 2,166, Polestar 309), including 540 candidate final submissions.
The initial candidate version had sixteen exact reusable prior verdicts. Final review
found export-only supervision flags in some judge context messages; that version was
superseded before training. The v2 evidence builder removes those flags. Only verdicts
whose cleaned item/model/rubric hashes remain identical may be reused. Existing 33
rejected/held targets remain quarantined; no new positive admission is inferred from them.

The durable controller is configured in
`configs/sft/admitted-recipe-controller-20261008-v2.json`. Its state is
`outputs/admitted-recipe-20261008-v2/controller/state.json`; live admission progress is
`outputs/admitted-recipe-20261008-v2/judged/admission.json`. Pending requests cause a
one-hour retry, reusing valid caches. Export requires at least one approved final
submission per source in addition to nonempty source supervision. This minimum is
an execution guard, not evidence of adequate final-answer coverage or model quality.
After admission/export validation, the controller runs
`configs/sft/admitted-joint-20261008-v2.yaml` on GPU1 and its configured held-out comparison.

The actual training environment uses Transformers 5.15.1; the sampler override was
checked against its optional `train_dataset` argument. Local testing alone with an
older system Transformers would not establish remote compatibility.

V2 launch verification: all 3,981 candidate items contain no export supervision keys
in judge context/target message fields; 101 exact prior verdicts remain reusable.
Full Python suite: 310 passed, 1 skipped (local torch dependency unavailable). Changed
Python files pass Ruff. The persistent service was restarted on the v2 controller.

## All-turn execution supersedes capped runs

The user's explicit instruction removes the six-turn cap from candidate preparation,
export, continuation and training. All 12,104 currently eligible turns are candidates.
Known rejected/held targets remain excluded; quality and token-fit checks remain.
No candidate is omitted merely because its episode has more than six turns.
The balanced sampler epoch also covers every retained training example at least once.

Active controller: `configs/sft/admitted-recipe-controller-20261008-all-turns.json`.
Active state: `outputs/admitted-recipe-20261008-all-turns/controller/state.json`.
Prior exact evidence caches are imported from the superseded v2 judge workspace.
