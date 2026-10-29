"""Replay the remote runtime lineage locally (sandbox), step by step, from transcript-extracted patch code."""
import os, shutil, subprocess, sys, tarfile, hashlib, json
from pathlib import Path
sys.path.insert(0, os.path.dirname(__file__))
import tx
OLDSP = Path("/tmp/claude-1006/-home-swlee-projects/47c4a666-edf6-440b-93bc-c522a28efa1a/scratchpad")
REPO = Path("/home/swlee/projects/rca-model-lab-mcp")
TR = REPO / "outputs/flash-next-rl-pilot-20261014/training_runtime"
B = Path(sys.argv[1])            # sandbox stand-in for /home/work/flash-rl-quant-probe-20261014
LOG = []
def log(step, ln, what):
    LOG.append(dict(step=step, ln=ln, ts=tx.ts(ln) if ln else None, what=what)); print(f"[{step}] ln={ln} {what}")
def py(code, cwd, *argv):
    r = subprocess.run([sys.executable, "-", *map(str, argv)], input=code, text=True, cwd=cwd, capture_output=True)
    if r.returncode: raise SystemExit(f"patch failed in {cwd}: {r.stderr[-1500:]}")
    return r.stdout.strip()
def rhd(ln, k=0, script=0, contains=None):
    """k-th heredoc body of the script-th remote ssh command at transcript line ln."""
    hs = tx.heredocs(tx.remote_scripts(tx.cmd(ln))[script])
    if contains: hs = [h for h in hs if contains in h[1]]
    return hs[k][1]
def lhd(ln, contains):
    return [h for h in tx.heredocs(tx.cmd(ln)) if contains in h[1]][0][1]
def cp(src, dst):
    dst = Path(dst); dst.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(src, dst)
def cpa(a, b):
    assert not (B / b).exists(); shutil.copytree(B / a, B / b, symlinks=True)
def rm_pycache(d):
    for p in list((B / d).rglob("__pycache__")): shutil.rmtree(p)

