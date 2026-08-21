"""모든 단계(합성→정제→학습→평가)가 공유하는 샘플 스키마. JSONL 한 줄 = RcaSample 하나."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class RcaSample(BaseModel):
    id: str
    scenario_key: str  # e.g. plopvape-shop/scenario-01
    messages: list[Message]  # TRL conversational 포맷 그대로
    ground_truth: str  # 스펙의 root_cause (RL reward / eval 용)
    teacher: str | None = None  # claude | codex | ...
    meta: dict = Field(default_factory=dict)

    def to_trl(self) -> dict:
        return {"messages": [m.model_dump() for m in self.messages]}
