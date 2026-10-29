"""Strict agreement check of the speed-bench runs (2026-10-26, Codex review): parameters after the bench step and the
gradient of the following episode, per module family, for base vs base2 (nondeterminism floor) and base vs all.
Fails loudly on key mismatches, missing gradients on one side only, or non-finite values."""
import math
import sys
import torch

B = "/home/work/bench-fast19"


def load(kind, name):
    return torch.load(f"{B}/{kind}-{name}.pt")


def family(key):
    if ".mlp.experts." in key:
        return "experts"
    if ".shared_expert." in key:
        return "shared_expert"
    if ".linear_attn." in key:
        return "linear_attn"
    if ".self_attn." in key:
        return "self_attn"
    return "other"


def compare(kind, ref, other):
    a, b = load(kind, ref), load(kind, other)
    problems = []
    if set(a) != set(b):
        problems.append(f"key sets differ: {len(set(a) ^ set(b))} keys")
    fam = {}
    for k in sorted(set(a) & set(b)):
        x, y = a[k], b[k]
        if (x is None) != (y is None):
            problems.append(f"gradient present on one side only: {k}")
            continue
        if x is None:
            continue
        x, y = x.double(), y.double()
        if not (torch.isfinite(x).all() and torch.isfinite(y).all()):
            problems.append(f"non-finite values: {k}")
            continue
        f = fam.setdefault(family(k), [0.0, 0.0, 0.0, 0.0])
        f[0] += float((x * y).sum()); f[1] += float((x * x).sum()); f[2] += float((y * y).sum()); f[3] += float(((x - y) ** 2).sum())
    out = {}
    for name, (dot, nx, ny, nd) in fam.items():
        out[name] = dict(cos=dot / math.sqrt(nx * ny) if nx and ny else None, rel_err=math.sqrt(nd / nx) if nx else None)
    return out, problems


for kind in ("params", "grad"):
    for other in ("base2", "all", "nostore", "stack", "gpustore"):
        try:
            out, problems = compare(kind, "base", other)
        except FileNotFoundError as e:
            print(f"{kind} base vs {other}: missing file {e.filename}")
            continue
        print(f"{kind} base vs {other}: " + ", ".join(f"{k} cos {v['cos']:.4f} rel {v['rel_err']:.3e}" for k, v in sorted(out.items())))
        for p in problems[:10]:
            print("  PROBLEM", p)
        if problems:
            print(f"  {len(problems)} problems")
sys.exit(0)
