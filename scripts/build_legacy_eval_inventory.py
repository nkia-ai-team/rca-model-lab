"""Inventory historical non-MCP experiments without exporting prompts or responses."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CONDITION_KEYS = (
    "partition", "reasoning_strength", "structured_output_backend", "schema_enforcement",
    "actor_temperature", "actor_top_p", "actor_top_k", "actor_max_completion_tokens",
    "actor_seed", "agent_turns", "agent_max_duration", "guided_action_thought",
    "request_contract_enforced", "decision_contract", "model_artifact_sha256",
)


def read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def build(source: Path) -> dict:
    roots = {p for p in source.iterdir() if p.is_dir()}
    roots.update(p.parent for p in source.rglob("run-manifest.json"))
    roots.update(p.parent for p in source.rglob("scores.json"))
    reports = {p.name.removesuffix("-report.json"): p for p in source.glob("*-report.json")}
    roots.update(source / name for name in reports)
    rows = []
    for root in sorted(roots):
        manifest = read_json(root / "run-manifest.json") or {}
        report_path = reports.get(root.name) if root.parent == source else None
        report = read_json(report_path) if report_path else None
        scores_path = root / "scores.json"
        scores = read_json(scores_path)
        if isinstance(scores, dict):
            scores = scores.get("rows")
        episodes = []
        if isinstance(scores, list):
            for record in scores:
                if isinstance(record, list) and len(record) == 3 and isinstance(record[2], dict):
                    episodes.append({**record[2], "case_id": record[0]})
                elif isinstance(record, dict):
                    episodes.append(record)
        elif isinstance(report, dict) and isinstance(report.get("runs"), list):
            episodes = [r for r in report["runs"] if isinstance(r, dict)]
        scored = [r for r in episodes if number(r.get("root_f1"))]
        turns = [r["turns"] for r in episodes if number(r.get("turns"))]
        walls = [r["wall_seconds"] for r in episodes if number(r.get("wall_seconds"))]
        cases = sorted({r["case_id"] for r in episodes if isinstance(r.get("case_id"), str)})
        planned_cases = manifest.get("cases")
        repeats = manifest.get("runs")
        expected = len(planned_cases) * repeats if isinstance(planned_cases, list) and isinstance(repeats, int) else None
        completed = len(episodes) if episodes else (report.get("completed_runs") if isinstance(report, dict) else None)
        hits = sum(r["root_f1"] == 1 for r in scored) if scored else None
        legacy_strict = sum(r.get("strict_correct") is True for r in episodes) if episodes else None
        telemetry = root / "inference-telemetry.jsonl"
        api_tokens = api_seconds = requests = 0
        if telemetry.exists():
            for line in telemetry.read_text().splitlines():
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                elapsed, tokens = item.get("elapsed_seconds"), item.get("completion_tokens")
                if number(elapsed) and elapsed > 0 and number(tokens) and tokens >= 0:
                    requests += 1
                    api_seconds += elapsed
                    api_tokens += tokens
        trajectory_count = sum(1 for p in root.glob("case-*/traj-run*") if p.is_dir())
        notes = ["구형 non-MCP 하네스: strict_correct는 근거 충족 기준이며 원인+기전 채점과 다릅니다."]
        if not episodes:
            notes.append("저장된 점수 집계 없음. 궤적 디렉터리 수는 완료 수나 정답률이 아닙니다.")
        if requests:
            notes.append("tok/s는 inference-telemetry의 usage와 API 경과시간이 함께 있는 요청 기준입니다. 전체 시나리오 속도가 아닙니다.")
        rows.append({
            "name": str(root.relative_to(source)), "category": "legacy-nonmcp",
            "source_directory": str(root), "report_path": str(report_path) if report_path else None,
            "scores_path": str(scores_path) if scores_path.exists() else None,
            "manifest_path": str(root / "run-manifest.json") if manifest else None,
            "model": manifest.get("model") if isinstance(manifest.get("model"), str) else None,
            "conditions": {k: manifest[k] for k in CONDITION_KEYS if isinstance(manifest.get(k), (str, int, float, bool))},
            "expected_runs": expected, "completed_runs": completed,
            "trajectory_directory_count": trajectory_count,
            "case_count": len(cases) if cases else None,
            "planned_case_count": len(planned_cases) if isinstance(planned_cases, list) else None,
            "root_hits": hits, "root_scored_runs": len(scored),
            "root_rate": hits / len(scored) if scored else None,
            "mean_root_f1": sum(r["root_f1"] for r in scored) / len(scored) if scored else None,
            "mechanism_hits": None, "mechanism_rate": None, "legacy_strict_hits": legacy_strict,
            "mean_wall_seconds": sum(walls) / len(walls) if walls else None,
            "wall_coverage": len(walls), "mean_turns": sum(turns) / len(turns) if turns else None,
            "wall_seconds_per_turn": sum(walls) / sum(turns) if walls and len(walls) == len(episodes) == len(turns) and sum(turns) else None,
            "output_tokens": api_tokens if requests else None,
            "tokens_per_second": api_tokens / api_seconds if api_seconds else None,
            "throughput_source": "inference-telemetry API elapsed" if requests else None,
            "api_measured_requests": requests,
            "mean_api_request_seconds": api_seconds / requests if requests else None,
            "metric_notes": notes,
        })
    return {"schema_version": 1, "source_root": str(source),
            "generated_at_kst": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
            "notes": ["과거 실험 인벤토리. 훈련 모니터·smoke·부분 실행을 포함하며 같은 테스트셋으로 간주하지 않습니다.",
                      "원인 정답률은 저장된 root_f1==1 비율. 미완료 예정 실행은 분모에 임의로 추가하지 않습니다.",
                      "기전 점수는 모든 항목 미측정(null). 텍스트 로그에서 점수·토큰 수를 추정하지 않습니다."],
            "experiments": rows}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[2] / "rca-model-lab/outputs/eval")
    parser.add_argument("--output", type=Path, default=Path("outputs/reports/all-evaluations-20261013/legacy-nonmcp.json"))
    args = parser.parse_args()
    document = build(args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output": str(args.output), "experiments": len(document["experiments"]),
                      "with_scores": sum(r["root_scored_runs"] > 0 for r in document["experiments"]),
                      "with_api_timing": sum(r["api_measured_requests"] > 0 for r in document["experiments"])}))


if __name__ == "__main__":
    main()
