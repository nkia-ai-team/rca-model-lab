# Qwen thinking evaluation repair

Goal: complete normal evaluation on the fixed Opus12 scenario set, two repeats each, then GPT-6.1-sol mechanism grading. Accuracy is measured, not an acceptance threshold.

Prior broken run: outputs/qwen38-fp8-thinking-opus12-20261008 (preserved; controller stopped).
New run: outputs/qwen38-thinking-repaired-opus12-20261008.

Observed failures: 45056 server context versus 40961 input +4096 output reservation; reasoning-only responses exhausting4096 output; incomplete submit_rca arguments. The old proxy also imposed600s upstream timeout.

Repair: context131072 within model native262144; output32768; request timeout3600; bypass short-timeout thinking proxy, use server with thinkingON verified. Legacy observation policy,40000 soft threshold,60turns,same model revision/MTP1/native tools/sampling retained. Fixed generous output budget, no answer-quality resampling. Explicit length-exhaustion telemetry rather than calling truncation ordinary no_tool_call.

Gate: 24 unique result records, all valid submit_rca terminations, no context/API capacity or output exhaustion failures, zero unresolved mechanism hits. Preserve abnormal attempts separately; do not mix protocol revisions to claim24 normal completions.

Source: https://huggingface.co/Qwen/Qwen3.8-27B-FP8/blob/main/README.md confirms native262144 context and default thinking xhigh. Full model results are not known yet.

## Launch evidence

2026-10-08 19:46 KST: both actual submit_rca schema smokes passed with default thinking enabled and MTP1. /models confirms131072; serving reports1,172,071 KV token capacity. Smoke output165tokens including92reasoning tokens; reservation32768 accepted.

32 targeted runtime tests passed. Controller thinkfl-qwen38-thinking-repaired-opus12.service and incremental judge thinkfl-live-mechanism-qwen-thinking-repaired.service started. Goal remains active until24 validated submissions and mechanism grading complete.

## Real evaluation evidence

First F01-H run, turn8: input36594, completion6044 (reasoning5412), finish_reason=tool_calls, output_budget_exhausted=false. This exceeds the former4096 cap and demonstrates a real formerly truncated-length response reaching a valid tool-call boundary. Full24 completion remains unproven.

## Submission format recovery, 2026-10-09

F20-R run1 reached the forced final-answer boundary but supplied `causes` as a JSON string, not an array. The original run remains `forced_answer_rejected` with root/strict zero. This is a model submission-schema failure after a functioning inference request, not a context HTTP failure or output-token exhaustion.

Added opt-in `--forced-submit-validation-retries 1` and a submit-only replay helper. The helper cannot query MCP/databases or receive a golden answer. It preserves the original trajectory and aliases, passes validation feedback back to the model, and caps retries. Default retry budget remains zero. Forced-submit phase now blocks investigation tools before execution.

Recovery artifact: `outputs/mcp-eval/qwen38-submit-recovery-opus12-20261009/case-f20-r-v3-caf04820/run1`. One additional model response (148.2 seconds) produced a valid submission, but its root score is still zero. Original source hashes all remain unchanged. This is a separate format-recovery experiment, not a replacement score or a claim that the original run submitted normally.

42 targeted tests passed, including immutable source artifacts, inference-before-reference-loading, alias restoration, bounded retry, and blocked investigation. Ruff passed. The final report must distinguish completed benchmark attempts, valid original submissions, and recovered submissions; a model schema error remains a benchmark failure even when the recovery implementation works.

## Final result, 2026-10-09 01:42 KST

The fixed12 scenarios ×2 original attempts completed: root14/24 and root+mechanism14/24 (58.3%). All14 root hits have valid GPT-6.1-sol v2 judgments; pending0. Output exhaustion0; no context/API failure in the completed case logs. Mean1593.3 seconds, median1677.5 seconds, p952177.0 seconds, mean9.83 turns.

Original completion audit correctly remains false because it requires every original run to submit:23 did,1 failed schema validation. The controller's exit1 records that single failure; all12 case subprocesses exited0. This state was not overwritten or reset to claim a clean original run.

The separate continued cohort reuses23 original valid submissions and the bounded submission-only continuation of the failed run. Its audit verifies24 valid submissions, unchanged original hashes and trajectory prefix, no new investigation calls, no missing cases, and no pending mechanism judgments. It passes with the same14/24 score. **This completes the24-case/run evaluation with one explicitly recovered submission; it is not evidence of24 fresh rollouts under the revised retry policy.**

Evidence: `completion-audit.json`, `resumed-completion-audit.json`, and `final-statistics.json` in the work directory; member-level continuation provenance is in `outputs/mcp-eval/qwen38-submission-resumed-opus12-20261009/cohort-provenance.json`.

## Sealed-run configuration regression, 2026-10-12

`outputs/sealed-qwen-20261012` reintroduced the old 4,096 output cap and 45,056 server context while preparing an equal-budget Sonnet comparison. First sealed F04-H run terminated `no_tool_call` after 1,472.3 seconds, with four output-limit hits, last prompt 40,363 tokens, and a reasoning-only final trajectory message; no result.json was produced. This run did not preserve the previously validated generous-output configuration.

User authorized restoring the Qwen fix and running the sealed evaluation. Preserve original attempts and use a separate corrected protocol with 32,768 output capacity, sufficient server context, and the historical 3,600-second request timeout. The user subsequently corrected the common investigation protocol to sealed12x2, legacy, 60 investigation turns and no soft prompt threshold; retain server hard-context reservation checks. This corrected Qwen output budget differs from Sonnet/Opus 4,096 and must be disclosed. Do not choose/retry results based on answer accuracy. Preflight must assert effective context capacity and perform a real thinking+tool-call smoke before rollout.
