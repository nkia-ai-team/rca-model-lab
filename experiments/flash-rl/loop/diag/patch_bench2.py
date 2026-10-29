"""fast19 bench/diagnostic upgrades (2026-10-26, Codex review):
- --bench-step-after N: after N benchmark episodes take a real optimizer step on the accumulated gradient (flush/refresh of
  stacked expert leaves exercised), save the parameters, then keep accumulating; --max-episodes still ends the run.
- direction test: optimizer param_group settings actually used, tree-path vs full-forward agreement on the scored turns,
  and the token-weighted surrogate change sum_i A_i * dlogp_i."""
from pathlib import Path

r = Path("general_update_runner.py"); t = r.read_text()


def rep(text, old, new):
    assert text.count(old) == 1, old[:80]
    return text.replace(old, new)


t = rep(t, '''    p.add_argument("--direction-test", action="store_true",''', '''    p.add_argument("--bench-step-after", type=int, default=0, help="benchmark: optimizer step after N episodes, then continue")
    p.add_argument("--save-params", help="benchmark: write parameters after the bench step here")
    p.add_argument("--direction-test", action="store_true",''')
t = rep(t, '''            if args.max_episodes and len(tree_stats) >= args.max_episodes:''', '''            if args.bench_step_after and len(tree_stats) == args.bench_step_after:
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
            if args.max_episodes and len(tree_stats) >= args.max_episodes:''')
t = rep(t, '''                    passed = update.step_groups([g])
                    after = score_all()''', '''                    passed = update.step_groups([g])
                    after = score_all()
                    g0 = update._optimizer.param_groups[0] if update._optimizer is not None else {}
                    one = next(iter(update._optimizer.state.values()), {}) if update._optimizer is not None else {}
                    opt_info = dict(lr=g0.get("lr"), betas=g0.get("betas"), eps=g0.get("eps"), weight_decay=g0.get("weight_decay"),
                                    state_step=float(one["step"]) if "step" in one else None, start_optimizer_steps=update.start_step)
                    prox = {(r_["episode"], r_["turn"]): r_["proximal"].reshape(-1).float() for r_ in update.rows if r_["group"] == g}
                    tree_vs_full = [float((prox[k] - before1[k].reshape(-1)).abs().mean()) for k in picks if k in prox]
                    tree_vs_full_signed = [float((prox[k] - before1[k].reshape(-1)).mean()) for k in picks if k in prox]''')
t = rep(t, '''                    corr = statistics.correlation(xs, ys) if len(set(xs)) > 1 and len(set(ys)) > 1 else None''',
        '''                    corr = statistics.correlation(xs, ys) if len(set(xs)) > 1 and len(set(ys)) > 1 else None
                    total_tokens = sum(r_["tokens"] for r_ in rows_out)
                    surrogate = sum(r_["advantage"] * r_["delta_mean"] * r_["tokens"] for r_ in rows_out) / total_tokens
                    surrogate_noise = sum(r_["advantage"] * r_["repeat_delta_mean"] * r_["tokens"] for r_ in rows_out) / total_tokens''')
t = rep(t, '''"corr_adv_delta": corr,''', '''"corr_adv_delta": corr, "surrogate_change": surrogate, "surrogate_repeat_noise": surrogate_noise,
                                      "optimizer": opt_info, "tree_vs_full_abs": tree_vs_full, "tree_vs_full_signed": tree_vs_full_signed,''')
r.write_text(t)
print("patched bench2")
