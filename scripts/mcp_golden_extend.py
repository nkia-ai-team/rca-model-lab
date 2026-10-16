"""Derive a golden entry for a re-capture of an already-golden scenario.

Golden roots are canonical target UUIDs (service target + its k8s resources, external proxies,
boundary targets), and those UUIDs differ per capture: pods/replicasets carry a fresh hash, and
every k8s resource row is re-registered. A re-capture therefore needs its own entry. This script
reads the sibling entry, resolves each id to (type, name) in the sibling's restored Postgres,
normalises the name (drop the cluster-uuid prefix and Deployment pod/replicaset hashes), and looks
up the same (type, normalised name) in the re-capture's restored Postgres.

  uv run python scripts/mcp_golden_extend.py --sibling case-f01-p-v3-d691440c --case case-f01-p-v3-20bfac59 \
      --sibling-pg gsib-f01-p-postgres-1 --case-pg gnew-f01-p-postgres-1 [--write]

Both captures must be restored (`mcp_case_run.py --runner prepare --keep-db`) — the *-postgres-1
containers are queried through `docker exec … psql`. Without --write the entry is printed only.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# Deployment-managed names: <workload>-<replicaset hash>[-<pod id>]; hashes are 5-10 lowercase alnum
POD_SUFFIX_RE = re.compile(r"^(?P<base>.+?)-[0-9a-z]{5,10}-[0-9a-z]{5}$")
RS_SUFFIX_RE = re.compile(r"^(?P<base>.+?)-[0-9a-z]{5,10}$")


def psql(container: str, sql: str) -> list[list[str]]:
    proc = subprocess.run(["docker", "exec", container, "psql", "-U", "lucida", "-d", "lucida", "-At", "-F", "\t", "-c", sql], capture_output=True, text=True, check=True)
    return [line.split("\t") for line in proc.stdout.splitlines() if line.strip()]


def normalise(kind: str, name: str) -> str:
    """Capture-independent identity. k8s identities arrive as `<resource_kind>:<resource_key>`
    (legacy names as `<cluster-uuid>:<kind>:<key>`); Deployment-managed pod/replicaset/container
    keys carry a fresh hash per rollout, which is replaced by `*`."""
    if kind != "kubernetes_resource":
        return name
    parts = name.split(":")
    if len(parts) >= 3 and UUID_RE.match(parts[0]):
        parts = parts[1:]
    rkind, rest = (parts[0], ":".join(parts[1:])) if len(parts) >= 2 else ("", name)
    segs = rest.split("/")  # namespace/object[/container]
    if len(segs) >= 2:
        if rkind in ("pod", "container"):
            match = POD_SUFFIX_RE.match(segs[1])
            if match:
                segs[1] = match.group("base") + "-*"
        elif rkind == "replicaset":
            match = RS_SUFFIX_RE.match(segs[1])
            if match:
                segs[1] = match.group("base") + "-*"
    return f"{rkind}:{'/'.join(segs)}"


def load_targets(container: str) -> dict[str, tuple[str, str]]:
    """id -> (type, identity). For kubernetes_resource the identity is `<resource_kind>:<resource_key>`
    from meta (the `name` column changed format between capture generations: the newer registry
    drops the `<cluster-uuid>:<kind>:` prefix); everything else uses the name column."""
    rows = psql(container, "SELECT id, type, name, COALESCE(meta->>'resource_kind',''), COALESCE(meta->>'resource_key','') FROM targets")
    out: dict[str, tuple[str, str]] = {}
    for tid, kind, name, rkind, rkey in rows:
        if kind == "kubernetes_resource" and rkind and rkey:
            out[tid] = (kind, f"{rkind}:{rkey}")
        else:
            out[tid] = (kind, name)
    return out


def remap(ids: list[str], sib: dict[str, tuple[str, str]], new: dict[str, tuple[str, str]], new_index: dict[tuple[str, str], list[str]], label: str) -> list[str]:
    out: list[str] = []
    kept = 0
    for tid in ids:
        if tid in new and (tid not in sib or new[tid][0] == sib[tid][0]):
            out.append(tid)  # stable registry id (StatefulSets, services, DB/application targets)
            kept += 1
            continue
        if tid not in sib:
            print(f"  [{label}] {tid}: not in sibling targets — dropped", file=sys.stderr)
            continue
        kind, name = sib[tid]
        key = (kind, normalise(kind, name))
        hits = new_index.get(key, [])
        if not hits:
            print(f"  [{label}] {tid} ({kind} {name}) -> no match in re-capture for {key[1]!r}", file=sys.stderr)
        elif len(hits) > 1:
            print(f"  [{label}] {tid} ({kind} {name}) -> {len(hits)} matches, taking all", file=sys.stderr)
        out.extend(h for h in hits if h not in out)
    if kept:
        print(f"  [{label}] {kept} id(s) identical across captures", file=sys.stderr)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sibling", required=True)
    parser.add_argument("--case", required=True)
    parser.add_argument("--sibling-pg", required=True, help="postgres container of the restored sibling capture")
    parser.add_argument("--case-pg", required=True, help="postgres container of the restored re-capture")
    parser.add_argument("--contract", default=str(ROOT / "configs/eval/train-family-v2.yaml"))
    parser.add_argument("--write", action="store_true", help="append the entry to the contract file")
    args = parser.parse_args()

    contract_path = Path(args.contract)
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    cases = contract["cases"]
    if args.sibling not in cases:
        raise SystemExit(f"sibling {args.sibling} has no golden entry")
    if args.case in cases:
        raise SystemExit(f"{args.case} already has a golden entry")
    sibling = cases[args.sibling]

    sib_targets = load_targets(args.sibling_pg)
    new_targets = load_targets(args.case_pg)
    new_index: dict[tuple[str, str], list[str]] = {}
    for tid, (kind, name) in new_targets.items():
        new_index.setdefault((kind, normalise(kind, name)), []).append(tid)
    print(f"sibling targets={len(sib_targets)} re-capture targets={len(new_targets)}", file=sys.stderr)

    roots_out: list[dict[str, Any]] = []
    for index, root in enumerate(sibling["roots"]):
        entry: dict[str, Any] = {}
        for key in ("target_ids", "proxy_target_ids", "boundary_target_ids"):
            if root.get(key):
                mapped = remap(list(root[key]), sib_targets, new_targets, new_index, f"root{index}.{key}")
                if not mapped:
                    raise SystemExit(f"root {index} {key}: nothing resolved — refusing to write a hollow entry")
                entry[key] = mapped
                print(f"  root{index}.{key}: {len(root[key])} -> {len(mapped)}", file=sys.stderr)
        for key in ("target_aliases", "pseudo_kind", "pseudo_ids", "boundary_target_aliases"):
            if key in root:
                entry[key] = root[key]
        # keep the sibling's key order
        roots_out.append({k: entry[k] for k in root if k in entry} | {k: v for k, v in entry.items() if k not in root})

    new_entry = {"expected_status": sibling["expected_status"], "roots": roots_out}
    text = yaml.safe_dump({args.case: new_entry}, allow_unicode=True, sort_keys=False, default_flow_style=None, width=100000)
    indented = "".join("  " + line + "\n" for line in text.rstrip("\n").splitlines())
    header = f"  # {args.case}: {args.sibling} 의 재캡처 (같은 scenario_metadata_sha256). mcp_golden_extend.py 로 targets 테이블에서 (type, 정규화 이름) 대응해 도출.\n"
    if args.write:
        with contract_path.open("a", encoding="utf-8") as sink:
            sink.write(header + indented)
        print(f"appended {args.case} to {contract_path}")
    else:
        print(header + indented)


if __name__ == "__main__":
    main()
