"""Per-flag summary (2026-10-27): time, peak memory and per-family agreement of each variant vs base2, next to base vs base2
(nondeterminism floor). Files in /home/work/models/bench-recon-20261027/."""
import json
import math
from pathlib import Path

import torch

B = Path("/home/work/models/bench-recon-20261027")
REF = "base2"


def bench(name):
    for line in (B / f"log-{name}.txt").read_text().splitlines():
        if line.startswith('{"bench"'):
            return json.loads(line)
    return None


def family(key):
    for tag in (".mlp.experts.", ".shared_expert.", ".linear_attn.", ".self_attn."):
        if tag in key:
            return tag.strip(".")
    return "other"


def agree(kind, name):
    a, b = torch.load(B / f"{kind}-{REF}.pt"), torch.load(B / f"{kind}-{name}.pt")
    if set(a) != set(b):
        return "KEY MISMATCH"
    worst_cos, worst_rel = 1.0, 0.0
    fam = {}
    for k in a:
        x, y = a[k], b[k]
        if (x is None) != (y is None):
            return f"PRESENCE MISMATCH {k}"
        if x is None:
            continue
        x, y = x.double(), y.double()
        if not (torch.isfinite(x).all() and torch.isfinite(y).all()):
            return f"NON-FINITE {k}"
        f = fam.setdefault(family(k), [0.0, 0.0, 0.0, 0.0])
        f[0] += float((x * y).sum()); f[1] += float((x * x).sum()); f[2] += float((y * y).sum()); f[3] += float(((x - y) ** 2).sum())
    for dot, nx, ny, nd in fam.values():
        worst_cos = min(worst_cos, dot / math.sqrt(nx * ny)); worst_rel = max(worst_rel, math.sqrt(nd / nx))
    return f"min cos {worst_cos:.4f} max rel {worst_rel:.2e}"


rows = []
for name in ["base", "stack", "chunk", "empty", "gpustore", "nostore", "all"]:
    if not (B / f"log-{name}.txt").exists() or bench(name) is None:
        continue
    d = bench(name)
    secs = [round(e["seconds"]) for e in d["episodes"]]
    peak = [round(x, 1) for x in d["peak_gib"]]
    p = agree("params", name) if (B / f"params-{name}.pt").exists() else "-"
    g = agree("grad", name) if (B / f"grad-{name}.pt").exists() else "-"
    print(f"{name:9s} seconds {secs} peak {peak} | params vs {REF}: {p} | grad vs {REF}: {g}")
ref = bench(REF)
print(f"{REF:9s} seconds {[round(e['seconds']) for e in ref['episodes']]} peak {[round(x, 1) for x in ref['peak_gib']]} (reference)")
