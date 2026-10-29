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
import hashlib
import re
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

# V3 (2026-10-21 audit): V2 graded "the triggering change" without saying what counts as one, so a candidate that named
# the load as the trigger and a candidate that named the starting change while saying its initiator was unobserved
# could both land on 0.5 or 1 (eval-u034 vs reference, same telemetry). The scenario references treat load as the
# condition that exposes a defect and identify the trigger through its observable effect.
JUDGE_SYSTEM_V3 = JUDGE_SYSTEM_V2.replace(
    'Answer with JSON only:',
    """What counts as the trigger: the change or fault that started the failure at the root - a deployment or rollout, a configuration
or resource-limit change, a removal or schema change, a scale change, a stopped dependency, a long-held transaction or other operation,
or an injected fault. A load or traffic condition that only exposed the defect is not the trigger: naming such a condition as the trigger
while leaving out the starting change does not match the trigger. When the candidate identifies the starting change itself from telemetry
(for instance through its observable effect) but says that its initiator, the machine or tool it came from, or the exact command was not
observed, count the trigger as matched - the initiator is usually invisible in telemetry even when the reference narrates it.
Answer with JSON only:""", 1)
assert JUDGE_SYSTEM_V3 != JUDGE_SYSTEM_V2

JUDGE_PROMPTS = {"v1": JUDGE_SYSTEM, "v2": JUDGE_SYSTEM_V2, "v3": JUDGE_SYSTEM_V3}

EVIDENCE_RUBRIC_VERSION = "rca-trajectory-evidence-v1"
EVIDENCE_JUDGE_SYSTEM = f"""You grade whether one completed RCA trajectory supports its submitted conclusion.

Return exactly one JSON object with this schema:
{{
  "path_score": 0.0,
  "hallucination_score": 0.0,
  "supported_inferences": [
    {{"claim": "short claim", "evidence_refs": ["m3"], "explanation": "why the observation supports it"}}
  ],
  "unsupported_or_fabricated_claims": [
    {{"claim": "short claim", "evidence_refs": ["m7"], "severity": "minor|major", "explanation": "why unsupported or contradicted"}}
  ],
  "reason": "one short overall reason"
}}

Rubric version: {EVIDENCE_RUBRIC_VERSION}.

Use only the supplied trajectory evidence. Evidence refs point to actual user/tool observations
available before the final submit call. Treat assistant text as claims, not evidence. Do not use
the reference answer, case identity, outside knowledge, or later hidden data.

path_score is 0..1 for how well the submitted root and mechanism are grounded in the observed
investigation path. Reward concise, decisive use of relevant observations, but do not reward more
turns or repeated queries by itself. A reasonable inference from converging observations may score
well when the cited observations are real.

hallucination_score is 0..1 for fabricated observed facts, contradicted measurements, invented
configuration changes, or claiming direct observation of unobserved internals. Do not penalize a
clearly marked uncertainty or a cautious inference merely because one extra check was not run.

Every supported or contradicted factual judgment must cite evidence_refs from the supplied list.
If the evidence is insufficient to judge, use lower path_score and explain; do not invent evidence.
"""

FENCE_RE = re.compile(r"\A```(?:json)?\s*(\{.*\})\s*```\Z", re.DOTALL)


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


