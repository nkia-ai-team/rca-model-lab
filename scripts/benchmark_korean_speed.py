#!/usr/bin/env python3
"""Korean OpenAI-compatible streaming speed benchmark for vanilla RCA models.

This measures API-visible streaming timing. It is not a pure decoder benchmark:
servers batch SSE chunks, and usage arrives only after the model/server decides
to report it. Raw SSE and raw /metrics snapshots are kept for re-analysis.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

Clock = Callable[[], float]


PROMPTS: tuple[dict[str, str], ...] = (
    {
        "id": "ko-rca-payment-latency",
        "title": "결제 서비스 지연",
        "prompt": (
            "당신은 SRE RCA 분석가입니다. 아래 관측만 보고 한국어로 원인과 기전을 설명하세요.\n\n"
            "증상: 10:05 이후 checkout API p95 지연이 220ms에서 4.8s로 증가했습니다.\n"
            "관측:\n"
            "- checkout, payment, inventory 중 payment의 outbound HTTP 5xx가 동시에 증가했습니다.\n"
            "- payment pod CPU와 메모리는 정상 범위입니다.\n"
            "- payment 로그에는 `connection pool exhausted for postgres-payment`가 반복됩니다.\n"
            "- DB 노드 db-payment-2의 active session이 평소 40개에서 620개로 증가했습니다.\n"
            "- 10:03에 payment deployment의 DB pool max 설정이 80에서 800으로 변경되었습니다.\n\n"
            "요구사항: root cause, 증거, 전파 기전, 배제한 후보를 간결하지만 충분히 쓰세요."
        ),
    },
    {
        "id": "ko-rca-recommend-error",
        "title": "추천 서비스 오류율",
        "prompt": (
            "다음은 장애 조사용 요약 관측입니다. 외부 도구 없이 한국어로 RCA 결론을 작성하세요.\n\n"
            "증상: recommendation 서비스의 HTTP 500 비율이 0.2%에서 18%로 상승했습니다.\n"
            "관측:\n"
            "- recommendation의 upstream인 feature-store 호출 지연과 timeout이 급증했습니다.\n"
            "- feature-store 자체의 application log에는 `schema version mismatch: expected v17 got v18`가 보입니다.\n"
            "- 같은 시간대 Kafka lag, 노드 CPU, 네트워크 packet loss는 정상입니다.\n"
            "- 14:21에 feature-store configmap이 갱신되었고, 일부 pod만 재시작되었습니다.\n"
            "- 재시작된 pod만 v18 스키마를 사용하며, 이전 pod는 v17을 계속 사용합니다.\n\n"
            "요구사항: 가장 가능성 높은 원인, 장애가 recommendation 500으로 보인 이유, 확인해야 할 추가 신호를 쓰세요."
        ),
    },
    {
        "id": "ko-rca-search-saturation",
        "title": "검색 서비스 포화",
        "prompt": (
            "아래 증거를 기반으로 한국어 RCA를 작성하세요. 추측은 관측과 분리해서 표시하세요.\n\n"
            "증상: search-api의 성공률이 99.8%에서 92%로 하락하고 p99 지연이 9초까지 증가했습니다.\n"
            "관측:\n"
            "- search-api pod 수는 24개로 동일하며 CPU 사용률은 35% 이하입니다.\n"
            "- search-worker 큐 depth가 2천에서 90만으로 증가했습니다.\n"
            "- elasticsearch ingest thread pool rejected가 같은 시각부터 증가했습니다.\n"
            "- elasticsearch data node disk util이 98%이고 iowait이 45%입니다.\n"
            "- 09:58에 catalog reindex job이 수동 실행되었고 평소보다 12배 많은 문서를 재색인했습니다.\n\n"
            "요구사항: root cause와 mechanism을 나누고, 왜 search-api CPU가 낮아도 장애가 발생했는지 설명하세요."
        ),
    },
)


@dataclass
class StreamObservation:
    raw_events: list[dict[str, Any]] = field(default_factory=list)
    content_parts: list[str] = field(default_factory=list)
    reasoning_parts: list[str] = field(default_factory=list)
    first_delta_at: float | None = None
    last_delta_at: float | None = None
    end_at: float | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    finish_reason: str | None = None

    @property
    def content(self) -> str:
        return "".join(self.content_parts)

    @property
    def reasoning(self) -> str:
        return "".join(self.reasoning_parts)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def read_json_arg(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    text = Path(value[1:]).read_text(encoding="utf-8") if value.startswith("@") else value
    data = json.loads(text)
    if not isinstance(data, dict):
        raise TypeError("--extra-body must be a JSON object")
    return data


def _choice_delta_text(choice: dict[str, Any]) -> tuple[str, str]:
    delta = choice.get("delta") or {}
    content = delta.get("content") or ""
    reasoning = (
        delta.get("reasoning_content")
        or delta.get("reasoning")
        or delta.get("reasoning_text")
        or ""
    )
    if choice.get("finish_reason"):
        content = content or ""
    return str(content), str(reasoning)


def parse_sse_lines(
    lines: Iterable[bytes | str],
    *,
    start_at: float,
    clock: Clock,
) -> StreamObservation:
    observation = StreamObservation()
    end_at = start_at
    for line in lines:
        text = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line
        text = text.strip()
        if not text or text.startswith(":") or not text.startswith("data:"):
            continue
        received_at = clock()
        end_at = received_at
        payload = text[5:].strip()
        if payload == "[DONE]":
            observation.raw_events.append({"received_at": received_at, "done": True})
            observation.end_at = received_at
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError as exc:
            observation.raw_events.append(
                {"received_at": received_at, "raw": payload, "decode_error": str(exc)}
            )
            continue
        observation.raw_events.append({"received_at": received_at, "event": event})
        usage = event.get("usage")
        if isinstance(usage, dict):
            observation.usage = usage
        for choice in event.get("choices") or []:
            if choice.get("finish_reason"):
                observation.finish_reason = choice.get("finish_reason")
            content, reasoning = _choice_delta_text(choice)
            if content or reasoning:
                if observation.first_delta_at is None:
                    observation.first_delta_at = received_at
                observation.last_delta_at = received_at
                if content:
                    observation.content_parts.append(content)
                if reasoning:
                    observation.reasoning_parts.append(reasoning)
    if observation.end_at is None:
        observation.end_at = end_at
    return observation


def openai_stream_request(
    endpoint: str,
    request_body: dict[str, Any],
    *,
    timeout: float,
    api_key: str,
    clock: Clock = time.perf_counter,
) -> tuple[StreamObservation, float, float]:
    url = endpoint.rstrip("/") + "/v1/chat/completions"
    body = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    start_at = clock()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        observation = parse_sse_lines(response, start_at=start_at, clock=clock)
    total_seconds = (observation.end_at or clock()) - start_at
    return observation, total_seconds, start_at


def fetch_metrics(endpoint: str, *, timeout: float) -> str | None:
    request = urllib.request.Request(endpoint.rstrip("/") + "/metrics", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError):
        return None


METRIC_LINE = re.compile(
    r"^([a-zA-Z_:][a-zA-Z0-9_:]*)({[^}]*})?\s+"
    r"(-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)$"
)
SPEC_ACCEPTED_COUNTERS = {"vllm:spec_decode_num_accepted_tokens_total"}
SPEC_DRAFT_COUNTERS = {
    "vllm:spec_decode_num_draft_tokens_total",
}
SPEC_DRAFT_STEP_COUNTERS = {
    "vllm:spec_decode_num_drafts_total",
}
SPEC_KNOWN_COUNTERS = SPEC_ACCEPTED_COUNTERS | SPEC_DRAFT_COUNTERS | SPEC_DRAFT_STEP_COUNTERS


def extract_speculative_metrics(metrics_text: str | None) -> dict[str, dict[str, Any]] | None:
    if metrics_text is None:
        return None
    values: dict[str, dict[str, Any]] = {}
    for raw_line in metrics_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = METRIC_LINE.match(line)
        if not match:
            continue
        name = match.group(1)
        if name not in SPEC_KNOWN_COUNTERS:
            continue
        labels = match.group(2) or ""
        key = name + labels
        values[key] = {"name": name, "labels": labels, "value": float(match.group(3))}
    return values


def diff_metrics(
    before: dict[str, dict[str, Any]] | None,
    after: dict[str, dict[str, Any]] | None,
) -> dict[str, Any]:
    if before is None or after is None:
        return {"available": False, "reason": "/metrics unavailable"}
    keys = sorted(set(before) | set(after))
    deltas = {}
    for key in keys:
        before_row = before.get(key, {"value": 0.0, "name": after.get(key, {}).get("name")})
        after_row = after.get(key, {"value": 0.0, "name": before.get(key, {}).get("name")})
        deltas[key] = {
            "name": after_row.get("name") or before_row.get("name"),
            "labels": after_row.get("labels") or before_row.get("labels") or "",
            "delta": after_row.get("value", 0.0) - before_row.get("value", 0.0),
        }
    accepted = sum(
        row["delta"] for row in deltas.values() if row["name"] in SPEC_ACCEPTED_COUNTERS
    )
    drafted = sum(
        row["delta"] for row in deltas.values() if row["name"] in SPEC_DRAFT_COUNTERS
    )
    draft_steps = sum(
        row["delta"] for row in deltas.values() if row["name"] in SPEC_DRAFT_STEP_COUNTERS
    )
    if not deltas:
        return {
            "available": False,
            "reason": "Known vLLM speculative counters were not present in /metrics.",
            "known_counters": sorted(SPEC_KNOWN_COUNTERS),
        }
    acceptance_ratio = accepted / drafted if drafted > 0 else None
    return {
        "available": True,
        "counter_deltas": deltas,
        "accepted_counter_delta_sum": accepted,
        "draft_token_counter_delta_sum": drafted,
        "draft_step_counter_delta_sum": draft_steps,
        "acceptance_ratio_from_token_counters": acceptance_ratio,
        "mean_accepted_tokens_per_draft_step": accepted / draft_steps if draft_steps > 0 else None,
        "draft_or_spec_counter_delta_sum": drafted,
        "acceptance_ratio_from_named_counters": acceptance_ratio,
        "note": "Only known vLLM speculative counters are aggregated; raw /metrics snapshots are authoritative.",
    }


def usage_int(usage: dict[str, Any], key: str) -> int:
    value = usage.get(key, 0)
    return int(value) if isinstance(value, int | float) else 0


def build_request(
    model: str,
    prompt: str,
    *,
    max_tokens: int,
    temperature: float,
    extra_body: dict[str, Any],
) -> dict[str, Any]:
    request = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    request.update(extra_body)
    return request


def summarize_observation(
    observation: StreamObservation,
    *,
    total_seconds: float,
    start_at: float,
    request_body: dict[str, Any],
    before_metrics: str | None,
    after_metrics: str | None,
) -> dict[str, Any]:
    first = observation.first_delta_at
    last = observation.last_delta_at
    generation_window = (last - first) if first is not None and last is not None and last >= first else None
    completion_tokens = usage_int(observation.usage, "completion_tokens")
    prompt_tokens = usage_int(observation.usage, "prompt_tokens")
    total_tokens = usage_int(observation.usage, "total_tokens")
    reasoning_tokens: int | None = None
    details = observation.usage.get("completion_tokens_details")
    if isinstance(details, dict):
        reasoning_tokens = usage_int(details, "reasoning_tokens")
    if reasoning_tokens == 0 and observation.reasoning:
        reasoning_tokens = None
    before = extract_speculative_metrics(before_metrics)
    after = extract_speculative_metrics(after_metrics)
    return {
        "timing": {
            "total_api_seconds": total_seconds,
            "ttft_seconds": (first - start_at) if first is not None else None,
            "generation_window_seconds": generation_window,
            "completion_tokens_per_generation_window_second": (
                completion_tokens / generation_window
                if generation_window and generation_window > 0 and completion_tokens
                else None
            ),
            "rate_note": "Approximate API-visible streaming rate; SSE chunk batching prevents pure decoder tok/s.",
        },
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "reasoning_tokens": reasoning_tokens,
            "raw": observation.usage,
        },
        "finish_reason": observation.finish_reason,
        "output_truncated_by_max_tokens": observation.finish_reason == "length",
        "hashes": {
            "request_sha256": sha256_text(canonical_json(request_body)),
            "content_sha256": sha256_text(observation.content),
            "reasoning_sha256": sha256_text(observation.reasoning),
            "raw_sse_sha256": sha256_text(canonical_json(observation.raw_events)),
            "metrics_before_sha256": sha256_text(before_metrics or ""),
            "metrics_after_sha256": sha256_text(after_metrics or ""),
        },
        "sizes": {
            "content_chars": len(observation.content),
            "reasoning_chars": len(observation.reasoning),
            "sse_events": len(observation.raw_events),
        },
        "speculative_metrics": diff_metrics(before, after),
    }


def weighted_rate(rows: list[dict[str, Any]]) -> float | None:
    tokens = sum(row["usage"]["completion_tokens"] for row in rows)
    seconds = sum(
        row["timing"]["generation_window_seconds"] or 0.0
        for row in rows
        if row["timing"]["generation_window_seconds"] is not None
    )
    return tokens / seconds if tokens and seconds > 0 else None


def total_reasoning_tokens(rows: list[dict[str, Any]]) -> int | None:
    values = [row["usage"].get("reasoning_tokens") for row in rows]
    return sum(values) if values and all(value is not None for value in values) else None


def reported_reasoning_token_count(rows: list[dict[str, Any]]) -> int:
    return sum(1 for row in rows if row["usage"].get("reasoning_tokens") is not None)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_case(
    args: argparse.Namespace,
    prompt_case: dict[str, str],
    repeat_index: int,
    *,
    warmup: bool,
    extra_body: dict[str, Any],
) -> dict[str, Any]:
    request_body = build_request(
        args.model,
        prompt_case["prompt"],
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        extra_body=extra_body,
    )
    case_dir = args.output / prompt_case["id"] / ("warmup" if warmup else f"repeat-{repeat_index:02d}")
    case_dir.mkdir(parents=True, exist_ok=True)
    before_metrics = fetch_metrics(args.endpoint, timeout=args.metrics_timeout)
    observation, total_seconds, start_at = openai_stream_request(
        args.endpoint,
        request_body,
        timeout=args.timeout,
        api_key=args.api_key,
    )
    after_metrics = fetch_metrics(args.endpoint, timeout=args.metrics_timeout)
    (case_dir / "metrics.before.prom").write_text(before_metrics or "", encoding="utf-8")
    (case_dir / "metrics.after.prom").write_text(after_metrics or "", encoding="utf-8")
    write_json(case_dir / "request.json", request_body)
    write_json(case_dir / "raw_sse.json", observation.raw_events)
    (case_dir / "content.txt").write_text(observation.content, encoding="utf-8")
    (case_dir / "reasoning.txt").write_text(observation.reasoning, encoding="utf-8")
    summary = summarize_observation(
        observation,
        total_seconds=total_seconds,
        start_at=start_at,
        request_body=request_body,
        before_metrics=before_metrics,
        after_metrics=after_metrics,
    )
    summary.update(
        {
            "case_id": prompt_case["id"],
            "case_title": prompt_case["title"],
            "repeat_index": repeat_index,
            "warmup": warmup,
            "model": args.model,
            "endpoint": args.endpoint,
            "prompt_sha256": sha256_text(prompt_case["prompt"]),
        }
    )
    write_json(case_dir / "summary.json", summary)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True, help="OpenAI-compatible endpoint base URL")
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--extra-body", default="{}", help="JSON object or @path merged into request body")
    parser.add_argument("--api-key", default="EMPTY")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--warmup", type=int, default=1, help="Warmup requests per prompt, excluded")
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--metrics-timeout", type=float, default=5.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.repeats < 1:
        raise SystemExit("--repeats must be >= 1")
    if args.warmup < 0:
        raise SystemExit("--warmup must be >= 0")
    extra_body = read_json_arg(args.extra_body)
    args.output.mkdir(parents=True, exist_ok=True)
    write_json(
        args.output / "benchmark_config.json",
        {
            "endpoint": args.endpoint,
            "model": args.model,
            "extra_body": extra_body,
            "repeats": args.repeats,
            "warmup": args.warmup,
            "max_tokens": args.max_tokens,
            "temperature": args.temperature,
            "prompt_cases": [
                {"id": case["id"], "title": case["title"], "prompt_sha256": sha256_text(case["prompt"])}
                for case in PROMPTS
            ],
            "same_input_hash": sha256_text(canonical_json(PROMPTS)),
            "claim_scope": "Korean streaming speed benchmark only; no RCA correctness scoring or LLM judge.",
        },
    )
    measured: list[dict[str, Any]] = []
    all_runs: list[dict[str, Any]] = []
    for prompt_case in PROMPTS:
        for warmup_index in range(args.warmup):
            print(
                f"[warmup] {prompt_case['id']} {warmup_index + 1}/{args.warmup}",
                flush=True,
            )
            all_runs.append(
                run_case(args, prompt_case, warmup_index, warmup=True, extra_body=extra_body)
            )
        for repeat_index in range(args.repeats):
            print(
                f"[measure] {prompt_case['id']} {repeat_index + 1}/{args.repeats}",
                flush=True,
            )
            row = run_case(args, prompt_case, repeat_index, warmup=False, extra_body=extra_body)
            measured.append(row)
            all_runs.append(row)
    manifest = {
        "model": args.model,
        "endpoint": args.endpoint,
        "same_input_hash": sha256_text(canonical_json(PROMPTS)),
        "expected_measured_requests": len(PROMPTS) * args.repeats,
        "actual_measured_requests": len(measured),
        "warmup_requests_excluded": len(PROMPTS) * args.warmup,
        "weighted_completion_tokens_per_generation_window_second": weighted_rate(measured),
        "total_prompt_tokens": sum(row["usage"]["prompt_tokens"] for row in measured),
        "total_completion_tokens": sum(row["usage"]["completion_tokens"] for row in measured),
        "total_reasoning_tokens": total_reasoning_tokens(measured),
        "total_reasoning_tokens_reported_count": reported_reasoning_token_count(measured),
        "elapsed_api_seconds_sum": sum(row["timing"]["total_api_seconds"] for row in measured),
        "runs": all_runs,
    }
    write_json(args.output / "manifest.json", manifest)


if __name__ == "__main__":
    main()
