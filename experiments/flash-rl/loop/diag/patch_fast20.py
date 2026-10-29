"""fast19 -> fast20 (2026-10-27, user "muon도 쓰는게좋겠는데"): selectable optimizer for per-group steps.
--optimizer adamw (default, unchanged) | muon (MuonLoRA on every LoRA factor) | hybrid (Muon where min(shape) >= --muon-min-dim,
AdamW for thinner factors). The saved optimizer state records its kind; a start state of another kind is not loaded (fresh
moments, recorded as optimizer_state_restored=False). The configured lr is re-applied after loading, as for AdamW.
Requires muon_lora.py next to the runner."""
from pathlib import Path

p = Path("general_update.py"); s = p.read_text()
r = Path("general_update_runner.py"); t = r.read_text()


def rep(text, old, new):
    assert text.count(old) == 1, old[:80]
    return text.replace(old, new)


s = rep(s, '''                 gradient_scopes=None, per_group_steps=False):''', '''                 gradient_scopes=None, per_group_steps=False, optimizer_kind="adamw", muon_min_dim=4):''')
s = rep(s, '''        self.per_group_steps = bool(per_group_steps)
        self.identity["per_group_steps"] = self.per_group_steps''', '''        self.per_group_steps = bool(per_group_steps)
        self.identity["per_group_steps"] = self.per_group_steps
        if optimizer_kind not in ("adamw", "muon", "hybrid"):
            raise ValueError("optimizer_kind must be adamw, muon or hybrid")
        if optimizer_kind != "adamw" and not self.per_group_steps:
            raise ValueError("muon/hybrid optimizers are wired for per-group steps only")
        self.optimizer_kind, self.muon_min_dim, self.optimizer_state_restored = optimizer_kind, int(muon_min_dim), None
        self.identity["optimizer_kind"] = optimizer_kind
        if optimizer_kind == "hybrid":
            self.identity["muon_min_dim"] = self.muon_min_dim''')
s = rep(s, '''    def _optimizer_for(self, params, ordered):
        if self._optimizer is None:
            self._optimizer = torch.optim.AdamW(params, lr=self.training["learning_rate"], weight_decay=0.,
                                                betas=(self.training["adam_beta1"], self.training["adam_beta2"]), eps=self.training["adam_eps"])
            if self.start_optimizer is not None:
                if self.start_optimizer.get("names") != ordered:
                    raise ValueError("Optimizer state parameter order mismatch")
                self._optimizer.load_state_dict(self.start_optimizer["state"])''', '''    def _optimizer_for(self, params, ordered):
        if self._optimizer is None:
            lr, betas, eps = self.training["learning_rate"], (self.training["adam_beta1"], self.training["adam_beta2"]), self.training["adam_eps"]
            if self.optimizer_kind == "adamw":
                self._optimizer = torch.optim.AdamW(params, lr=lr, weight_decay=0., betas=betas, eps=eps)
            else:
                import muon_lora
                self._optimizer = (muon_lora.MuonLoRA(params, lr=lr) if self.optimizer_kind == "muon" else
                                   muon_lora.HybridMuonAdamW(params, lr=lr, min_dim=self.muon_min_dim, adam_betas=betas, adam_eps=eps))
            saved_kind = (self.start_optimizer or {}).get("kind", "adamw")
            self.optimizer_state_restored = False
            if self.start_optimizer is not None and saved_kind == self.optimizer_kind:
                if self.start_optimizer.get("names") != ordered:
                    raise ValueError("Optimizer state parameter order mismatch")
                self._optimizer.load_state_dict(self.start_optimizer["state"])
                self.optimizer_state_restored = True''')
s = rep(s, '''        state = dict(state_dict=updated, optimizer=dict(names=ordered, state=self._optimizer.state_dict()),
                     optimizer_steps=self.start_step + self.steps, parent_policy_version=self.policy_version, identity=self.identity)
        output_dir = Path(output_dir)''', '''        state = dict(state_dict=updated, optimizer=dict(names=ordered, state=self._optimizer.state_dict(), kind=self.optimizer_kind),
                     optimizer_steps=self.start_step + self.steps, parent_policy_version=self.policy_version, identity=self.identity)
        output_dir = Path(output_dir)''')
s = rep(s, '''                      effective_learning_rate=self._optimizer.param_groups[0]["lr"],''', '''                      effective_learning_rate=self._optimizer.param_groups[0]["lr"], optimizer_kind=self.optimizer_kind,
                      optimizer_state_restored=self.optimizer_state_restored,''')
p.write_text(s)

t = rep(t, '''    p.add_argument("--speed", default="",''', '''    p.add_argument("--optimizer", default="adamw", choices=("adamw", "muon", "hybrid"), help="2026-10-27 (fast20)")
    p.add_argument("--muon-min-dim", type=int, default=4, help="hybrid: factors thinner than this stay on AdamW")
    p.add_argument("--speed", default="",''')
t = rep(t, '''gradient_scopes=scopes, per_group_steps=args.per_group_steps)''', '''gradient_scopes=scopes, per_group_steps=args.per_group_steps,
                           optimizer_kind=args.optimizer, muon_min_dim=args.muon_min_dim)''')
r.write_text(t)
print("patched fast20")
