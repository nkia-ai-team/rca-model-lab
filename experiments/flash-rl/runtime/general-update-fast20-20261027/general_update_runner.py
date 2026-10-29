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
    p.add_argument("--optimizer", default="adamw", choices=("adamw", "muon", "hybrid"), help="2026-10-27 (fast20)")
    p.add_argument("--muon-min-dim", type=int, default=4, help="hybrid: factors thinner than this stay on AdamW")
    p.add_argument("--speed", default="", help="2026-10-26 (fast19): comma list of speedpatch flags (empty,chunk,stack,gpustore)")
    p.add_argument("--max-episodes", type=int, default=0, help="benchmark: stop after N episodes (no release)")
    p.add_argument("--save-grad", help="benchmark: write the accumulated gradient here")
    p.add_argument("--dump-rows", help="reconstruction check: write per-turn mean(proximal - old) at the --max-episodes exit")
    p.add_argument("--bench-step-after", type=int, default=0, help="benchmark: optimizer step after N episodes, then continue")
    p.add_argument("--save-params", help="benchmark: write parameters after the bench step here")
    p.add_argument("--direction-test", action="store_true", help="step-1 diagnostic: log-prob change vs advantage after one group step")
    p.add_argument("--direction-lr-scale", type=float, default=1.0, help="direction test: multiply the step's learning rate")
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
    import speedpatch
    speed_flags = [f for f in args.speed.split(",") if f]
    summary["speed"] = speedpatch.install(policy, speed_flags)
    if "stack" in speed_flags:
        speedpatch.wrap_update(GeneralUpdate)
    stamp(f"installs done (speed {speed_flags})")
    policy.train()
    patterns = {"experts": ".mlp.experts.", "shared_mlp": ".shared_expert.", "linear_attention": ".linear_attn.", "full_attention": ".self_attn."}
    scopes = {k: [n for n, q in policy.named_parameters() if q.requires_grad and v in n] for k, v in patterns.items()}
    update = GeneralUpdate(policy, groups_bytes, profile, bindings, policy_version=args.policy_version, start_state=start_state,
                           training=dict(learning_rate=args.learning_rate), gradient_scopes=scopes, per_group_steps=args.per_group_steps,
                           optimizer_kind=args.optimizer, muon_min_dim=args.muon_min_dim)
    if args.resume and args.per_group_steps:
        raise ValueError("per-group steps change the adapter mid-update; gradient prefixes cannot be resumed")
    if args.resume:
        update.resume(args.resume, args.resume_sha256)
    if "stack" in speed_flags:
        speedpatch.refresh()   # the stacked expert leaves take the start adapter's values
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
            t_ep = time.time()
            stats = update.accumulate_episode_tree(g, i, lambda turns: tree_forward.EpisodeTree(policy, turns, layer_devices=devices))
            tree_stats.append(dict(group=g, episode=i, seconds=time.time() - t_ep, **stats))
            stamp(f"episode g{g} e{i} done: {stats['per_turn_tokens']} per-turn tokens -> {stats['computed_tokens']} computed ({time.time() - t_ep:.0f}s)")
            if args.bench_step_after and len(tree_stats) == args.bench_step_after:
                if "stack" in speed_flags:
                    speedpatch.flush()
                ordered = sorted(update.parameters)
                params = [update.parameters[n] for n in ordered]
                bench_norm = float(torch.nn.utils.clip_grad_norm_(params, update.training["max_grad_norm"], error_if_nonfinite=True))
                opt = update._optimizer_for(params, ordered)
                opt.step()
                for q in params:
                    q.grad = None
                if "stack" in speed_flags:
                    speedpatch.refresh()
                if args.save_params:
                    torch.save({n: q.detach().cpu() for n, q in update.parameters.items()}, args.save_params)
                g0 = opt.param_groups[0]
                stamp(f"bench step after {len(tree_stats)} episodes: grad norm {bench_norm:.4e} lr {g0['lr']} betas {g0['betas']} eps {g0['eps']} wd {g0['weight_decay']}")
            if args.max_episodes and len(tree_stats) >= args.max_episodes and args.dump_rows:
                out_rows = []
                for row in update.rows:
                    turn = update.episode(row["group"], row["episode"])["turns"][row["turn"]]
                    prox = row["proximal"].reshape(-1).float()
                    old = torch.tensor(turn["old_logprobs"], dtype=torch.float32)
                    out_rows.append(dict(group=row["group"], episode=row["episode"], turn=row["turn"], tokens=prox.numel(),
                                         signed_mean=float((prox - old).mean())))
                Path(args.dump_rows).write_text(json.dumps(out_rows))
            if args.max_episodes and len(tree_stats) >= args.max_episodes:
                if "stack" in speed_flags:
                    speedpatch.flush()
                if args.save_grad:
                    torch.save({n: (None if q.grad is None else q.grad.detach().cpu()) for n, q in policy.named_parameters() if q.requires_grad}, args.save_grad)
                peak = [torch.cuda.max_memory_allocated(d) / 2**30 for d in range(torch.cuda.device_count())]
                print(json.dumps({"bench": True, "speed": speed_flags, "episodes": tree_stats, "peak_gib": peak}), flush=True)
                return
            summary["tree_episodes"] = tree_stats
            if args.per_group_steps:   # 2026-10-24: step at group ends (2026-10-26: every N groups); no prefixes across a step
                if args.direction_test and update.group_done(g):
                    episodes = update.groups[g]["episodes"]
                    picks = [(i, j) for i, ep in enumerate(episodes) for j in sorted({len(ep["turns"]) - 1, len(ep["turns"]) // 2})]
                    def score_all():
                        with torch.no_grad():
                            return {(i, j): existing.completion_logps(policy, episodes[i]["turns"][j], "cuda:0", 16).float().cpu()
                                    for i, j in picks}
                    t_dir = time.time()
                    before1, before2 = score_all(), score_all()
                    if args.direction_lr_scale != 1.0:
                        update.training["learning_rate"] *= args.direction_lr_scale
                    passed = update.step_groups([g])
                    after = score_all()
                    g0 = update._optimizer.param_groups[0] if update._optimizer is not None else {}
                    one = next(iter(update._optimizer.state.values()), {}) if update._optimizer is not None else {}
                    opt_info = dict(lr=g0.get("lr"), betas=g0.get("betas"), eps=g0.get("eps"), weight_decay=g0.get("weight_decay"),
                                    state_step=float(one["step"]) if "step" in one else None, start_optimizer_steps=update.start_step)
                    prox = {(r_["episode"], r_["turn"]): r_["proximal"].reshape(-1).float() for r_ in update.rows if r_["group"] == g}
                    tree_vs_full = [float((prox[k] - before1[k].reshape(-1)).abs().mean()) for k in picks if k in prox]
                    tree_vs_full_signed = [float((prox[k] - before1[k].reshape(-1)).mean()) for k in picks if k in prox]
                    adv = update.groups[g]["advantages"].tolist()
                    rows_out = []
                    for i in range(len(episodes)):
                        ks = [k for k in picks if k[0] == i]
                        n = sum(before1[k].numel() for k in ks)
                        d = sum(float((after[k] - before1[k]).sum()) for k in ks) / n
                        noise = sum(float((before2[k] - before1[k]).sum()) for k in ks) / n
                        noise_abs = sum(float((before2[k] - before1[k]).abs().sum()) for k in ks) / n
                        rows_out.append(dict(episode=i, advantage=adv[i], reward=episodes[i]["reward"], tokens=n,
                                             delta_mean=d, repeat_delta_mean=noise, repeat_abs_mean=noise_abs))
                    import statistics
                    xs, ys = [r_["advantage"] for r_ in rows_out], [r_["delta_mean"] for r_ in rows_out]
                    corr = statistics.correlation(xs, ys) if len(set(xs)) > 1 and len(set(ys)) > 1 else None
                    from rca_lab.train import grpo_correction
                    def implemented_loss(cur_map):
                        total = 0.
                        for (i, j) in picks:
                            turn = episodes[i]["turns"][j]
                            cur = cur_map[(i, j)].reshape(1, -1).float()
                            prox = before1[(i, j)].reshape(1, -1).float()
                            old = torch.tensor(turn["old_logprobs"], dtype=torch.float32)[None]
                            res = grpo_correction.corrected_grpo_loss(
                                cur, prox, old, prox, torch.ones_like(cur, dtype=torch.bool),
                                update.groups[g]["advantages"][i:i + 1].float(), importance_cap=update.training["importance_cap"],
                                clip_epsilon=update.training["clip_epsilon"], kl_beta=0.)
                            total += float(res.loss) * cur.numel() / episodes[i]["generated_tokens"] / len(update.active)
                        return total
                    surrogate_loss = dict(before=implemented_loss(before1), repeat=implemented_loss(before2), after=implemented_loss(after))
                    total_tokens = sum(r_["tokens"] for r_ in rows_out)
                    surrogate = sum(r_["advantage"] * r_["delta_mean"] * r_["tokens"] for r_ in rows_out) / total_tokens
                    surrogate_noise = sum(r_["advantage"] * r_["repeat_delta_mean"] * r_["tokens"] for r_ in rows_out) / total_tokens
                    print(json.dumps({"direction_test": True, "group": g, "stepped": passed, "lr": update.training["learning_rate"],
                                      "speed": speed_flags, "corr_adv_delta": corr, "surrogate_change": surrogate, "implemented_surrogate_loss": surrogate_loss, "surrogate_repeat_noise": surrogate_noise,
                                      "optimizer": opt_info, "tree_vs_full_abs": tree_vs_full, "tree_vs_full_signed": tree_vs_full_signed, "sign_agree": sum((x > 0) == (y > 0) for x, y in zip(xs, ys) if x != 0),
                                      "nonzero_adv": sum(1 for x in xs if x != 0), "episodes": rows_out, "seconds": time.time() - t_dir}), flush=True)
                    return
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
