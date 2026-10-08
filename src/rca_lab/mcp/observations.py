"""Run-scoped observation storage for large MCP tool results.

The student should not have to infer absence from a prefix of a large response.  This module
keeps the full, already-blinded/aliased tool text outside the prompt and returns a compact JSON
envelope with a deterministic id plus enough metadata to page the exact text back in.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VERSION = "mcp-observation-v1"
READ_OBSERVATION_TOOL_NAME = "read_observation"
DEFAULT_PAGE_LIMIT = 4_000
MAX_PAGE_LIMIT = 12_000
MIN_PRESENTATION_BUDGET = 800

READ_OBSERVATION_TOOL: dict[str, Any] = {
    "name": READ_OBSERVATION_TOOL_NAME,
    "description": (
        "Read a stored observation. Prefer json_pointer to select the exact JSON field or array "
        "element you need while preserving its path. Without it, read the original text. "
        "offset and limit page characters within the selected text; use next_offset to continue."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "observation_id": {
                "type": "string",
                "description": "Run-scoped observation id from an observation envelope.",
            },
            "json_pointer": {
                "type": "string",
                "description": "Optional RFC 6901 JSON Pointer: slash-separated object keys or zero-based array indexes; ~1 encodes slash and ~0 encodes tilde. Empty selects the root. Offset applies inside this selection.",
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "description": "Zero-based character offset into the stored observation text.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_PAGE_LIMIT,
                "description": "Maximum characters to return.",
            },
        },
        "required": ["observation_id"],
        "additionalProperties": False,
    },
}

OBSERVATION_PROMPT_GUIDANCE = (
    "도구 응답이 observation envelope이면 원문 전체가 컨텍스트에 들어온 것이 아니다. "
    "envelope의 observation_id로 read_observation을 호출해 필요한 offset/limit 범위를 정확히 읽어라. "
    "omitted_fields나 complete=false가 있으면 보지 않은 범위의 부재를 근거로 결론내리지 마라."
)


@dataclass(frozen=True)
class _Observation:
    observation_id: str
    tool_name: str
    text: str
    is_error: bool
    sha256: str
    chars: int
    source: dict[str, Any]


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _bounded_json(value: dict[str, Any], budget: int) -> str:
    """Keep mandatory provenance; add only whole optional facts that fit."""
    out = {key: item for key, item in value.items() if key not in {"fields", "omitted_fields", "tool_name"}}
    out["presentation"] = dict(out.get("presentation", {}))
    out["presentation"].pop("note", None)
    omitted = value.get("omitted_fields", [])
    fields = value.get("fields", {})
    out["presentation"]["omitted_field_count"] = len(omitted) + len(fields)
    if len(_json_dumps(out)) > budget and "json_pointer" in out.get("retrieval", {}).get("arguments", {}):
        # A very long selector may not fit an old-observation view. Keep access to
        # the same source rather than a broken/truncated selector.
        out.pop("page", None)
        out["retrieval"] = {"tool": READ_OBSERVATION_TOOL_NAME, "arguments": {"observation_id": out["observation_id"], "offset": 0}}
        out["presentation"]["selection_omitted"] = True
    if len(_json_dumps(out)) > budget:
        raise ValueError("observation metadata exceeds presentation budget")
    if "tool_name" in value:
        candidate = {**out, "tool_name": value["tool_name"]}
        if len(_json_dumps(candidate)) <= budget:
            out = candidate
    out["fields"] = {}
    priority = ["status", "no_data_reason", "summary", "truncated", "refs", "scopes"]
    keys = [key for key in priority if key in fields] + [key for key in fields if key not in priority]
    for key in keys:
        candidate = {**out, "fields": {**out["fields"], key: fields[key]}}
        if len(_json_dumps(candidate)) <= budget:
            out = candidate
    out["presentation"]["omitted_field_count"] = len(omitted) + len(fields) - len(out["fields"])
    candidate = {**out, "omitted_fields": omitted}
    if omitted and len(_json_dumps(candidate)) <= budget:
        out = candidate
    if len(_json_dumps(out)) > budget:
        out.pop("fields", None)
    return _json_dumps(out)


def _source_metadata(text: str) -> dict[str, Any]:
    # Diagnostic prose belongs in optional fields, never mandatory page metadata: a
    # giant summary must not make even a one-character raw read impossible.
    meta: dict[str, Any] = {"content_type": "text", "truncated": None}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return meta
    meta["content_type"] = "json"
    if isinstance(payload, dict):
        for key in ("status", "no_data_reason"):
            value = payload.get(key)
            if isinstance(value, str) and len(_json_dumps(value)) <= 128:
                meta[key] = value
            elif key in payload:
                meta["metadata_omitted"] = True
        for key in ("truncated", "resultTruncated", "source_truncated"):
            if key in payload:
                meta["truncated"] = payload[key] if type(payload[key]) is bool else None
                break
        for key in ("no_data", "noData"):
            if key in payload:
                meta["no_data"] = payload[key] if type(payload[key]) is bool else None
                break
    return meta


def _field_entry(key: str, value: Any) -> tuple[tuple[str, Any] | None, dict[str, Any] | None]:
    encoded = _json_dumps(value)
    if len(encoded) <= 700:
        return (key, value), None
    omitted: dict[str, Any] = {
        "path": key,
        "chars": len(str(value)) if isinstance(value, str) else len(encoded),
        "sha256": _sha256(encoded),
        "reason": "field_too_large_for_initial_view",
    }
    if isinstance(value, list):
        omitted["items"] = len(value)
    elif isinstance(value, dict):
        omitted["keys"] = sorted(str(item) for item in value)[:30]
    return None, omitted


def _selected_fields(text: str, budget: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}, []
    if not isinstance(payload, dict):
        return {}, [{"path": "$", "chars": len(text), "reason": "json_root_not_object"}]
    priority = [
        "status",
        "summary",
        "error",
        "message",
        "total",
        "returned",
        "limit",
        "offset",
        "truncated",
        "resultTruncated",
        "source_truncated",
        "no_data",
        "refs",
        "counts",
        "severity_counts",
        "scopes",
        "scope",
        "query",
        "window",
    ]
    ordered = [key for key in priority if key in payload] + sorted(str(key) for key in payload if str(key) not in priority)
    fields: dict[str, Any] = {}
    omitted: list[dict[str, Any]] = []
    for key in ordered:
        field, missing = _field_entry(key, payload[key])
        if field is not None:
            candidate = {**fields, field[0]: field[1]}
            if len(_json_dumps(candidate)) <= budget:
                fields = candidate
            else:
                omitted.append({"path": key, "chars": len(_json_dumps(payload[key])), "reason": "initial_view_budget_exhausted"})
        elif missing is not None:
            omitted.append(missing)
    return fields, omitted


def _is_observation_envelope(text: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict) and payload.get("version") == VERSION and isinstance(payload.get("observation_id"), str):
        return payload
    return None


def compact_observation(text: str, budget: int = MIN_PRESENTATION_BUDGET) -> str | None:
    """Compact an existing observation/page envelope for rolling context.

    Legacy text returns None so callers can keep their previous truncation policy.
    """
    payload = _is_observation_envelope(text)
    if payload is None:
        return None
    if budget < MIN_PRESENTATION_BUDGET:
        raise ValueError(f"observation compact budget must be >= {MIN_PRESENTATION_BUDGET}")
    retrieval = payload.get("retrieval") if isinstance(payload.get("retrieval"), dict) else {}
    compact = {
        "version": VERSION,
        "observation_id": payload["observation_id"],
        "tool_name": payload.get("tool_name"),
        "is_error": bool(payload.get("is_error")),
        "chars": payload.get("chars"),
        "sha256": payload.get("sha256"),
        "source": payload.get("source", {}),
        "fields": payload.get("fields", {}),
        "retrieval": retrieval,
        "presentation": {
            "complete": False,
            "compacted_from_observation": True,
            "budget_chars": budget,
        },
    }
    if isinstance(payload.get("page"), dict):
        compact["page"] = {
            "offset": payload["page"].get("offset"),
            "next_offset": payload["page"].get("next_offset"),
            "complete": payload["page"].get("complete"),
        }
        compact["page"]["json_pointer"] = payload["page"].get("json_pointer")
        args = dict(retrieval.get("arguments", {}))
        args["offset"] = payload["page"].get("offset", 0)
        compact["retrieval"] = {"tool": READ_OBSERVATION_TOOL_NAME, "arguments": args}
    return _bounded_json(compact, budget)


def _select_json(text: str, pointer: str) -> str:
    """Select a JSON value without losing its structural identity (RFC 6901)."""
    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
        raise ValueError("json_pointer must be empty or start with slash")
    if len(pointer) > 512:
        raise ValueError("json_pointer exceeds 512 characters")
    value = json.loads(text)
    for encoded in pointer.split("/")[1:] if pointer else []:
        for index, char in enumerate(encoded):
            if char == "~" and (index + 1 == len(encoded) or encoded[index + 1] not in "01"):
                raise ValueError("invalid JSON Pointer escape")
        key = encoded.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            if key not in value:
                raise ValueError("JSON Pointer does not exist")
            value = value[key]
        elif isinstance(value, list):
            if not key.isascii() or not key.isdigit() or (len(key) > 1 and key.startswith("0")) or int(key) >= len(value):
                raise ValueError("JSON Pointer array index does not exist")
            value = value[int(key)]
        else:
            raise TypeError("JSON Pointer traverses a scalar")
    return _json_dumps(value)


class ObservationStore:
    """Run-scoped registry for observation text.

    Disk files are optional durability for debugging/resume, but reads are permitted only for ids
    registered in this in-memory store.  The tool never accepts paths.
    """

    def __init__(self, directory: Path | None = None) -> None:
        self._records: dict[str, _Observation] = {}
        self._ids_by_digest: dict[str, str] = {}
        self._directory = directory
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)

    def present(self, text: str, tool_name: str, is_error: bool, budget: int) -> str:
        if budget < MIN_PRESENTATION_BUDGET:
            raise ValueError(f"observation presentation budget must be >= {MIN_PRESENTATION_BUDGET}")
        digest = _sha256(text)
        source = _source_metadata(text)
        provenance_digest = _sha256(_json_dumps({"sha256": digest, "tool_name": tool_name, "is_error": is_error, "source": source}))
        observation_id = self._ids_by_digest.get(provenance_digest)
        if observation_id is None:
            observation_id = f"O{len(self._ids_by_digest) + 1}"
            self._ids_by_digest[provenance_digest] = observation_id
        record = _Observation(
            observation_id=observation_id,
            tool_name=tool_name,
            text=text,
            is_error=is_error,
            sha256=digest,
            chars=len(text),
            source=source,
        )
        self._records[observation_id] = record
        if self._directory is not None:
            (self._directory / f"{observation_id}.json").write_text(
                _json_dumps(
                    {
                        "version": VERSION,
                        "observation_id": observation_id,
                        "tool_name": tool_name,
                        "is_error": is_error,
                        "chars": len(text),
                        "sha256": digest,
                        "source": source,
                        "text": text,
                    }
                ),
                encoding="utf-8",
            )
        fields, omitted = _selected_fields(text, budget // 2)
        envelope: dict[str, Any] = {
            "version": VERSION,
            "observation_id": observation_id,
            "tool_name": tool_name,
            "is_error": is_error,
            "chars": len(text),
            "sha256": digest,
            "source": source,
            "fields": fields,
            "omitted_fields": omitted,
            "retrieval": {
                "tool": READ_OBSERVATION_TOOL_NAME,
                "arguments": {"observation_id": observation_id, "offset": 0, "limit": min(DEFAULT_PAGE_LIMIT, len(text))},
            },
            "presentation": {
                "complete": False,
                "source_truncated": source.get("truncated"),
                "budget_chars": budget,
                "note": "initial view contains whole selected fields only; use read_observation for exact text",
            },
        }
        return _bounded_json(envelope, budget)

    def read(self, args: dict[str, Any], budget: int) -> tuple[str, bool]:
        if budget < MIN_PRESENTATION_BUDGET:
            raise ValueError(f"observation read budget must be >= {MIN_PRESENTATION_BUDGET}")
        raw_observation_id = str(args.get("observation_id", ""))
        observation_id = raw_observation_id if len(_json_dumps(raw_observation_id)) <= 128 else raw_observation_id[:16] + f":sha256:{_sha256(raw_observation_id)[:16]}"
        record = self._records.get(observation_id)
        if record is None:
            return _bounded_json(
                {
                    "version": VERSION,
                    "error": "unknown observation_id",
                    "observation_id": observation_id,
                    "known": False,
                    "presentation": {"complete": True, "budget_chars": budget},
                },
                budget,
            ), True
        selected = record.text
        pointer = args.get("json_pointer")
        if "json_pointer" in args:
            try:
                selected = _select_json(record.text, pointer)
            except (ValueError, TypeError):
                return _json_dumps({"version": VERSION, "observation_id": observation_id, "error": "invalid json_pointer or source is not valid JSON"}), True
        offset_raw = args.get("offset", 0)
        limit_raw = args.get("limit", DEFAULT_PAGE_LIMIT)
        if isinstance(offset_raw, bool) or not isinstance(offset_raw, int) or isinstance(limit_raw, bool) or not isinstance(limit_raw, int):
            return _json_dumps({"version": VERSION, "error": "offset and limit must be integers", "observation_id": observation_id}), True
        offset = offset_raw
        limit = limit_raw
        if offset < 0:
            return _json_dumps({"version": VERSION, "error": "offset must be >= 0", "observation_id": observation_id}), True
        if offset > len(selected):
            return _json_dumps({"version": VERSION, "error": "offset exceeds observation length", "observation_id": observation_id, "chars": len(selected)}), True
        if limit <= 0 or limit > MAX_PAGE_LIMIT:
            return _json_dumps({"version": VERSION, "observation_id": observation_id, "error": "limit is out of range"}), True
        selection_args = {"json_pointer": pointer} if "json_pointer" in args else {}
        base = {
            "version": VERSION,
            "observation_id": observation_id,
            "tool_name": record.tool_name,
            "is_error": record.is_error,
            "chars": record.chars,
            "sha256": record.sha256,
            "source": record.source,
            "retrieval": {"tool": READ_OBSERVATION_TOOL_NAME, "arguments": {"observation_id": observation_id, **selection_args, "offset": offset, "limit": limit}},
            "page": {"offset": offset, "limit": limit, "selected_chars": len(selected), "json_pointer": pointer, "source_truncated": record.source.get("truncated"), "text": ""},
        }
        high = min(limit, len(selected) - offset)
        low = 0
        best = None
        while low <= high:
            mid = (low + high) // 2
            chunk = selected[offset : offset + mid]
            next_offset = offset + len(chunk)
            complete = next_offset >= len(selected)
            candidate = json.loads(json.dumps(base, ensure_ascii=False))
            candidate["page"]["text"] = chunk
            candidate["retrieval"]["arguments"] = {"observation_id": observation_id, **selection_args, "offset": next_offset, "limit": limit}
            candidate["page"].update(
                {
                    "returned_chars": len(chunk),
                    "next_offset": None if complete else next_offset,
                    "complete": complete,
                    "presentation_omission": not complete,
                }
            )
            if len(_json_dumps(candidate)) <= budget:
                best = candidate
                low = mid + 1
            else:
                high = mid - 1
        if best is None or (best["page"].get("returned_chars", 0) == 0 and offset < len(selected)):
            return _bounded_json(
                {
                    "version": VERSION,
                    "error": "budget too small for observation page metadata",
                    "observation_id": observation_id,
                    "chars": record.chars,
                    "presentation": {"complete": True, "budget_chars": budget},
                },
                budget,
            ), True
        return _json_dumps(best), record.is_error
