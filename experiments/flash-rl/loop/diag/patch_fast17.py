from pathlib import Path

p = Path("general_update.py"); s = p.read_text()
r = Path("general_update_runner.py"); t = r.read_text()


def rep(text, old, new):
    assert text.count(old) == 1, old[:80]
    return text.replace(old, new)


s = rep(s, '''Rollout tokens/log-probs are immutable; no turns or tokens are dropped from an active episode.
"""''', '''Rollout tokens/log-probs are immutable; no turns or tokens are dropped from an active episode.

2026-10-24 (user, fast17): optional per-group steps. With per_group_steps the optimizer steps after each active group's
last episode (drift gate of that group first), so one update makes one step per active group at the same compute. Later
groups are scored by the already-stepped adapter: proximal = that adapter (same forward), the behavior ratio stays capped
(importance_cap) and the per-group gate measures it. Gradient prefixes cannot resume across a step and are not saved.
"""''')
s = rep(s, '''                 gradient_scopes=None):''', '''                 gradient_scopes=None, per_group_steps=False):''')
s = rep(s, '''        for p in self.parameters.values():
            p.grad = None
        self.rows, self.loss_total = [], 0.
''', '''        self.per_group_steps = bool(per_group_steps)
        self.identity["per_group_steps"] = self.per_group_steps
        for p in self.parameters.values():
            p.grad = None
        self.rows, self.loss_total = [], 0.
        self.steps, self.group_gate, self.group_norms, self._optimizer = 0, {}, {}, None
''')
s = rep(s, '''    def state(self):''', '''    def _optimizer_for(self, params, ordered):
        if self._optimizer is None:
            self._optimizer = torch.optim.AdamW(params, lr=self.training["learning_rate"], weight_decay=0.,
                                                betas=(self.training["adam_beta1"], self.training["adam_beta2"]), eps=self.training["adam_eps"])
            if self.start_optimizer is not None:
                if self.start_optimizer.get("names") != ordered:
                    raise ValueError("Optimizer state parameter order mismatch")
                self._optimizer.load_state_dict(self.start_optimizer["state"])
        return self._optimizer

    def group_done(self, g):
        """True once every turn of active group g has been accumulated."""
        done = {(r["group"], r["episode"], r["turn"]) for r in self.rows}
        return all(key in done for key in self.order if key[0] == g)

    def _group_report(self, g):
        item = self.groups[g]
        by_turn = {(r["group"], r["episode"], r["turn"]): r for r in self.rows if r["group"] == g}
        samples = [dict(episode_id=ep["episode_id"], turns=[dict(
            prompt_tokens=len(t["prompt_token_ids"]), completion_token_ids=t["completion_token_ids"],
            old_logprobs=t["old_logprobs"], proximal_logprobs=by_turn[(g, i, j)]["proximal"].reshape(-1).tolist())
            for j, t in enumerate(ep["turns"])]) for i, ep in enumerate(item["episodes"])]
        return grpo_drift.build_drift_report(
            samples, controls=None, expected_group_size=len(samples), importance_cap=self.training["importance_cap"],
            min_raw_ess=REVISED_BUDGET["min_raw_ess"], max_truncated_mass=REVISED_BUDGET["max_truncated_mass"])

    @staticmethod
    def _gate(r):
        eps = [e["summary"] for e in r["episodes"]]
        tokens = sum(e["tokens"] for e in eps)
        fraction = sum(e["truncated_mass"] for e in eps) / tokens
        alarms = [dict(episode=e2.get("episode_id"), raw_normalized_ess=e["raw_normalized_ess"],
                       truncated_fraction=e["truncated_mass"] / e["tokens"])
                  for e2, e in zip(r["episodes"], eps)
                  if e["raw_normalized_ess"] < REVISED_BUDGET["min_raw_ess"] or e["truncated_mass"] / e["tokens"] > REVISED_BUDGET["max_truncated_mass"]]
        return dict(group_truncated_fraction=fraction, passed=fraction <= REVISED_BUDGET["max_truncated_mass"], alarms=alarms)

    def step_group(self, g):
        """Per-group step: gate group g on its own drift report, then one AdamW step on its gradient (or drop it)."""
        if not self.per_group_steps or not self.group_done(g) or g in self.group_gate:
            raise ValueError("step_group needs per_group_steps and a complete, not yet stepped group")
        report = self._group_report(g)
        gate = self._gate(report)
        gate["report"] = report
        self.group_gate[g] = gate
        ordered = sorted(self.parameters)
        params = [self.parameters[n] for n in ordered]
        if gate["passed"]:
            norm = torch.nn.utils.clip_grad_norm_(params, self.training["max_grad_norm"], error_if_nonfinite=True)
            self._optimizer_for(params, ordered).step()
            self.steps += 1
            self.group_norms[g] = float(norm)
        for q in params:
            q.grad = None
        return gate["passed"]

    def _release_stepped(self, output_dir):
        """Release after per-group steps: the adapter after every passed group's step (blocked if none passed)."""
        if len(self.rows) != len(self.order) or set(self.group_gate) != set(self.identity["active_groups"]):
            raise ValueError("Every active group must be accumulated and gated before release")
        gate = {self.groups[g]["group"]["group_id"]: {k: v for k, v in r.items() if k != "report"} for g, r in self.group_gate.items()}
        reports = {self.groups[g]["group"]["group_id"]: r["report"] for g, r in self.group_gate.items()}
        if self.steps == 0:
            return dict(status="blocked_drift_budget", failed_groups=list(gate), gate=gate, reports=reports, optimizer_steps=self.start_step)
        updated = {n: q.detach().cpu().clone() for n, q in self.parameters.items()}
        if tensor_digest(updated) == self.initial_digest:
            raise ValueError("Optimizer did not change adapter values")
        ordered = sorted(self.parameters)
        state = dict(state_dict=updated, optimizer=dict(names=ordered, state=self._optimizer.state_dict()),
                     optimizer_steps=self.start_step + self.steps, parent_policy_version=self.policy_version, identity=self.identity)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        adapter_sha = atomic_save(state, output_dir / "adapter.pt")
        result = dict(status="passed", optimizer_steps=self.start_step + self.steps, steps_this_update=self.steps,
                      preclip_gradient_norm=max(self.group_norms.values()), preclip_gradient_norms=self.group_norms,
                      loss_total=self.loss_total, adapter_sha256=adapter_sha, policy_version="adapter-" + adapter_sha[:16],
                      active_groups=self.identity["active_groups"], gate=gate, reports=reports,
                      failed_groups=[gid for gid, g in gate.items() if not g["passed"]])
        (output_dir / "release-result.json").write_text(json.dumps(result, indent=2, default=str))
        return result

    def state(self):''')
