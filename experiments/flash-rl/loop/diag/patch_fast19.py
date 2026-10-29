"""fast18 -> fast19 (2026-10-26): speed patches (speedpatch.py, --speed flags) and a benchmark mode (--max-episodes N,
--save-grad PATH: stop after N episodes, write the accumulated gradient, skip release)."""
from pathlib import Path

r = Path("general_update_runner.py"); t = r.read_text()


def rep(text, old, new):
    assert text.count(old) == 1, old[:80]
    return text.replace(old, new)


t = rep(t, '''    p.add_argument("--groups-per-step", type=int, default=1, help="2026-10-26 (fast18): with --per-group-steps, step every N completed groups")''',
        '''    p.add_argument("--groups-per-step", type=int, default=1, help="2026-10-26 (fast18): with --per-group-steps, step every N completed groups")
    p.add_argument("--speed", default="", help="2026-10-26 (fast19): comma list of speedpatch flags (empty,chunk,stack,gpustore)")
    p.add_argument("--max-episodes", type=int, default=0, help="benchmark: stop after N episodes (no release)")
    p.add_argument("--save-grad", help="benchmark: write the accumulated gradient here")''')
t = rep(t, '''    if args.tree:
        summary["tree"] = tree_forward.install_tree_patches(policy)
    stamp("installs done")''', '''    if args.tree:
        summary["tree"] = tree_forward.install_tree_patches(policy)
    import speedpatch
    speed_flags = [f for f in args.speed.split(",") if f]
    summary["speed"] = speedpatch.install(policy, speed_flags)
    if "stack" in speed_flags:
        speedpatch.wrap_update(GeneralUpdate)
    stamp(f"installs done (speed {speed_flags})")''')
t = rep(t, '''    if args.resume:
        update.resume(args.resume, args.resume_sha256)
    stamp("GeneralUpdate ready")''', '''    if args.resume:
        update.resume(args.resume, args.resume_sha256)
    if "stack" in speed_flags:
        speedpatch.refresh()   # the stacked expert leaves take the start adapter's values
    stamp("GeneralUpdate ready")''')
t = rep(t, '''            stats = update.accumulate_episode_tree(g, i, lambda turns: tree_forward.EpisodeTree(policy, turns, layer_devices=devices))
            tree_stats.append(dict(group=g, episode=i, **stats))
            stamp(f"episode g{g} e{i} done: {stats['per_turn_tokens']} per-turn tokens -> {stats['computed_tokens']} computed")''',
        '''            t_ep = time.time()
            stats = update.accumulate_episode_tree(g, i, lambda turns: tree_forward.EpisodeTree(policy, turns, layer_devices=devices))
            tree_stats.append(dict(group=g, episode=i, seconds=time.time() - t_ep, **stats))
            stamp(f"episode g{g} e{i} done: {stats['per_turn_tokens']} per-turn tokens -> {stats['computed_tokens']} computed ({time.time() - t_ep:.0f}s)")
            if args.max_episodes and len(tree_stats) >= args.max_episodes:
                if "stack" in speed_flags:
                    speedpatch.flush()
                if args.save_grad:
                    torch.save({n: (None if q.grad is None else q.grad.detach().cpu()) for n, q in policy.named_parameters() if q.requires_grad}, args.save_grad)
                peak = [torch.cuda.max_memory_allocated(d) / 2**30 for d in range(torch.cuda.device_count())]
                print(json.dumps({"bench": True, "speed": speed_flags, "episodes": tree_stats, "peak_gib": peak}), flush=True)
                return''')
r.write_text(t)
print("patched fast19")
