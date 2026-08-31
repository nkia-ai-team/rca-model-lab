"""Training-only RCA taskset with deterministic, evaluation-identical reward."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Literal

import verifiers.v1 as vf
import yaml
from pydantic import Field

from rca_lab.eval.scoring import (
    EvalContract,
    ExpectedCase,
    load_episode_text,
    score_episode,
)
from rca_lab.prime_rl import ScenarioLease, parse_incident_id
from rca_lab.prime_rl.paths import project_path


class RCAStudentData(vf.TaskData):
    case_id: str
    expected: ExpectedCase


class RCAStudentTaskConfig(vf.TaskConfig):
    restore_path: Path = Field(
        default_factory=lambda: project_path("scripts/restore_eval_case.sh")
    )
    lease_dir: Path = Path("/tmp/rca-prime-rl-scenario")
    max_episode_bytes: int = 64 * 1024 * 1024
    reward_stage: Literal["exploration_bootstrap", "diagnosis"] = "diagnosis"


class RCAStudentTask(vf.Task[RCAStudentData, vf.State, RCAStudentTaskConfig]):
    def __init__(self, data: RCAStudentData, config: RCAStudentTaskConfig | None = None):
        super().__init__(data, config)
        self._lease: ScenarioLease | None = None

    @property
    def key(self) -> str:
        return self.data.case_id

    async def setup(self, runtime: vf.Runtime) -> None:
        self._lease = ScenarioLease(self.config.lease_dir)
        state = await asyncio.to_thread(self._lease.acquire, self.data.case_id)
        try:
            if self._lease.needs_restore:
                result = await runtime.run(
                    [str(self.config.restore_path), self.data.case_id], {}
                )
                if result.exit_code:
                    detail = (result.stderr or result.stdout).strip()[-2000:]
                    raise RuntimeError(f"scenario restore failed: {detail}")
                incident_id = parse_incident_id(result.stdout + result.stderr)
                state = await asyncio.to_thread(
                    self._lease.publish, self.data.case_id, incident_id
                )
            if state is None:
                raise RuntimeError("scenario lease has no restored state")
            await runtime.write(".rca-incident-id", (state.incident_id + "\n").encode())
        except BaseException:
            await asyncio.to_thread(self._lease.release)
            self._lease = None
            raise

    async def finalize(self, trace: vf.Trace, runtime: vf.Runtime) -> None:
        if self._lease is not None:
            await asyncio.to_thread(self._lease.release)
            self._lease = None

    async def _score(self, runtime: vf.Runtime) -> dict[str, Any]:
        raw = await runtime.read("episode.jsonl", max_bytes=self.config.max_episode_bytes)
        # Reuse the exact deterministic scorer used for sealed evaluation.
        episode = load_episode_text(raw.decode("utf-8"), source="Prime-RL episode.jsonl")
        return score_episode(
            self.data.case_id,
            self.data.expected.model_dump(mode="json"),
            episode,
        )

    @vf.reward(weight=1.0)
    async def rca_reward(self, runtime: vf.Runtime) -> float:
        score = await self._score(runtime)
        reward_key = {
            "exploration_bootstrap": "exploration_bootstrap_reward",
            "diagnosis": "optimization_reward",
        }[self.config.reward_stage]
        return float(score[reward_key])

    @vf.metric
    async def rca_metrics(self, runtime: vf.Runtime) -> dict[str, float]:
        score = await self._score(runtime)
        return {
            "evaluation_reward": float(score["reward"]),
            "optimization_reward": float(score["optimization_reward"]),
            "exploration_bootstrap_reward": float(
                score["exploration_bootstrap_reward"]
            ),
            "root_f1": float(score["root_f1"]),
            "strict_correct": float(score["strict_correct"]),
            "proof_rate": float(score["proof_rate"]),
            "evidence_complete": float(score["evidence_complete"]),
            "format_errors": float(score["format_errors"]),
            "unsupported_confirmation": float(score["unsupported_confirmation"]),
            "turns": float(score["turns"]),
            "observed_evidence_refs": float(score["observed_evidence_refs"]),
            "grounded_answer_refs": float(score["grounded_answer_refs"]),
        }


class RCAStudentConfig(vf.TasksetConfig):
    contract: Path = Field(
        default_factory=lambda: project_path("configs/eval/train-family-v2.yaml")
    )
    eligible_dataset: Path = Field(
        default_factory=lambda: project_path(
            "data/processed/sft-teacher-v3-contract-clean.jsonl"
        )
    )
    task: RCAStudentTaskConfig = RCAStudentTaskConfig()


class RCAStudentTaskset(vf.Taskset[RCAStudentTask, RCAStudentConfig]):
    def load(self) -> list[RCAStudentTask]:
        contract = EvalContract.model_validate(
            yaml.safe_load(self.config.contract.read_text(encoding="utf-8"))
        )
        eligible = {
            str(json.loads(line)["scenario_id"])
            for line in self.config.eligible_dataset.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        if eligible != set(contract.cases):
            raise ValueError(
                "Prime-RL train contract must exactly match the accepted SFT scenario population"
            )
        return [
            RCAStudentTask(
                RCAStudentData(
                    idx=index,
                    name=case_id,
                    case_id=case_id,
                    prompt="Investigate the restored RCA incident using the production student harness.",
                    expected=expected,
                    timeout=vf.TaskTimeout(setup=600, agent=1800, finalize=30, scoring=60),
                ),
                self.config.task,
            )
            for index, (case_id, expected) in enumerate(contract.cases.items())
        ]