def strict_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    match = FENCE_RE.fullmatch(stripped)
    if match:
        stripped = match.group(1).strip()
    decoder = json.JSONDecoder()
    value, end = decoder.raw_decode(stripped)
    if stripped[end:].strip():
        raise ValueError("response contains trailing text after JSON object")
    if not isinstance(value, dict):
        raise TypeError("response must be a JSON object")
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def build_trajectory_evidence_item(
    messages: list[dict[str, Any]],
    result: dict[str, Any],
    score: dict[str, Any],
) -> dict[str, Any]:
    """Build a full-observation evidence item for one submitted RCA episode."""
    evidence_refs = []
    submit_positions = []
    for position, message in enumerate(messages):
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                if function.get("name") == "submit_rca":
                    submit_positions.append(position)
    final_submit_position = submit_positions[-1] if submit_positions else None
    if score.get("stop_reason") == "submitted" and final_submit_position is None:
        raise ValueError("submitted episode has no submit_rca tool call")
    call_lookup = {}
    for position, message in enumerate(messages[:final_submit_position]):
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if call.get("id"):
                call_lookup[str(call["id"])] = {
                    "message_position": position,
                    "function": call.get("function") or {},
                }
    for position, message in enumerate(messages[:final_submit_position]):
        role = message.get("role")
        if role not in {"user", "tool"} or not str(message.get("content", "")):
            continue
        ref = {
            "ref_id": f"m{position}",
            "message_position": position,
            "role": role,
            "content": message.get("content", ""),
            "source_sha256": hashlib.sha256(str(message.get("content", "")).encode("utf-8")).hexdigest(),
        }
        if role == "tool":
            call_id = message.get("tool_call_id")
            ref["tool_name"] = message.get("name")
            ref["tool_call_id"] = call_id
            if call_id in call_lookup:
                ref["paired_call"] = call_lookup[str(call_id)]
        evidence_refs.append(ref)
    return {
        "rubric_version": EVIDENCE_RUBRIC_VERSION,
        "input": {
            "final_submit_position": final_submit_position,
            "stop_reason": score.get("stop_reason"),
        },
        "submitted_conclusion": {
            "status": result.get("status"),
            "summary": result.get("summary", ""),
            "causes": result.get("causes", []),
            "external_causes": result.get("external_causes", []),
        },
        "trajectory_evidence": {
            "source": "full trajectory user/tool observations before final submit; assistant text is excluded except paired tool-call arguments",
            "refs": evidence_refs,
            "total_refs": len(evidence_refs),
            "truncated": False,
        },
    }


def validate_trajectory_evidence_verdict(item: dict[str, Any], verdict: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(verdict, dict):
        raise TypeError("evidence verdict must be a JSON object")
    available = {ref["ref_id"] for ref in item.get("trajectory_evidence", {}).get("refs", [])}

    def score(name: str) -> float:
        value = verdict.get(name)
        if isinstance(value, bool) or type(value) not in (int, float) or not 0 <= float(value) <= 1:
            raise ValueError(f"{name} must be a number in [0,1]")
        return float(value)

    def claims(name: str, *, require_refs: bool) -> list[dict[str, Any]]:
        value = verdict.get(name, [])
        if not isinstance(value, list):
            raise TypeError(f"{name} must be a list")
        normalized = []
        for row in value:
            if not isinstance(row, dict):
                raise TypeError(f"{name} items must be objects")
            refs = row.get("evidence_refs", [])
            if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs):
                raise ValueError(f"{name} items require an evidence_refs list of strings")
            # The rubric requires refs for supported or contradicted judgments; a fabricated claim that no observation
            # supports has nothing to cite (same rule as evidence_audit). Refs that are given must still be real.
            if require_refs and not refs:
                raise ValueError(f"{name} items require nonempty evidence_refs")
            missing = sorted(set(refs) - available)
            if missing:
                raise ValueError(f"{name} references unavailable evidence refs: {missing}")
            normalized.append({
                "claim": str(row.get("claim", "")).strip(),
                "evidence_refs": refs,
                "explanation": str(row.get("explanation", "")).strip(),
                **({"severity": row["severity"]} if "severity" in row else {}),
            })
            if not normalized[-1]["claim"] or not normalized[-1]["explanation"]:
                raise ValueError(f"{name} items require claim and explanation")
        return normalized

    reason = str(verdict.get("reason", "")).strip()
    if not reason:
        raise ValueError("evidence verdict requires a reason")
    supported = claims("supported_inferences", require_refs=True)
    unsupported = claims("unsupported_or_fabricated_claims", require_refs=False)
    path_score = score("path_score")
    hallucination_score = score("hallucination_score")
    if path_score > 0 and not supported:
        raise ValueError("positive path_score requires at least one supported inference")
    if hallucination_score > 0 and not unsupported:
        raise ValueError("positive hallucination_score requires at least one unsupported/fabricated claim")
    return {
        "path_score": path_score,
        "hallucination_score": hallucination_score,
        "supported_inferences": supported,
        "unsupported_or_fabricated_claims": unsupported,
        "reason": reason,
    }


