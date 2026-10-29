"""fast17 -> fast18 (2026-10-26, user "1번으로 하자"): one optimizer step per N completed active groups (--groups-per-step)."""
from pathlib import Path

p = Path("general_update.py"); s = p.read_text()
r = Path("general_update_runner.py"); t = r.read_text()


def rep(text, old, new):
    assert text.count(old) == 1, old[:80]
    return text.replace(old, new)


s = rep(s, '''Gradient prefixes cannot resume across a step and are not saved.
"""''', '''Gradient prefixes cannot resume across a step and are not saved.

2026-10-26 (user, fast18): step_groups(gs) steps once on the summed gradient of several completed groups (runner
--groups-per-step N), so each step averages more cases; if any group of the batch fails its drift gate the whole step is
dropped (the gradients are already summed and cannot be split).
"""''')
s = rep(s, '''    def step_group(self, g):''', '''    def step_groups(self, gs):
        """One AdamW step on the summed gradient of completed groups gs, each gated on its own drift report first."""
        gs = list(gs)
        if not self.per_group_steps or not gs or any(not self.group_done(g) or g in self.group_gate for g in gs):
            raise ValueError("step_groups needs per_group_steps and complete, not yet stepped groups")
        gates = {}
        for g in gs:
            report = self._group_report(g)
            gate = self._gate(report)
            gate["report"] = report
            gates[g] = gate
        passed = all(gt["passed"] for gt in gates.values())
        ordered = sorted(self.parameters)
        params = [self.parameters[n] for n in ordered]
        norm = None
        if passed:
            norm = torch.nn.utils.clip_grad_norm_(params, self.training["max_grad_norm"], error_if_nonfinite=True)
            self._optimizer_for(params, ordered).step()
            self.steps += 1
        for g, gt in gates.items():
            gt["step"] = self.steps if passed else None
            gt["step_groups"] = gs
            if not passed:
                gt["passed"] = False
            self.group_gate[g] = gt
            if passed:
                self.group_norms[g] = float(norm)
        for q in params:
            q.grad = None
        return passed

    def step_group(self, g):''')
p.write_text(s)

t = rep(t, '''    p.add_argument("--per-group-steps", action="store_true", help="2026-10-24 (fast17): one optimizer step per active group")''',
        '''    p.add_argument("--per-group-steps", action="store_true", help="2026-10-24 (fast17): one optimizer step per active group")
    p.add_argument("--groups-per-step", type=int, default=1, help="2026-10-26 (fast18): with --per-group-steps, step every N completed groups")''')
t = rep(t, '''            if args.per_group_steps:   # 2026-10-24: step at each group's end; prefixes cannot resume across a step
                if update.group_done(g):
                    passed = update.step_group(g)
                    stamp(f"group g{g} {'stepped' if passed else 'dropped by drift gate'} (steps {update.steps})")
                    summary["group_steps"] = {str(k): {kk: vv for kk, vv in v.items() if kk != "report"} for k, v in update.group_gate.items()}
                continue''', '''            if args.per_group_steps:   # 2026-10-24: step at group ends (2026-10-26: every N groups); no prefixes across a step
                if update.group_done(g):
                    pending_groups.append(g)
                    last_group = g == update.identity["active_groups"][-1]
                    if len(pending_groups) >= args.groups_per_step or last_group:
                        passed = update.step_groups(pending_groups)
                        stamp(f"groups {pending_groups} {'stepped' if passed else 'dropped by drift gate'} (steps {update.steps})")
                        pending_groups = []
                        summary["group_steps"] = {str(k): {kk: vv for kk, vv in v.items() if kk != "report"} for k, v in update.group_gate.items()}
                continue''')
t = rep(t, '''        tree_stats = []
''', '''        tree_stats = []
        pending_groups = []
''')
r.write_text(t)
print("patched fast18")
