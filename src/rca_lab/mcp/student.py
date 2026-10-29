"""Student agent loop: OpenAI-compatible chat completions (vLLM) with native tool calls.

One loop for baseline, validation and rollout collection. The model sees exactly: the shared
system prompt, the observed-alarm seed, the MCP tool catalog plus `submit_rca`, and tool envelopes rendered by the selected observation policy. Legacy uses a character prefix;
structured-v1 preserves full responses outside context and offers a local reader.
"""

from __future__ import annotations

import copy
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rca_lab.mcp.alias import UUIDAliaser
from rca_lab.mcp.answer import SUBMIT_TOOL_NAME, submit_tool, validate_result
from rca_lab.mcp.answer import UUID_RE as ANSWER_UUID_RE
from rca_lab.mcp.client import MCPClient, openai_tools
from rca_lab.mcp.context import rolling_view
from rca_lab.mcp.completion_errors import ChatCompletionError
from rca_lab.mcp.muse_grammar import guided_request_fields
from rca_lab.mcp.observations import READ_OBSERVATION_TOOL, ObservationStore, compact_observation
from rca_lab.mcp.prompts import HINT_TEMPLATE, raw_system_prompt, user_prompt
from rca_lab.mcp.prompts import system_prompt as revision_system_prompt

ALIAS_NOTE = "\n\n대상 식별자 표기: 이 조사에서 대상 UUID 는 T1, T2 … 같은 핸들로 표시된다. 도구 인자와 submit_rca 의 target 에도 그 핸들을 그대로 쓴다. 본 적 없는 핸들은 만들지 않는다."


TRANSPORT_RETRY_DELAYS_DEFAULT = (5, 10, 20, 30, 30, 30)  # see _post_chat


