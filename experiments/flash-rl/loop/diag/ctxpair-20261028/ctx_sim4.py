"""A = every tool result of the latest assistant response stays whole; all earlier results compacted (legacy head-slice)."""
import json, sys
from rca_lab.mcp.context import rolling_view, _compacted, OLD_CHARS
from ctx_sim2_lib import render, lcp

def view_A(msgs):
    items = rolling_view(msgs, recent_full=10**6)  # strip reasoning, history tool_calls envelope, nothing compacted
    last = max((k for k, m in enumerate(items) if m.get("role") == "assistant"), default=-1)
    for k, m in enumerate(items):
        if m.get("role") == "tool" and k < last:
            c = str(m.get("content", ""))
            if len(c) > OLD_CHARS: m["content"] = _compacted(c, OLD_CHARS)
    return items

arms = {"rolling8": lambda m: rolling_view(m), "budget140k": lambda m: rolling_view(m, policy="budget"), "A_lastresp": view_A}
tot = {a: [0, 0, []] for a in arms}
for f in sys.argv[1:]:
    msgs = [json.loads(l) for l in open(f)]
    ends = [k for k, m in enumerate(msgs) if m.get("role") == "assistant"]
    for a, fn in arms.items():
        prev = None; rec = mx = br = 0
        for k in ends:
            t = render(fn(msgs[:k])); l = lcp(t, prev) if prev else 0
            br += bool(prev) and l < len(prev); rec += len(t) - l; mx = max(mx, len(t)); prev = t
        tot[a][0] += rec; tot[a][1] += br; tot[a][2].append(mx)
base = tot["rolling8"][0]
for a, (r, b, mx) in tot.items():
    mx.sort()
    print(f"{a:12s} recompute {r/base:.2f}x  breaks {b:5d}  max view p50 {mx[len(mx)//2]:,} p95 {mx[int(.95*(len(mx)-1))]:,} max {mx[-1]:,}")
