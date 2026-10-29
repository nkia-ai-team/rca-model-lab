# Reconstruction of KT runtime `general-update-fast16-20261021` (+ `lora-export-20261015/export_vllm_lora.py`)

Remote original: `/home/work/flash-rl-quant-probe-20261014/general-update-fast16-20261021` (lost when the KT session was recreated).
Built on 2026-10-27, locally only. Nothing on any remote machine was touched, and nothing under `outputs/flash-rl-loop-20261015/` was modified.

- Transcript = `~/.claude/projects/-home-swlee-projects/47c4a666-edf6-440b-93bc-c522a28efa1a.jsonl`. "ln" means the JSONL line of the tool_use call. Times are the transcript UTC timestamps (KST = UTC+9).
- Old scratchpad `OLDSP` = `/tmp/claude-1006/-home-swlee-projects/47c4a666-edf6-440b-93bc-c522a28efa1a/scratchpad`.
- Method: `_reconstruction_tools/replay.py` rebuilds the whole lineage in a sandbox, from the 10-14 tarball through fast16. It runs the exact patch code taken from the transcript. Remote ssh arguments are decoded with POSIX shell quoting, so quote loss behaves exactly as it did on the remote. Local scratchpad files are copied only when their mtime is earlier than the scp that shipped them. Every `rep()`/`assert count==1` in the original patches passed during replay, and each one would have failed if a base had been wrong. `replay-log.json` lists each step. `fast16-sha256.txt` holds the sha256 of every reconstructed file.
- Not reconstructed: run artifacts and logs (`validate-tree-gpu.log/.json` from fast6, `memtest.log` from fast10, all `__pycache__`), and the 10-14 `initial_update_runner` probe outputs. None of these is code.

## Lineage (remote dir -> created from, transcript ln, time UTC)

```
initial-update-offload-a054eb4b453eceb2   (10-14; local tarball training_runtime/initial-update-offload-a054eb4b453eceb2.tar.gz, all 50 files = staged.json sha256)
 └ initial-update-2gpu-20261015      cp -a  ln 94296  10-15 01:08   (+94379 01:10, +94395 01:11)
   └ initial-update-fast-20261015    cp -a  ln 94870  01:48
     └ initial-update-fast2-20261015 cp -a  ln 95125  01:59
       └ initial-update-fast3-20261015 cp -a ln 95383 02:24
         └ general-update-fast4-20261015 cp -a ln 96284 04:38   (+96912 05:29, +98781 06:58)
           └ general-update-fast5-20261015 cp -a ln 98183 06:26 (+98781, +99364 07:39, +100788 09:47 gate)
             ├ general-update-fast7-20261015 cp -a ln 101263 12:52  (side branch: long_context trial; NOT in fast16 lineage except long_context.py)
             └ general-update-fast6-20261015 cp -a ln 100788 09:47 (+100807, +100974, +101055, +101786)
               └ general-update-fast8-20261015 cp -a ln 101331 13:07 (+101786; +101797/101820 release_from_prefix.py: fast8 only)
                 └ general-update-fast9-20261015 cp -a ln 101493 14:24 (+101509, +101535, +101545, +101786 17:51)
                   └ general-update-fast10-20261016 cp -a ln 102353 10-16 01:57 (+102416 02:09)
                     └ general-update-fast11-20261019 cp -a ln 108625 10-19 02:18
                       └ general-update-fast12-20261019 cp -a ln 111879 13:23 (+111888)
                         └ general-update-fast13-20261019 cp -a ln 112571 13:57 (+112580)
                           └ general-update-fast14-20261020 cp -a ln 113511 10-20 08:46
                             └ general-update-fast15-20261020 cp -a ln 113957 09:04
                               └ general-update-fast16-20261021 cp -a ln 114814 10-21 01:55
```
I checked every later transcript call: none touched fast16 after ln 114814. The transcript ends 10-23. The two subagent transcripts that mention these dirs only touched `lora-export-20261015`.

## Independent checks against remote output recorded in the transcript

