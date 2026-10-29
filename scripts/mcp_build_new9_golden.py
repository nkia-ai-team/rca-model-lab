"""Build the typed golden contract v4 (2026-10-18): the v3 contract plus nine Polestar captures of 2026-10-15..18.

Same entry shape and expansion as scripts/mcp_build_new14_golden.py (one internal root per case: the root component's
topology node(s) plus every k8s identity of its workload, read from the case's own postgres.dump, including targets
deleted during the capture). The nine captures were verified before this run (fault applied in the injection phase,
load continuous, decisive evidence present; reports in the 2026-10-18 session scratchpad capture-verify-[ABC].md).

New here: a Kubernetes Job root (f59-r, user decision 2026-10-18 "백필 Job"). The Job has no topology node, so its root is
the Job target and its pods/containers (live or tombstoned) from kcm_resource_targets / target_tombstones.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

from mcp_build_new14_golden import (BANKING_ACCOUNT, FOOD_DISPATCH, FOOD_ORDER, ORACLE_BANKING, ROOT, TOMBSTONE_KINDS, _ts,
                                    build_case, dump_table, entry_block, has_anchor, render)
from mcp_build_new17_golden import CASES_ROOT

FOOD_PAYMENT = {"aliases": ["food-delivery-payment", "payment-service"], "workloads": ["rca-testbed-food/testbed-payment"]}
COMMERCE_POSTGRES = {"aliases": ["PostgreSQL-commerce", "commerce-postgresql"], "workloads": ["rca-testbed-commerce/testbed-postgres"]}

# root = injection origin, as in train-family-v2 / new17 / new14.
NEW9_ROOTS: dict[str, dict[str, Any]] = {
    "case-f54-r-v3-31a93bbe": {**ORACLE_BANKING, "note": "BANKING 계정에 LOGICAL_READS_PER_CALL 30000 프로필 적용(DB 설정 변경) → DB 인스턴스 root (DDL/DB 변경 선례)"},
    "case-f49-p-v3-0a4fe786": {**FOOD_PAYMENT, "note": "testbed-payment 릴리스 1.3.0 롤아웃(결제 id → pay_ 문자열) → payment root (order 는 파싱 실패 피해자, f49-r 선례)"},
    "case-f55-r-v3-ba950165": {**FOOD_DISPATCH, "note": "testbed-dispatch 릴리스 1.8.0 롤아웃(IP별 rate limit) → dispatch root (order 는 429 피해자)"},
    "case-f56-r-v3-24b89933": {**ORACLE_BANKING, "note": "운영자 퍼지가 운영 정산 계정 2개 삭제(DB 데이터 변경) → DB 인스턴스 root (DB 측 변경 선례)"},
    "case-f51-h-v3-c093a2ed": {**BANKING_ACCOUNT, "note": "testbed-account 릴리스 1.4.0 롤아웃(커넥션 미반환) → account root (f43-p 커넥션 누수 선례)"},
    "case-f57-r-v3-419736f6": {**ORACLE_BANKING, "note": "TRANSFERS 이관 후 identity 시퀀스 상태 미이전(DDL/DB 변경) → DB 인스턴스 root"},
    "case-f23-p-v3-339d153c": {**COMMERCE_POSTGRES, "note": "운영 commerce PostgreSQL 에서 products 테이블 drop(DDL) → DB 인스턴스 root (f25-h 와 같은 식별자 집합)"},
    "case-f58-r-v3-4568fd65": {**FOOD_ORDER, "note": "testbed-order 릴리스 2.5.0 롤아웃(개발 프로필에만 있는 설정 키) → order root"},
    "case-f59-r-v3-cdc14d3d": {"aliases": ["notify-delivery-backfill"], "job": "rca-testbed-food/notify-delivery-backfill",
                               "note": "notify-delivery-backfill Job 의 offset 깊은 페이지 조회가 dispatch 커넥션 풀·food MySQL 포화 → Job root "
                                       "(사용자 결정 2026-10-18, 토폴로지 노드 없음: Job 과 그 pod/container). dispatch·MySQL 은 피해자"},
}


def job_rows(rows: list[list[str]], job: str) -> list[list[str]]:
    """The Job target and its pods/containers (pod names: <job>-<5 chars>)."""
    ns, name = job.split("/", 1)
    pod = re.compile(rf"^{re.escape(ns)}/{re.escape(name)}-[0-9a-z]{{5}}$")
    out = []
    for tid, kind, key in rows:
        if kind == "job" and key == job:
            out.append([tid, kind, key])
        elif kind == "pod" and pod.match(key):
            out.append([tid, kind, key])
        elif kind == "container" and "/" in key and pod.match(key.rsplit("/", 1)[0]):
            out.append([tid, kind, key])
    return out


def build_job_case(case_id: str, spec: dict[str, Any], image: str) -> tuple[dict[str, Any], dict[str, Any]]:
    case_dir = CASES_ROOT / case_id
    meta = json.loads((case_dir / "meta.json").read_text(encoding="utf-8"))
    live = [[str(r["target_id"]), str(r["resource_kind"]), str(r["resource_key"])] for r in dump_table(case_dir, "kcm_resource_targets", image)]
    start = _ts(meta["capture_start"])
    gone = []
    for r in dump_table(case_dir, "target_tombstones", image):
        if r.get("type") == "kubernetes_resource" and r.get("resource_key") and r.get("deleted_at") and _ts(str(r["deleted_at"])) >= start:
            kind = str(r["resource_kind"])
            gone.append([str(r["target_id"]), TOMBSTONE_KINDS.get(kind, kind), str(r["resource_key"])])
    k8s, seen = [], set()
    for source, rows in (("kcm_resource_targets", live), ("target_tombstones", gone)):
        for tid, kind, key in sorted(job_rows(rows, spec["job"]), key=lambda row: (row[2], row[1])):
            if tid not in seen:
                seen.add(tid)
                k8s.append({"id": tid, "kind": kind, "key": key, "source": source})
    if not any(row["kind"] == "job" for row in k8s):
        raise RuntimeError(f"{case_id}: no job target for {spec['job']}")
    ids = [row["id"] for row in k8s]
    entry = {"expected_status": "confirmed", "roots": [{"target_ids": ids, "target_aliases": list(spec["aliases"])}]}
    return entry, {"nodes": [], "k8s": k8s, "count": len(ids), "note": spec.get("note", "")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", type=Path, default=ROOT / "configs/eval/train-family-v3-expanded-20261015.yaml")
    parser.add_argument("--out", type=Path, default=ROOT / "configs/eval/train-family-v4-expanded-20261018.yaml")
    parser.add_argument("--new-only-out", type=Path, default=ROOT / "configs/eval/polestar-new9-20261018.yaml")
    parser.add_argument("--pg-image", default="postgres:17-alpine")
    parser.add_argument("--detail-json", type=Path)
    args = parser.parse_args()

    contract = yaml.safe_load(args.base.read_text(encoding="utf-8"))
    base_cases: dict[str, Any] = contract["cases"]
    dupes = [c for c in NEW9_ROOTS if c in base_cases]
    if dupes:
        raise RuntimeError(f"already exist in {args.base}: {dupes}")
    blocks = [entry_block(c, e) for c, e in base_cases.items()]
    new_cases, new_blocks, details = {}, [], {}
    for case_id, spec in NEW9_ROOTS.items():
        entry, detail = build_job_case(case_id, spec, args.pg_image) if "job" in spec else build_case(case_id, spec, args.pg_image, True)
        new_cases[case_id], details[case_id] = entry, detail
        new_blocks.append(entry_block(case_id, entry, spec.get("note", "")))
        kinds: dict[str, int] = {}
        for row in detail["k8s"]:
            kinds[row["kind"]] = kinds.get(row["kind"], 0) + 1
        nodes = ", ".join(f"{n['label']}({n['type']})" for n in detail["nodes"]) or "-"
        print(f"{case_id}: {detail['count']} ids | nodes: {nodes} | k8s: {kinds} "
              f"(tombstoned {sum(r['source'] == 'target_tombstones' for r in detail['k8s'])})", file=sys.stderr)

    out_text = render(contract, blocks + ["  # --- new9 (scripts/mcp_build_new9_golden.py, 2026-10-18): captures of 2026-10-15..18 ---\n"] + new_blocks,
                      f"# Generated by scripts/mcp_build_new9_golden.py from {args.base.name}: historical entries unchanged, "
                      f"plus {len(new_cases)} new9 entries.\n")
    merged = yaml.safe_load(out_text)
    if merged["cases"] != {**base_cases, **new_cases} or has_anchor(out_text):
        raise RuntimeError("expanded contract does not round-trip (or contains anchors)")
    new_text = render({k: v for k, v in contract.items() if k in ("version", "cases")}, new_blocks,
                      f"# Generated by scripts/mcp_build_new9_golden.py. The same {len(new_cases)} entries are appended to {args.out.name}.\n")
    if yaml.safe_load(new_text)["cases"] != new_cases or has_anchor(new_text):
        raise RuntimeError("new-only contract does not round-trip (or contains anchors)")
    args.out.write_text(out_text, encoding="utf-8")
    args.new_only_out.write_text(new_text, encoding="utf-8")
    if args.detail_json:
        args.detail_json.write_text(json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"out": str(args.out), "cases": len(merged["cases"]), "new": len(new_cases)}))


if __name__ == "__main__":
    main()
