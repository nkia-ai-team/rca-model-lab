"""Causal-depth judge (2026-10-19, user: explain the failing target and its cause precisely — not only "memory pressure"
but what produced it and what started that).

A separate judge from the trajectory-evidence judge (whose rubric, caches and trainer validation stay unchanged). It reads
the same full-observation item (observations before the final submit + the submitted conclusion) and reports, for the
root cause only, which links of its causal chain the conclusion states AND the observations support:

  failure_mode     what failed at the origin (the resource, state or error at the origin component)
  proximate_cause  what at the origin produced that failure (the specific process, workload, query, operation,
                   connection or configuration value)
  trigger          what started it (a change or start event: deployment / rollout, configuration or limit change,
                   data or schema change, job start, dependency change, injected or load condition)

depth = supported link types / 3, computed here from the verdict (not taken from the judge as a number). Unsupported
detail earns nothing; fabrication is penalised by the evidence judge. Correctness against the reference is graded by the
mechanism judge, so this judge never sees the reference.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from rca_lab.mcp.rl_reward import build_trajectory_evidence_item, sha256_json, strict_json_object

CAUSAL_DEPTH_RUBRIC_VERSION = "rca-causal-depth-v1"
CAUSAL_DEPTH_RUBRIC_V2 = "rca-causal-depth-v2"
LINKS = ("failure_mode", "proximate_cause", "trigger")
CAUSAL_DEPTH_SYSTEM = f"""You grade how deeply one submitted RCA conclusion explains its ROOT cause, using only the supplied trajectory evidence.

Return exactly one JSON object:
{{
  "links": [
    {{"link": "failure_mode|proximate_cause|trigger", "stated": "short paraphrase of what the conclusion says",
      "supported": true, "evidence_refs": ["m3"], "explanation": "why the cited observations do or do not support it"}}
  ],
  "reason": "one short overall reason"
}}

Rubric version: {CAUSAL_DEPTH_RUBRIC_VERSION}.

The causal chain of the root cause has three links:
- failure_mode: what failed at the origin component - the resource, state or error there.
- proximate_cause: what at the origin produced that failure - the specific process, workload, query, operation,
  connection or configuration value responsible.
- trigger: what started it - a change or start event such as a deployment or rollout, a configuration or limit change,
  a data or schema change, a job or batch start, a dependency change, or an injected or load condition.

