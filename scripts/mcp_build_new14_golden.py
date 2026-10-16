"""Build the typed golden contract v3 (2026-10-15): new14 captures + per-capture rebuild of 9 new17 entries.

Same contract shape as scripts/mcp_build_new17_golden.py: one internal root per case whose
canonical target_ids are the root component's topology node(s) in the case's latest topology graph
plus, for a k8s workload root, every identity of that workload (mcp_golden_from_legacy.family_rows:
Deployment/StatefulSet, Services incl. -external, ReplicaSets, pods, containers).

Differences from the new17 run (see the 2026-10-15 grader report):
  * The workload identities are read from *this case's own* postgres.dump (kcm_resource_targets),
    not from one shared restored database. The new17 entries in train-family-v2-expanded-20261012
    were all expanded from case-f30-r's database, so the ReplicaSet/pod ids of the nine new17
    service cases (NEW17_REBUILD) belong to that capture and miss each case's own fault-window
    ReplicaSet. Those nine entries are rebuilt here from their own dumps (target_ids only;
    expected_status and target_aliases are kept); every other historical entry is copied unchanged.
  * Targets of the workload deleted during the capture (target_tombstones with
    deleted_at >= capture_start, e.g. the pod that ran the faulty release or was scaled away) are
    added too; --no-tombstones restores the strict kcm_resource_targets-only expansion.
  * The data/postgres CSV lookup of the new17 script (workload_ids) is not used: in these captures
    target_availability_history / kcm_resources_history rows carry the *cluster* target id
    (prod-k8s-01) in target_id, so a substring match on the workload name would make the cluster a
    root.

The dump is read with pg_restore in a throw-away container (read-only bind mount, no network);
/data/eval-cases is never written. Every case is written as its own block without YAML anchors, so
no list is shared between cases. The script refuses to run if a new case already exists in --base.

  uv run python scripts/mcp_build_new14_golden.py [--base ...] [--out ...] [--new-only-out ...]
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from mcp_build_new17_golden import CASES_ROOT, NEW17_ROOTS, latest_graph, node_ids_by_alias
from mcp_golden_from_legacy import family_rows

ROOT = Path(__file__).resolve().parents[1]

MYSQL_FOOD = {"aliases": ["MySQL-fooddelivery", "food-mysql", "testbed-mysql"], "workloads": ["rca-testbed-food/testbed-mysql"]}
ORACLE_BANKING = {"aliases": ["Oracle-corebanking", "BANKING"], "workloads": ["rca-testbed-banking/testbed-oracle"]}
FOOD_DISPATCH = {"aliases": ["food-delivery-dispatch", "dispatch-service"], "workloads": ["rca-testbed-food/testbed-dispatch"]}
FOOD_ORDER = {"aliases": ["food-delivery-order", "order-service"], "workloads": ["rca-testbed-food/testbed-order"]}
FOOD_RESTAURANT = {"aliases": ["food-delivery-restaurant", "restaurant-service"], "workloads": ["rca-testbed-food/testbed-restaurant"]}
BANKING_TRANSFER = {"aliases": ["core-banking-transfer", "banking-transfer", "transfer-service"], "workloads": ["rca-testbed-banking/testbed-transfer"]}
BANKING_ACCOUNT = {"aliases": ["core-banking-account", "banking-account", "account-service"], "workloads": ["rca-testbed-banking/testbed-account"]}
# 192.168.200.137 is also Oracle-corebanking's address in the graph, so the host is resolved by name only.
HOST_TB_W2 = {"aliases": ["tb-w2", "192.168.200.137"], "resolve": ["tb-w2"]}

# root = injection origin (the component the fault was applied to), as in train-family-v2 / new17.
# extra_k8s: further (resource_kind, resource_key) identities from kcm_resource_targets accepted for the same root.
NEW14_ROOTS: dict[str, dict[str, Any]] = {
    "case-f32-p-v3-58d43138": {**MYSQL_FOOD, "note": "DDL: fooddelivery.dispatches 를 운영 MySQL 에서 drop(rename) → DB 인스턴스 root (f33-r 선례)"},
    "case-f33-h-v3-f19ed3f1": {**FOOD_DISPATCH, "note": "testbed-dispatch 릴리스 1.6.0 롤아웃(목록 COUNT) → dispatch root, MySQL 은 포화되는 공유 자원 (f32-h 선례)"},
    "case-f35-h-v3-a1206bfd": {**ORACLE_BANKING, "note": "DDL: BANKING.TRANSFERS drop → DB 인스턴스 root (f35-r·f14-p 선례)"},
    "case-f40-h-v3-b472a2f0": {**BANKING_TRANSFER, "note": "testbed-transfer 릴리스 2.3.0 롤아웃(whole-won 검증) → transfer root (f17-h·f42-r 선례)"},
    "case-f43-p-v3-e132f196": {**FOOD_DISPATCH, "note": "testbed-dispatch 릴리스 1.7.0 롤아웃(커넥션 누수) → dispatch root (f32-h 선례)"},
    "case-f48-p-v3-3667031d": {**MYSQL_FOOD, "note": "DDL: 취소된 backfill 이 order_outbox_events 를 hold 로 rename → DB 인스턴스 root"},
    "case-f48-r-v3-574963e1": {**MYSQL_FOOD, "note": "DDL: 취소된 backfill 이 menu_popularity_summary 를 hold 로 rename → DB 인스턴스 root"},
    "case-f49-h-v3-b873cb3e": {**FOOD_ORDER, "note": "testbed-order 릴리스 2.3.0 롤아웃(ObjectMapper 날짜 포맷) → order root (dispatch 는 정상 응답)"},
    "case-f49-r-v3-1af8dad9": {**FOOD_RESTAURANT, "note": "testbed-restaurant 릴리스 1.4.0 롤아웃(응답 계약 변경) → restaurant root (order 는 파싱 실패 피해자)"},
    "case-f50-r-v3-75ae96d0": {
        **BANKING_TRANSFER,
        "aliases": [*BANKING_TRANSFER["aliases"], "rca-testbed-banking"],
        "extra_k8s": [("namespace", "rca-testbed-banking")],
        "note": "rca-testbed-banking ResourceQuota < 사용량 + transfer 재배포 → transfer 워크로드 ∪ quota 가 걸린 네임스페이스 (둘 다 인정, 2026-10-15 오케스트레이터 결정)",
    },
    "case-f51-r-v3-0c3126f0": {**BANKING_ACCOUNT, "note": "kubectl scale testbed-account --replicas=0 → account root (f04-r shipping 0 replica 선례)"},
    "case-f52-r-v3-13501208": {**HOST_TB_W2, "note": "tb-w2 containerd 이미지 삭제(ctr images rm) → 호스트 root (f44-p tb-w2 방화벽 선례), transfer 재배포는 trigger. 노드 측 결정적 증거 미수집 → split 에서 training quarantine"},
    "case-f53-p-v3-b029727d": {**MYSQL_FOOD, "note": "DDL: fooddelivery.payments drop(rename) → DB 인스턴스 root"},
    "case-f53-r-v3-d0f23131": {**MYSQL_FOOD, "note": "DDL: fooddelivery.orders drop(rename) → DB 인스턴스 root"},
}

# new17 service entries whose k8s identities came from case-f30-r's database; rebuilt from their own dumps.
NEW17_REBUILD = (
    "case-f17-h-v3-632a04d1", "case-f32-h-v3-531094f2", "case-f32-r-v3-63b10537", "case-f37-r-v3-43c16731",
    "case-f39-r-v3-dc45499a", "case-f40-r-v3-50e3ecb9", "case-f41-r-v3-f37b1984", "case-f42-r-v3-26143861",
    "case-f43-r-v3-aecd7e38",
)
NEW17_WORKLOAD_NS = {
    "testbed-transfer": "rca-testbed-banking", "testbed-api": "rca-testbed-banking", "testbed-account": "rca-testbed-banking",
    "testbed-dispatch": "rca-testbed-food", "testbed-pricing": "rca-testbed-commerce", "testbed-cart": "rca-testbed-commerce",
}
REBUILD_NOTE = "new17 entry 재생성(2026-10-15): v2-expanded 는 case-f30-r DB 기준 RS/pod id 였음 → 자기 캡처 dump 로 재전개 (target_ids 만 교체)"

TOMBSTONE_KINDS = {"replica_set": "replicaset", "stateful_set": "statefulset", "daemon_set": "daemonset"}


class NoAliasDumper(yaml.SafeDumper):
    """Never emit anchors/aliases: every case carries its own lists."""

    def ignore_aliases(self, data: Any) -> bool:
        return True


def dump_table(case_dir: Path, table: str, image: str) -> list[dict[str, str | None]]:
    """Rows of one table of the case's postgres.dump (pg_restore --data-only to stdout)."""
    cmd = [
        "docker", "run", "--rm", "--network", "none", "-v", f"{case_dir / 'data'}:/in:ro", image,
        "pg_restore", "--data-only", "--table", table, "--file", "-", "/in/postgres.dump",
    ]
    text = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    columns: list[str] | None = None
    rows: list[dict[str, str | None]] = []
    for line in text.splitlines():
        if columns is None:
            if line.startswith(f"COPY public.{table} ("):
                columns = [c.strip() for c in line.split("(", 1)[1].split(")", 1)[0].split(",")]
            continue
        if line == "\\.":
            break
        values = line.split("\t")
        rows.append({c: (None if v == "\\N" else v) for c, v in zip(columns, values)})
    if columns is None:
        raise RuntimeError(f"{case_dir.name}: table {table} not found in postgres.dump")
    return rows


