# Next training model and harness decision

User request: choose the final harness and student model to train, using completed comparable evidence. This decision is a required follow-up to the active Qwen normal-evaluation goal. Decision: select Qwen3.8-27B with the legacy observation/tool contract for the next student training. Final Qwen accounting is complete: original24 root+mechanism14/24, with23 valid original submissions and one format failure. A separate one-response continuation recovers that submission and remains incorrect. This is a training allocation decision, not a production release claim.

## Decision contract

1. Reject evaluation infrastructure failures as evidence of model reasoning quality. Qwen repaired24 must finish execution and mechanism scoring before its final score is published. A training allocation decision may use a conservative fixed-denominator lower bound when it already exceeds the alternatives; format failures must stay visible.
2. Main quality metric is root+mechanism strict success. Report root-only success separately. Compare common completed case/run pairs, never different partial denominators.
3. Report median/p95 and mean scenario time, turns, output exhaustion and tool failures. Model capability includes the intended serving configuration. Do not equate unlike GPU timing with inherent model speed.
4. Harness choice uses the matched Muse legacy versus structured-v2 comparison and Opus diagnostics. The completed matched Muse comparison favors legacy; retrieval architecture alone is not grounds to adopt v2.
5. Model choice considers Muse base, repaired Qwen thinkingON, GemmaON/OFF, and existing Muse training validation. Base-only comparison does not prove final post-training ranking. Prefer lower latency only when accuracy evidence supports the tradeoff; a one-run difference in24 is weak evidence, not a decisive superiority claim.
6. Preserve current all-turn Muse joint SFT as the existing-recipe baseline. It is not structured-v2 training: its49Ksequence/40Kprompt legacy reconstruction and existing teacher trajectories do not establish read_observation skill transfer.
7. If choosing structured-v2 for training, collect teacher trajectories under the same observation/tool contract, including raw-observation requery where needed. Mere JSON reformatting cannot create missing tool actions or reasoning grounded in newly visible evidence. Keep no6turn cap; serialize with the chosen student template and verify reasoning/action loss masks.
8. Final deliverable: one recommended student+harness pair, teacher configuration, data compatibility/migration plan, rejected alternatives with reasons, and the exact evaluation evidence/limitations. Do not launch multiple expensive full-training alternatives just to defer the choice.

## Current evidence snapshot

- Opus5.5 structured-v2:24/24 finished; root22/24, strict19/24, no pendingmechanism judgments (2026-10-08).
- Historical Opuslegacy:root22/24,strict20/24; differentrunner/judge provenance means v2 is not proven superior by this comparison.
- Gemma4 26B-A4B FP8 OFF:root5/24,strict3/24; ON:root6/24,strict2/24. Speed advantage alone does not justify selecting it as primary RCA student.
- Muse matched A/B completed24 each: legacy root9/24, strict6/24; structured-v2 root6/24, strict3/24. No pending mechanism judgments. See completed paired comparison below.
- Repaired Qwen thinkingON, completed 2026-10-09 01:42 KST: original24 root14/24, root+mechanism14/24 (58.3%), all14 hits judged by GPT-6.1-sol; no pending or invalid judgments. Original23 submitted plus1 forced submission schema failure. One separately preserved submission-only continuation yields a valid but incorrect answer, so recovered24 also scores14/24. Original all-submitted audit remains failed; continued-evaluation audit passes. This is not a fresh uniform24-run evaluation of the new retry policy.
- Current Muse jointSFT ongoing; validationbase/adapter comparison remains outstanding.

## Completed Muse harness comparison (verified 2026-10-08 23:01 KST)

Both harnesses have finished the identical24 case/run pairs; all applicable GPT-6.1-sol mechanism judgments are complete.

| Harness | Root exact | Root+mechanism | Mean seconds | Median seconds | P95 seconds* | Mean turns |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| legacy | 9/24 | 6/24 | 783.3 | 796.3 | 1009.4 | 43.5 |
| structured-v2 | 6/24 | 3/24 | 1014.0 | 971.6 | 1439.6 | 52.2 |

*Nearest-rank percentile. Durations include unsuccessful terminations: legacy has19 submitted and5 forced_answer_rejected; v2 has21 submitted and3 forced_answer_rejected. Both denominators include all24 attempts. V2 took29.5% longer on average and did not improve strict accuracy.

Harness decision for the next training recipe: retain the evaluated legacy contract. Its result is better in this benchmark, with lower latency; this is not a statistical claim of universal superiority. V2 remains a research candidate requiring separate evidence before adoption. If Qwen is selected, its measured harness is also legacy; no measured Qwen-v2 result exists. The next training student is Qwen; completed evaluation and any submission-recovery results must remain separately traceable.

