"""fast19 (2026-10-27 reconstruction check): --dump-rows PATH writes, at the --max-episodes exit, every accumulated turn's
mean(proximal - old) logprob so it can be compared with the drift report recorded by the original runtime (u063)."""
from pathlib import Path

r = Path("general_update_runner.py"); t = r.read_text()
old = '''    p.add_argument("--bench-step-after", type=int, default=0,'''
new = '''    p.add_argument("--dump-rows", help="reconstruction check: write per-turn mean(proximal - old) at the --max-episodes exit")
    p.add_argument("--bench-step-after", type=int, default=0,'''
assert t.count(old) == 1; t = t.replace(old, new)
old2 = '''            if args.max_episodes and len(tree_stats) >= args.max_episodes:'''
new2 = '''            if args.max_episodes and len(tree_stats) >= args.max_episodes and args.dump_rows:
                out_rows = []
                for row in update.rows:
                    turn = update.episode(row["group"], row["episode"])["turns"][row["turn"]]
                    prox = row["proximal"].reshape(-1).float()
                    old = torch.tensor(turn["old_logprobs"], dtype=torch.float32)
                    out_rows.append(dict(group=row["group"], episode=row["episode"], turn=row["turn"], tokens=prox.numel(),
                                         signed_mean=float((prox - old).mean())))
                Path(args.dump_rows).write_text(json.dumps(out_rows))
            if args.max_episodes and len(tree_stats) >= args.max_episodes:'''
assert t.count(old2) == 1; t = t.replace(old2, new2)
r.write_text(t)
print("patched dump-rows")
