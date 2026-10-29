"""fast19: --direction-test (2026-10-26 learnability step 1): after the first active group is accumulated, score each
episode's last and middle turn twice (same full-forward path, no grad) before the step and once after it, then report
per-episode mean log-prob change against the group advantage. Stops without release."""
from pathlib import Path

r = Path("general_update_runner.py"); t = r.read_text()


def rep(text, old, new):
    assert text.count(old) == 1, old[:80]
    return text.replace(old, new)


t = rep(t, '''    p.add_argument("--save-grad", help="benchmark: write the accumulated gradient here")''',
        '''    p.add_argument("--save-grad", help="benchmark: write the accumulated gradient here")
    p.add_argument("--direction-test", action="store_true", help="step-1 diagnostic: log-prob change vs advantage after one group step")
    p.add_argument("--direction-lr-scale", type=float, default=1.0, help="direction test: multiply the step's learning rate")''')
t = rep(t, '''            if args.per_group_steps:   # 2026-10-24: step at group ends (2026-10-26: every N groups); no prefixes across a step
                if update.group_done(g):''', '''            if args.per_group_steps:   # 2026-10-24: step at group ends (2026-10-26: every N groups); no prefixes across a step
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
                    print(json.dumps({"direction_test": True, "group": g, "stepped": passed, "lr": update.training["learning_rate"],
                                      "speed": speed_flags, "corr_adv_delta": corr, "sign_agree": sum((x > 0) == (y > 0) for x, y in zip(xs, ys) if x != 0),
                                      "nonzero_adv": sum(1 for x in xs if x != 0), "episodes": rows_out, "seconds": time.time() - t_dir}), flush=True)
                    return
                if update.group_done(g):''')
r.write_text(t)
print("patched direction test")