Machine-readable paired evidence: `outputs/muse-harness-paired-decision-20261008.json`; each row records case/run identity, root score, mechanism verdict, duration, turns, and termination reason.

## Evidence locations

- outputs/mcp-eval/opus55-structured-v2-opus12-20261008
- outputs/mcp-eval/muse-assistant-structured-v2-ab-opus12-20261008
- outputs/mcp-eval/muse-assistant-legacy-ab-opus12-20261008
- outputs/mcp-eval/qwen38-thinking-repaired-opus12-20261008
- outputs/joint-baseline-all-20261008
- docs/joint-baseline-all-20261008.md

## Selected training recipe

Select **Qwen3.8-27B + legacy**, thinking ON, native tool calling and per-episode target aliases. The evaluated FP8 checkpoint is pinned above in the evaluation plan; initialize the training base from the matching full-precision lineage after verifying model/tokenizer/template compatibility. Do not silently substitute a different 27B model or assume an FP8 inference checkpoint is directly suitable for the existing Muse trainer.

The completed result of14 confirmed passes out of the fixed24 attempts exceeds Muse legacy 6/24 and Gemma ON 2/24. These are observed benchmark outcomes, not statistical proof or a guarantee of post-training superiority. Qwen used a larger output allowance (32768 versus Muse 4096) and different hardware; this is a choice of the measured model-serving package, not an isolated causal model comparison. Qwen latency must be reported before production acceptance. Across all24 original attempts, its mean is1593.3 seconds (26.6 minutes), median1677.5 seconds, nearest-rank p952177.0 seconds and mean9.83 turns. This is substantially slower than Muse legacy in the observed configurations despite fewer turns; the Qwen choice prioritizes RCA learning quality, not current interactive latency. Long-reasoning supervision at weight1.0 does not by itself train shorter reasoning; any later latency recipe needs a separate quality-controlled comparison.

Retain legacy observation handling (6000-character tool views, historical response compression and 40000-token soft prompt boundary) for the first training/evaluation comparison. Do not claim this eliminates loss of raw evidence. Structured-v2 is deferred because matched Muse evaluation reduced strict success and increased runtime. Bounded submission schema-feedback repair is a separate execution-contract revision: preserve historical scores, record retry counts, and validate it separately before calling its results comparable to legacy without repair.

Teacher: Opus5.5 with the same selected tool schemas, aliasing and observation contract. Reuse existing compatible trajectories and their valid judgments. Generate additional trajectories only where schema/observation differences require actions that existing data do not contain; JSON reformatting alone does not teach those actions. Mechanism judging remains GPT-6.1-sol. Do not resume blanket per-turn LLM admission.

Student serialization must use Qwen's own thinking and tool-call template. Supervise each eligible assistant turn's reasoning and action tokens at weight 1.0; mask system, user, tool observations and historical assistant tokens. Retain all eligible turns, with no six-turn cap. Keep the current case-balanced objective for the initial comparison. Record token-fit exclusions using the Qwen tokenizer rather than importing Muse token lengths. Retain known supervision flags.

Start with the existing conservative adapter recipe (rank16/alpha32, LR1e-5, one epoch) as an initial setting, subject to actual Qwen module names, precision and gradient-flow validation. Match training context reconstruction to the selected runtime. Sequence length must accommodate the supervised completion; do not copy Muse's 49152 limit without measuring Qwen lengths. This document chooses the next training target; it does not claim a Qwen-compatible trainer/config has already been implemented or launched.

Preserve the currently running Muse all-turn SFT and its planned base/adapter validation as the existing-recipe baseline. Do not interrupt it merely because Qwen is selected for the next run. Model-development success requires a Qwen base-versus-adapter comparison under the same harness, thinking, output budget, sampling and held-out validation configuration, with root+mechanism, submission validity and latency reported. Training loss alone does not meet that criterion.


## Final completion evidence

- `outputs/qwen38-thinking-repaired-opus12-20261008/completion-audit.json`: original24,23 submitted,1 model format failure; root+mechanism14/24, pending0. The original controller exits1 on that all-submitted gate; this record is preserved. All12 case processes exited0.
- `outputs/qwen38-thinking-repaired-opus12-20261008/resumed-completion-audit.json`: all24 continued submissions valid, one recovery, no missing records or audit issues, same14/24.
- `outputs/mcp-eval/qwen38-submission-resumed-opus12-20261009/cohort-provenance.json`: explicit source for every case/run, not a fresh uniform protocol run.
- Original output-limit hits0; all response reservations32768; no context/API errors in controller case logs. Runtime repair and immutable continuation tests:42 passed; Ruff passed.