| check | result |
|---|---|
| 08:57 10-15 fetch of fast5 files (ln 100073 → `OLDSP/treefwd/src/`): flash_train, chunked_attention, fast_indexer, pipeline_split, grouped_experts, triton_dequant, grpo_train, general_update, activation_offload/__init__ | replayed fast5 is **byte-identical** for all 9 |
| fast10 `grpo_train.py` fetched 10-19 (ln 108551 → `OLDSP/fast11/grpo_train.fast10.py`) | byte-identical to replayed fast10 |
| ln 94296 `diff` output (2gpu runner patch) | identical |
| ln 94395 `grep -n migrate\|pipeline_split` | identical line numbers |
| ln 94870 `grep -n atomic_save/tensor_digest initial_update/__init__.py` (25, 38) | identical |
| ln 99129 `sed -n 30,140p` fast5 runner (pre-stamps) | 81/81 lines match fast4/5 runner |
| ln 101535 `diff fast8 fast9 general_update.py` | identical |
| ln 101786 `sed -n 128,152p` fast9 runner | identical |
| ln 99067/99075/99091 `sed` flash_train.py fast4 | all lines match |
| ln 108469 fast10 `sed 108,135p grpo_train.py`; md5 src grpo.py `4cd11a0a…` | match (= base tarball grpo.py = git 76e88c3) |
| ln 111853 fast11 grpo_train 36-80/120-145/440-450; ln 112414 fast12 97-125 | all lines match |
| ln 111862 md5 fast11 src grpo.py `8bd3d441…` | = `OLDSP/fast11/grpo.py` used |
| ln 113957 `grep -c rca-evidence-v7 grpo_train.py` = 15 | 15 |
| ln 96912 sha256 prefixes of public_splits yamls | identical (configs/teacher unchanged since) |
| ln 97018 `grep -n add_argument` export_vllm_lora.py (lines 151–161) | identical |
| ln 102453 memtest log prints `live` (debug field added 102416) | confirms the final tree_forward reached fast10 |

## Cross-check vs facts from the 10-26 runtime: **no mismatch**

- runner argparse: `--groups --policy-version --start-adapter --output --checkpoint --repo-src --preparation --split-source --learning-rate --tree --checkpoint-every --pipeline-split-at --public-splits-dir --resume --resume-sha256`. Exactly these.
- `GeneralUpdate.__init__` admits groups from the start policy or its parent (`behavior_policy_version`, `stale_steps`): yes (fast9 patch).
- `DEFAULTS = dict(learning_rate=1e-5, clip_epsilon=.2, kl_beta=0., std_floor=1., max_grad_norm=1., adam_beta1=.9, adam_beta2=.999, adam_eps=1e-8, importance_cap=2.)` and `REVISED_BUDGET = {"min_raw_ess": .90, "max_truncated_mass": .01}`: yes. The release gate blocks only on `group_truncated_fraction`; raw ESS and per-episode truncation are recorded as alarms (patch_gate).
- grouped_experts `PROJECTIONS=("gate_proj","up_proj","down_proj")`, `RANK_PAD=8`, `DEQUANT_CHUNK=64`, `EXPERT_CHUNK=128`: yes.
- `tree_forward.EpisodeTree(policy, turns, *, layer_devices, store_device="cpu", min_segment=tree_plan.MIN_SEGMENT)` and `MIN_SEGMENT = 64`: yes.
- runner profile `logit_chunk_size=16, activation_offload="cpu-boundaries-pinned"`: yes. The progress json shows `activation_offload.mode="cpu-boundaries"`. That is the module's own install-summary string (`activation_offload/__init__.py:71`), so it is consistent.
- grpo_train accepts `rca-v1`, `rca-evidence-v2`…`v7`. v7 needs `major_claim_penalty .1`, and v5/v7 are both accepted (ln 114814 validated `group-v7-real` and `group-v5-real`).
- Static checks: `py_compile` passes on every .py file. All local imports (top level, packages, `rca_lab.*` in `src/`, and from-import names) resolve inside the tree. The only external modules are torch, transformers, triton, fla, safetensors, yaml, pydantic, peft, pyarrow, wandb, modelopt, compressed_tensors and pytest.

## Per-file provenance

Confidence: **A** = byte-verified against remote output or a direct remote fetch. **B** = exact replay of transcript patches on a verified base, with every patch's assert passing and the success printed. **C** = depends on an inference such as an mtime that predates the scp. **D** = best effort.

