"""Decisive-evidence recall (2026-10-21, user: score whether the answer found the evidence the scenario author calls
decisive, not only whether its claims are grounded).

The scenario metadata's distinguishing_evidence lists the observations that decide the case. The judge splits it into
positive decisive items (contrasts of the form "it is not X: ..." are left out) and, for each, decides from the
trajectory whether the submitted conclusion used it (stated, backed by a cited observation), the investigation only
observed it (present in an observation before the submit, not used), or it is missing. Measurement only.

  recall   = cited / items          (the answer rests on the decisive evidence)
  observed = (cited + observed) / items   (the investigation reached it)
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from rca_lab.mcp.rl_reward import build_trajectory_evidence_item, sha256_json, strict_json_object

DECISIVE_RUBRIC_VERSION = "rca-decisive-evidence-v2"   # v2: items extracted once per case, every run judged against them
STATUSES = ("cited", "observed", "missing")
MAX_ITEMS = 6
EXTRACT_SYSTEM = f"""Split a scenario author's description of the decisive evidence of an incident into at most {MAX_ITEMS}
positive decisive observations - what is seen in the telemetry: an error text, a metric change, an event, a span pattern.
Leave out contrasts that only describe other scenarios ("it is not X: ...") and narrative that no telemetry shows.
Return exactly one JSON object: {{"items": ["one decisive observation in a short phrase", ...]}}"""

DECISIVE_SYSTEM = f"""You check whether one RCA investigation found the evidence that decides its case.

Input: "decisive_items" (a fixed numbered list of the decisive observations of the case), the submitted conclusion, and the
trajectory observations (refs m<N>, user/tool messages before the final submit).

Return exactly one JSON object with one entry per decisive item, in the same order:
{{
  "items": [
    {{"evidence": "the decisive item as given", "status": "cited|observed|missing",
      "evidence_refs": ["m3"], "explanation": "one sentence"}}
  ],
  "reason": "one short overall reason"
}}

Rubric version: {DECISIVE_RUBRIC_VERSION}.

For each decisive item:
- cited: the submitted conclusion states this observation (wording, units and time zone may differ) and an observation
  in the refs shows it; cite those refs.
