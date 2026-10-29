"""CPU logic test of fast18 step_groups (2 groups per step) with real loop groups and a tiny LoRA-named stand-in policy."""
import json, sys, tempfile
from pathlib import Path
import torch

RT = Path("/home/work/flash-rl-quant-probe-20261014/general-update-fast18-20261026")
sys.path.insert(0, str(RT)); sys.path.insert(0, str(RT / "src"))
import general_update as gu

groups = [Path(p).read_bytes() for p in sys.argv[1:4]]
behavior = json.loads(groups[0])["policy_version"]


class Tiny(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.a = torch.nn.ParameterDict({"lora_A": torch.nn.Parameter(torch.randn(2, 3) * .1),
                                         "lora_B": torch.nn.Parameter(torch.zeros(3, 2))})


def run(per_step, fail_group=None):
    torch.manual_seed(0)
    policy = Tiny()

    def current_for(turn):
        old = torch.tensor(turn["old_logprobs"])[None]
        tie = (policy.a["lora_B"] @ policy.a["lora_A"]).sum()
        return old + (tie - tie.detach())

    gu.require_matching_profile = lambda identity, profile: None
    up = gu.GeneralUpdate(policy, groups, {"p": 1}, {"b": 1}, policy_version=behavior, training=dict(learning_rate=1e-3),
                          per_group_steps=True)
    if fail_group is not None:   # force a drift-gate failure on one group
        real = up._gate
        up._gate = lambda r, _g=[None]: real(r)
        orig = up._group_report
        def rep(g):
            r = orig(g); r["__fail"] = g == fail_group; return r
        up._group_report = rep
        up._gate = lambda r: {**real(r), "passed": not r.get("__fail")}
    pending, log = [], []
    active = up.identity["active_groups"]
    for g, i, j in up.order:
        up.accumulate(g, i, j, current_for(up.episode(g, i)["turns"][j]))
        if up.group_done(g) and g not in up.group_gate and g not in pending:
            pending.append(g)
            if len(pending) >= per_step or g == active[-1]:
                log.append((list(pending), up.step_groups(pending)))
                pending = []
    res = up.release(tempfile.mkdtemp(), None)
    return active, log, res


active, log, res = run(2)
print("active", active, "steps", log, "release", res["status"], res["steps_this_update"], res["failed_groups"])
assert [s for s, _ in log] == [[0, 1], [2]] and res["steps_this_update"] == 2
active, log, res = run(2, fail_group=1)
print("with g1 failing:", log, res["status"], res["steps_this_update"], res["failed_groups"])
assert log[0][1] is False and log[1][1] is True and res["steps_this_update"] == 1 and len(res["failed_groups"]) == 2
active, log, res = run(1)
print("per group:", log, res["steps_this_update"])
assert res["steps_this_update"] == 3
print("ALL OK")
