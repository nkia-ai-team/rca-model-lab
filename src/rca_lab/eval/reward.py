"""RCA 답변 채점기. SFT 평가와 GRPO reward 가 같은 함수를 쓴다.

v0 은 자리만 잡는다. 실제 채점은 (a) 키워드/엔티티 매칭, (b) LLM-judge 로 rubric(expected_rca_root_cause)
대비 채점 — 둘을 조합하는 방향.
"""

from __future__ import annotations


def score(answer: str, ground_truth: str, rubric: str = "") -> float:
    """0.0 ~ 1.0. v0: 미구현."""
    raise NotImplementedError