if B.exists(): shutil.rmtree(B)
B.mkdir(parents=True)
# ---- base: initial-update-offload-a054eb4b453eceb2 (10-14 staged tarball)
base = B / "initial-update-offload-a054eb4b453eceb2"; base.mkdir()
with tarfile.open(TR / "initial-update-offload-a054eb4b453eceb2.tar.gz") as t: t.extractall(base)
staged = json.load(open(TR / "initial-update-offload-staged.json"))["files"]
bad = [f for f, h in staged.items() if hashlib.sha256((base / f).read_bytes()).hexdigest() != h]
assert not bad, bad
log("base", None, "tarball extracted; all %d files match initial-update-offload-staged.json sha256" % len(staged))
# side dirs on the remote that files were copied from (versions = local scratchpad at scp time; mtimes predate scp)
F2 = OLDSP / "flash2gpu"; TF = OLDSP / "treefwd"
# ---- 2gpu (94296, 94379, 94395)
cpa("initial-update-offload-a054eb4b453eceb2", "initial-update-2gpu-20261015"); rm_pycache("initial-update-2gpu-20261015")
N = B / "initial-update-2gpu-20261015"
cp(F2 / "pipeline_split.py", N / "pipeline_split.py")
print(py(rhd(94296), N, N / "initial_update_runner.py")); log("2gpu", 94296, "cp -a offload; pipeline_split.py; runner --pipeline-split-at patch")
cp(F2 / "prefix_migration.py", N / "prefix_migration.py")
print(py(lhd(94379, "migrate-prefix"), N, N / "initial_update_runner.py")); log("2gpu", 94379, "prefix_migration.py + runner --migrate-prefix patch")
print(py(lhd(94395, "removed migration source"), N, N / "initial_update_runner.py")); log("2gpu", 94395, "runner: drop prefix_migration from active_sources")
# ---- fast (94870)
cpa("initial-update-2gpu-20261015", "initial-update-fast-20261015"); rm_pycache("initial-update-fast-20261015")
N = B / "initial-update-fast-20261015"
for f in ("grouped_experts.py", "fast_indexer.py", "revised_release.py"): cp(F2 / f, N / f)   # grouped_experts v1 here; replaced in fast3
print(py(rhd(94870), N, N / "initial_update_runner.py")); log("fast", 94870, "grouped_experts(v1)/fast_indexer/revised_release + runner flags")
# ---- fast2 (95125)
cpa("initial-update-fast-20261015", "initial-update-fast2-20261015"); rm_pycache("initial-update-fast2-20261015")
cp(F2 / "offload_pinned/__init__.py", B / "initial-update-fast2-20261015/activation_offload/__init__.py"); log("fast2", 95125, "activation_offload/__init__.py <- offload_pinned")
# ---- fast3 (95383)
cpa("initial-update-fast2-20261015", "initial-update-fast3-20261015"); rm_pycache("initial-update-fast3-20261015")
for f in ("grouped_experts.py", "triton_dequant.py"): cp(F2 / f, B / "initial-update-fast3-20261015" / f)
log("fast3", 95383, "grouped_experts v2 + triton_dequant")
# ---- general-update-fast4 (96284, 96912)
cpa("initial-update-fast3-20261015", "general-update-fast4-20261015"); rm_pycache("general-update-fast4-20261015")
N = B / "general-update-fast4-20261015"
for f in ("general_update.py", "chunked_attention.py", "test_general_update.py"): cp(F2 / f, N / f)
cp(F2 / "general_update_runner.py", N / "general_update_runner.py")    # 04:38 version, overwritten 05:29 by the same file (96912)
# rl_reward.py <- local repo at 10-15 04:38 (version unknown; superseded in fast16) -> keep base copy, flagged
print(py(rhd(96284), N, N / "grpo_train.py")); log("fast4", 96284, "general_update/runner/chunked_attention/test + grpo_train judge-model patch (+ rl_reward.py from repo: NOT reproducible, superseded in fast16)")
(N / "public_splits").mkdir()
for f in ("openrca2-all-split.yaml", "rcaeval-tt4-split.yaml", "rcaeval-ob4-split.yaml"): cp(REPO / "configs/teacher" / f, N / "public_splits" / f)
log("fast4", 96912, "runner (05:29 public-splits version) + public_splits/*.yaml")
# ---- fast5 (98183, 99364)
cpa("general-update-fast4-20261015", "general-update-fast5-20261015"); N = B / "general-update-fast5-20261015"
print(py(rhd(98183), N)); log("fast5", 98183, "grpo_train split-v3 counts patch")
log("fast5", 98781, "rl_reward require_refs patch (skipped: rl_reward replaced in fast16)")
shutil.copy2(N / "general_update_runner.py", N / "general_update_runner.py.pre-stamps")
print(py(rhd(99364), N)); log("fast5", 99364, "runner stage stamps")
# ---- fast6 (100788)
shutil.copy2(N / "general_update.py", N / "general_update.py.pre-gate")
print(py((TF / "patch_gate.py").read_text(), B, "general-update-fast5-20261015")); log("fast5", 100788, "patch_gate on fast5 general_update.py")
cpa("general-update-fast5-20261015", "general-update-fast6-20261015"); N = B / "general-update-fast6-20261015"
cp(TF / "general_update_fast6.py", N / "general_update.py")
for f in ("tree_plan.py", "tree_forward.py"): cp(TF / f, N / f)   # tree_forward: 09:47 version (later replaced)
print(py((TF / "patch_runner_fast6.py").read_text(), N)); log("fast6", 100788, "general_update_fast6 + tree_plan/tree_forward + patch_runner_fast6")
cp(TF / "validate_tree_gpu.py", N / "validate_tree_gpu.py"); log("fast6", 100807, "validate_tree_gpu.py")
log("fast6", 100974, "tree_forward.py update (intermediate; final version installed in fast10)")
log("fast6", 101055, "tree_forward.py update (intermediate; final version installed in fast10)")
# ---- fast8 (101331): from fast6 + long_context (fast7's 12:58 copy) + runner patch
cpa("general-update-fast6-20261015", "general-update-fast8-20261015"); N = B / "general-update-fast8-20261015"
cp(TF / "long_context.py", N / "long_context.py")
print(py(rhd(101331), N)); log("fast8", 101331, "long_context.py + runner long_context/compact/empty_cache patch")
# ---- fast9 (101493, 101509, 101535)
cpa("general-update-fast8-20261015", "general-update-fast9-20261015"); N = B / "general-update-fast9-20261015"
print(py(rhd(101493), N)); log("fast9", 101493, "general_update stale-parent admission (quote-stripped by ssh double quoting)")
print(py((TF / "fix_fast9.py").read_text(), N)); log("fast9", 101509, "fix_fast9")
print(py((TF / "fix_fast9b.py").read_text(), N)); log("fast9", 101535, "fix_fast9b")
cp(TF / "test_fast9_admission.py", N / "test_fast9_admission.py"); log("fast9", 101545, "test_fast9_admission.py")
for d in ("general-update-fast6-20261015", "general-update-fast8-20261015", "general-update-fast9-20261015"):
    print(py((TF / "fix_runner_release.py").read_text(), B, d))
