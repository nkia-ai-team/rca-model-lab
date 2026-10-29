"""Per-case training variants (2026-10-21, user: collector-coverage variants + turn-budget variants).

For every Polestar train case with a joint-success rollout in the loop history:
  masks        collectors (masking.COVERAGE) whose absence still leaves decisive evidence on another path: the decisive
               items the successful answer cited (decisive_evidence judge) are traced to the collector of the tool that
               showed them; a collector qualifies when it carried a cited item and another cited item came from elsewhere,
               and the incident keeps at least one alarm without it.
  turn_budget  max(12, round(0.8 * median turns of the case's joint-success episodes)) — a budget the policy has
               already met on some episodes, below its usual length.
Writes data-v4/coverage-variant-table-20261021.json. Judge calls are cached next to each episode.
"""
import json, statistics, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
ROOT = Path("/home/swlee/projects/rca-model-lab-mcp"); W = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src")); sys.path.insert(0, str(ROOT / "scripts"))
from rca_lab.mcp.decisive_evidence import extract_decisive_items, judge_decisive_evidence
from rca_lab.mcp.masking import COVERAGE, filter_seed
from mcp_judge_runs import case_dir

TRACE = {"get_trace_spans", "get_trace_activity", "get_trace_summaries", "breakdown_endpoints", "expand_topology"}
HOST = {"search_host_data", "get_processes", "get_process_snapshot", "get_processes_snapshot"}
PREFIX = {"dpm.": "dpm", "db.": "dpm", "kcm.": "kcm", "sms.": "sms", "system.": "sms", "process.": "sms", "jvm.": "apm"}


def channel(tool, content):
    if tool in TRACE: return "apm"
    if tool in ("db_blocking", "db_slow_queries"): return "dpm"
    if tool == "get_k8s_state": return "kcm"
    if tool in HOST: return "sms"
    if tool == "sample_logs": return "logs"
    if tool in ("scan_metrics", "read_timeseries", "describe_target"):
        for p, c in PREFIX.items():
            if f'"{p}' in content or f"vm:{p}" in content: return c
        return "metrics"
    return tool or "other"


def episodes(state):
    out = {}
    for u in state["history"]:
        for g in (u.get("groups") or []) + (u.get("flat_groups") or []):
            gp = Path(g)
            if not gp.exists() or "-openrca2-" in g or "-rcaeval-" in g or "__" in gp.parent.name: continue
            for sp in gp.parent.glob("episodes/*/*/run*/score.json"):
                out.setdefault(gp.parent.name, []).append(sp.parent)
    return out


def analyse(case, runs, model="claude-opus-5-5"):
    joint = []
    for r in runs:
        s = json.loads((r / "score.json").read_text())
        mj = next((json.loads(j.read_text()).get("score") for j in sorted(r.glob("mechanism_judge*.json")) if "lock" not in j.name), None)
        if s.get("root_f1") == 1 and mj == 1 and s.get("stop_reason") == "submitted": joint.append((r, s))
    if not joint: return case, None
    budget = max(12, round(0.8 * statistics.median(s.get("turns", 0) for _, s in joint)))
    r, s = joint[0]
    meta = json.loads((case_dir(case) / "meta.json").read_text()); ev = (meta.get("scenario_metadata") or {}).get("distinguishing_evidence")
    items = extract_decisive_items(ev, W / "decisive-items" / f"{case}.json", model=model) if ev else None
    masks = []
    if items:
        msgs = [json.loads(l) for l in open(r / "trajectory.jsonl")]
        rec, _ = judge_decisive_evidence(r, messages=msgs, result=json.loads((r / "result.json").read_text()), score=s,
                                         decisive_items=items, model=model)
        if rec:
            calls = {tc["id"]: tc["function"]["name"] for m in msgs for tc in (m.get("tool_calls") or [])}
            def item_channels(it):
                out = set()
                for ref in it["evidence_refs"]:
                    m = msgs[int(ref[1:])]
                    out.add(channel(calls.get(m.get("tool_call_id")), str(m.get("content"))))
                return out
            cited = [item_channels(it) for it in rec["verdict"]["items"] if it["status"] == "cited"]
            seed = json.loads((r.parent / "seed.json").read_text()) if (r.parent / "seed.json").exists() else None
            for c in COVERAGE:
                carried = any(c in chs for chs in cited)
                elsewhere = any(chs - {c} for chs in cited)
                if not (carried and elsewhere): continue
                try:
                    if seed is not None: filter_seed(seed, (c,))
                except ValueError:
                    continue
                masks.append(c)
    return case, {"masks": masks, "turn_budget": budget, "joint_episodes": len(joint), "source_episode": str(r.relative_to(W))}


state = json.loads((W / "state.json").read_text())
eps = episodes(state)
table = {}
with ThreadPoolExecutor(max_workers=6) as pool:
    for case, row in pool.map(lambda kv: analyse(*kv), eps.items()):
        if row: table[case] = row; print(case.split("-v3-")[0], row["masks"], row["turn_budget"], flush=True)
out = W / "data-v4/coverage-variant-table-20261021.json"
out.write_text(json.dumps({"made": "2026-10-21", "rules": __doc__, "cases": table}, ensure_ascii=False, indent=1))
print("written", out, len(table))
