"""시나리오 ground truth → 교사 모델 → RcaSample JSONL.

v0: 시나리오 스펙/스크립트만으로 '장애 상황 묘사(user)'와 'RCA 답변(assistant)'을 함께 합성한다.
실제 관측 데이터를 입력으로 쓰는 버전은 data/raw 를 읽는 별도 builder 로 추가한다.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml
from rich.progress import track

from rca_lab.data.io import write_jsonl
from rca_lab.data.schema import Message, RcaSample
from rca_lab.scenarios import Scenario, load_scenarios
from rca_lab.settings import REPO_ROOT, SYNTH_DIR

from .teachers import get_teacher

PROMPTS_DIR = REPO_ROOT / "prompts"


def _render(template: str, sc: Scenario, variant: int) -> str:
    return template.format(
        domain=sc.domain,
        title=sc.title,
        description=sc.description,
        root_cause=sc.root_cause,
        propagation=sc.propagation,
        expected_alarms="\n".join(f"- {a}" for a in sc.expected_alarms),
        expected_rca_root_cause=sc.expected_rca_root_cause,
        script=sc.script_text()[:6000],
        variant=variant,
    )


def _parse(raw: str) -> dict:
    """교사 출력에서 JSON 객체({"situation": ..., "analysis": ...}) 를 뽑는다."""
    s, e = raw.find("{"), raw.rfind("}")
    if s < 0 or e < 0:
        raise ValueError("no JSON object in teacher output")
    return json.loads(raw[s : e + 1])


def run(config_path: Path, teacher_name: str, out_path: Path | None = None) -> Path:
    cfg = yaml.safe_load(config_path.read_text())
    teacher = get_teacher(teacher_name, model=cfg.get("teacher_model", {}).get(teacher_name))
    template = (PROMPTS_DIR / cfg["prompt"]).read_text()
    system_prompt = (PROMPTS_DIR / cfg["student_system_prompt"]).read_text().strip()
    n_var = int(cfg.get("variants_per_scenario", 1))
    domains = set(cfg.get("domains") or [])

    scenarios = [s for s in load_scenarios() if not domains or s.domain in domains]
    out_path = out_path or SYNTH_DIR / f"{teacher_name}_{config_path.stem}.jsonl"
    samples: list[RcaSample] = []
    failures: list[str] = []

    for sc in track(scenarios, description=f"synth[{teacher_name}]"):
        for v in range(n_var):
            sid = hashlib.sha1(f"{sc.key}:{teacher_name}:{v}".encode()).hexdigest()[:12]
            try:
                obj = _parse(teacher.complete(_render(template, sc, v)))
                samples.append(
                    RcaSample(
                        id=sid,
                        scenario_key=sc.key,
                        messages=[
                            Message(role="system", content=system_prompt),
                            Message(role="user", content=obj["situation"]),
                            Message(role="assistant", content=obj["analysis"]),
                        ],
                        ground_truth=sc.root_cause,
                        teacher=teacher_name,
                        meta={"variant": v, "difficulty": sc.difficulty, "title": sc.title},
                    )
                )
            except Exception as ex:  # noqa: BLE001 — 한 샘플 실패로 전체를 멈추지 않는다
                failures.append(f"{sc.key}#{v}: {ex}")

    n = write_jsonl(out_path, samples)
    if failures:
        (out_path.with_suffix(".failures.txt")).write_text("\n".join(failures))
    print(f"wrote {n} samples → {out_path} ({len(failures)} failures)")
    return out_path
