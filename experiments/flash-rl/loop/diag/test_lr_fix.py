"""CPU check: with a start optimizer saved at lr 5e-5, a per-group-steps update configured at 1e-3 must step at 1e-3."""
import json, sys, tempfile
from pathlib import Path
import torch

RT = Path(sys.argv[1])
sys.path.insert(0, str(RT)); sys.path.insert(0, str(RT / "src"))
import general_update as gu

groups = [Path(p).read_bytes() for p in sys.argv[2:4]]
behavior = json.loads(groups[0])["policy_version"]


class Tiny(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.a = torch.nn.ParameterDict({"lora_A": torch.nn.Parameter(torch.randn(2, 3) * .1),
                                         "lora_B": torch.nn.Parameter(torch.zeros(3, 2))})


policy = Tiny()
names = sorted(n for n, _ in policy.named_parameters())
old_opt = torch.optim.AdamW([dict(policy.named_parameters())[n] for n in names], lr=5e-5)
start = {"state_dict": {n: q.detach().clone() for n, q in policy.named_parameters()},
         "optimizer": {"names": names, "state": old_opt.state_dict()}, "optimizer_steps": 7, "parent_policy_version": behavior}
gu.require_matching_profile = lambda identity, profile: None
for mode in (True, False):
    up = gu.GeneralUpdate(policy, groups, {"p": 1}, {"b": 1}, policy_version=behavior, start_state=start,
                          training=dict(learning_rate=1e-3), per_group_steps=mode)
    def cur(turn):
        old = torch.tensor(turn["old_logprobs"])[None]
        tie = (policy.a["lora_B"] @ policy.a["lora_A"]).sum()
        return old + (tie - tie.detach())
    for g, i, j in up.order:
        up.accumulate(g, i, j, cur(up.episode(g, i)["turns"][j]))
        if mode and up.group_done(g) and g not in up.group_gate:
            up.step_groups([g])
    if mode:
        print("per-group lr", up._optimizer.param_groups[0]["lr"]); assert up._optimizer.param_groups[0]["lr"] == 1e-3
        res = up.release(tempfile.mkdtemp(), None); print("effective_learning_rate", res["effective_learning_rate"])
    else:
        res = up.release(tempfile.mkdtemp(), None)
        saved = torch.load(Path(tempfile.gettempdir()) / "x", weights_only=False) if False else None
        print("classic status", res["status"])
print("OK")
