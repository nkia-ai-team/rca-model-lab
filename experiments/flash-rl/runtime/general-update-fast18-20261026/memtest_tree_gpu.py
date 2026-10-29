"""GPU memory check of the hook-injected tree backward on the episode that OOM'd in u6 (u2 f25-h episode 3)."""
import json, os, sys, time
sys.path.insert(0, os.path.abspath("src")); sys.path.insert(0, os.path.abspath("."))
import torch
import flash_train as loader, pipeline_split, grouped_experts, fast_indexer, chunked_attention, activation_offload, long_context, tree_forward
t0 = time.time(); stamp = lambda m: print(f"[{time.strftime('%H:%M:%S')} +{time.time() - t0:.0f}s] {m}", flush=True)
start = torch.load("/home/work/models/rca-adapters/flash-rl-loop-20261015/u005/adapter.pt", map_location="cpu", weights_only=False)
torch.manual_seed(20261014); torch.set_num_threads(8)
loader.enable_serving_convolution()
policy = loader.load_policy("/var/tmp/flash-ckpt-local/qwen-flash-next-w4a16-autoround-4c67bf68", device="cuda:0", expert_rank=2, dense_rank=16)
loader.enable_serving_delta_rule(policy, "fla"); loader.enable_serving_hyperconnections(policy)
pipeline_split.install(policy, split_at=24); grouped_experts.install(policy); fast_indexer.install(policy); chunked_attention.install()
activation_offload.install(policy); long_context.install(policy); print(long_context.compact_small_tensors(policy)); tree_forward.install_tree_patches(policy); policy.train()
with torch.no_grad():
    for n, p in policy.named_parameters():
        if p.requires_grad: p.copy_(start["state_dict"][n])
stamp("ready")
g = json.load(open("/home/work/flash-rl-loop-20261015/u002/groups/case-f25-h-v3-dc3d4fc8.json"))
turns = g["episodes"][3]["turns"]
devices = [torch.device("cuda:1") if l >= 24 else torch.device("cuda:0") for l in range(48)]
for d in (0, 1): torch.cuda.reset_peak_memory_stats(d)
t = time.time()
tree = tree_forward.EpisodeTree(policy, turns, layer_devices=devices)
tree.run(lambda j, lp: lp.sum() * 1e-3)
stamp(f"episode done in {time.time() - t:.0f}s plan {tree.stats} peak GiB {[round(torch.cuda.max_memory_allocated(d) / 2**30, 1) for d in (0, 1)]}")
