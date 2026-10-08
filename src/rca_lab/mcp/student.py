"""Student agent loop: OpenAI-compatible chat completions (vLLM) with native tool calls.

One loop for baseline, validation and rollout collection. The model sees exactly: the shared
system prompt, the observed-alarm seed, the MCP tool catalog plus `submit_rca`, and tool envelopes rendered by the selected observation policy. Legacy uses a character prefix;
structured-v1 preserves full responses outside context and offers a local reader.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rca_lab.mcp.alias import UUIDAliaser
from rca_lab.mcp.answer import SUBMIT_TOOL, SUBMIT_TOOL_NAME, validate_result
from rca_lab.mcp.client import MCPClient, openai_tools
from rca_lab.mcp.context import rolling_view
from rca_lab.mcp.muse_grammar import guided_request_fields
from rca_lab.mcp.observations import READ_OBSERVATION_TOOL, ObservationStore, compact_observation
from rca_lab.mcp.prompts import HINT_TEMPLATE, SYSTEM_PROMPT, raw_system_prompt, user_prompt

ALIAS_NOTE = "\n\n대상 식별자 표기: 이 조사에서 대상 UUID 는 T1, T2 … 같은 핸들로 표시된다. 도구 인자와 submit_rca 의 target 에도 그 핸들을 그대로 쓴다. 본 적 없는 핸들은 만들지 않는다."


TRANSPORT_RETRY_DELAYS_DEFAULT = (5, 10, 20, 30, 30, 30)  # see _post_chat


@dataclass
class StudentConfig:
    endpoint: str
    model: str
    alias_targets: bool = True
    guided: bool = True  # enforce Muse reasoning→ATEM grammar via xgrammar structural tag
    reasoning_guard: bool = True  # no ATEM markup inside the reasoning channel (muse_grammar.REASONING_EXCLUDES)
    max_turns: int = 40
    max_calls_per_turn: int = 4
    max_prompt_tokens: int = 40_000  # 49152 server limit − 4096 completion − one more tool envelope
    tool_result_max_chars: int = 6_000
    observation_policy: str = "legacy"
    observation_dir: Path | None = None
    max_tokens: int = 4_096
    temperature: float = 1.0
    top_p: float = 0.95
    top_k: int = 64
    seed: int = 0
    max_nudges: int = 3
    request_timeout: int = 900
    extra_body: dict[str, Any] = field(default_factory=dict)
    transport_retry_delays: tuple[int, ...] = TRANSPORT_RETRY_DELAYS_DEFAULT
    hint: str | None = None  # ladder guidance: present in the live prompt only, never in the stored trajectory
    tool_surface: str = "harness"  # "raw": generic SQL/PromQL access (rca_lab.mcp.raw_server) instead of rca-mcp


@dataclass
class StudentRun:
    messages: list[dict[str, Any]]
    result: dict[str, Any] | None
    stop_reason: str
    turns: int
    tool_calls: int
    tool_errors: int
    observed_refs: set[str]
    prompt_tokens_last: int
    wall_seconds: float
    alias_map: dict[str, str] = field(default_factory=dict)


# Transport-level failures are retried: the vLLM endpoint sits behind an SSH tunnel whose far end (KT proxy)
# closes sessions on its own (2026-09-29 08:03 and 2026-09-30 01:01 UTC); a supervisor reconnects within
# seconds, but without a retry every in-flight episode died and its whole capture was dropped. HTTP errors
# (the server answered) and read timeouts are not retried.
def _is_transport_error(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return False
    if isinstance(exc, urllib.error.URLError):
        return not isinstance(exc.reason, TimeoutError)
    return isinstance(exc, ConnectionError)


def _post_chat(config: StudentConfig, body: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps(body).encode("utf-8")
    for attempt, delay in enumerate((*config.transport_retry_delays, None)):
        request = urllib.request.Request(config.endpoint.rstrip("/") + "/chat/completions", data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=config.request_timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")[:500]
            raise RuntimeError(f"chat completion HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, ConnectionError) as exc:
            if delay is None or not _is_transport_error(exc):
                raise
            print(f"[student] transport error ({type(exc).__name__}: {exc}); retry {attempt + 1}/{len(config.transport_retry_delays)} in {delay}s", flush=True)
            time.sleep(delay)
    raise AssertionError("unreachable")


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[응답 절단: 전체 {len(text)}자 중 {limit}자. 더 좁은 인자로 다시 조회하라]"


def _collect_refs(text: str, sink: set[str]) -> None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return
    stack: list[Any] = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            refs = node.get("refs")
            if isinstance(refs, list):
                sink.update(str(ref) for ref in refs)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def _assistant_message(message: dict[str, Any]) -> dict[str, Any]:
    stored: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
    reasoning = message.get("reasoning_content") or message.get("reasoning")
    if reasoning:
        stored["reasoning_content"] = reasoning
    if message.get("tool_calls"):
        stored["tool_calls"] = [
            {
                "id": call.get("id"),
                "type": "function",
                "function": {"name": call["function"]["name"], "arguments": call["function"].get("arguments") or "{}"},
            }
            for call in message["tool_calls"]
        ]
    return stored


def _shrink_context(messages: list[dict[str, Any]], *, keep_chars: int) -> None:
    """Truncate the three largest tool observations in place (oldest first on ties)."""
    tool_messages = sorted(
        (index for index, message in enumerate(messages) if message.get("role") == "tool"),
        key=lambda index: (-len(str(messages[index].get("content", ""))), index),
    )
    for index in tool_messages[:3]:
        content = str(messages[index].get("content", ""))
        if len(content) > keep_chars:
            compact = compact_observation(content, keep_chars)
            messages[index]["content"] = compact if compact is not None else content[:keep_chars] + "\n…[컨텍스트 절약을 위해 절단됨]"


def _strip_prior_reasoning_wire(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Inference-time context (used by rationalization): the canonical rolling view."""
    return _wire_messages(messages)