log("fast6/8/9", 101786, "fix_runner_release")
# ---- fast10 (102353, 102416)
cpa("general-update-fast9-20261015", "general-update-fast10-20261016"); N = B / "general-update-fast10-20261016"
cp(TF / "tree_forward.py", N / "tree_forward.py"); cp(TF / "memtest_tree_gpu.py", N / "memtest_tree_gpu.py")
log("fast10", 102416, "tree_forward.py final (reference-cycle fix) + memtest_tree_gpu.py")
# ---- fast11 (108625)
cpa("general-update-fast10-20261016", "general-update-fast11-20261019"); N = B / "general-update-fast11-20261019"
assert (N / "grpo_train.py").read_bytes() == (OLDSP / "fast11/grpo_train.fast10.py").read_bytes(), "fast10 grpo_train differs from the copy fetched 10-19"
cp(OLDSP / "fast11/grpo_train.py", N / "grpo_train.py"); cp(OLDSP / "fast11/grpo.py", N / "src/rca_lab/train/grpo.py")
(N / "tests").mkdir()
log("fast11", 108625, "grpo_train v3 + src grpo.py (fast11 scratch copies); tests/test_grpo.py (repo 10-19 02:18, not reproducible)")
# ---- fast12..15
cpa("general-update-fast11-20261019", "general-update-fast12-20261019"); N = B / "general-update-fast12-20261019"
print(py(rhd(111888), N)); log("fast12", 111888, "grpo_train v4 (+ src grpo.py from repo 13:23, superseded)")
cpa("general-update-fast12-20261019", "general-update-fast13-20261019"); N = B / "general-update-fast13-20261019"
print(py(rhd(112580), N)); log("fast13", 112580, "grpo_train v5 (+ src grpo.py, causal_depth.py from repo 13:57, superseded)")
cpa("general-update-fast13-20261019", "general-update-fast14-20261020"); N = B / "general-update-fast14-20261020"
print(py(rhd(113511, script=1), N)); log("fast14", 113511, "grpo_train v6 (+ src grpo.py from repo 08:46, superseded)")
cpa("general-update-fast14-20261020", "general-update-fast15-20261020"); N = B / "general-update-fast15-20261020"
cp(REPO / "src/rca_lab/train/grpo.py", N / "src/rca_lab/train/grpo.py")
print(py(rhd(113957, script=1), N)); log("fast15", 113957, "grpo_train v7 + src grpo.py (repo, mtime 10-20 09:03:35 < scp 09:04:06)")
# ---- fast16 (114814)
cpa("general-update-fast15-20261020", "general-update-fast16-20261021"); N = B / "general-update-fast16-20261021"
for f in ("causal_depth.py", "rl_reward.py"): cp(REPO / "src/rca_lab/mcp" / f, N / "src/rca_lab/mcp" / f)
log("fast16", 114814, "src causal_depth.py + rl_reward.py (repo, mtimes 10-21 01:52/01:53 < scp 01:55:04)")
json.dump(LOG, open(B / "replay-log.json", "w"), indent=1)