For each link the conclusion states about the root cause, add one entry. Set supported=true only when observations in
the supplied refs show it (directly or by a reasonable inference from converging observations) and cite those refs.
A link the conclusion states without such observations gets supported=false. Omit links the conclusion does not state.
Grade only the root cause, not downstream propagation. Do not reward length or repetition: one entry per link type.
Treat assistant text as claims, not evidence. Do not use outside knowledge, the case identity or any reference answer.
"""


# v2 (2026-10-21): v1 listed "injected or load condition" under trigger, which rewarded naming the load as the trigger
# (trained policies then wrote "the trigger was normal traffic" and dropped the starting change). Load is the condition
# that exposed a defect, never a trigger; a starting change seen through its effect counts even with an unseen initiator.
CAUSAL_DEPTH_SYSTEM_V2 = CAUSAL_DEPTH_SYSTEM.replace(f"Rubric version: {CAUSAL_DEPTH_RUBRIC_VERSION}.", f"Rubric version: {CAUSAL_DEPTH_RUBRIC_V2}.").replace(
    """- trigger: what started it - a change or start event such as a deployment or rollout, a configuration or limit change,
  a data or schema change, a job or batch start, a dependency change, or an injected or load condition.""",
    """- trigger: what started it - the change or start event at or affecting the origin: a deployment or rollout, a configuration
  or limit change, a removal or schema change, a scale change, a job or batch start, a stopped dependency, a long-held
  transaction or other operation, or an injected fault. A load or traffic condition that only exposed the defect is NOT a
  trigger and earns no link. A starting change identified through its observable effect counts even when its initiator
  or exact command is not observed.""")
assert CAUSAL_DEPTH_SYSTEM_V2 != CAUSAL_DEPTH_SYSTEM and "earns no link" in CAUSAL_DEPTH_SYSTEM_V2
DEPTH_RUBRICS = {CAUSAL_DEPTH_RUBRIC_VERSION: CAUSAL_DEPTH_SYSTEM, CAUSAL_DEPTH_RUBRIC_V2: CAUSAL_DEPTH_SYSTEM_V2}


def depth_item(messages: list[dict[str, Any]], result: dict[str, Any], score: dict[str, Any],
               rubric: str = CAUSAL_DEPTH_RUBRIC_VERSION) -> dict[str, Any]:
    item = build_trajectory_evidence_item(messages, result, score)
    item["rubric_version"] = rubric
    return item


def validate_causal_depth_verdict(item: dict[str, Any], verdict: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(verdict, dict):
        raise TypeError("causal depth verdict must be a JSON object")
    available = {ref["ref_id"] for ref in item.get("trajectory_evidence", {}).get("refs", [])}
    rows = verdict.get("links")
    if not isinstance(rows, list):
        raise TypeError("links must be a list")
    links, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError("link items must be objects")
        link = row.get("link")
        if link not in LINKS:
            raise ValueError(f"unknown link {link!r}")
        if link in seen:
            raise ValueError(f"duplicate link {link}")
        seen.add(link)
        supported = row.get("supported")
        if type(supported) is not bool:
            raise ValueError("supported must be a boolean")
        refs = row.get("evidence_refs", [])
        if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs):
            raise ValueError("evidence_refs must be a list of strings")
        missing = sorted(set(refs) - available)
        if missing:
            raise ValueError(f"link references unavailable evidence refs: {missing}")
        if supported and not refs:
            raise ValueError("a supported link must cite evidence refs")
        stated, explanation = str(row.get("stated", "")).strip(), str(row.get("explanation", "")).strip()
        if not stated or not explanation:
            raise ValueError("link items require stated and explanation")
        links.append({"link": link, "stated": stated, "supported": supported, "evidence_refs": refs, "explanation": explanation})
    reason = str(verdict.get("reason", "")).strip()
    if not reason:
        raise ValueError("causal depth verdict requires a reason")
    depth = round(sum(1 for row in links if row["supported"]) / len(LINKS), 4)
    return {"links": links, "reason": reason, "depth": depth}


def causal_depth_cache_path(run: Path, provider: str, *, model: str, rubric: str = CAUSAL_DEPTH_RUBRIC_VERSION) -> Path:
    return run / f"causal_depth.{provider}.{quote(model, safe='')}.{rubric}.json"


def causal_depth_cache_valid(record: dict[str, Any], item: dict[str, Any], *, model: str, provider: str) -> bool:
    if not isinstance(record, dict) or record.get("provider") != provider or record.get("model") != model:
        return False
    rubric = record.get("rubric_version")
    if rubric not in DEPTH_RUBRICS or item.get("rubric_version") != rubric:
        return False
    if record.get("rubric_prompt_sha256") != hashlib.sha256(DEPTH_RUBRICS[rubric].encode("utf-8")).hexdigest():
        return False
    if record.get("item_sha256") != sha256_json(item):
        return False
    try:
        return validate_causal_depth_verdict(item, record.get("verdict")) == record.get("verdict")
    except (TypeError, ValueError):
        return False


def judge_causal_depth(run: Path, *, messages: list[dict[str, Any]], result: dict[str, Any], score: dict[str, Any],
                       model: str, provider: str = "claude", timeout_seconds: int = 240,
                       rubric: str = CAUSAL_DEPTH_RUBRIC_VERSION) -> tuple[dict[str, Any] | None, str]:
    system = DEPTH_RUBRICS[rubric]
    item = depth_item(messages, result, score, rubric)
    path = causal_depth_cache_path(run, provider, model=model, rubric=rubric)
    if path.exists():
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            record = None
        if record is not None and causal_depth_cache_valid(record, item, model=model, provider=provider):
            return record, "cached"
    prompt = json.dumps({"item": item}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if provider == "codex":
        from rca_lab.mcp.codex_cli import run_codex

        text, diag = run_codex(system_prompt=system, prompt=prompt, model=model, timeout_seconds=timeout_seconds)
    else:
        from rca_lab.mcp.claude_cli import run_claude

        text, diag = run_claude(system_prompt=system, prompt=prompt, model=model, timeout_seconds=timeout_seconds)
    if text is None:
        return None, diag
    try:
        verdict = validate_causal_depth_verdict(item, strict_json_object(text))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return None, f"unusable causal depth response: {exc}; raw={text[:300]!r}"
    record = {
        "provider": provider, "model": model, "rubric_version": rubric,
        "rubric_prompt_sha256": hashlib.sha256(system.encode("utf-8")).hexdigest(),
        "item_sha256": sha256_json(item), "item": item, "verdict": verdict,
    }
    path.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return record, "judged"
