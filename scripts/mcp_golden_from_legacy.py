"""Expand a legacy golden entry (one canonical UUID per root) into the MCP contract shape.

The MCP scorer matches roots by canonical target UUID, and an investigator may cite any identity of
the root workload (application/database target, its Deployment/StatefulSet, ReplicaSets, pods,
containers, Services). The train contract was built with that expansion; this script reproduces it
for other captures (sealed test set) from the restored Postgres:

  root target (application|database|server) + every kcm_resource_targets row of the same workload:
    <ns>/<W> and <ns>/<W>-external            (deployment | statefulset | service)
    <ns>/<W>-<rs hash>                         (replicaset)
    <ns>/<W>-<rs hash>-<pod id> | <ns>/<W>-<n> (pod)
    <pod>/<container>                          (container)
  external-dependency roots additionally get proxy_target_ids = every namespace's
  testbed-external-pg-mock workload (the in-capture stand-in for the external PG).

Workload W is taken from a `testbed-*` alias when the legacy entry has one, else derived from the
target name (commerce-X -> rca-testbed-commerce/testbed-X, food-delivery-X -> rca-testbed-food/
testbed-X, core-banking-X / banking-X -> rca-testbed-banking/testbed-X, known database targets).
Hosts are not expanded. Every expanded root is printed with its resource counts so a zero match is
visible, and --check compares against an existing MCP contract entry.

  uv run python scripts/mcp_golden_from_legacy.py --legacy <legacy.yaml> --case <case-id> --pg <container> \
      [--check configs/eval/train-family-v2.yaml] [--write <out.yaml>]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

DB_WORKLOADS = {
    "Oracle-corebanking": "rca-testbed-banking/testbed-oracle",
    "PostgreSQL-commerce": "rca-testbed-commerce/testbed-postgres",
    "MySQL-fooddelivery": "rca-testbed-food/testbed-mysql",
}
PG_MOCK = "testbed-external-pg-mock"


def psql(container: str, sql: str) -> list[list[str]]:
    proc = subprocess.run(["docker", "exec", container, "psql", "-U", "lucida", "-d", "lucida", "-At", "-F", "\t", "-c", sql],
                          capture_output=True, text=True, check=True)
    return [line.split("\t") for line in proc.stdout.splitlines() if line.strip()]


def workload_for(target_name: str, aliases: list[str]) -> str | None:
    if target_name in DB_WORKLOADS:
        return DB_WORKLOADS[target_name]
    tb = next((a for a in aliases if a.startswith("testbed-")), None)
    prefix_ns = [("commerce-", "rca-testbed-commerce"), ("food-delivery-", "rca-testbed-food"),
                 ("core-banking-", "rca-testbed-banking"), ("banking-", "rca-testbed-banking")]
    for prefix, ns in prefix_ns:
        if target_name.startswith(prefix):
            short = tb or "testbed-" + target_name[len(prefix):]
            return f"{ns}/{short}"
    return None  # hosts, or a name without a known domain prefix: not expanded (printed as 0 rows)


def family_rows(rows: list[list[str]], workload: str) -> list[list[str]]:
    ns, w = workload.split("/", 1)
    top = re.compile(rf"^{re.escape(ns)}/{re.escape(w)}(-external)?$")
    rs = re.compile(rf"^{re.escape(ns)}/{re.escape(w)}-[0-9a-z]{{5,10}}$")
    pod = re.compile(rf"^{re.escape(ns)}/{re.escape(w)}-([0-9a-z]{{5,10}}-[0-9a-z]{{5}}|\d+)$")
    out = []
    for tid, kind, key in rows:
        if kind in ("deployment", "statefulset", "service") and top.match(key):
            out.append([tid, kind, key])
        elif kind == "replicaset" and rs.match(key):
            out.append([tid, kind, key])
        elif kind == "pod" and pod.match(key):
            out.append([tid, kind, key])
        elif kind == "container" and "/" in key and pod.match(key.rsplit("/", 1)[0]):
            out.append([tid, kind, key])
    return out


def expand(entry: dict[str, Any], pg: str) -> dict[str, Any]:
    kcm = psql(pg, "SELECT target_id::text, resource_kind, resource_key FROM kcm_resource_targets")
    namespaces = {key.split("/", 1)[0] for _, _, key in kcm if "/" in key}
    names = dict((r[0], (r[1], r[2])) for r in psql(pg, "SELECT id::text, type, name FROM targets"))
    roots = []
    for index, root in enumerate(entry["roots"]):
        new = dict(root)
        if root.get("target_ids"):
            base = list(root["target_ids"])
            ids = list(base)
            for tid in base:
                typ, name = names.get(tid, ("?", "?"))
                wl = workload_for(name, list(root.get("target_aliases") or []))
                fam = family_rows(kcm, wl) if wl else []
                print(f"  root{index} {tid[:8]} {typ} {name} -> workload {wl} : {len(fam)} k8s rows "
                      f"({', '.join(sorted({k for _, k, _ in fam}))})", file=sys.stderr)
                ids += [t for t, _, _ in fam if t not in ids]
            new["target_ids"] = ids
        if root.get("pseudo_kind") == "external_dependency" and any("pg" in p for p in root.get("pseudo_ids") or []):
            proxy = []
            for ns in sorted(namespaces):
                proxy += [t for t, _, _ in family_rows(kcm, f"{ns}/{PG_MOCK}") if t not in proxy]
            print(f"  root{index} external pg proxy: {len(proxy)} k8s rows", file=sys.stderr)
            new["proxy_target_ids"] = proxy
        order = ["target_ids", "target_aliases", "pseudo_kind", "pseudo_ids", "proxy_target_ids", "boundary_target_ids", "boundary_target_aliases"]
        roots.append({k: new[k] for k in order if k in new} | {k: v for k, v in new.items() if k not in order})
    return {"expected_status": entry["expected_status"], "roots": roots}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--legacy", required=True)
    parser.add_argument("--case", required=True)
    parser.add_argument("--pg", required=True)
    parser.add_argument("--check", help="MCP contract to compare against (exact id-set match per root)")
    parser.add_argument("--write", help="append the expanded entry to this contract file")
    args = parser.parse_args()
    legacy = yaml.safe_load(Path(args.legacy).read_text(encoding="utf-8"))["cases"]
    if args.case not in legacy:
        raise SystemExit(f"{args.case} not in {args.legacy}")
    entry = expand(legacy[args.case], args.pg)
    if args.check:
        ref = yaml.safe_load(Path(args.check).read_text(encoding="utf-8"))["cases"][args.case]
        ok = True  # status is re-graded under rule A separately; only identity sets are compared
        for i, (a, b) in enumerate(zip(entry["roots"], ref["roots"])):
            for key in ("target_ids", "proxy_target_ids", "boundary_target_ids"):
                sa, sb = set(a.get(key) or []), set(b.get(key) or [])
                if sa != sb:
                    ok = False
                    print(f"  MISMATCH root{i}.{key}: extra={sorted(sa - sb)} missing={sorted(sb - sa)}", file=sys.stderr)
        print(f"check {args.case}: {'MATCH' if ok else 'DIFF'}")
    text = yaml.safe_dump({args.case: entry}, allow_unicode=True, sort_keys=False, default_flow_style=None, width=100000)
    block = "".join("  " + line + "\n" for line in text.rstrip("\n").splitlines())
    if args.write:
        out = Path(args.write)
        if not out.exists():
            out.write_text("version: 1\ncases:\n", encoding="utf-8")
        with out.open("a", encoding="utf-8") as sink:
            sink.write(f"  # {args.case}: legacy {Path(args.legacy).name} 의 단일 UUID 루트를 mcp_golden_from_legacy.py 로 워크로드 전체 UUID 집합으로 확장.\n" + block)
        print(f"appended {args.case} to {out}")
    elif not args.check:
        print(block)


if __name__ == "__main__":
    main()
