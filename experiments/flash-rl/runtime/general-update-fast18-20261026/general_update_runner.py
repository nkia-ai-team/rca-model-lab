"""Run one multi-group GRPO update from the current adapter on both KT GPUs (2026-10-15 loop automation).

  python general_update_runner.py --groups g1.json [g2.json ...] --policy-version adapter-xxxx|base-... \
      --start-adapter /path/adapter.pt|none --output OUT --checkpoint MODEL --repo-src SRC --preparation P --split-source S \
      [--learning-rate 1e-5] [--checkpoint-every 10] [--resume PREFIX --resume-sha256 SHA]
Accumulates every turn of every non-flat group, saves a prefix every N turns, then releases one AdamW step if every
active group passes the user-approved revised drift budget (raw ESS >= .90, truncated mass <= .01). The runtime is the
fast one: two-GPU split, grouped-GEMM experts, vectorized QSA indexer, causal-chunked SDPA, pinned boundary offload.
"""
from __future__ import annotations

import argparse, hashlib, json, os, sys, time
from pathlib import Path


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--groups", nargs="+", required=True)
    p.add_argument("--policy-version", required=True)
    p.add_argument("--start-adapter", required=True, help="adapter.pt of the policy that produced the groups, or 'none' for base")
    for name in ("output", "checkpoint", "repo-src", "preparation", "split-source"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--learning-rate", type=float, default=1e-5)
    p.add_argument("--tree", action="store_true", help="exact prefix-sharing tree schedule (2026-10-15)")
    p.add_argument("--checkpoint-every", type=int, default=10)
    p.add_argument("--pipeline-split-at", type=int, default=24)
    p.add_argument("--public-splits-dir", default=str(Path(__file__).resolve().parent / "public_splits"),
                   help="copies of public split YAMLs (OpenRCA 2.0 / RCAEval) for public-data groups")
    p.add_argument("--resume"); p.add_argument("--resume-sha256")
    p.add_argument("--per-group-steps", action="store_true", help="2026-10-24 (fast17): one optimizer step per active group")
    p.add_argument("--groups-per-step", type=int, default=1, help="2026-10-26 (fast18): with --per-group-steps, step every N completed groups")
    args = p.parse_args(argv)
    if os.environ.get("CUDA_VISIBLE_DEVICES") != "0,1":
        raise ValueError("General runner uses both KT GPUs: CUDA_VISIBLE_DEVICES=0,1")
    sys.path.insert(0, str(Path(args.repo_src).resolve()))
    import torch
    import flash_train as loader, grpo_train as existing, pipeline_split, grouped_experts, fast_indexer, chunked_attention
    import tree_forward, tree_plan
    import activation_offload
    import long_context
    from general_update import GeneralUpdate
    from runtime_identity import bind_profile, collect_runtime, digest

    _t0 = time.time()
    def stamp(msg):  # 2026-10-15: stage timing (model load was ~12 min, traced to cold NFS reads)
        print(f"[stage {time.strftime('%H:%M:%S')} +{time.time() - _t0:.1f}s] {msg}", flush=True)
    stamp(f"start checkpoint={args.checkpoint}")
    groups_bytes = [Path(g).read_bytes() for g in args.groups]

    def validate_split(group):
        if not group.get("public"):
            return existing.validate_training_split(group, args.preparation, args.split_source)
        import yaml
        path = Path(args.public_splits_dir) / Path(group["split_source"]).name
        if hashlib.sha256(path.read_bytes()).hexdigest() != group["split_sha256"]:
            raise ValueError("Public split copy differs from the one used at collection: " + path.name)
        split = yaml.safe_load(path.read_text())
        held = {c for k, v in split.items() if k != "train" and isinstance(v, list) for c in v}
        if group["case_id"] not in split.get("train", []) or group["case_id"] in held:
            raise ValueError("Public group case is not a train-partition case: " + group["case_id"])
        return group["split_sha256"]

    split_shas = sorted({validate_split(json.loads(b)) for b in groups_bytes})
    existing.validate_checkpoint(args.checkpoint, existing.BASE_REVISION)
    sources = {n: m.__file__ for n, m in (("pipeline_split", pipeline_split), ("grouped_experts", grouped_experts),
                                          ("fast_indexer", fast_indexer), ("chunked_attention", chunked_attention),
                                          ("activation_offload", activation_offload), ("long_context", long_context))}
    if args.tree:
        sources.update(tree_forward=tree_forward.__file__, tree_plan=tree_plan.__file__)
    def current_profile():
        return bind_profile(dict(probability_backend="fla-matched", serving_hyperconnections=True, base_policy_version=existing.BASE_REVISION,
                                 implementation="fast-general-tree" if args.tree else "fast-general", pipeline_split_at=args.pipeline_split_at, expert_rank=2, dense_rank=16,
                                 logit_chunk_size=16, activation_offload="cpu-boundaries-pinned"),
                            collect_runtime(fla_active=True, active_sources=sources))
    profile = current_profile()
    bindings = dict(checkpoint_identity_sha256=digest({"checkpoint": args.checkpoint, "revision": existing.BASE_REVISION}),
                    split_sha256=split_shas[0] if len(split_shas) == 1 else digest(split_shas), split_sha256_all=split_shas, loader_sha256=existing.sha256_file(loader.__file__),
                    runner_sha256=existing.sha256_file(__file__), group_validator_sha256=existing.sha256_file(existing.__file__))
    output = Path(args.output); output.mkdir(parents=True, exist_ok=True)
    if (output / "adapter.pt").exists():
        raise ValueError("Output already holds a released adapter")
    stamp("validated groups/checkpoint/profile")
    start_state = None
    if args.start_adapter != "none":
        start_state = torch.load(args.start_adapter, map_location="cpu", weights_only=False)
    started = time.time()
    torch.manual_seed(20261014); torch.set_num_threads(8)
    loader.enable_serving_convolution()
    stamp("start adapter loaded")
    policy = loader.load_policy(args.checkpoint, device="cuda:0", expert_rank=2, dense_rank=16)
    stamp("load_policy done")
    loader.enable_serving_delta_rule(policy, "fla"); loader.enable_serving_hyperconnections(policy)
    summary = dict(status="loading", optimizer_steps=None, groups=args.groups, policy_version=args.policy_version)
    summary["pipeline_split"] = pipeline_split.install(policy, split_at=args.pipeline_split_at)
    summary["grouped_experts"] = grouped_experts.install(policy)
    summary["fast_indexer"] = fast_indexer.install(policy)
    summary["chunked_attention"] = chunked_attention.install()
    summary["activation_offload"] = activation_offload.install(policy)
    summary["long_context"] = long_context.install(policy)   # 2026-10-15: no T x T masks, one GQA expansion per call
    summary["compact"] = long_context.compact_small_tensors(policy)   # release load-time small-pool fragmentation
    if args.tree:
        summary["tree"] = tree_forward.install_tree_patches(policy)
    stamp("installs done")
    policy.train()
    patterns = {"experts": ".mlp.experts.", "shared_mlp": ".shared_expert.", "linear_attention": ".linear_attn.", "full_attention": ".self_attn."}
    scopes = {k: [n for n, q in policy.named_parameters() if q.requires_grad and v in n] for k, v in patterns.items()}
    update = GeneralUpdate(policy, groups_bytes, profile, bindings, policy_version=args.policy_version, start_state=start_state,
                           training=dict(learning_rate=args.learning_rate), gradient_scopes=scopes, per_group_steps=args.per_group_steps)
    if args.resume and args.per_group_steps:
        raise ValueError("per-group steps change the adapter mid-update; gradient prefixes cannot be resumed")
    if args.resume:
        update.resume(args.resume, args.resume_sha256)
    stamp("GeneralUpdate ready")
    summary.update(active_groups=update.identity["active_groups"], turns=len(update.order),
                   flat_groups=[g for g in range(len(groups_bytes)) if g not in update.identity["active_groups"]])
    write = lambda: (output / "progress.json").write_text(json.dumps(summary, indent=2, default=str))
    try:
      if args.tree:
        # 2026-10-15: exact prefix-sharing schedule, one episode at a time; prefixes are saved at episode boundaries only
        devices = [torch.device("cuda:1") if l >= args.pipeline_split_at else torch.device("cuda:0") for l in range(len(policy.backbone.layers))]
        first = {}
        for pos, (g, i, j) in enumerate(update.order):
            first.setdefault((g, i), pos)
        last_saved = len(update.rows)
        tree_stats = []
        pending_groups = []
        for (g, i), pos in first.items():
            if pos < len(update.rows):
                continue
            summary.update(status="accumulating", completed_turns=len(update.rows)); write()
            stats = update.accumulate_episode_tree(g, i, lambda turns: tree_forward.EpisodeTree(policy, turns, layer_devices=devices))
            tree_stats.append(dict(group=g, episode=i, **stats))
            stamp(f"episode g{g} e{i} done: {stats['per_turn_tokens']} per-turn tokens -> {stats['computed_tokens']} computed")
            summary["tree_episodes"] = tree_stats
            if args.per_group_steps:   # 2026-10-24: step at group ends (2026-10-26: every N groups); no prefixes across a step
                if update.group_done(g):
                    pending_groups.append(g)
                    last_group = g == update.identity["active_groups"][-1]
                    if len(pending_groups) >= args.groups_per_step or last_group:
                        passed = update.step_groups(pending_groups)
                        stamp(f"groups {pending_groups} {'stepped' if passed else 'dropped by drift gate'} (steps {update.steps})")
                        pending_groups = []
                        summary["group_steps"] = {str(k): {kk: vv for kk, vv in v.items() if kk != "report"} for k, v in update.group_gate.items()}
                continue
            if len(update.rows) - last_saved >= args.checkpoint_every:
                prefix = output / f"gradient-prefix-{len(update.rows):04d}.pt"
                summary.update(prefix_path=str(prefix), prefix_sha256=update.save_prefix(prefix))
                last_saved = len(update.rows)
      else:
        for g, i, j in update.order[len(update.rows):]:
            summary.update(status="accumulating", completed_turns=len(update.rows)); write()
            current = existing.completion_logps(policy, update.episode(g, i)["turns"][j], "cuda:0", 16)
            update.accumulate(g, i, j, current); del current
            torch.cuda.empty_cache()
            if len(update.rows) <= 3:
                stamp(f"turn {len(update.rows)} done")
            if len(update.rows) % args.checkpoint_every == 0:
                prefix = output / f"gradient-prefix-{len(update.rows):04d}.pt"
                summary.update(prefix_path=str(prefix), prefix_sha256=update.save_prefix(prefix))
      # release for both schedules (2026-10-15 fix: this block had been indented into the per-turn branch)
      summary.update(status="releasing", completed_turns=len(update.rows)); write()
      result = update.release(output, current_profile())
      summary.update(status=result["status"], optimizer_steps=result.get("optimizer_steps"), release=result)
    except Exception as error:
        summary.update(status="failed", error_type=type(error).__name__, error=str(error)[:2000], completed_turns=len(update.rows))
        write(); raise
    summary.update(elapsed_seconds=time.time() - started,
                   peak_gpu_gib_by_device=[torch.cuda.max_memory_allocated(d) / 2**30 for d in range(torch.cuda.device_count())])
    write(); print(json.dumps({k: summary[k] for k in ("status", "optimizer_steps")}, default=str), flush=True)


if __name__ == "__main__":
    main()
