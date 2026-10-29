"""Initial-base full-group accumulation, followed by separately reviewed release.

No launch occurs on import. GPU0 only; the existing audit/replay runner is unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time


def checkpoint_identity(path):
    """Bind metadata, tensor headers and file stats, not claim full weight hashing."""
    path = Path(path)
    files = {}
    for file in sorted(path.glob("*.json")):
        files[file.name] = {"sha256": hashlib.sha256(file.read_bytes()).hexdigest()}
    for file in sorted(path.glob("*.safetensors")):
        stat = file.stat()
        with file.open("rb") as stream:
            length = int.from_bytes(stream.read(8), "little")
            if not 0 < length <= min(stat.st_size - 8, 128 * 1024 * 1024):
                raise ValueError("Invalid safetensors header: " + file.name)
            header = stream.read(length)
        if len(header) != length:
            raise ValueError("Incomplete checkpoint header")
        files[file.name] = dict(size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                               header_sha256=hashlib.sha256(header).hexdigest())
    if not any(name.endswith(".safetensors") for name in files):
        raise ValueError("No checkpoint tensor shards")
    return {"coverage": "metadata/header SHA and shard size/mtime; not full weight-byte SHA", "files": files}


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("preflight", "accumulate", "release"))
    for name in ("group", "preparation", "split-source", "checkpoint", "output", "repo-src", "serving-controls"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--implementation", choices=("original",), required=True)
    p.add_argument("--resume")
    p.add_argument("--resume-sha256")
    p.add_argument("--review")
    p.add_argument("--checkpoint-every", type=int, default=1)
    p.add_argument("--activation-offload", choices=("none", "cpu-boundaries"), default="none")
    p.add_argument("--expert-rank", type=int, default=2)
    p.add_argument("--dense-rank", type=int, default=16)
    p.add_argument("--logit-chunk-size", type=int, default=16)
    p.add_argument("--migrate-prefix", help="single-GPU gradient prefix to continue under the split runtime")
    p.add_argument("--migrate-prefix-sha256")
    p.add_argument("--grouped-experts", action="store_true", help="vectorized grouped-GEMM routed experts (2026-10-15 speed fix)")
    p.add_argument("--fast-indexer", action="store_true", help="vectorized QSA indexer, identical masks (2026-10-15 speed fix)")
    p.add_argument("--revised-budget", action="store_true", help="release under the user-revised drift budget (ESS>=.90, truncated mass<=.01)")
    p.add_argument("--pipeline-split-at", type=int, default=0,
                   help="two-GPU layer split (2026-10-15 OOM recovery): decoder layers >= N on cuda:1; 0 = single GPU")
    return p


def gradient_scopes(policy):
    patterns = {"experts": ".mlp.experts.", "shared_mlp": ".shared_expert.",
                "linear_attention": ".linear_attn.", "full_attention": ".self_attn."}
    scopes = {name: [] for name in patterns}
    for name, parameter in policy.named_parameters():
        if not parameter.requires_grad:
            continue
        matches = [scope for scope, pattern in patterns.items() if pattern in name]
        if len(matches) != 1:
            raise ValueError("Unclassified trainable adapter: " + name)
        scopes[matches[0]].append(name)
    if not all(scopes.values()):
        raise ValueError("All four intended adapter scopes must be present")
    return scopes


def main(argv=None):
    args = parser().parse_args(argv)
    if (args.resume is None) != (args.resume_sha256 is None):
        raise ValueError("Resume path and hash must be supplied together")
    if (args.migrate_prefix is None) != (args.migrate_prefix_sha256 is None):
        raise ValueError("Migration path and hash must be supplied together")
    if args.migrate_prefix and (args.resume or args.action != "accumulate" or not args.pipeline_split_at):
        raise ValueError("Prefix migration is only for a fresh two-GPU accumulate")
    if args.action == "release" and not (args.resume and args.review):
        raise ValueError("Release requires sealed accumulation and explicit review")
    if args.action != "release" and args.review:
        raise ValueError("Review is only consumed by release")
    if min(args.checkpoint_every, args.expert_rank, args.dense_rank, args.logit_chunk_size) <= 0:
        raise ValueError("Counts and ranks must be positive")
    gpu0 = "GPU-566de8c5-883d-01f1-cd99-585c5b0759e8"
    if args.pipeline_split_at:
        # User 2026-10-15: after the single-GPU OOM, use both KT GPUs (layer split, no arithmetic change).
        if os.environ.get("CUDA_VISIBLE_DEVICES") != "0,1":
            raise ValueError("Two-GPU split requires CUDA_VISIBLE_DEVICES=0,1")
    else:
        if os.environ.get("CUDA_VISIBLE_DEVICES", "0") not in ("0", gpu0):
            raise ValueError("This runner is restricted to Flash GPU0")
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", "0")
    sys.path.insert(0, str(Path(args.repo_src).resolve()))
    import torch
    import flash_train as loader
    import grpo_train as existing
    from initial_update import InitialUpdate
    from runtime_identity import bind_profile, collect_runtime, digest

    group_bytes = Path(args.group).read_bytes()
    group = json.loads(group_bytes)
    episodes = existing.validate_group(group, existing.BASE_REVISION)
    if len(episodes) != 4 or group.get("reward_version") != "rca-evidence-v2":
        raise ValueError("Requires four complete evidence-reward episodes")
    split_sha = existing.validate_training_split(group, args.preparation, args.split_source)
    existing.validate_checkpoint(args.checkpoint, existing.BASE_REVISION)
    if group.get("serving_backend", {}).get("gdn_prefill_backend") != "triton":
        raise ValueError("Requires fingerprinted Triton rollout backend")
    controls = json.loads(Path(args.serving_controls).read_text())
    group_sha = hashlib.sha256(group_bytes).hexdigest()
    if (controls.get("status") != "complete" or controls.get("group_sha256") != group_sha
            or controls.get("serving_backend_sha256") != group.get("serving_backend_sha256")):
        raise ValueError("Serving controls do not match group")
    def current_profile():
        active_sources = {}
        if args.activation_offload == "cpu-boundaries":
            import activation_offload
            active_sources["activation_offload"] = activation_offload.__file__
        if args.pipeline_split_at:
            import pipeline_split
            active_sources["pipeline_split"] = pipeline_split.__file__
        if args.grouped_experts:
            import grouped_experts
            active_sources["grouped_experts"] = grouped_experts.__file__
        if args.fast_indexer:
            import fast_indexer
            active_sources["fast_indexer"] = fast_indexer.__file__
        return bind_profile(dict(probability_backend="fla-matched", serving_hyperconnections=True,
                                 base_policy_version=existing.BASE_REVISION,
                                 implementation=args.implementation,
                                 expert_rank=args.expert_rank, dense_rank=args.dense_rank,
                                 logit_chunk_size=args.logit_chunk_size,
                                 activation_offload=args.activation_offload,
                                 pipeline_split_at=args.pipeline_split_at,
                                 grouped_experts=args.grouped_experts, fast_indexer=args.fast_indexer),
                            collect_runtime(fla_active=True, active_sources=active_sources))
    profile = current_profile()
    base = checkpoint_identity(args.checkpoint)
    bindings = dict(checkpoint_identity_sha256=digest(base), split_sha256=split_sha,
                    loader_sha256=existing.sha256_file(loader.__file__),
                    runner_sha256=existing.sha256_file(__file__),
                    group_validator_sha256=existing.sha256_file(existing.__file__),
                    controls_sha256=existing.sha256_file(args.serving_controls))
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    summary = dict(status="preflight_passed", action=args.action, optimizer_steps=0,
                   probability_profile=profile, probability_profile_sha256=digest(profile),
                   bindings=bindings, checkpoint_identity=base, group_sha256=group_sha,
                   turns=sum(len(e["turns"]) for e in episodes),
                   tokens=sum(e["generated_tokens"] for e in episodes))
    existing.write_json(output / (args.action + "-preflight.json"), summary)
    if args.action == "preflight":
        print(json.dumps({k: summary[k] for k in ("status", "turns", "tokens")}), flush=True)
        return
    if (output / "adapter.pt").exists() or (output / "adapter.pt.reserved").exists():
        raise ValueError("An optimizer result/reservation already exists")
    if args.action == "accumulate" and (output / "sealed-accumulation.pt").exists():
        raise ValueError("Accumulation already sealed; use release or a new output")
    started = time.time()
    torch.manual_seed(20261014)
    torch.set_num_threads(8)
    loader.enable_serving_convolution()
    policy = loader.load_policy(args.checkpoint, device="cuda:0",
                                expert_rank=args.expert_rank, dense_rank=args.dense_rank)
    loader.enable_serving_delta_rule(policy, "fla")
    loader.enable_serving_hyperconnections(policy)
    if args.pipeline_split_at:
        import pipeline_split
        summary["pipeline_split"] = pipeline_split.install(policy, split_at=args.pipeline_split_at)
    if args.grouped_experts:
        import grouped_experts
        summary["grouped_experts"] = grouped_experts.install(policy)
    if args.fast_indexer:
        import fast_indexer
        summary["fast_indexer"] = fast_indexer.install(policy)
    if args.activation_offload == "cpu-boundaries":
        from activation_offload import install
        summary["activation_offload_installation"] = install(policy)
    policy.train()
    if getattr(policy.backbone.config, "attention_dropout", None) != 0:
        raise ValueError("Explicit zero attention dropout required")
    if current_profile() != profile:
        raise ValueError("Runtime/source identity changed during model setup")
    if args.migrate_prefix:
        import prefix_migration
        update, record = prefix_migration.migrate(
            InitialUpdate, policy, group_bytes, profile, bindings, args.migrate_prefix, args.migrate_prefix_sha256,
            gradient_scopes=gradient_scopes(policy), episodes=episodes, completion_logps=existing.completion_logps,
            logit_chunk_size=args.logit_chunk_size, output=output)
        summary["prefix_migration"] = record
    elif args.resume:
        if args.revised_budget:
            from revised_release import RevisedInitialUpdate as InitialUpdate  # noqa: F811 (release-time budget only)
        update = InitialUpdate.resume(policy, group_bytes, profile, bindings,
                                      args.resume, args.resume_sha256,
                                      gradient_scopes=gradient_scopes(policy))
    else:
        update = InitialUpdate(policy, group_bytes, profile, bindings,
                               gradient_scopes=gradient_scopes(policy))
    if args.action == "release":
        review = json.loads(Path(args.review).read_text())
        if digest(checkpoint_identity(args.checkpoint)) != bindings["checkpoint_identity_sha256"]:
            raise ValueError("Checkpoint identity changed before release")
        result = update.release(review, current_profile(), output / "adapter.pt")
        summary.update({k: v for k, v in result.items() if k not in ("state_dict", "identity")})
    else:
        try:
            for i, j in update.order[len(update.rows):]:
                summary.update(status="accumulating", episode=i, turn=j, completed_turns=len(update.rows))
                existing.write_json(output / "progress.json", summary)
                current = existing.completion_logps(policy, episodes[i]["turns"][j], "cuda:0", args.logit_chunk_size)
                update.accumulate(i, j, current)
                del current
                if len(update.rows) % args.checkpoint_every == 0:
                    prefix = output / f"gradient-prefix-{len(update.rows):04d}.pt"
                    if prefix.exists():
                        raise ValueError("Committed prefix already exists: " + str(prefix))
                    prefix_sha = update.save_prefix(prefix)
                    summary.update(prefix_sha256=prefix_sha, prefix_path=str(prefix),
                                   saved_completed_turns=len(update.rows))
                summary.update(completed_turns=len(update.rows))
                existing.write_json(output / "progress.json", summary)
            binding, report = update.seal(output / "sealed-accumulation.pt", controls)
            existing.write_json(output / "drift-report.json", report)
            existing.write_json(output / "release-request.json", dict(binding=binding, optimizer_steps=0,
                                all_experimental_budgets_pass=report["all_experimental_budgets_pass"]))
            summary.update(status="complete_accumulation_requires_review", binding=binding,
                           completed_turns=len(update.rows), all_experimental_budgets_pass=report["all_experimental_budgets_pass"])
        except Exception as error:
            summary.update(status="failed", error_type=type(error).__name__, error=str(error),
                           completed_turns=len(update.rows), optimizer_steps=0,
                           recovery="fresh process; only last committed prefix is reusable")
            existing.write_json(output / "accumulate-result.json", summary)
            existing.write_json(output / "progress.json", summary)
            raise
    summary.update(elapsed_seconds=time.time() - started,
                   peak_gpu_gib=max(torch.cuda.max_memory_allocated(d) for d in range(torch.cuda.device_count())) / 2**30,
                   peak_gpu_gib_by_device=[torch.cuda.max_memory_allocated(d) / 2**30 for d in range(torch.cuda.device_count())])
    existing.write_json(output / (args.action + "-result.json"), summary)
    print(json.dumps({k: summary[k] for k in ("status", "optimizer_steps")}), flush=True)


if __name__ == "__main__":
    main()
