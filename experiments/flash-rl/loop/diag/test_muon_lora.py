"""CPU tests for diag/muon_lora.py."""
import copy
import math
import sys
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from muon_lora import HybridMuonAdamW, MuonLoRA, newton_schulz5

torch.manual_seed(0)

# 1. NS5 approximately orthogonalises: singular values of the result lie in a band around 1 (quintic NS is not exact)
for shape in [(16, 64), (64, 16), (2, 2048), (48, 2)]:
    g = torch.randn(*shape)
    s = torch.linalg.svdvals(newton_schulz5(g))
    print("ns5", shape, "singular values min/max", round(float(s.min()), 3), round(float(s.max()), 3))
    assert 0.5 < float(s.min()) and float(s.max()) < 1.3, shape

# 2. batched == per-matrix
b = torch.randn(8, 2, 64)
assert torch.allclose(newton_schulz5(b), torch.stack([newton_schulz5(x) for x in b]), atol=1e-5)

# 3. update RMS ~ rms_scale (0.2) * lr for non-degenerate shapes
p = torch.nn.Parameter(torch.zeros(64, 128))
opt = MuonLoRA([p], lr=1.0, momentum=0.0, nesterov=False)
p.grad = torch.randn(64, 128)
opt.step()
rms = float(p.detach().pow(2).mean().sqrt())
print("update rms (lr=1)", round(rms, 4))
assert 0.1 < rms < 0.3

# 4. grouping by shape + multiple steps + state_dict round trip reproduces the same trajectory
def make():
    torch.manual_seed(1)
    return [torch.nn.Parameter(torch.randn(2, 32) * .1) for _ in range(5)] + [torch.nn.Parameter(torch.randn(24, 2) * .1) for _ in range(3)]

def run(params, opt, steps, seed=7):
    torch.manual_seed(seed)
    for _ in range(steps):
        for q in params:
            q.grad = torch.randn_like(q)
        opt.step()

pa = make(); oa = MuonLoRA(pa, lr=1e-2); run(pa, oa, 4)
pb = make(); ob = MuonLoRA(pb, lr=1e-2); run(pb, ob, 2)
sd = copy.deepcopy(ob.state_dict()); vals = [q.detach().clone() for q in pb]
pc = [torch.nn.Parameter(v) for v in vals]; oc = MuonLoRA(pc, lr=1e-2); oc.load_state_dict(sd)
torch.manual_seed(7)
for _ in range(2):   # replay the first two gradient draws to keep the RNG aligned, then two more steps
    for q in pc:
        torch.randn_like(q)
for _ in range(2):
    for q in pc:
        q.grad = torch.randn_like(q)
    oc.step()
diff = max(float((x - y).abs().max()) for x, y in zip(pa, pc))
print("resume vs continuous max diff", diff)
assert diff < 1e-6

# 5. lr re-applied after load (the loop's fix for AdamW applies here too)
for g in oc.param_groups:
    g["lr"] = 5e-3
assert oc.param_groups[0]["lr"] == 5e-3

# 6. hybrid routing: rank-2 factors -> AdamW, others -> Muon
ph = [torch.nn.Parameter(torch.randn(2, 64)), torch.nn.Parameter(torch.randn(16, 64))]
h = HybridMuonAdamW(ph, lr=1e-3, min_dim=4)
assert len(h.adam_params) == 1 and len(h.muon_params) == 1
for q in ph:
    q.grad = torch.randn_like(q)
h.step(); sd = h.state_dict(); h.load_state_dict(sd)
print("ALL OK")
