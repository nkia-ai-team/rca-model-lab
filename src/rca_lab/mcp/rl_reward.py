"""RL reward for the rca-mcp student (on-policy GRPO). Separate from the evaluation score
(rca_lab.mcp.score), which stays unchanged so hit rate / root F1 remain comparable across runs.

Differences from the evaluation reward, each closing a gap found in the 2026-09-29 review:
  * mechanism: the evaluation only matches root-cause target UUIDs, so a right target with a wrong
    causal story scored full marks. A Claude judge (claude-opus-5-5 by default) grades the submitted mechanism against the
    scenario's reference cause (grader-only meta, train partition only; the student sees only the
    scalar reward) as 1 / 0.5 / 0, weighted by root F1.
  * wrong confirmation: the evaluation clips at 0, so "wrong + confirmed" and "wrong + provisional"
    both scored 0. Here the penalty is applied without clipping: a confident wrong answer is worse.
  * efficiency: a budget-forced answer and tool calls beyond the soft budget cost a little; a run
    that never submits is below a wrong-but-honest submission.

rl = 0.45 q + 0.15 [f1=1] + 0.10 grounding q + 0.30 mech q
     - 0.10 [forced answer] - 0.05 min(1, max(0, calls-30)/20),   q = recall x precision^2
no submission: -0.30

Root term q (2026-10-01, user decision "B+C"): with plain F1 partial credit, naming extra candidates
paid off whenever the student was unsure (1 root: a 30%-sure single guess earns 0.30 in expectation,
two candidates that cover the root 60% of the time earned 0.6 x 0.567 = 0.34). Squaring precision keeps the
credit for naming one of two roots (recall 0.5 -> q 0.5) but makes a wrong extra candidate cost
more than it can win (one right + one wrong: F1 0.67 -> q 0.25). The answer contract also caps
candidates at 3 (answer.MAX_CANDIDATES). `root_term="f1"` reproduces the earlier formula.

The confidence level (status) is not a training target (user decision 2026-10-01): the former
+0.10 [status correct] f1 term moved to the mechanism weight and the -0.30 wrong-confirmation
penalty is gone. `status_terms=True` reproduces the pre-2026-10-01 formula
(0.45 f1 + 0.15 [f1=1] + 0.10 grounding f1 + 0.10 [status] f1 + 0.20 mech f1 - 0.30 [confirmed and f1<1] ...).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

FORCED_MARKERS = ("조사 예산이 끝났다", "컨텍스트 한도에 도달했다")
SOFT_CALL_BUDGET = 30
CALL_SPAN = 20


def forced_answer(messages: list[dict[str, Any]]) -> bool:
    return any(m.get("role") == "user" and isinstance(m.get("content"), str) and m["content"].startswith(FORCED_MARKERS) for m in messages)


def root_quality(score: dict[str, Any], root_term: str = "precision2") -> float:
    """Root-cause credit of one run: recall x precision^2 (default) or plain F1 (legacy)."""
    if root_term == "f1":
        return float(score.get("root_f1", 0.0))
    if "root_precision" not in score or "root_recall" not in score:
        raise ValueError("score has no root_precision/root_recall (rescore it, or use root_term='f1')")
    return float(score["root_recall"]) * float(score["root_precision"]) ** 2


def rl_reward(score: dict[str, Any], *, mechanism: float | None, forced: bool, status_terms: bool = False, root_term: str = "precision2") -> float:
    """`mechanism` is the judge grade in {0, 0.5, 1} (None when not judged: f1 == 0, or judge failure -> 0.5).
    `status_terms` = legacy confidence-level terms (datasets built before 2026-10-01); `root_term="f1"` = legacy root credit."""
    if score.get("stop_reason") != "submitted" or score.get("status") in (None, "missing"):
        return -0.30
    f1 = float(score.get("root_f1", 0.0))
    q = root_quality(score, root_term)
    mech = 0.5 if mechanism is None else float(mechanism)
    reward = 0.45 * q + 0.15 * float(f1 == 1.0) + 0.10 * float(score.get("ref_grounding", 0.0)) * q
    if status_terms:
        reward += 0.10 * float(bool(score.get("status_correct"))) * q + 0.20 * mech * q
        if score.get("status") == "confirmed" and f1 < 1.0:
            reward -= 0.30
    else:
        reward += 0.30 * mech * q
    if forced:
        reward -= 0.10
    calls = int(score.get("tool_calls", 0))
    reward -= 0.05 * min(1.0, max(0.0, (calls - SOFT_CALL_BUDGET) / CALL_SPAN))
    return round(reward, 6)


JUDGE_SYSTEM = """You grade whether an incident root-cause conclusion identifies the same causal mechanism as a reference description.
The target system/component naming is scored elsewhere; do NOT grade naming or the confidence level. Grade only the mechanism:
what failed or changed, and how it produced the impact.
- 1   : same mechanism (same trigger / failure mode / causal chain; wording, language and level of detail may differ)
- 0.5 : right failure area but a key element is missing or different (e.g. right component but a different resource or reason;
        one of two independent causes described, the other missing or wrong)