| file (in fast16) | base | modifications replayed (ln, time UTC, success shown in transcript?) | conf. |
|---|---|---|---|
| `flash_train.py` | 10-14 tarball (sha 1cdea585…) | none | A (=fast5 fetch) |
| `pipeline_split.py` | `OLDSP/flash2gpu` (cat> ln 94254 + patch 94270, mtime 01:08:02) → remote `pipeline-split-20261015` (scp 94279) → 2gpu (94296) | none later | A |
| `grouped_experts.py` | v2: `OLDSP/flash2gpu` (cat> 94547, patch 95357/95373, mtime 02:23:38) → fast3 (95383, grep printed `EXPERT_CHUNK = 128`) | none later. v1 was in initial-update-fast/fast2 only | A |
| `fast_indexer.py` | `OLDSP/flash2gpu` (94807) → fast (94870) | none | A |
| `triton_dequant.py` | `OLDSP/flash2gpu` (95156) → fast3 (95383) | none | A |
| `chunked_attention.py` | `OLDSP/flash2gpu` (96047) → fast4 (96284) | none | A |
| `activation_offload/__init__.py` | `OLDSP/flash2gpu/offload_pinned/__init__.py` (95104) → fast2 (95125, "runtime ready") | none | A |
| `activation_offload/probe_full_depth.py` | tarball | none | A |
| `initial_update/__init__.py` (atomic_save, tensor_digest, InitialUpdate) | tarball sha 4652750d… | none | A |
| `runtime_identity/__init__.py` (SCHEMA, digest, collect_runtime, bind_profile, require_matching_profile) | tarball sha 386e5ba4… | none | A |
| `grpo_train.py` | tarball (8560244b…) | 96284 04:38 judge_model/provider from group ("grpo_train patched"); 98183 06:26 split-v3 expected_counts ("patched fast5"). These two give the A-verified fast5/fast10 copies. 108568 10-19 02:17 local v3 → `OLDSP/fast11/grpo_train.py`, scp 108625 ("uploaded"); 111888 13:23 v4 ("patched", "syntax ok"); 112580 13:57 v5 + validate_causal_depth ("patched", "syntax ok"); 113511 10-20 08:46 v6 fabricated_success_cap ("patched", "syntax ok"); 113957 09:04 v7 major_claim_penalty ("patched", "syntax ok", count 15) | A through fast12; B for fast13–16 (v5/v6/v7 patches applied with asserts; v7 count matches) |
| `general_update.py` | `OLDSP/flash2gpu/general_update.py` (cat> 96127, mtime 04:35:23) → fast4 (96284) | fast6: replaced by `OLDSP/treefwd/general_update_fast6.py` (100458 `_turn_loss` / `accumulate_episode_tree`, 09:14 + patch_gate 100771, 09:47) at ln 100788 ("fast6 syntax ok", gate grep 1). fast9: 101493 stale-parent admission. Its ssh double quoting stripped the inner `"` and the ast check failed (Exit 1). 101509 `fix_fast9.py` and 101535 `fix_fast9b.py` repaired it (lines printed). | A (diff 101535 identical) |
| `general_update.py.pre-gate` | backup of fast5 general_update made at 100788, propagated through `cp -a` | — | A |
| `general_update_runner.py` | `OLDSP/flash2gpu/general_update_runner.py` (cat> 96267; patch 96903 public splits, mtime 05:29:17) scp to fast4 at 96912 | 99364 07:39 stage stamps ("stamps added", syntax ok); 100788 `patch_runner_fast6.py` (Write 100476): --tree / EpisodeTree / tree_plan ("runner patched"); 101331 13:07 long_context + compact_small_tensors + empty_cache ("fast8 runner patched"); 101786 17:51 `fix_runner_release.py`: release for both schedules ("release block fixed" on fast6/8/9) | A (99129 and 101786 outputs match) |
| `general_update_runner.py.pre-stamps` | copy made at 99364 | — | A |
| `tree_plan.py` | Write 100313 + local patches 100333/100350 (`OLDSP/treefwd`, mtime 09:06:52) → fast6 (100788) | none later | B/C (mtime) |
| `tree_forward.py` | Write 100360 + local patches 100586, 100632, 100974, 101055, 102336, 102416 (`OLDSP/treefwd`, mtime 10-16 02:09:47) | fast6 got intermediate versions (100788/100974/101055). fast10 got 102353 (01:57) and then the final version at 102416 (02:09). That scp was not echoed, but the memtest log at 102453 shows the new `live` field. | B |
| `long_context.py` | Write 101225 + patch 101244 + append 101288 (`compact_small_tensors`), mtime 12:58:32 → fast7 (scp 101288) → copied into fast8 (101331) | none later | B/C |
| `revised_release.py` | `OLDSP/flash2gpu` (cat> 94621, mtime 01:32:39) → fast (94870) | none | B/C |
| `prefix_migration.py` | `OLDSP/flash2gpu` (cat> 94370, mtime 01:10:38) → 2gpu (94379) | none | B/C |
| `initial_update_runner.py` | tarball | 94296 pipeline split ("runner patched", diff identical); 94379 migrate-prefix ("patched"); 94395 drop migration source ("removed…", grep identical); 94870 grouped/fast-indexer/revised-budget flags ("patched") | A/B |
| `public_splits/{openrca2-all,rcaeval-tt4,rcaeval-ob4}-split.yaml` | `configs/teacher/` (scp 96912) | — | A (sha prefixes match) |
| `test_general_update.py` | `OLDSP/flash2gpu` (96153 + sed 96181) → fast4 | — | B |
| `validate_tree_gpu.py` | `OLDSP/treefwd` (mtime 09:48:39) → fast6 (100807 "uploaded") | — | C |
| `test_fast9_admission.py` | `OLDSP/treefwd` (101545) → fast9 | — | B |
| `memtest_tree_gpu.py` | `OLDSP/treefwd` (102353) → fast10 | — | B |
| `tests/test_grpo.py` | repo `tests/test_grpo.py` scp'd at 108625 (10-19 02:18). Rebuilt as git `76e88c3:tests/test_grpo.py` (10-16, already contains the 10-15 append ln 98739) plus the `cat >>` append at ln 108523 (10-19 02:17). Edits by other sessions between 10-16 and 10-19 cannot be ruled out. The test count (19 defs plus 1 parametrize×3 = 21) matches "21 passed" at 108597. | D/C |
| `src/rca_lab/train/grpo.py` | scp'd from the repo at 113957 (10-20 09:04:06). The current repo file's mtime is 10-20 09:03:35 (local patch ln 113930), so it is the shipped version. | C (high) |
| `src/rca_lab/mcp/rl_reward.py`, `src/rca_lab/mcp/causal_depth.py` | scp'd from the repo at 114814 (10-21 01:55:04). Current repo mtimes are 01:52:22 and 01:53:20, both before the scp, so these are the shipped versions. | C (high) |
| all other `src/rca_lab/**` | tarball (offload runtime src, 10-14) unchanged through fast16 | — | A |
| `lora-export-20261015/export_vllm_lora.py` | subagent `agent-a3d5f6b5ce1a798aa` Write (ln 692, 10-15 04:35:09). Byte-identical to `OLDSP/flash-lora-serve/export_vllm_lora.py`. scp at ln 702. No later edits in any transcript. | A (remote argparse grep matches) |

