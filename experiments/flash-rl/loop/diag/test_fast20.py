"""CPU logic test of fast20 optimizer selection with real loop groups and a tiny LoRA-named stand-in policy."""
import json, sys, tempfile
from pathlib import Path
import torch

RT = Path(sys.argv[1]); sys.path.insert(0, str(RT)); sys.path.insert(0, str(RT / "src"))
import general_update as gu

groups = [Path(p).read_bytes() for p in sys.argv[2:4]]
behavior = json.loads(groups[0])["policy_version"]
gu.require_matching_profile = lambda identity, profile: None


class Tiny(torch.nn.Module):
    def __init__(self):
        super().__init__()
        torch.manual_seed(0)
        self.a = torch.nn.ParameterDict({"lora_A": torch.nn.Parameter(torch.randn(16, 32) * .1),
                                         "lora_B": torch.nn.Parameter(torch.zeros(24, 16))})
        self.b = torch.nn.ParameterDict({"lora_A": torch.nn.Parameter(torch.randn(2, 32) * .1),
                                         "lora_B": torch.nn.Parameter(torch.zeros(24, 2))})


def run(kind, start=None, lr=1e-3):
    policy = Tiny()
    if start is not None:
        with torch.no_grad():
            for n, q in policy.named_parameters():
                q.copy_(start["state_dict"][n])
    up = gu.GeneralUpdate(policy, groups, {"p": 1}, {"b": 1}, policy_version=behavior, start_state=start,
                          training=dict(learning_rate=lr), per_group_steps=True, optimizer_kind=kind, muon_min_dim=4)
    def cur(turn):
        old = torch.tensor(turn["old_logprobs"])[None]
        tie = sum((m["lora_B"] @ m["lora_A"]).sum() for m in (policy.a, policy.b))
        return old + (tie - tie.detach())
    for g, i, j in up.order:
        up.accumulate(g, i, j, cur(up.episode(g, i)["turns"][j]))
        if up.group_done(g) and g not in up.group_gate:
            up.step_groups([g])
    d = tempfile.mkdtemp()
    res = up.release(d, None)
    saved = torch.load(Path(d) / "adapter.pt", weights_only=False)
    return res, saved, up


# AdamW start state (as the loop has now), then switch to muon: fresh moments, lr as configured
res_a, saved_a, _ = run("adamw")
print("adamw", res_a["status"], res_a["optimizer_kind"], res_a["optimizer_state_restored"], res_a["effective_learning_rate"])
res_m, saved_m, up_m = run("muon", start=saved_a, lr=2e-3)
print("muon from adamw state", res_m["status"], res_m["optimizer_state_restored"], res_m["effective_learning_rate"], saved_m["optimizer"]["kind"])
assert res_m["optimizer_state_restored"] is False and res_m["effective_learning_rate"] == 2e-3 and saved_m["optimizer"]["kind"] == "muon"
moved = max(float((saved_m["state_dict"][k] - saved_a["state_dict"][k]).abs().max()) for k in saved_a["state_dict"])
print("muon moved params by", moved); assert moved > 0
res_m2, saved_m2, _ = run("muon", start=saved_m, lr=3e-3)
print("muon from muon state", res_m2["optimizer_state_restored"], res_m2["effective_learning_rate"])
assert res_m2["optimizer_state_restored"] is True and res_m2["effective_learning_rate"] == 3e-3
res_h, saved_h, up_h = run("hybrid", start=saved_a)
print("hybrid muon params", len(up_h._optimizer.muon_params), "adam params", len(up_h._optimizer.adam_params), res_h["status"])
assert len(up_h._optimizer.muon_params) == 2 and len(up_h._optimizer.adam_params) == 2
res_h2, _, _ = run("hybrid", start=saved_h)
assert res_h2["optimizer_state_restored"] is True
print("ALL OK")
