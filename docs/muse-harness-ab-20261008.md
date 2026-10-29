# Muse base harness comparison — 2026-10-08

## Question
Does the corrected structured-v2 harness improve RCA performance over legacy on the same Muse base model?

## Fixed conditions
- 104 L40S GPUs 0 and 2; identical vLLM image and model/assistant revisions in serving command manifests.
- RedHatAI Muse-Glimmer-30B FP8-block base, native high reasoning, Muse assistant DFlash 15-token greedy, guided tool calls.
- Native context 131072, BF16 KV cache, max-num-seqs 2, output limit 4096.
- Same Opus comparison cases: 12 cases × 2 runs per arm (48 total); concurrency 2 per arm; max 60 turns; initial tool view budget 6000 characters.
- Root target scoring and cached GPT-6.1-sol mechanism judge rubric v2; completed results judged incrementally.

## Arms
| GPU | Harness | Soft prompt budget | Output directory |
|---|---|---|---|
| 0 | structured-v2 | disabled (0) | outputs/muse-assistant-structured-v2-ab-opus12-20261008 |
| 2 | legacy | 40000 | outputs/muse-assistant-legacy-ab-opus12-20261008 |

This compares complete harness bundles, including initial observation presentation, history retention and soft-stop policy. It does not isolate one component. Prior Muse 24-run results mixed assistant ON/OFF and are not the matched baseline.

## Execution and evidence
- Controllers verify frozen source hashes and real guided tool-call smoke success before evaluation.
- Source snapshots are stored under each arm's source-snapshot directory.
- Serving commands, logs and smoke evidence: outputs/muse-harness-ab-20261008/serving/.
- Controllers: thinkfl-muse-ab-structured-v2-20261008.service and thinkfl-muse-ab-legacy-20261008.service.
- Incremental judge: thinkfl-muse-ab-live-mechanism-20261008.service.
- Aggregate trajectories are linked under outputs/mcp-eval/ and discoverable in the replay viewer.
- Report root accuracy, mechanism success, elapsed time, turns and context/API failures. Preserve failures; do not silently truncate structured-v2 evidence or treat missing runs as completed scores.
- Two requests can run concurrently at shorter contexts; the shared KV pool cannot hold two simultaneous full-length 131072-token requests. Compare measured serving throughput and preemption alongside latency.

Evaluation is in progress; no improvement claim is established yet. Use state.json and actual score files for current progress.