def _ts(raw: str) -> datetime:
    """Postgres ('2026-10-13 19:59:38.2+00') or meta ('2026-10-13T19:49:15Z') timestamp, tz-aware."""
    value = re.sub(r"([+-]\d{2})$", r"\1:00", raw.strip().replace(" ", "T").replace("Z", "+00:00"))
    return datetime.fromisoformat(value)


def k8s_identities(case_dir: Path, workloads: list[str], extra: list[tuple[str, str]], image: str, tombstones: bool) -> list[dict[str, str]]:
    """Workload family rows (live, then deleted during the capture) plus explicitly named extra identities."""
    meta = json.loads((case_dir / "meta.json").read_text(encoding="utf-8"))
    live = [[str(r["target_id"]), str(r["resource_kind"]), str(r["resource_key"])] for r in dump_table(case_dir, "kcm_resource_targets", image)]
    gone: list[list[str]] = []
    if tombstones and workloads:
        start = _ts(meta["capture_start"])
        for r in dump_table(case_dir, "target_tombstones", image):
            if r.get("type") != "kubernetes_resource" or not r.get("resource_key") or not r.get("deleted_at"):
                continue
            if _ts(str(r["deleted_at"])) >= start:
                kind = str(r["resource_kind"])
                gone.append([str(r["target_id"]), TOMBSTONE_KINDS.get(kind, kind), str(r["resource_key"])])
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for workload in workloads:
        for source, rows in (("kcm_resource_targets", live), ("target_tombstones", gone)):
            for tid, kind, key in sorted(family_rows(rows, workload), key=lambda row: (row[2], row[1])):
                if tid not in seen:
                    seen.add(tid)
                    out.append({"id": tid, "kind": kind, "key": key, "source": source})
    for want_kind, want_key in extra:
        hits = [tid for tid, kind, key in live if kind == want_kind and key == want_key]
        if len(hits) != 1:
            raise RuntimeError(f"{case_dir.name}: expected exactly one kcm_resource_targets row for {want_kind} {want_key}, got {hits}")
        if hits[0] not in seen:
            seen.add(hits[0])
            out.append({"id": hits[0], "kind": want_kind, "key": want_key, "source": "kcm_resource_targets (extra)"})
    return out