- observed: an observation in the refs shows it, but the conclusion does not use it; cite those refs.
- missing: no observation in the refs shows it.
Judge only from the supplied observations; treat assistant text as claims, not evidence.
"""


def extract_decisive_items(decisive_evidence: str, cache: Path, *, model: str, provider: str = "claude",
                           timeout_seconds: int = 180) -> list[str] | None:
    """Decisive items of one case, extracted once and cached (cache is keyed by the evidence text and prompt)."""
    key = hashlib.sha256((EXTRACT_SYSTEM + "\n" + decisive_evidence).encode("utf-8")).hexdigest()
    if cache.exists():
        try:
            record = json.loads(cache.read_text(encoding="utf-8"))
            if record.get("key") == key and record.get("model") == model:
                return record["items"]
        except (json.JSONDecodeError, KeyError):
            pass
    if provider == "codex":
        from rca_lab.mcp.codex_cli import run_codex
        text, _ = run_codex(system_prompt=EXTRACT_SYSTEM, prompt=decisive_evidence, model=model, timeout_seconds=timeout_seconds)
    else:
        from rca_lab.mcp.claude_cli import run_claude
        text, _ = run_claude(system_prompt=EXTRACT_SYSTEM, prompt=decisive_evidence, model=model, timeout_seconds=timeout_seconds)
    if text is None:
        return None
    try:
        items = [str(x).strip() for x in strict_json_object(text)["items"] if str(x).strip()][:MAX_ITEMS]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not items:
        return None
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"key": key, "model": model, "items": items}, ensure_ascii=False, indent=1), encoding="utf-8")
    return items


def decisive_item(messages: list[dict[str, Any]], result: dict[str, Any], score: dict[str, Any], decisive_items: list[str]) -> dict[str, Any]:
    item = build_trajectory_evidence_item(messages, result, score)
    item["rubric_version"] = DECISIVE_RUBRIC_VERSION
    item["decisive_items"] = list(decisive_items)
    return item


def validate_decisive_verdict(item: dict[str, Any], verdict: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(verdict, dict):
        raise TypeError("decisive verdict must be a JSON object")
    available = {ref["ref_id"] for ref in item.get("trajectory_evidence", {}).get("refs", [])}
    rows = verdict.get("items")
    if not isinstance(rows, list) or len(rows) != len(item.get("decisive_items") or []) or not rows:
        raise ValueError("items must have one entry per decisive item")
    items = []
    for row in rows:
        if not isinstance(row, dict) or row.get("status") not in STATUSES:
            raise ValueError("each item needs a status in cited|observed|missing")
        refs = row.get("evidence_refs", [])
        if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
            raise ValueError("evidence_refs must be a list of strings")
        if sorted(set(refs) - available):
            raise ValueError("item references unavailable evidence refs")
        if row["status"] != "missing" and not refs:
            raise ValueError("cited / observed items must cite evidence refs")
        evidence, explanation = str(row.get("evidence", "")).strip(), str(row.get("explanation", "")).strip()
        if not evidence or not explanation:
            raise ValueError("items require evidence and explanation")
        items.append({"evidence": evidence, "status": row["status"], "evidence_refs": refs, "explanation": explanation})
    reason = str(verdict.get("reason", "")).strip()
    if not reason:
        raise ValueError("decisive verdict requires a reason")
    n = len(items)
    cited = sum(1 for r in items if r["status"] == "cited")
    observed = sum(1 for r in items if r["status"] != "missing")
    return {"items": items, "reason": reason, "recall": round(cited / n, 4), "observed": round(observed / n, 4)}


def decisive_cache_path(run: Path, provider: str, *, model: str) -> Path:
    return run / f"decisive_evidence.{provider}.{quote(model, safe='')}.{DECISIVE_RUBRIC_VERSION}.json"


def judge_decisive_evidence(run: Path, *, messages: list[dict[str, Any]], result: dict[str, Any], score: dict[str, Any],
                            decisive_items: list[str], model: str, provider: str = "claude",
                            timeout_seconds: int = 300) -> tuple[dict[str, Any] | None, str]:
    item = decisive_item(messages, result, score, decisive_items)
    path = decisive_cache_path(run, provider, model=model)
    prompt_sha = hashlib.sha256(DECISIVE_SYSTEM.encode("utf-8")).hexdigest()
    if path.exists():
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if (record.get("model") == model and record.get("provider") == provider and record.get("rubric_prompt_sha256") == prompt_sha
                    and record.get("item_sha256") == sha256_json(item)
                    and validate_decisive_verdict(item, record["verdict"]) == record["verdict"]):
                return record, "cached"
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass
    prompt = json.dumps({"item": item}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if provider == "codex":
        from rca_lab.mcp.codex_cli import run_codex

        text, diag = run_codex(system_prompt=DECISIVE_SYSTEM, prompt=prompt, model=model, timeout_seconds=timeout_seconds)
    else:
        from rca_lab.mcp.claude_cli import run_claude

        text, diag = run_claude(system_prompt=DECISIVE_SYSTEM, prompt=prompt, model=model, timeout_seconds=timeout_seconds)
    if text is None:
        return None, diag
    try:
        verdict = validate_decisive_verdict(item, strict_json_object(text))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return None, f"unusable decisive evidence response: {exc}; raw={text[:300]!r}"
    record = {"provider": provider, "model": model, "rubric_version": DECISIVE_RUBRIC_VERSION, "rubric_prompt_sha256": prompt_sha,
              "item_sha256": sha256_json(item), "verdict": verdict}
    path.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return record, "judged"