EVIDENCE_PROVIDERS = ("codex", "claude")


def evidence_judge_cache_path(run: Path, provider: str = "codex", *, model: str = "gpt-6.1-sol") -> Path:
    if provider not in EVIDENCE_PROVIDERS:
        raise ValueError(f"unsupported evidence judge provider: {provider}")
    return run / f"evidence_judge.{provider}.{quote(model, safe='')}.{EVIDENCE_RUBRIC_VERSION}.json"


def evidence_cache_valid(record: dict[str, Any], item: dict[str, Any], *, model: str, provider: str = "codex") -> bool:
    if not isinstance(record, dict):
        return False
    if record.get("provider") != provider or record.get("model") != model:
        return False
    if record.get("rubric_version") != EVIDENCE_RUBRIC_VERSION:
        return False
    if record.get("rubric_prompt_sha256") != hashlib.sha256(EVIDENCE_JUDGE_SYSTEM.encode("utf-8")).hexdigest():
        return False
    if record.get("item_sha256") != sha256_json(item):
        return False
    verdict = record.get("verdict")
    if not isinstance(verdict, dict):
        return False
    try:
        validate_trajectory_evidence_verdict(item, verdict)
    except (TypeError, ValueError):
        return False
    return True


def judge_trajectory_evidence(
    run: Path,
    *,
    messages: list[dict[str, Any]],
    result: dict[str, Any],
    score: dict[str, Any],
    model: str = "gpt-6.1-sol",
    timeout_seconds: int = 240,
    provider: str = "codex",
) -> tuple[dict[str, Any] | None, str]:
    """Evidence judge; provider "claude" (claude CLI, e.g. claude-opus-5-5) added 2026-10-15 while Codex is quota-limited.
    The rubric prompt, item and verdict validation are identical; caches are provider/model specific."""
    item = build_trajectory_evidence_item(messages, result, score)
    path = evidence_judge_cache_path(run, provider, model=model)
    if path.exists():
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            record = None
        if record is not None and evidence_cache_valid(record, item, model=model, provider=provider):
            return record, "cached"
    prompt = json.dumps({"item": item}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if provider == "codex":
        from rca_lab.mcp.codex_cli import run_codex

        text, diag = run_codex(system_prompt=EVIDENCE_JUDGE_SYSTEM, prompt=prompt, model=model, timeout_seconds=timeout_seconds)
    else:
        from rca_lab.mcp.claude_cli import run_claude

        text, diag = run_claude(system_prompt=EVIDENCE_JUDGE_SYSTEM, prompt=prompt, model=model, timeout_seconds=timeout_seconds)
    if text is None:
        return None, diag
    try:
        verdict = validate_trajectory_evidence_verdict(item, strict_json_object(text))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return None, f"unusable evidence judge response: {exc}; raw={text[:300]!r}"
    record = {
        "provider": provider,
        "model": model,
        "rubric_version": EVIDENCE_RUBRIC_VERSION,
        "rubric_prompt_sha256": hashlib.sha256(EVIDENCE_JUDGE_SYSTEM.encode("utf-8")).hexdigest(),
        "item_sha256": sha256_json(item),
        "item": item,
        "verdict": verdict,
    }
    path.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return record, "judged"


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


def judge_cache_path(run: Path, provider: str = "claude", *, model: str | None = None, failure: bool = False,
                     rubric: str | None = None) -> Path:
    if provider not in {"claude", "codex"}:
        raise ValueError(f"unsupported mechanism judge provider: {provider}")
    suffix = ".codex" if provider == "codex" else ""
    if provider == "codex" and model:
        suffix += "." + quote(model, safe="")
    if rubric not in (None, "v1", "v2"):   # v3+ verdicts live beside the v1/v2 file instead of overwriting it
        suffix += f".{rubric}"
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
