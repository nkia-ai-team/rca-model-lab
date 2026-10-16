"""Build the typed golden contract for the 17 newly audited Polestar captures.

The source of truth is the capture metadata plus the topology graph bundled in
each case. This writes a separate expanded contract so the historical
train-family-v2 contract remains immutable.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
CASES_ROOT = Path("/data/eval-cases")


NEW17_ROOTS: dict[str, dict[str, Any]] = {
    "case-f17-h-v3-632a04d1": {"aliases": ["core-banking-transfer", "banking-transfer"], "workloads": ["testbed-transfer"]},
    "case-f30-r-v3-0cd9193b": {"aliases": ["food-delivery-payment", "payment-service"], "workloads": ["testbed-payment"]},
    "case-f32-h-v3-531094f2": {"aliases": ["food-delivery-dispatch", "dispatch-service"], "workloads": ["testbed-dispatch"]},
    "case-f32-r-v3-63b10537": {"aliases": ["food-delivery-dispatch", "dispatch-service"], "workloads": ["testbed-dispatch"]},
    "case-f33-p-v3-e79a2db0": {"aliases": ["PostgreSQL-commerce", "commerce-postgresql"]},
    "case-f33-r-v3-73fed92a": {"aliases": ["MySQL-fooddelivery", "food-mysql"]},
    "case-f35-r-v3-bbbee443": {"aliases": ["Oracle-corebanking", "BANKING"]},
    "case-f36-r-v3-776b522b": {"aliases": ["MySQL-fooddelivery", "food-mysql"]},
    "case-f37-r-v3-43c16731": {"aliases": ["core-banking-api", "api-service"], "workloads": ["testbed-api"]},
    "case-f38-r-v3-54918c10": {"aliases": ["MySQL-fooddelivery", "food-mysql"]},
    "case-f39-r-v3-dc45499a": {"aliases": ["core-banking-account", "account-service"], "workloads": ["testbed-account"]},
    "case-f40-r-v3-50e3ecb9": {"aliases": ["commerce-pricing", "pricing-service"], "workloads": ["testbed-pricing"]},
    "case-f41-r-v3-f37b1984": {"aliases": ["core-banking-account", "account-service"], "workloads": ["testbed-account"]},
    "case-f42-r-v3-26143861": {"aliases": ["core-banking-transfer", "banking-transfer"], "workloads": ["testbed-transfer"]},
    "case-f43-r-v3-aecd7e38": {"aliases": ["commerce-cart", "cart-service"], "workloads": ["testbed-cart"]},
    "case-f44-p-v3-4ee5c786": {"aliases": ["tb-w2"]},
    "case-f44-r-v3-f7f70c65": {"aliases": ["tb-w3"]},
}


def _strip_target(raw: str) -> str:
    return raw.removeprefix("target:")


def latest_graph(case_dir: Path) -> dict[str, Any]:
    manifest = json.loads((case_dir / "topology/manifest.json").read_text(encoding="utf-8"))
    graph_rel = manifest["snapshots"][-1]["graph"][0]["path"]
    return json.loads((case_dir / "topology" / graph_rel).read_text(encoding="utf-8"))


def node_ids_by_alias(graph: dict[str, Any], aliases: list[str]) -> list[str]:
    wanted = {alias.lower() for alias in aliases}
    out: list[str] = []
    for node in graph.get("nodes", []):
        candidates = {
            str(node.get("label", "")).lower(),
            str((node.get("refKeys") or {}).get("serviceName", "")).lower(),
            str((node.get("refKeys") or {}).get("hostName", "")).lower(),
            str((node.get("refKeys") or {}).get("address", "")).lower(),
        }
        if candidates & wanted:
            out.append(_strip_target(str(node["id"])))
    return sorted(dict.fromkeys(out))


def workload_ids(case_dir: Path, workloads: list[str]) -> list[str]:
    ids: list[str] = []
    for name in ("target_availability_history.csv", "kcm_resources_history.csv"):
        path = case_dir / "data/postgres" / name
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                text = " ".join(str(row.get(key, "")) for key in ("resource_key", "name", "yaml"))
                if any(workload in text for workload in workloads):
                    target_id = row.get("target_id")
                    if target_id:
                        ids.append(target_id)
    return sorted(dict.fromkeys(ids))


def build_case(case_id: str, spec: dict[str, Any]) -> dict[str, Any]:
    case_dir = CASES_ROOT / case_id
    graph = latest_graph(case_dir)
    ids = node_ids_by_alias(graph, spec["aliases"])
    ids.extend(workload_ids(case_dir, spec.get("workloads", [])))
    ids = sorted(dict.fromkeys(ids))
    if not ids:
        raise RuntimeError(f"{case_id}: no root target ids resolved for aliases={spec['aliases']}")
    return {
        "expected_status": "confirmed",
        "roots": [{"target_ids": ids, "target_aliases": spec["aliases"]}],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=ROOT / "configs/eval/train-family-v2.yaml")
    parser.add_argument("--out", type=Path, default=ROOT / "configs/eval/train-family-expanded-20261012.yaml")
    args = parser.parse_args()
    contract = yaml.safe_load(args.base.read_text(encoding="utf-8"))
    cases = dict(contract["cases"])
    for case_id, spec in NEW17_ROOTS.items():
        if case_id in cases:
            raise RuntimeError(f"{case_id}: already exists in {args.base}")
        cases[case_id] = build_case(case_id, spec)
    out = {**contract, "cases": cases}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        "# Generated by scripts/mcp_build_new17_golden.py. Historical entries copied from train-family-v2.yaml.\n"
        + yaml.safe_dump(out, allow_unicode=True, sort_keys=False, width=100000),
        encoding="utf-8",
    )
    print(json.dumps({"out": str(args.out), "cases": len(cases), "added": len(NEW17_ROOTS)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
