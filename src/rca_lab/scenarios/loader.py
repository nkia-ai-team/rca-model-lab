"""rca-scenario-runner 의 service-spec.yaml 을 ground truth 로 읽는다."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel

from rca_lab.settings import SCENARIO_RUNNER_DIR


class Scenario(BaseModel):
    domain: str
    id: str
    title: str
    description: str = ""
    root_cause: str
    propagation: str = ""
    expected_alarms: list[str] = []
    expected_rca_root_cause: str = ""
    difficulty: int | None = None
    script_path: Path | None = None

    @property
    def key(self) -> str:
        return f"{self.domain}/{self.id}"

    def script_text(self) -> str:
        return (
            self.script_path.read_text() if self.script_path and self.script_path.exists() else ""
        )


def load_scenarios(runner_dir: Path = SCENARIO_RUNNER_DIR) -> list[Scenario]:
    out: list[Scenario] = []
    for spec in sorted((runner_dir / "scenarios" / "services").glob("*/service-spec.yaml")):
        doc = yaml.safe_load(spec.read_text()) or {}
        domain = doc.get("service", {}).get("name", spec.parent.name)
        for s in doc.get("scenarios", []):
            script = spec.parent / "scripts" / s["file"] if s.get("file") else None
            out.append(
                Scenario(
                    domain=domain,
                    id=s["id"],
                    title=s.get("title", s["id"]),
                    description=s.get("description", ""),
                    root_cause=s.get("root_cause", ""),
                    propagation=s.get("propagation", ""),
                    expected_alarms=s.get("expected_alarms", []) or [],
                    expected_rca_root_cause=s.get("expected_rca_root_cause", ""),
                    difficulty=s.get("difficulty"),
                    script_path=script,
                )
            )
    return out
