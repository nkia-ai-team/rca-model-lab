"""Recompute (chars not shared with the previous prompt) and max view size under the real rolling_view policies."""
import json, sys
from rca_lab.mcp.context import rolling_view

def render(view):
    return "\x01".join(m.get("role", "") + "\x00" + str(m.get("content", "")) + json.dumps(m.get("tool_calls", ""), ensure_ascii=False) for m in view)

def lcp(a, b):
    lo, hi = 0, min(len(a), len(b))
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if a[:mid] == b[:mid]: lo = mid
        else: hi = mid - 1
    return lo