### `src/` vs the repo's `src/`
- Runtime `src/` differs from the 10-14 base tarball in only 3 files: `train/grpo.py` (replaced), `mcp/rl_reward.py` (replaced) and `mcp/causal_depth.py` (added).
- Intermediate versions of those files are superseded and are not needed for fast16. They were: rl_reward from the repo at 10-15 04:38 plus the require_refs patch (ln 98781), which is md5 `db25fcb3…` = git `76e88c3` per ln 112571; grpo.py at fast11–14; causal_depth at fast13.
- Compared with the current repo (2026-10-27), these runtime src files now differ: `mcp/answer.py`, `mcp/prompts.py`, `mcp/rationale_writer.py`, `mcp/rationalize.py`, `mcp/student.py`, `train/checkpoint.py`, `train/mcp_sft.py`. Newer repo modules such as `numeric_grounding.py`, `decisive_evidence.py` and `masking.py` are not in the runtime, and nothing in it imports them.

## Residual uncertainty
1. `tests/test_grpo.py` (D/C). It is a test file and is never imported by the runner.
2. Repo-sourced `src` files (grpo.py, rl_reward.py, causal_depth.py) rest on mtime-before-scp. If something rewrote them with a preserved mtime this would break, which is unlikely. The fast16 validate_group run at ln 114814 used exactly these and passed.
3. Files from the local scratchpad (tree_plan, tree_forward, long_context, revised_release, prefix_migration, validate_tree_gpu) rest on their mtime predating the scp. tree_forward is also confirmed by the `live` log field.
4. Non-code artifacts (logs/json written by validation runs inside the runtime dirs) are not reproduced.

## Regenerate
`python3 _reconstruction_tools/extract.py <transcript.jsonl> main.jsonl` (then set `tx.MAIN`), followed by `python3 _reconstruction_tools/replay.py <sandbox>`. This rebuilds every sibling dir (2gpu … fast16), and `imports.py <dir>` repeats the static import check.
