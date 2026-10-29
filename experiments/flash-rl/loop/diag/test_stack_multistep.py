"""GPU micro-test (seconds) of speedpatch 'stack' over consecutive steps (2026-10-26, Codex review): a small expert bank,
original grouped_experts._lora vs speedpatch._lora_stacked — forward values, per-expert gradients after flush, three AdamW
steps with refresh, one rejected step (gradients cleared without stepping), and the saved parameters at the end."""
import sys
from pathlib import Path

import torch

RT = Path("/home/work/flash-rl-quant-probe-20261014/general-update-fast19-20261026")
sys.path.insert(0, str(RT))
import grouped_experts as ge
import speedpatch as sp

torch.manual_seed(0)
dev = torch.device("cuda:0")
E, ni, no, r = 16, 64, 48, 2


class Lin(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.lora_A = torch.nn.Parameter(torch.randn(r, ni, device=dev) * .05)
        self.lora_B = torch.nn.Parameter(torch.randn(no, r, device=dev) * .05)
        self.scale = 1.0


class Bank(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.experts = torch.nn.ModuleList([torch.nn.ModuleDict({p: Lin() for p in ge.PROJECTIONS}) for _ in range(E)])


def make():
    torch.manual_seed(1)
    return Bank()


def run(stacked):
    bank = make()
    params = [q for q in bank.parameters()]
    opt = torch.optim.AdamW(params, lr=1e-2, weight_decay=0.)
    original = ge._lora
    if stacked:
        sp._STACKS.clear()
        layer = type("L", (), {})(); layer.mlp = type("M", (), {})(); layer.mlp.experts = bank
        policy = type("P", (), {})(); policy.backbone = type("B", (), {})(); policy.backbone.layers = [layer]
        sp._install_stack(policy)
    outs, grads = [], []
    try:
        for step in range(4):
            torch.manual_seed(100 + step)
            counts = torch.randint(1, 6, (E,))
            offs = torch.cumsum(counts, 0).to(torch.int32).to(dev)
            xs = torch.randn(int(counts.sum()), ni, device=dev, dtype=torch.bfloat16)
            fn = sp._lora_stacked if stacked else original
            y = fn(bank, "gate_proj", xs, offs, 0, E)
            loss = (y.float() ** 2).mean()
            loss.backward()
            if stacked:
                sp.flush()
            outs.append(y.detach().float().cpu())
            grads.append(torch.cat([(q.grad if q.grad is not None else torch.zeros_like(q)).flatten() for q in params]).cpu())
            if step == 2:     # rejected step: clear gradients, no optimizer step
                for q in params:
                    q.grad = None
                continue
            opt.step()
            for q in params:
                q.grad = None
            if stacked:
                sp.refresh()
    finally:
        ge._lora = original
    return outs, grads, torch.cat([q.detach().flatten() for q in params]).cpu()


o0, g0, p0 = run(False)
o1, g1, p1 = run(True)
ok = True
for s in range(4):
    dy = float((o0[s] - o1[s]).abs().max()); dg = float((g0[s] - g1[s]).abs().max() / (g0[s].abs().max() + 1e-30))
    print(f"step {s}: max |dy| {dy:.3e}  max rel dgrad {dg:.3e}")
    ok &= dy < 1e-2 and dg < 1e-3
dp = float((p0 - p1).abs().max())
print(f"final params max |d| {dp:.3e}")
ok &= dp < 1e-5
print("STACK MULTISTEP", "OK" if ok else "MISMATCH")