def _wire_messages(messages: list[dict[str, Any]], *, live_system: str | None = None) -> list[dict[str, Any]]:
    """The context the server sees: rolling observation view without stored reasoning;
    optionally the system prompt is swapped for its hinted live version so the hint never
    reaches the stored trajectory."""
    wire = rolling_view(messages)
    if live_system is not None:
        wire[0] = {**wire[0], "content": live_system}
    return wire


def run_student(config: StudentConfig, seed: dict[str, Any], mcp: MCPClient) -> StudentRun:
    if config.observation_policy not in {"legacy", "structured-v1"}:
        raise ValueError("unknown observation policy")
    if config.tool_surface not in {"harness", "raw"}:
        raise ValueError("unknown tool surface")
    structured = config.observation_policy == "structured-v1"
    if structured and config.tool_result_max_chars < 1200:
        raise ValueError("structured-v1 requires tool_result_max_chars >= 1200")
    store = ObservationStore(config.observation_dir) if structured else None
    catalog = mcp.list_tools()
    if store is not None:
        if any(tool["name"] == READ_OBSERVATION_TOOL["name"] for tool in catalog):
            raise ValueError("MCP catalog conflicts with local observation reader")
        catalog = [*catalog, READ_OBSERVATION_TOOL]
    tools = openai_tools(catalog) + openai_tools([SUBMIT_TOOL])
    request_log = config.observation_dir / "requests.jsonl" if store is not None and config.observation_dir is not None else None
    if request_log is not None:
        request_log.write_text("", encoding="utf-8")
    known = {tool["name"] for tool in catalog}
    aliaser = UUIDAliaser() if config.alias_targets else None
    shown_seed = aliaser.alias(seed) if aliaser else seed
    system_prompt = (raw_system_prompt() if config.tool_surface == "raw" else SYSTEM_PROMPT) + (ALIAS_NOTE if aliaser else "")
    if structured:
        system_prompt += (
            "\n관측 전달 정책 structured-v1: 응답은 원본 참조와 표시된 필드로 구성된다. "
            "생략된 필드는 미관측이지 정상이나 부재의 증거가 아니다. "
            "필요한 필드가 빠졌으면 read_observation으로 해당 observation_id의 원문을 읽는다. "
            "구조화 필드는 json_pointer로 경로를 지정하고, 긴 값은 next_offset으로 이어 읽는다. "
            "이전 관측이 현재 문맥에서 생략됐다면 read_observation은 같은 인자로 다시 호출해도 된다. "
            "저장 원문은 도구 반환 범위이며 "
            "원천 조회 자체의 truncated/no_data는 재조회 페이지로 해소되지 않는다."
        )
    clean_user = user_prompt(shown_seed, max_turns=config.max_turns)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": clean_user},
    ]
    # Ladder guidance goes into the *system* prompt for the student: Muse echoes the head of the
    # user message into its reasoning (observed: "Feedback says previous investigation…"), which
    # would leak the hint into training targets; system-prompt text is not echoed.
    live_system = system_prompt + HINT_TEMPLATE.format(hint=config.hint.strip()) if config.hint and config.hint.strip() else None

    def outbound(text: str) -> str:
        return aliaser.alias_text(text) if aliaser else text

    def inbound(name: str, args: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
        if not aliaser:
            return args, set()
        if config.tool_surface == "raw" and name in {"pg_sql", "ch_sql"} and isinstance(args.get("query"), str):
            query, unknown = aliaser.resolve_sql_literals(args["query"])
            return {**args, "query": query}, unknown
        return aliaser.resolve(args), aliaser.unknown_handles(args)
    observed_refs: set[str] = set()
    # Candidates are taken only from successful source responses. Recognition below checks
    # what the model actually received, not the full hidden source.
    ref_candidates: dict[str, str] = {}

    def collect_visible_refs(view: str) -> None:
        visible: set[str] = set()
        _collect_refs(view, visible)
        try:
            payload = json.loads(view)
        except json.JSONDecodeError:
            return
        chunk = ""
        if isinstance(payload, dict):
            chunk = payload.get("page", {}).get("text", "")
            fields = payload.get("fields")
            if isinstance(fields, dict):
                _collect_refs(json.dumps(fields, ensure_ascii=False), visible)
            elif isinstance(fields, list):
                for field in fields:
                    if isinstance(field, dict):
                        _collect_refs(json.dumps({field.get("path", ""): field.get("value")}, ensure_ascii=False), visible)
        for shown, original in ref_candidates.items():
            if shown in visible or (chunk and json.dumps(shown, ensure_ascii=False) in chunk):
                observed_refs.add(original)

    tool_calls = tool_errors = nudges = prompt_tokens = 0
    result: dict[str, Any] | None = None
    stop_reason = "max_turns"
    started = time.time()
    turn = 0
    force_answer = False
    context_recoveries = 0
    while turn < config.max_turns + 2:  # +2: one forced-answer turn and one retry
        turn += 1
        body: dict[str, Any] = {
            "model": config.model,
            "messages": _wire_messages(messages, live_system=live_system),
            "tools": tools,
            "tool_choice": {"type": "function", "function": {"name": SUBMIT_TOOL_NAME}} if force_answer else "auto",
            "max_tokens": config.max_tokens,
            "temperature": config.temperature,
            "top_p": config.top_p,
            "top_k": config.top_k,
            "seed": config.seed + turn,
            "stream": False,
            **config.extra_body,
        }
        if config.guided:
            body.update(guided_request_fields(tools, only=SUBMIT_TOOL_NAME if force_answer else None, reasoning_guard=config.reasoning_guard))
        if request_log is not None:
            with request_log.open("a", encoding="utf-8") as sink:
                sink.write(json.dumps({"turn": turn, "request": body}, ensure_ascii=False) + "\n")
        try:
            reply = _post_chat(config, body)
        except RuntimeError as exc:
            if "maximum context length" not in str(exc) or force_answer:
                raise
            # Preserve the old bailout for legacy. The structured arm gets one compacted
            # retry with its reader still available, within the episode's turn budget.
            _shrink_context(messages, keep_chars=800)
            context_recoveries += 1
            if structured and context_recoveries == 1 and turn < config.max_turns:
                messages.append({"role": "user", "content": "컨텍스트를 줄였다. 필요한 생략 관측은 read_observation으로 확인하고, 남은 조사 예산 안에서 결론을 제출하라."})
            else:
                force_answer = True
                messages.append({"role": "user", "content": "컨텍스트 한도에 도달했다. 지금까지의 관측만으로 submit_rca 를 호출하라. 근거가 부족하면 status=insufficient 로 제출하라."})
            continue
        if structured:
            for delivered in body["messages"]:
                if delivered.get("role") == "tool":
                    collect_visible_refs(str(delivered.get("content", "")))
        choice = reply["choices"][0]
        message = choice["message"]
        prompt_tokens = int(reply.get("usage", {}).get("prompt_tokens", prompt_tokens))
        messages.append(_assistant_message(message))
        calls = message.get("tool_calls") or []
        if not calls:
            nudges += 1
            if nudges > config.max_nudges:
                stop_reason = "no_tool_call"
                break
            messages.append({"role": "user", "content": "도구를 호출하거나, 조사가 끝났으면 submit_rca 로 결론을 제출하라. 산문만 쓰지 마라."})
            continue
        submitted = False
        for call in calls[: config.max_calls_per_turn]:
            name = call["function"]["name"]
            raw_args = call["function"].get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except json.JSONDecodeError as exc:
                tool_errors += 1
                messages.append({"role": "tool", "tool_call_id": call.get("id"), "name": name, "content": json.dumps({"error": f"arguments are not valid JSON: {exc}"}, ensure_ascii=False)})
                continue
            if not isinstance(args, dict):
                tool_errors += 1
                messages.append({"role": "tool", "tool_call_id": call.get("id"), "name": name, "content": json.dumps({"error": "arguments must be an object"})})
                continue
            if store is not None and name == READ_OBSERVATION_TOOL["name"]:
                tool_calls += 1
                text, is_error = store.read(args, budget=config.tool_result_max_chars)
                tool_errors += int(is_error)
                messages.append({"role": "tool", "tool_call_id": call.get("id"), "name": name, "content": text})
                continue
            resolved, unknown = inbound(name, args)
            if unknown:
                tool_errors += 1
                messages.append({"role": "tool", "tool_call_id": call.get("id"), "name": name, "content": json.dumps({"error": f"본 적 없는 대상 핸들 {sorted(unknown)} — seed 나 이전 도구 응답에 나온 핸들만 쓸 수 있다"}, ensure_ascii=False)})
                continue
            if name == SUBMIT_TOOL_NAME:
                try:
                    result = validate_result(resolved)
                except ValueError as exc:
                    tool_errors += 1
                    messages.append({"role": "tool", "tool_call_id": call.get("id"), "name": name, "content": json.dumps({"error": outbound(str(exc))}, ensure_ascii=False)})
                    continue
                messages.append({"role": "tool", "tool_call_id": call.get("id"), "name": name, "content": "accepted"})
                submitted = True
                break
            tool_calls += 1
            if name not in known:
                tool_errors += 1
                text, is_error = json.dumps({"error": f"unknown tool {name}", "available": sorted(known)}, ensure_ascii=False), True
            else:
                text, is_error = mcp.call(name, resolved)
            tool_errors += int(is_error)
            if store is None:
                if not is_error:
                    _collect_refs(text, observed_refs)
                view = _truncate(outbound(text), config.tool_result_max_chars)
            else:
                shown_text = outbound(text)
                if not is_error:
                    try:
                        source_payload = json.loads(text)
                    except json.JSONDecodeError:
                        source_payload = None
                    usable = not (isinstance(source_payload, dict) and source_payload.get("status") == "no_data")
                    if usable:
                        refs: set[str] = set()
                        _collect_refs(text, refs)
                        ref_candidates.update({outbound(ref): ref for ref in refs})
                view = store.present(shown_text, tool_name=name, is_error=is_error, budget=config.tool_result_max_chars)
            messages.append({"role": "tool", "tool_call_id": call.get("id"), "name": name, "content": view})
        for call in calls[config.max_calls_per_turn :]:
            messages.append({"role": "tool", "tool_call_id": call.get("id"), "name": call["function"]["name"], "content": json.dumps({"error": f"한 턴에 최대 {config.max_calls_per_turn}개 호출만 실행된다. 필요하면 다음 턴에 다시 호출하라."}, ensure_ascii=False)})
        if submitted:
            stop_reason = "submitted"
            break
        if force_answer:
            stop_reason = "forced_answer_rejected"
            break
        if prompt_tokens >= config.max_prompt_tokens or turn >= config.max_turns:
            force_answer = True
            messages.append({"role": "user", "content": "조사 예산이 끝났다. 지금까지의 관측만으로 submit_rca 를 호출하라. 근거가 부족하면 status=insufficient 로 제출하라."})
    return StudentRun(
        messages=messages,
        result=result,
        stop_reason=stop_reason,
        turns=turn,
        tool_calls=tool_calls,
        tool_errors=tool_errors,
        observed_refs=observed_refs,
        prompt_tokens_last=prompt_tokens,
        wall_seconds=time.time() - started,
        alias_map=aliaser.mapping if aliaser else {},
    )
