"""xgrammar structural tag that forces Muse's native wire format for a multi-tool catalog.

Muse emits Harmony-style channels: a private `to=self` reasoning message, then one
`to=<tool>` message carrying an ATEM function-call block. Without a grammar the base model
drifts (Harmony tokens inside content, truncated UUIDs, several calls per turn), so — exactly as
the old single-tool proxy did — the request carries an explicit structural tag:

    <|start|>assistant to=self<|message|> …reasoning… <|eom|>
    <|start|>assistant to=<tool><|message|><atem:function_calls>
    <atem:invoke name="<tool>">
    <atem:parameter name="…">…</atem:parameter>
    …
    </atem:invoke>
    </atem:function_calls><|eom|>

Optional parameters are modelled as independent optional slots (each `or(empty, param+"\\n")`)
instead of enumerating subsets, so tools with many optional arguments stay tractable.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

MUSE_EOM_TOKEN_ID = 200007
MUSE_START_TOKEN_ID = 200022
MUSE_MESSAGE_TOKEN_ID = 200023
MUSE_EOT_TOKEN = "<|eot|>"
MUSE_GUIDED_TOOL_STOP = "</atem:function_calls><|eom|>"

_EMPTY = {"type": "const_string", "value": ""}


def _value_format(schema: Mapping[str, Any]) -> dict[str, Any]:
    if "const" in schema:
        value = schema["const"]
        return {"type": "const_string", "value": value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))}
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        choices = [{"type": "const_string", "value": value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))} for value in enum]
        return choices[0] if len(choices) == 1 else {"type": "or", "elements": choices}
    if schema.get("type") == "string":
        return {"type": "any_text"}
    return {"type": "json_schema", "json_schema": dict(schema), "style": "json", "any_order": False}


def _parameter_slot(name: str, schema: Mapping[str, Any], *, required: bool) -> dict[str, Any]:
    tag = {"type": "tag", "begin": f'<atem:parameter name="{name}">', "content": _value_format(schema), "end": "</atem:parameter>"}
    slot = {"type": "sequence", "elements": [tag, {"type": "const_string", "value": "\n"}]}
    return slot if required else {"type": "or", "elements": [_EMPTY, slot]}


def _parameters_format(parameters: Mapping[str, Any]) -> dict[str, Any]:
    properties = parameters.get("properties") or {}
    required = set(parameters.get("required") or [])
    ordered = [name for name in properties if name in required] + [name for name in properties if name not in required]
    return {"type": "sequence", "elements": [_parameter_slot(name, properties[name], required=name in required) for name in ordered]}


def tool_call_format(tool: Mapping[str, Any]) -> dict[str, Any]:
    function = tool["function"]
    name = function["name"]
    return {
        "type": "tag",
        "begin": f'<|start|>assistant to={name}<|message|><atem:function_calls>\n<atem:invoke name="{name}">\n',
        "content": _parameters_format(function.get("parameters") or {"type": "object", "properties": {}}),
        "end": "</atem:invoke>\n" + MUSE_GUIDED_TOOL_STOP,
    }


# Reasoning guard (2026-09-29, Fable review C1): the reasoning channel used to allow any token except
# <|start|>/<|message|>, so the student could write the ATEM call block as *text* inside its reasoning and
# close the channel with <|eom|>; the request stop string "</atem:function_calls><|eom|>" then matched
# there and the turn ended with no tool call (51 of 57 call-less turns in RL iteration-3 rollouts). With
# the guard the reasoning is `any_text` that may not contain ATEM markup or the channel header tokens, so
# a call can only be emitted in the tool channel. Verified with xgrammar on the serving venv.
REASONING_EXCLUDES = ("<atem:", "</atem:", "<|start|>", "<|message|>")


def reasoning_format(*, guard: bool = True) -> dict[str, Any]:
    content = {"type": "any_text", "excludes": list(REASONING_EXCLUDES)} if guard else {"type": "any_tokens", "exclude_tokens": [MUSE_START_TOKEN_ID, MUSE_MESSAGE_TOKEN_ID]}
    return {
        "type": "sequence",
        "elements": [
            {"type": "const_string", "value": " to=self"},
            {
                "type": "tag",
                "begin": {"type": "token", "token": MUSE_MESSAGE_TOKEN_ID},
                "content": content,
                "end": {"type": "token", "token": MUSE_EOM_TOKEN_ID},
            },
        ],
    }


def structural_tag(tools: list[Mapping[str, Any]], *, only: str | None = None, reasoning_guard: bool = True) -> str:
    """Serialize the grammar: reasoning, then exactly one call to one of `tools` (or to `only`)."""
    calls = [tool_call_format(tool) for tool in tools if only is None or tool["function"]["name"] == only]
    if not calls:
        raise ValueError(f"no tool matches {only!r}")
    call = calls[0] if len(calls) == 1 else {"type": "or", "elements": calls}
    elements = [reasoning_format(guard=reasoning_guard), call]
    return json.dumps({"type": "structural_tag", "format": {"type": "sequence", "elements": elements}}, ensure_ascii=False, separators=(",", ":"))


def _const_value(value: Any) -> dict[str, Any]:
    rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return {"type": "const_string", "value": rendered}


def exact_call_format(name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """The ATEM block for one fixed call: every parameter value is a constant, in the given order.
    Used for rationalization — the model may only write the reasoning, the action is teacher-forced."""
    elements: list[dict[str, Any]] = []
    for key, value in arguments.items():
        elements.append({"type": "tag", "begin": f'<atem:parameter name="{key}">', "content": _const_value(value), "end": "</atem:parameter>"})
        elements.append({"type": "const_string", "value": "\n"})
    return {
        "type": "tag",
        "begin": f'<|start|>assistant to={name}<|message|><atem:function_calls>\n<atem:invoke name="{name}">\n',
        "content": {"type": "sequence", "elements": elements} if elements else _EMPTY,
        "end": "</atem:invoke>\n" + MUSE_GUIDED_TOOL_STOP,
    }


def structural_tag_exact(name: str, arguments: Mapping[str, Any], *, reasoning_guard: bool = True) -> str:
    elements = [reasoning_format(guard=reasoning_guard), exact_call_format(name, arguments)]
    return json.dumps({"type": "structural_tag", "format": {"type": "sequence", "elements": elements}}, ensure_ascii=False, separators=(",", ":"))


def _request_fields(tag: str) -> dict[str, Any]:
    return {
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "structured_outputs": {"structural_tag": tag},
        "stop": [MUSE_GUIDED_TOOL_STOP],
        "include_stop_str_in_output": True,
        "ignore_eos": True,
        "bad_words": [MUSE_EOT_TOKEN],
    }


def guided_request_fields(tools: list[Mapping[str, Any]], *, only: str | None = None, reasoning_guard: bool = True) -> dict[str, Any]:
    """Request fields that enforce the grammar on a vLLM/xgrammar server (Muse parity backend)."""
    return _request_fields(structural_tag(tools, only=only, reasoning_guard=reasoning_guard))


def exact_call_request_fields(name: str, arguments: Mapping[str, Any], *, reasoning_guard: bool = True) -> dict[str, Any]:
    """Request fields that force one exact call after free reasoning."""
    return _request_fields(structural_tag_exact(name, arguments, reasoning_guard=reasoning_guard))