- 0   : a different mechanism, or no mechanism stated
Answer with JSON only: {"score": 0 | 0.5 | 1, "reason": "<one sentence>"}"""


# V2 (2026-09-29 audit): V1 graded omitted implementation-level links as "key element missing" and collapsed to 0.5
# (Opus 5.5: 31/38). V2 states what the investigator can observe and grades the root of the chain.
JUDGE_SYSTEM_V2 = """You grade whether an incident root-cause conclusion identifies the same causal mechanism as a reference description written by the scenario author.
Context: the investigator only sees telemetry - metrics, logs, traces, Kubernetes events, and database sessions and queries. Source code, annotations,
class or method names, retry / circuit-breaker settings and configuration-file contents are NOT visible to them. The reference, however, often narrates the
full implementation-level chain from the root fault to user impact.
Grade the ROOT of the chain: the triggering change or fault and the failure mode it caused at the root component. Do not lower the grade for omitting
implementation internals the investigator cannot observe, or for describing the downstream propagation to users less completely. A different wording, or a
different but equivalent observable description of the same phenomenon, counts as the same mechanism. Target naming and the confidence level are scored
elsewhere; ignore them.
- 1   : the root trigger and the root failure mode both match
- 0.5 : right root area, but the trigger or the root failure mode is missing, vague or different (e.g. right component but a different resource or reason),
        or only one of several independent root causes is described
- 0   : a different root mechanism, or only symptoms without a mechanism
Answer with JSON only: {"score": 0 | 0.5 | 1, "reason": "<one sentence>"}"""

JUDGE_PROMPTS = {"v1": JUDGE_SYSTEM, "v2": JUDGE_SYSTEM_V2}


def judge_mechanism(reference: str, conclusion: str, *, model: str, timeout_seconds: int = 180, rubric: str = "v2", provider: str = "claude") -> tuple[float | None, str]:
    from rca_lab.mcp.claude_cli import run_claude
    from rca_lab.mcp.codex_cli import run_codex

    prompt = json.dumps({"reference_cause": reference, "candidate_conclusion": conclusion}, ensure_ascii=False)
    runners = {"claude": run_claude, "codex": run_codex}
    if provider not in runners:
        raise ValueError(f"unsupported mechanism judge provider: {provider}")
    text, diag = runners[provider](system_prompt=JUDGE_PROMPTS[rubric], prompt=prompt, model=model, timeout_seconds=timeout_seconds)
    if not text:
        return None, diag
    start, end = text.find("{"), text.rfind("}")
    try:
        value = json.loads(text[start : end + 1]).get("score")
    except (ValueError, AttributeError):
        return None, f"unparsable: {text[:160]!r}"
    if type(value) not in (int, float) or value not in (0, 0.5, 1):
        return None, f"bad score {value!r}"
    return float(value), text[:300]


def conclusion_text(result: dict[str, Any]) -> str:
    parts = [f"summary: {result.get('summary', '')}"]
    parts += [f"cause: {c.get('mechanism', '')}" for c in result.get("causes", [])]
    parts += [f"external cause: {c.get('name', '')} {c.get('id', '')}" for c in result.get("external_causes", [])]
    return "\n".join(parts)


REFERENCE_OVERRIDES = Path(__file__).resolve().parents[3] / "configs/eval/reference-overrides.yaml"


def reference_override(case_id: str) -> dict[str, Any] | None:
    """Grader-side correction of a capture's reference cause (configs/eval/reference-overrides.yaml)."""
    if not REFERENCE_OVERRIDES.exists():
        return None
    import yaml

    return (yaml.safe_load(REFERENCE_OVERRIDES.read_text(encoding="utf-8")) or {}).get("cases", {}).get(case_id)


def reference_version(case_id: str) -> str:
    override = reference_override(case_id)
    return str(override["version"]) if override else "meta"


def judge_cache_path(run: Path, provider: str = "claude", *, model: str | None = None, failure: bool = False) -> Path:
    if provider not in {"claude", "codex"}:
        raise ValueError(f"unsupported mechanism judge provider: {provider}")
    suffix = ".codex" if provider == "codex" else ""
    if provider == "codex" and model:
        suffix += "." + quote(model, safe="")
    return run / f"mechanism_judge{suffix}{'.failure' if failure else ''}.json"


def judge_cache_valid(entry: dict[str, Any], case_id: str, *, model: str, rubric: str, provider: str = "claude") -> bool:
    """A cached mechanism verdict is reusable only for the same judge model, rubric and reference text."""
    return (entry.get("provider", "claude") == provider and entry.get("model") == model
            and entry.get("rubric", "v1") == rubric and entry.get("reference", "meta") == reference_version(case_id))


def reference_cause(case_dir: Path) -> str:
    override = reference_override(case_dir.name)
    if override:
        return f"{override['title']}\n{override['cause']}"
    meta = json.loads((case_dir / "meta.json").read_text(encoding="utf-8"))
    sm = meta.get("scenario_metadata") or {}
    return f"{sm.get('title', '')}\n{sm.get('cause', '')}"