s = rep(s, '''    def release(self, output_dir, current_profile):
        require_matching_profile(self.identity, current_profile)
''', '''    def release(self, output_dir, current_profile):
        require_matching_profile(self.identity, current_profile)
        if self.per_group_steps:
            return self._release_stepped(output_dir)
''')
p.write_text(s)

t = rep(t, '''    p.add_argument("--resume"); p.add_argument("--resume-sha256")''', '''    p.add_argument("--resume"); p.add_argument("--resume-sha256")
    p.add_argument("--per-group-steps", action="store_true", help="2026-10-24 (fast17): one optimizer step per active group")''')
t = rep(t, '''                           training=dict(learning_rate=args.learning_rate), gradient_scopes=scopes)
    if args.resume:''', '''                           training=dict(learning_rate=args.learning_rate), gradient_scopes=scopes, per_group_steps=args.per_group_steps)
    if args.resume and args.per_group_steps:
        raise ValueError("per-group steps change the adapter mid-update; gradient prefixes cannot be resumed")
    if args.resume:''')
t = rep(t, '''            summary["tree_episodes"] = tree_stats
            if len(update.rows) - last_saved >= args.checkpoint_every:''', '''            summary["tree_episodes"] = tree_stats
            if args.per_group_steps:   # 2026-10-24: step at each group's end; prefixes cannot resume across a step
                if update.group_done(g):
                    passed = update.step_group(g)
                    stamp(f"group g{g} {'stepped' if passed else 'dropped by drift gate'} (steps {update.steps})")
                    summary["group_steps"] = {str(k): {kk: vv for kk, vv in v.items() if kk != "report"} for k, v in update.group_gate.items()}
                continue
            if len(update.rows) - last_saved >= args.checkpoint_every:''')
r.write_text(t)
print("patched")
