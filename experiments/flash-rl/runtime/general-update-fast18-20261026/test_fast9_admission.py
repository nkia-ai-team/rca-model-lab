import json, os, sys
sys.path.insert(0, os.path.abspath("src")); sys.path.insert(0, os.path.abspath("."))
import torch
from general_update import GeneralUpdate
class P(torch.nn.Module):
    def __init__(self):
        super().__init__(); self.a = torch.nn.Module(); self.a.lora_A = torch.nn.Parameter(torch.zeros(2, 3)); self.a.lora_B = torch.nn.Parameter(torch.zeros(3, 2))
pol = P()
groups = [open(f"/home/work/flash-rl-loop-20261015/u002/groups/{f}", "rb").read() for f in sorted(os.listdir("/home/work/flash-rl-loop-20261015/u002/groups"))]
start = {"state_dict": {"a.lora_A": torch.zeros(2, 3), "a.lora_B": torch.ones(3, 2) * 1e-3}, "optimizer_steps": 2, "parent_policy_version": "adapter-15e40ca8f60b3295"}
u = GeneralUpdate(pol, groups, {"t": 1}, {"t": 1}, policy_version="adapter-NEWPOLICY", start_state=start, training=dict(learning_rate=5e-5))
print("admitted stale groups:", u.identity["group_behavior_policy_versions"], u.identity["group_stale_steps"], "turns", len(u.order))
try:
    GeneralUpdate(pol, groups, {"t": 1}, {"t": 1}, policy_version="adapter-NEWPOLICY", start_state={**start, "parent_policy_version": "adapter-OTHER"}, training=dict(learning_rate=5e-5))
    print("ERROR: unrelated parent admitted")
except ValueError as e:
    print("rejected as expected:", str(e)[:90])
