"""Full-model GPU validation of the tree schedule against the u2 per-turn run (same start adapter, same group data).

Tree: episode (group 0, episode 0) of u2 = 25 turns through GeneralUpdate.accumulate_episode_tree.
Reference logprobs: the per-turn proximal logprobs u2 recorded (gradient-prefix-0030.pt rows 0..24).
Reference gradient: u2's gradient-prefix-0020.pt + the per-turn path for turns 20..24 in this process.
Run inside the fast6 runtime directory with CUDA_VISIBLE_DEVICES=0,1.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.abspath("src"))
sys.path.insert(0, os.path.abspath("."))
import torch

assert os.environ.get("CUDA_VISIBLE_DEVICES") == "0,1"
import flash_train as loader, grpo_train as existing, pipeline_split, grouped_experts, fast_indexer, chunked_attention
import activation_offload
import tree_forward
from general_update import GeneralUpdate

CKPT = "/var/tmp/flash-ckpt-local/qwen-flash-next-w4a16-autoround-4c67bf68"
U2 = "/home/work/models/rca-adapters/flash-rl-loop-20261015/u002"
GROUPS = ["/home/work/flash-rl-loop-20261015/u002/groups/case-f25-h-v3-dc3d4fc8.json",
          "/home/work/flash-rl-loop-20261015/u002/groups/case-openrca2-o2ts-v3-df2c94d0.json"]
START = "/home/work/models/rca-adapters/flash-grpo-fast3-g0002-20261015/adapter.pt"
OUT = sys.argv[1] if len(sys.argv) > 1 else "validate-tree-gpu.json"
t0 = time.time()
stamp = lambda m: print(f"[{time.strftime('%H:%M:%S')} +{time.time() - t0:.0f}s] {m}", flush=True)

groups_bytes = [open(g, "rb").read() for g in GROUPS]
start_state = torch.load(START, map_location="cpu", weights_only=False)
torch.manual_seed(20261014); torch.set_num_threads(8)
loader.enable_serving_convolution()
policy = loader.load_policy(CKPT, device="cuda:0", expert_rank=2, dense_rank=16)
loader.enable_serving_delta_rule(policy, "fla"); loader.enable_serving_hyperconnections(policy)
pipeline_split.install(policy, split_at=24)
grouped_experts.install(policy)
fast_indexer.install(policy)
chunked_attention.install()
activation_offload.install(policy)
tree_forward.install_tree_patches(policy)
policy.train()
stamp("policy ready")
patterns = {"experts": ".mlp.experts.", "shared_mlp": ".shared_expert.", "linear_attention": ".linear_attn.", "full_attention": ".self_attn."}
scopes = {k: [n for n, q in policy.named_parameters() if q.requires_grad and v in n] for k, v in patterns.items()}
upd = GeneralUpdate(policy, groups_bytes, {"validation": "tree-vs-u2"}, {"validation": True}, policy_version="adapter-15e40ca8f60b3295",
                    start_state=start_state, training=dict(learning_rate=5e-5), gradient_scopes=scopes)
devices = [torch.device("cuda:1") if l >= 24 else torch.device("cuda:0") for l in range(len(policy.backbone.layers))]
n_turns = len(upd.episode(0, 0)["turns"])
torch.cuda.reset_peak_memory_stats(0); torch.cuda.reset_peak_memory_stats(1)
t = time.time()
stats = upd.accumulate_episode_tree(0, 0, lambda turns: tree_forward.EpisodeTree(policy, turns, layer_devices=devices))
t_tree = time.time() - t
peak_tree = [torch.cuda.max_memory_allocated(d) / 2**30 for d in (0, 1)]
stamp(f"tree episode done in {t_tree:.0f}s, plan {stats}, peak {peak_tree}")
tree_rows = list(upd.rows)
g_tree = {n: (p.grad.detach().cpu().clone() if p.grad is not None else None) for n, p in upd.parameters.items()}

p30 = torch.load(f"{U2}/gradient-prefix-0030.pt", map_location="cpu", weights_only=True)
lp = []
for j in range(n_turns):
    a, b = tree_rows[j]["proximal"].reshape(-1), p30["rows"][j]["proximal"].reshape(-1)
    d = (a - b).abs()
    lp.append(dict(turn=j, tokens=a.numel(), max_abs=d.max().item(), mean_abs=d.mean().item()))
stamp(f"logprob vs u2: max {max(x['max_abs'] for x in lp):.4f}, mean {sum(x['mean_abs'] * x['tokens'] for x in lp) / sum(x['tokens'] for x in lp):.5f}")

# reference gradient: u2 prefix at 20 turns + per-turn turns 20..n-1
p20 = torch.load(f"{U2}/gradient-prefix-0020.pt", map_location="cpu", weights_only=True)
for n, p in upd.parameters.items():
    g = p20["gradients"][n]
    p.grad = None if g is None else g.to(p.device).clone()
upd.rows, upd.loss_total = list(p20["rows"]), p20["loss_total"]
t = time.time()
per_turn_lp = {}
for j in range(20, n_turns):
    current = existing.completion_logps(policy, upd.episode(0, 0)["turns"][j], "cuda:0", 16)
    upd.accumulate(0, 0, j, current)
    per_turn_lp[j] = upd.rows[-1]["proximal"].reshape(-1)
    del current
t_ref = time.time() - t
stamp(f"per-turn reference for turns 20..{n_turns - 1} in {t_ref:.0f}s ({t_ref / (n_turns - 20):.0f}s/turn)")
num = na = nb = 0.0
worst = (0.0, None)
for n, p in upd.parameters.items():
    a = p.grad.detach().cpu() if p.grad is not None else None
    b = g_tree[n]
    if a is None and b is None:
        continue
    a = torch.zeros_like(b) if a is None else a
    b = torch.zeros_like(a) if b is None else b
    num += (a.double() * b.double()).sum().item(); na += a.double().square().sum().item(); nb += b.double().square().sum().item()
    s = a.abs().max().item()
    if s > 1e-12:
        r = (a - b).abs().max().item() / s
        if r > worst[0]:
            worst = (r, n)
same_process = max((per_turn_lp[j] - tree_rows[j]["proximal"].reshape(-1)).abs().max().item() for j in per_turn_lp)
result = dict(turns=n_turns, plan=stats, tree_seconds=t_tree, per_turn_seconds_per_turn=t_ref / (n_turns - 20),
              est_per_turn_episode_seconds=t_ref / (n_turns - 20) * n_turns, peak_gib_tree=peak_tree,
              logprob_vs_u2=dict(max_abs=max(x["max_abs"] for x in lp),
                                 mean_abs=sum(x["mean_abs"] * x["tokens"] for x in lp) / sum(x["tokens"] for x in lp)),
              logprob_tree_vs_per_turn_same_process_max_abs=same_process,
              grad_cosine=num / (na ** 0.5 * nb ** 0.5), grad_norm_ratio=(nb / na) ** 0.5, worst_param_rel=worst, per_turn_logprob=lp)
json.dump(result, open(OUT, "w"), indent=1, default=str)
stamp("result " + json.dumps({k: v for k, v in result.items() if k != "per_turn_logprob"}, default=str))