def build_case(case_id: str, spec: dict[str, Any], image: str, tombstones: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    case_dir = CASES_ROOT / case_id
    graph = latest_graph(case_dir)
    resolve = spec.get("resolve", spec["aliases"])
    node_ids = node_ids_by_alias(graph, resolve)
    if not node_ids:
        raise RuntimeError(f"{case_id}: no topology node for {resolve}")
    labels = {str(n["id"]).removeprefix("target:"): (str(n.get("type", "")), str(n.get("label", ""))) for n in graph.get("nodes", [])}
    workloads = spec.get("workloads", [])
    extra = list(spec.get("extra_k8s", []))
    k8s = k8s_identities(case_dir, workloads, extra, image, tombstones) if (workloads or extra) else []
    if workloads and not any(row["source"] == "kcm_resource_targets" for row in k8s):
        raise RuntimeError(f"{case_id}: no kcm_resource_targets rows for {workloads}")
    ids = list(node_ids) + [row["id"] for row in k8s if row["id"] not in node_ids]
    entry = {"expected_status": "confirmed", "roots": [{"target_ids": ids, "target_aliases": list(spec["aliases"])}]}
    detail = {
        "nodes": [{"id": tid, "type": labels.get(tid, ("?", "?"))[0], "label": labels.get(tid, ("?", "?"))[1]} for tid in node_ids],
        "k8s": k8s,
        "count": len(ids),
        "note": spec.get("note", ""),
    }
    return entry, detail


def new17_rebuild_spec(case_id: str) -> dict[str, Any]:
    spec = NEW17_ROOTS[case_id]
    return {"aliases": list(spec["aliases"]), "workloads": [f"{NEW17_WORKLOAD_NS[w]}/{w}" for w in spec["workloads"]], "note": REBUILD_NOTE}


def rebuild_new17(case_id: str, old: dict[str, Any], image: str, tombstones: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    """Old entry with target_ids replaced by the case's own expansion (status, aliases, other keys kept)."""
    if len(old["roots"]) != 1 or not old["roots"][0].get("target_ids"):
        raise RuntimeError(f"{case_id}: expected a single internal root in the historical entry")
    built, detail = build_case(case_id, new17_rebuild_spec(case_id), image, tombstones)
    entry = copy.deepcopy(old)
    entry["roots"][0]["target_ids"] = built["roots"][0]["target_ids"]
    detail["old_target_ids"] = list(old["roots"][0]["target_ids"])
    return entry, detail


def entry_block(case_id: str, entry: dict[str, Any], note: str = "") -> str:
    text = yaml.dump({case_id: entry}, Dumper=NoAliasDumper, allow_unicode=True, sort_keys=False, width=100000)
    comment = f"  # {case_id}: {note}\n" if note else ""
    return comment + "".join("  " + line + "\n" for line in text.rstrip("\n").splitlines())


def render(contract: dict[str, Any], blocks: list[str], header: str) -> str:
    """Top-level keys in their original order; `cases` is emitted from the prepared per-case blocks."""
    parts = [header]
    for key, value in contract.items():
        if key == "cases":
            parts.append("cases:\n" + "".join(blocks))
        else:
            parts.append(yaml.dump({key: value}, Dumper=NoAliasDumper, allow_unicode=True, sort_keys=False, width=100000))
    return "".join(parts)


def has_anchor(text: str) -> bool:
    return any(getattr(event, "anchor", None) for event in yaml.parse(text))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", type=Path, default=ROOT / "configs/eval/train-family-v2-expanded-20261012.yaml")
    parser.add_argument("--out", type=Path, default=ROOT / "configs/eval/train-family-v3-expanded-20261015.yaml")
    parser.add_argument("--new-only-out", type=Path, default=ROOT / "configs/eval/polestar-new14-20261015.yaml")
    parser.add_argument("--pg-image", default="postgres:17-alpine", help="image providing pg_restore (>= the dump's server version)")
    parser.add_argument("--no-tombstones", action="store_true", help="expand from kcm_resource_targets only")
    parser.add_argument("--no-rebuild-new17", action="store_true", help="copy the nine new17 service entries unchanged")
    parser.add_argument("--detail-json", type=Path, help="write the per-case resolution (node labels, k8s rows) here")
    args = parser.parse_args()
    tombstones = not args.no_tombstones

    contract = yaml.safe_load(args.base.read_text(encoding="utf-8"))
    base_cases: dict[str, Any] = contract["cases"]
    rebuild = () if args.no_rebuild_new17 else NEW17_REBUILD
    missing = [case_id for case_id in rebuild if case_id not in base_cases]
    if missing:
        raise RuntimeError(f"rebuild targets absent from {args.base}: {missing}")
    dupes = [case_id for case_id in NEW14_ROOTS if case_id in base_cases]
    if dupes:
        raise RuntimeError(f"already exist in {args.base}: {dupes}")

    details: dict[str, Any] = {}
    out_cases: dict[str, Any] = {}
    blocks: list[str] = []
    for case_id, old in base_cases.items():
        if case_id in rebuild:
            entry, detail = rebuild_new17(case_id, old, args.pg_image, tombstones)
            details[case_id] = detail
            out_cases[case_id] = entry
            blocks.append(entry_block(case_id, entry, REBUILD_NOTE))
            removed = len(set(detail["old_target_ids"]) - set(entry["roots"][0]["target_ids"]))
            added = len(set(entry["roots"][0]["target_ids"]) - set(detail["old_target_ids"]))
            print(f"{case_id}: rebuilt {len(detail['old_target_ids'])} -> {detail['count']} ids (+{added} -{removed})", file=sys.stderr)
        else:
            out_cases[case_id] = old
            blocks.append(entry_block(case_id, old))

    new_cases: dict[str, Any] = {}
    new_blocks: list[str] = []
    for case_id, spec in NEW14_ROOTS.items():
        entry, detail = build_case(case_id, spec, args.pg_image, tombstones)
        new_cases[case_id] = entry
        details[case_id] = detail
        new_blocks.append(entry_block(case_id, entry, spec.get("note", "")))
        kinds: dict[str, int] = {}
        for row in detail["k8s"]:
            kinds[row["kind"]] = kinds.get(row["kind"], 0) + 1
        nodes = ", ".join(f"{n['label']}({n['type']})" for n in detail["nodes"])
        gone = sum(row["source"] == "target_tombstones" for row in detail["k8s"])
        print(f"{case_id}: {detail['count']} ids | nodes: {nodes} | k8s: {kinds} (tombstoned {gone})", file=sys.stderr)

    out_text = render(
        contract,
        blocks + ["  # --- new14 (scripts/mcp_build_new14_golden.py, 2026-10-15): captures of 2026-10-12..15 ---\n"] + new_blocks,
        f"# Generated by scripts/mcp_build_new14_golden.py from {args.base.name}: historical entries unchanged except the "
        f"{len(rebuild)} new17 service entries rebuilt from their own captures, plus {len(new_cases)} new14 entries.\n",
    )
    expected = {**out_cases, **new_cases}
    merged = yaml.safe_load(out_text)
    if merged["cases"] != expected or list(merged["cases"]) != list(expected) or has_anchor(out_text):
        raise RuntimeError("expanded contract does not round-trip (or contains anchors)")
    new_text = render(
        {key: value for key, value in contract.items() if key in ("version", "cases")},
        new_blocks,
        f"# Generated by scripts/mcp_build_new14_golden.py. The same {len(new_cases)} entries are appended to {args.out.name}.\n",
    )
    if yaml.safe_load(new_text)["cases"] != new_cases or has_anchor(new_text):
        raise RuntimeError("new-only contract does not round-trip (or contains anchors)")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(out_text, encoding="utf-8")
    args.new_only_out.write_text(new_text, encoding="utf-8")
    if args.detail_json:
        args.detail_json.write_text(json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "out": str(args.out), "new_only_out": str(args.new_only_out), "cases": len(merged["cases"]),
        "rebuilt": len(rebuild), "added": len(new_cases),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