@dataclass
class StudentConfig:
    endpoint: str
    model: str
    alias_targets: bool = True
    guided: bool = False  # Muse only (legacy): enforce reasoning→ATEM grammar via xgrammar structural tag; Gemma runs unguided
    reasoning_guard: bool = True  # no ATEM markup inside the reasoning channel (muse_grammar.REASONING_EXCLUDES)
    max_turns: int = 40
    max_calls_per_turn: int = 4
    max_prompt_tokens: int = 40_000  # Soft submission threshold; 0 disables it, not the server context limit.
    tool_result_max_chars: int = 6_000
    observation_policy: str = "legacy"
    observation_dir: Path | None = None
    max_tokens: int = 4_096
    thinking_token_budget: int | None = None
    stop_on_output_limit: bool = False
    response_log: Path | None = None
    temperature: float = 1.0
    top_p: float = 0.95
    top_k: int = 64
    seed: int = 0
    max_nudges: int = 3
    forced_submit_validation_retries: int = 0
    request_timeout: int = 900
    extra_body: dict[str, Any] = field(default_factory=dict)
    transport_retry_delays: tuple[int, ...] = TRANSPORT_RETRY_DELAYS_DEFAULT
    hint: str | None = None  # ladder guidance: present in the live prompt only, never in the stored trajectory
    tool_surface: str = "harness"  # "raw": generic SQL/PromQL access (rca_lab.mcp.raw_server) instead of rca-mcp
    prompt_revision: int = 1  # 2: cause vs exposing-condition wording (prompts._EXTERNAL_RULE_R2); 1 is the original text
    context_policy: str = "rolling8"  # context.CONTEXT_POLICIES; "budget" keeps the history append-only between compactions


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
    output_limit_hits: int = 0
    forced_submit_validation_retries: int = 0


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
    if config.endpoint == "codex://local":
        from rca_lab.mcp.codex_completion import complete
        return complete(body, timeout_seconds=config.request_timeout)
    payload = json.dumps(body).encode("utf-8")
    for attempt, delay in enumerate((*config.transport_retry_delays, None)):
        request = urllib.request.Request(config.endpoint.rstrip("/") + "/chat/completions", data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=config.request_timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")
            raise ChatCompletionError(exc.code, detail) from exc
        except (urllib.error.URLError, ConnectionError) as exc:
            if delay is None or not _is_transport_error(exc):
                raise
            print(f"[student] transport error ({type(exc).__name__}: {exc}); retry {attempt + 1}/{len(config.transport_retry_delays)} in {delay}s", flush=True)
            time.sleep(delay)
    raise AssertionError("unreachable")


def _validate_token_budgets(config: StudentConfig) -> None:
    if config.max_tokens <= 0:
        raise ValueError("max_tokens must be > 0")
    if config.thinking_token_budget is None:
        return
    if config.thinking_token_budget <= 0:
        raise ValueError("thinking_token_budget must be > 0")
    if config.thinking_token_budget >= config.max_tokens:
        raise ValueError("thinking_token_budget must be < max_tokens")


def _apply_thinking_token_budget(config: StudentConfig, body: dict[str, Any]) -> None:
    if config.thinking_token_budget is not None:
        body["thinking_token_budget"] = config.thinking_token_budget


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


def _wire_messages(messages: list[dict[str, Any]], *, live_system: str | None = None, context_policy: str = "rolling8") -> list[dict[str, Any]]:
    """The context the server sees: rolling observation view without stored reasoning;
    optionally the system prompt is swapped for its hinted live version so the hint never
    reaches the stored trajectory."""
    wire = rolling_view(messages, policy=context_policy)
    if live_system is not None:
        wire[0] = {**wire[0], "content": live_system}
    return wire


def _aliaser_from_mapping(alias_map: dict[str, str]) -> UUIDAliaser:
    aliaser = UUIDAliaser()
    seen_handles: set[str] = set()
    ordered: list[tuple[int, str, str]] = []
    for uuid, handle in alias_map.items():
        original = str(uuid).lower()
        shown = str(handle)
        if not ANSWER_UUID_RE.match(original):
            raise ValueError(f"invalid alias UUID: {uuid!r}")
        if not shown.startswith("T") or not shown[1:].isdigit():
            raise ValueError(f"invalid alias handle: {shown!r}")
        if shown in seen_handles:
            raise ValueError(f"duplicate alias handle: {shown!r}")
        seen_handles.add(shown)
        ordered.append((int(shown[1:]), original, shown))
    for expected, (_, original, shown) in enumerate(sorted(ordered), start=1):
        if int(shown[1:]) != expected:
            raise ValueError("alias handles must be contiguous from T1")
        if aliaser.handle_for(original) != shown:
            raise ValueError("alias map is not reproducible")
    return aliaser


def _last_message_is_submit_rejection(messages: list[dict[str, Any]]) -> None:
    if not messages:
        raise ValueError("messages must end with a submit_rca validation rejection")
    last = messages[-1]
    if last.get("role") != "tool" or last.get("name") != SUBMIT_TOOL_NAME:
        raise ValueError("messages must end with a submit_rca validation rejection")
    try:
        content = json.loads(str(last.get("content", "")))
    except json.JSONDecodeError as exc:
        raise ValueError("submit_rca rejection content must be JSON") from exc
    error = content.get("error") if isinstance(content, dict) else None
    if not isinstance(error, str) or "submit_rca rejected:" not in error:
        raise ValueError("messages must end with a submit_rca validation rejection")
    tool_call_id = last.get("tool_call_id")
    for message in reversed(messages[:-1]):
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if call.get("id") == tool_call_id and call.get("function", {}).get("name") == SUBMIT_TOOL_NAME:
                return
        break
    raise ValueError("submit_rca rejection must reference a prior submit_rca tool call")


def run_forced_submit_repair(
    config: StudentConfig,
    messages: list[dict[str, Any]],
    alias_map: dict[str, str],
    *,
    retry_budget: int = 1,
    observed_refs: set[str] | None = None,
) -> StudentRun:
    """Replay only bounded submit_rca schema repair after an existing rejection."""
    if retry_budget < 0:
        raise ValueError("retry_budget must be >= 0")
    _validate_token_budgets(config)
    _last_message_is_submit_rejection(messages)
    started = time.time()
    repaired_messages = copy.deepcopy(messages)
    aliaser = _aliaser_from_mapping(alias_map) if config.alias_targets else None
    if not config.alias_targets and alias_map:
        raise ValueError("alias_map requires alias_targets")

    def outbound(text: str) -> str:
        return aliaser.alias_text(text) if aliaser else text

    def inbound(args: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
        if not aliaser:
            return args, set()
        return aliaser.resolve(args), aliaser.unknown_handles(args)

    if config.response_log is not None:
        config.response_log.parent.mkdir(parents=True, exist_ok=True)
        config.response_log.write_text("", encoding="utf-8")

    tools = openai_tools([submit_tool(config.prompt_revision)])
    repaired_messages.append({"role": "user", "content": "submit_rca 검증 오류가 있다. 새 조사는 금지된다. 위 오류만 고쳐 submit_rca 를 다시 호출하라."})
    start_turn = sum(1 for message in messages if message.get("role") == "assistant")
    prompt_tokens = 0
    output_limit_hits = 0
    tool_errors = 0
    result: dict[str, Any] | None = None
    stop_reason = "forced_answer_rejected"
    attempts = 0
    for attempts in range(1, retry_budget + 1):
        body: dict[str, Any] = {
            "model": config.model,
            "messages": _wire_messages(repaired_messages, context_policy=config.context_policy),
            "tools": tools,
            "tool_choice": {"type": "function", "function": {"name": SUBMIT_TOOL_NAME}},
            "max_tokens": config.max_tokens,
            "temperature": config.temperature,
            "top_p": config.top_p,
            "top_k": config.top_k,
            "seed": config.seed + start_turn + attempts,
            "stream": False,
            **config.extra_body,
        }
        _apply_thinking_token_budget(config, body)
        if config.guided:
            body.update(guided_request_fields(tools, only=SUBMIT_TOOL_NAME, reasoning_guard=config.reasoning_guard))
        reply = _post_chat(config, body)
        choice = reply["choices"][0]
        message = choice["message"]
        output_limited = choice.get("finish_reason") == "length"
        output_limit_hits += int(output_limited)
        if config.response_log is not None:
            with config.response_log.open("a", encoding="utf-8") as sink:
                sink.write(json.dumps({
                    "turn": start_turn + attempts,
                    "finish_reason": choice.get("finish_reason"),
                    "output_budget_exhausted": output_limited,
                    "requested_max_tokens": body["max_tokens"],
                    **({"requested_thinking_token_budget": config.thinking_token_budget} if config.thinking_token_budget is not None else {}),
                    "usage": reply.get("usage", {}),
                }, ensure_ascii=False) + "\n")
        prompt_tokens = int(reply.get("usage", {}).get("prompt_tokens", prompt_tokens))
        repaired_messages.append(_assistant_message(message))
        if output_limited and config.stop_on_output_limit:
            stop_reason = "output_budget_exhausted"
            break
        calls = message.get("tool_calls") or []
        if not calls:
            stop_reason = "no_tool_call"
            break
        call = calls[0]
        name = call["function"]["name"]
        raw_args = call["function"].get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except json.JSONDecodeError as exc:
            tool_errors += 1
            repaired_messages.append({"role": "tool", "tool_call_id": call.get("id"), "name": name, "content": json.dumps({"error": f"arguments are not valid JSON: {exc}"}, ensure_ascii=False)})
            if attempts < retry_budget:
                repaired_messages.append({"role": "user", "content": "submit_rca 검증 오류가 있다. 새 조사는 금지된다. 위 오류만 고쳐 submit_rca 를 다시 호출하라."})
            continue
        if not isinstance(args, dict):
            tool_errors += 1
            repaired_messages.append({"role": "tool", "tool_call_id": call.get("id"), "name": name, "content": json.dumps({"error": "arguments must be an object"})})
            if attempts < retry_budget:
                repaired_messages.append({"role": "user", "content": "submit_rca 검증 오류가 있다. 새 조사는 금지된다. 위 오류만 고쳐 submit_rca 를 다시 호출하라."})
            continue
        if name != SUBMIT_TOOL_NAME:
            tool_errors += 1
            repaired_messages.append({"role": "tool", "tool_call_id": call.get("id"), "name": name, "content": json.dumps({"error": "강제 제출 복구 단계에서는 submit_rca 만 호출할 수 있다"}, ensure_ascii=False)})
            break
        resolved, unknown = inbound(args)
        if unknown:
            tool_errors += 1
            repaired_messages.append({"role": "tool", "tool_call_id": call.get("id"), "name": name, "content": json.dumps({"error": f"본 적 없는 대상 핸들 {sorted(unknown)} — seed 나 이전 도구 응답에 나온 핸들만 쓸 수 있다"}, ensure_ascii=False)})
            if attempts < retry_budget:
                repaired_messages.append({"role": "user", "content": "submit_rca 검증 오류가 있다. 새 조사는 금지된다. 위 오류만 고쳐 submit_rca 를 다시 호출하라."})
            continue
        try:
            result = validate_result(resolved)
        except ValueError as exc:
            tool_errors += 1
            repaired_messages.append({"role": "tool", "tool_call_id": call.get("id"), "name": name, "content": json.dumps({"error": outbound(str(exc))}, ensure_ascii=False)})
            if attempts < retry_budget:
                repaired_messages.append({"role": "user", "content": "submit_rca 검증 오류가 있다. 새 조사는 금지된다. 위 오류만 고쳐 submit_rca 를 다시 호출하라."})
            continue
        repaired_messages.append({"role": "tool", "tool_call_id": call.get("id"), "name": name, "content": "accepted"})
        stop_reason = "submitted"
        break
    return StudentRun(
        messages=repaired_messages,
        result=result,
        stop_reason=stop_reason,
        turns=attempts,
        tool_calls=0,
        tool_errors=tool_errors,
        observed_refs=set(observed_refs or set()),
        prompt_tokens_last=prompt_tokens,
        wall_seconds=time.time() - started,
        alias_map=aliaser.mapping if aliaser else {},
        output_limit_hits=output_limit_hits,
        forced_submit_validation_retries=attempts,
    )


def run_student(config: StudentConfig, seed: dict[str, Any], mcp: MCPClient) -> StudentRun:
    if config.observation_policy not in {"legacy", "structured-v1", "structured-v2"}:
        raise ValueError("unknown observation policy")
    if config.tool_surface not in {"harness", "raw"}:
        raise ValueError("unknown tool surface")
    if config.max_prompt_tokens < 0:
        raise ValueError("max_prompt_tokens must be >= 0")
    _validate_token_budgets(config)
    if config.forced_submit_validation_retries < 0:
        raise ValueError("forced_submit_validation_retries must be >= 0")
    if config.response_log is not None:
        config.response_log.parent.mkdir(parents=True, exist_ok=True)
        config.response_log.write_text("", encoding="utf-8")
    structured = config.observation_policy != "legacy"
    if structured and config.tool_result_max_chars < 1200:
        raise ValueError("structured policies require tool_result_max_chars >= 1200")
    store = ObservationStore(config.observation_dir, policy=config.observation_policy) if structured else None
    catalog = mcp.list_tools()
    if store is not None:
        if any(tool["name"] == READ_OBSERVATION_TOOL["name"] for tool in catalog):
            raise ValueError("MCP catalog conflicts with local observation reader")
        catalog = [*catalog, READ_OBSERVATION_TOOL]
    tools = openai_tools(catalog) + openai_tools([submit_tool(config.prompt_revision)])
    request_log = config.observation_dir / "requests.jsonl" if store is not None and config.observation_dir is not None else None
    if request_log is not None:
        request_log.write_text("", encoding="utf-8")
    known = {tool["name"] for tool in catalog}
    aliaser = UUIDAliaser() if config.alias_targets else None
    shown_seed = aliaser.alias(seed) if aliaser else seed
    system_prompt = (raw_system_prompt() if config.tool_surface == "raw" else revision_system_prompt(config.prompt_revision)) + (ALIAS_NOTE if aliaser else "")
    if structured:
        system_prompt += (
            f"\n관측 전달 정책 {config.observation_policy}: 응답은 원본 참조와 표시된 필드로 구성된다. "
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
    output_limit_hits = 0
    forced_submit_validation_retries = 0
    # Investigation stays capped by max_turns; forced submission gets one bounded answer turn plus
    # the opt-in format repair attempts.
    while turn < config.max_turns + 2 + config.forced_submit_validation_retries:
        turn += 1
        body: dict[str, Any] = {
            "model": config.model,
            "messages": _wire_messages(messages, live_system=live_system, context_policy=config.context_policy),
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
        _apply_thinking_token_budget(config, body)
        if config.guided:
            body.update(guided_request_fields(tools, only=SUBMIT_TOOL_NAME if force_answer else None, reasoning_guard=config.reasoning_guard))
        if request_log is not None:
            with request_log.open("a", encoding="utf-8") as sink:
                sink.write(json.dumps({"turn": turn, "request": body}, ensure_ascii=False) + "\n")
        try:
            reply = _post_chat(config, body)
        except RuntimeError as exc:
            if isinstance(exc, ChatCompletionError) and not exc.context_capacity_exceeded:
                raise
            if isinstance(exc, ChatCompletionError) and exc.context_capacity_exceeded:
                terminal = config.observation_policy == "legacy" and force_answer
                if config.response_log is not None:
                    with config.response_log.open("a", encoding="utf-8") as sink:
                        sink.write(json.dumps({"turn": turn, "completion_error": exc.as_record(),
                                              "terminal": terminal, "requested_max_tokens": body["max_tokens"]}) + "\n")
                if terminal:
                    # The existing legacy shrink/forced-submit fallback was exhausted.
                    # Keep this attempt as a scored failure; do not retry a new trajectory.
                    stop_reason = "context_budget_exhausted"
                    break
            # V2 never converts an API capacity failure into a forced diagnostic
            # conclusion, nor deletes already-read evidence to manufacture success.
            if config.observation_policy == "structured-v2":
                raise
            context_capacity = (exc.context_capacity_exceeded if isinstance(exc, ChatCompletionError)
                                else "maximum context length" in str(exc))
            if not context_capacity or force_answer:
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
        output_limited = choice.get("finish_reason") == "length"
        output_limit_hits += int(output_limited)
        if config.response_log is not None:
            with config.response_log.open("a", encoding="utf-8") as sink:
                sink.write(json.dumps({
                    "turn": turn,
                    "finish_reason": choice.get("finish_reason"),
                    "output_budget_exhausted": output_limited,
                    "requested_max_tokens": body["max_tokens"],
                    **({"requested_thinking_token_budget": config.thinking_token_budget} if config.thinking_token_budget is not None else {}),
                    "usage": reply.get("usage", {}),
                }, ensure_ascii=False) + "\n")
        prompt_tokens = int(reply.get("usage", {}).get("prompt_tokens", prompt_tokens))
        messages.append(_assistant_message(message))
        # A truncated tool call is not a completed action. Opt-in preserves older
        # evaluation semantics while making capacity failures explicit in new runs.
        if output_limited and config.stop_on_output_limit:
            stop_reason = "output_budget_exhausted"
            break
        calls = message.get("tool_calls") or []
        if not calls:
            nudges += 1
            if nudges > config.max_nudges:
                stop_reason = "no_tool_call"
                break
            messages.append({"role": "user", "content": "도구를 호출하거나, 조사가 끝났으면 submit_rca 로 결론을 제출하라. 산문만 쓰지 마라."})
            continue
        submitted = False
        retry_forced_submission = False
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
            if force_answer and name != SUBMIT_TOOL_NAME:
                tool_errors += 1
                messages.append({"role": "tool", "tool_call_id": call.get("id"), "name": name, "content": json.dumps({"error": "조사 예산이 끝난 강제 제출 단계에서는 submit_rca 만 호출할 수 있다"}, ensure_ascii=False)})
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
                    if force_answer and forced_submit_validation_retries < config.forced_submit_validation_retries:
                        forced_submit_validation_retries += 1
                        messages.append({"role": "user", "content": "submit_rca 검증 오류가 있다. 새 조사는 금지된다. 위 오류만 고쳐 submit_rca 를 다시 호출하라."})
                        retry_forced_submission = True
                        break
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
        if retry_forced_submission:
            continue
        if force_answer:
            stop_reason = "forced_answer_rejected"
            break
        if (config.max_prompt_tokens > 0 and prompt_tokens >= config.max_prompt_tokens) or turn >= config.max_turns:
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
        output_limit_hits=output_limit_hits,
        forced_submit_validation_retries=forced_submit_validation_retries,
    )
