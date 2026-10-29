"""CPU logic test of fast17 per-group steps with real loop groups and a tiny LoRA-named stand-in policy."""
import json, sys, tempfile
from pathlib import Path
import torch

RT = Path("/home/work/flash-rl-quant-probe-20261014/general-update-fast17-20261024")
sys.path.insert(0, str(RT)); sys.path.insert(0, str(RT / "src"))
import general_update as gu

groups = [Path(p).read_bytes() for p in sys.argv[1:3]]
behavior = json.loads(groups[0])["policy_version"]


class Tiny(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.a = torch.nn.ParameterDict({"lora_A": torch.nn.Parameter(torch.randn(2, 3) * .1),
                                         "lora_B": torch.nn.Parameter(torch.zeros(3, 2))})


policy = Tiny()
names = dict(policy.named_parameters())
print("params", list(names))


def current_for(turn):
    old = torch.tensor(turn["old_logprobs"])[None]
    B = policy.a["lora_B"]; A = policy.a["lora_A"]
    tie = (B @ A).sum()
    return old + (tie - tie.detach())   # value = behavior logprobs (ratio 1), gradient flows into B


gu.require_matching_profile = lambda identity, profile: None
up = gu.GeneralUpdate(policy, groups, {"p": 1}, {"b": 1}, policy_version=behavior, start_state=None,
                      training=dict(learning_rate=1e-3), per_group_steps=True)
print("active groups", up.identity["active_groups"], "turns", len(up.order))
B0 = policy.a["lora_B"].detach().clone()
seen = []
for g, i, j in up.order:
    up.accumulate(g, i, j, current_for(up.episode(g, i)["turns"][j]))
    if up.group_done(g) and g not in up.group_gate:
        passed = up.step_group(g)
        seen.append((g, passed, float(policy.a["lora_B"].detach().abs().sum())))
        assert all(p.grad is None for p in names.values()), "grads must be cleared after a group step"
print("group steps", seen)
assert len(seen) == len(up.identity["active_groups"]) and up.steps == sum(p for _, p, _ in seen)
assert seen[0][2] > 0, "first group step must move B"
with tempfile.TemporaryDirectory() as d:
    res = up.release(d, None)
    saved = torch.load(Path(d) / "adapter.pt", weights_only=False)
print("release", res["status"], res["optimizer_steps"], res["steps_this_update"], res["failed_groups"], res["preclip_gradient_norms"])
assert res["status"] == "passed" and res["optimizer_steps"] == up.steps and saved["optimizer_steps"] == up.steps
assert not torch.equal(saved["state_dict"]["a.lora_B"], B0)
assert len(saved["optimizer"]["state"]["state"]) > 0
# a second update resumes from the stepped adapter + optimizer state
up2 = gu.GeneralUpdate(policy, groups, {"p": 1}, {"b": 1}, policy_version=behavior, start_state=saved,
                       training=dict(learning_rate=1e-3), per_group_steps=True)
assert up2.start_step == up.steps
# resume guard in the classic path still blocks a changed adapter
up3 = gu.GeneralUpdate(policy, groups, {"p": 1}, {"b": 1}, policy_version=behavior, start_state=saved, training=dict(learning_rate=1e-3))
for g, i, j in up3.order:
    up3.accumulate(g, i, j, current_for(up3.episode(g, i)["turns"][j]))
r3 = up3.release(tempfile.mkdtemp(), None)
print("classic path still works:", r3["status"], r3["optimizer_steps"])
print("ALL OK")
