"""Build the typed golden contract v5 (2026-10-23): the v4 contract plus 24 Polestar captures of 2026-10-18..22.

Same entry shape and expansion as scripts/mcp_build_new9_golden.py (one internal root per case: the root component's
topology node(s) plus every k8s identity of its workload from the case's own postgres.dump, tombstones included; a Job
root is the Job and its pods/containers). The 24 captures were verified before this run (reports in
outputs/flash-rl-loop-20261015/data-v5/verify/capture-verify2-[ABCD].md, all usable, 8 clean / 16 with caveats).

Roots by precedent: DB changes (DDL, metadata-lock queues behind an export, VACUUM FULL, table rename, account limits,
data corrections, sequence state) → DB instance; releases and env rollouts → service; backfill/export Jobs → Job (f59-r).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from mcp_build_new14_golden import (BANKING_ACCOUNT, BANKING_TRANSFER, FOOD_DISPATCH, FOOD_RESTAURANT, MYSQL_FOOD, ORACLE_BANKING,
                                    ROOT, build_case, entry_block, has_anchor, render)
from mcp_build_new9_golden import COMMERCE_POSTGRES, build_job_case

BANKING_API = {"aliases": ["core-banking-api", "banking-api", "api-service"], "workloads": ["rca-testbed-banking/testbed-api"]}
COMMERCE_NOTIFICATION = {"aliases": ["commerce-notification", "notification-service"], "workloads": ["rca-testbed-commerce/testbed-notification"]}
COMMERCE_GATEWAY = {"aliases": ["commerce-gateway", "gateway-service"], "workloads": ["rca-testbed-commerce/testbed-gateway"]}

NEW24_ROOTS: dict[str, dict[str, Any]] = {
    # train additions (a sibling of each family is already in train)
    "case-f16-p-v3-e40ec615": {**COMMERCE_NOTIFICATION, "note": "testbed-notification 릴리스 1.3.0(전체 회원 목록 조회 폭주) → notification root (user 는 피해자)"},
    "case-f35-p-v3-11824edf": {**ORACLE_BANKING, "note": "운영 Oracle 에서 BANKING.ACCOUNTS drop(DDL) → DB 인스턴스 root"},
    "case-f36-h-v3-b671604f": {**FOOD_RESTAURANT, "note": "testbed-restaurant 릴리스 1.6.0(개발 전용 설정 키 조회) → restaurant root (order 는 피해자)"},
    "case-f37-h-v3-a6cd30bd": {**BANKING_API, "note": "testbed-api 릴리스 1.6.0(개발 전용 이체 한도 설정 키) → api root"},
    "case-f39-h-v3-255629f2": {**BANKING_ACCOUNT, "note": "testbed-account 릴리스 1.6.0(이체 응답 계약 변경) → account root"},
    "case-f41-h-v3-2c392569": {**BANKING_ACCOUNT, "note": "testbed-account 릴리스 1.5.0(헬스 프로브 하위 경로 비활성 → liveness 404 재시작) → account root"},
    "case-f51-p-v3-1c6b181c": {**MYSQL_FOOD, "note": "빈 order-id 필터 재배차 보정이 배달완료 2,506건을 ASSIGNED 로 되돌림(DB 데이터 변경) → DB 인스턴스 root (f56-r 선례)"},
    "case-f53-h-v3-d20267b1": {**MYSQL_FOOD, "note": "payments ADD COLUMN 이 느린 export 뒤 메타데이터 잠금 대기열(DDL) → DB 인스턴스 root"},
    "case-f54-p-v3-fb67a747": {**MYSQL_FOOD, "note": "ALTER USER fooddelivery MAX_UPDATES_PER_HOUR 500(DB 계정 한도) → DB 인스턴스 root (f54-r 선례)"},
    "case-f57-p-v3-2b7db69d": {**COMMERCE_POSTGRES, "note": "commerce PostgreSQL 이관 후 inventory_movements_id_seq 100,000 뒤처짐(DB 상태) → DB 인스턴스 root (f57-r 선례)"},
    "case-f59-p-v3-6fd09c9b": {"aliases": ["ledger-statement-backfill"], "job": "rca-testbed-banking/ledger-statement-backfill",
                               "note": "ledger-statement-backfill Job(48 curl 워커)의 TRANSFERS 풀스캔이 transfer Hikari 풀 점유 → Job root (f59-r 선례, 토폴로지 노드 없음)"},
    # new validation (families never trained)
    "case-f60-h-v3-1aece1d2": {**MYSQL_FOOD, "note": "dispatches ADD COLUMN 이 느린 export 뒤 메타데이터 잠금 대기열(DDL) → DB 인스턴스 root"},
    "case-f60-p-v3-04d37516": {**MYSQL_FOOD, "note": "취소된 인덱스 백필이 dispatch_outbox_events 를 보류 테이블로 rename(DDL) → DB 인스턴스 root"},
    "case-f60-r-v3-22036218": {**MYSQL_FOOD, "note": "FK 마이그레이션의 dispatch_events copy to tmp table, 쓰기가 메타데이터 잠금 대기(DDL) → DB 인스턴스 root"},
    "case-f63-p-v3-ddf34faa": {**FOOD_DISPATCH, "note": "testbed-dispatch 릴리스 1.9.0(조회당 parked 스레드 누수 → 컨테이너 OOM) → dispatch root"},
    "case-f64-p-v3-7b180ead": {**BANKING_API, "note": "testbed-api env 롤아웃(transferClient slow-call 차단 100ms) → api root"},
    "case-f64-r-v3-9014da8a": {**COMMERCE_GATEWAY, "note": "testbed-gateway env 롤아웃(order 브레이커 slow-call 차단 30ms) → gateway root"},
    "case-f65-r-v3-b5383723": {**BANKING_TRANSFER, "note": "testbed-transfer 롤아웃이 낡은 DB_SERVICE=XEPDB1 재적용(ORA-12514, 엔드포인트 없음) → transfer root (Oracle 정상)"},
    "case-f66-p-v3-a79bda26": {**COMMERCE_POSTGRES, "note": "outbox_events ADD COLUMN 이 느린 export COPY 뒤 잠금 대기열(DDL) → DB 인스턴스 root"},
    "case-f66-r-v3-346c6170": {**COMMERCE_POSTGRES, "note": "users ADD COLUMN 이 느린 export COPY 뒤 잠금 대기열(DDL) → DB 인스턴스 root"},
    "case-f67-h-v3-53919475": {**COMMERCE_POSTGRES, "note": "order_items ADD COLUMN 이 느린 매출 export 뒤 잠금 대기열(DDL) → DB 인스턴스 root"},
    "case-f67-r-v3-654a133c": {**COMMERCE_POSTGRES, "note": "운영자 VACUUM (FULL) payment_logs 잠금(DB 운영 작업) → DB 인스턴스 root"},
    "case-f68-r-v3-3f6561ef": {**BANKING_TRANSFER, "note": "core-banking-transfer 2.4.0 릴리스(전역 transferVelocity rate limiter 1개) → transfer root"},
    "case-f69-r-v3-ca74a36b": {"aliases": ["payment-audit-export"], "job": "rca-testbed-food/payment-audit-export",
                               "note": "payment-audit-export Job 의 mysqldump 기본 --lock-tables 가 payments·settlement_summary 잠금 → Job root (f59-r 선례). MySQL·payment 는 피해자"},
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", type=Path, default=ROOT / "configs/eval/train-family-v4-expanded-20261018.yaml")
    parser.add_argument("--out", type=Path, default=ROOT / "configs/eval/train-family-v5-expanded-20261023.yaml")
    parser.add_argument("--new-only-out", type=Path, default=ROOT / "configs/eval/polestar-new24-20261023.yaml")
    parser.add_argument("--pg-image", default="postgres:17-alpine")
    parser.add_argument("--detail-json", type=Path)
    args = parser.parse_args()

    contract = yaml.safe_load(args.base.read_text(encoding="utf-8"))
    base_cases: dict[str, Any] = contract["cases"]
    dupes = [c for c in NEW24_ROOTS if c in base_cases]
    if dupes:
        raise RuntimeError(f"already exist in {args.base}: {dupes}")
    blocks = [entry_block(c, e) for c, e in base_cases.items()]
    new_cases, new_blocks, details = {}, [], {}
    for case_id, spec in NEW24_ROOTS.items():
        entry, detail = build_job_case(case_id, spec, args.pg_image) if "job" in spec else build_case(case_id, spec, args.pg_image, True)
        new_cases[case_id], details[case_id] = entry, detail
        new_blocks.append(entry_block(case_id, entry, spec.get("note", "")))
        kinds: dict[str, int] = {}
        for row in detail["k8s"]:
            kinds[row["kind"]] = kinds.get(row["kind"], 0) + 1
        nodes = ", ".join(f"{n['label']}({n['type']})" for n in detail["nodes"]) or "-"
        print(f"{case_id}: {detail['count']} ids | nodes: {nodes} | k8s: {kinds} "
              f"(tombstoned {sum(r['source'] == 'target_tombstones' for r in detail['k8s'])})", file=sys.stderr)

    out_text = render(contract, blocks + ["  # --- new24 (scripts/mcp_build_new24_golden.py, 2026-10-23): captures of 2026-10-18..22 ---\n"] + new_blocks,
                      f"# Generated by scripts/mcp_build_new24_golden.py from {args.base.name}: historical entries unchanged, "
                      f"plus {len(new_cases)} new24 entries.\n")
    merged = yaml.safe_load(out_text)
    if merged["cases"] != {**base_cases, **new_cases} or has_anchor(out_text):
        raise RuntimeError("expanded contract does not round-trip (or contains anchors)")
    new_text = render({k: v for k, v in contract.items() if k in ("version", "cases")}, new_blocks,
                      f"# Generated by scripts/mcp_build_new24_golden.py. The same {len(new_cases)} entries are appended to {args.out.name}.\n")
    if yaml.safe_load(new_text)["cases"] != new_cases or has_anchor(new_text):
        raise RuntimeError("new-only contract does not round-trip (or contains anchors)")
    args.out.write_text(out_text, encoding="utf-8")
    args.new_only_out.write_text(new_text, encoding="utf-8")
    if args.detail_json:
        args.detail_json.write_text(json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"out": str(args.out), "cases": len(merged["cases"]), "new": len(new_cases)}))


if __name__ == "__main__":
    main()
