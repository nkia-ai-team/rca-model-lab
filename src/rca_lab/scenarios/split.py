"""Family split manifest validation for teacher production."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class TeacherSplit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    teacher: str
    accepted_trajectories_per_case: int = Field(ge=1)
    max_attempts_per_case: int = Field(ge=1)
    max_turns: int = Field(ge=1)
    train: tuple[str, ...]
    sealed_eval: tuple[str, ...]
    excluded: tuple[str, ...]

    @model_validator(mode="after")
    def disjoint_and_unique(self) -> TeacherSplit:
        groups = {
            "train": self.train,
            "sealed_eval": self.sealed_eval,
            "excluded": self.excluded,
        }
        for name, values in groups.items():
            if len(values) != len(set(values)):
                raise ValueError(f"{name} contains duplicates")
        if set(self.train) & set(self.sealed_eval):
            raise ValueError("train and sealed_eval overlap")
        if set(self.train) & set(self.excluded):
            raise ValueError("train and excluded overlap")
        if set(self.sealed_eval) & set(self.excluded):
            raise ValueError("sealed_eval and excluded overlap")
        train_families = {_family_id(case_id) for case_id in self.train}
        sealed_families = {_family_id(case_id) for case_id in self.sealed_eval}
        overlap = sorted(train_families & sealed_families)
        if overlap:
            raise ValueError(f"train and sealed_eval families overlap: {overlap}")
        return self

    @property
    def all_cases(self) -> frozenset[str]:
        return frozenset((*self.train, *self.sealed_eval, *self.excluded))

    def validate_case_root(self, case_root: Path) -> None:
        actual = {path.parent.name for path in case_root.glob("case-*/meta.json")}
        missing = sorted(self.all_cases - actual)
        unexpected = sorted(actual - self.all_cases)
        if missing or unexpected:
            raise ValueError(f"case inventory mismatch: missing={missing}, unexpected={unexpected}")
        for case_id in self.train:
            meta = _read_meta(case_root / case_id / "meta.json")
            if meta.get("evaluation_eligible") is not True:
                raise ValueError(f"train case is not evaluation eligible: {case_id}")
        for case_id in self.excluded:
            meta = _read_meta(case_root / case_id / "meta.json")
            if meta.get("evaluation_eligible") is not False:
                raise ValueError(f"excluded case must be ineligible: {case_id}")


def load_teacher_split(path: Path) -> TeacherSplit:
    return TeacherSplit.model_validate(yaml.safe_load(path.read_text()))


def _read_meta(path: Path) -> dict[str, object]:
    return json.loads(path.read_text())


_FAMILY_RE = re.compile(r"^case-(f[0-9]+)-", re.IGNORECASE)


def _family_id(case_id: str) -> str:
    match = _FAMILY_RE.match(case_id)
    if match is None:
        # Synthetic unit-test IDs still receive deterministic family identity.
        return case_id.lower()
    return match.group(1).lower()
