"""Final-answer contract shared by teacher and student: the `submit_rca` tool."""

from __future__ import annotations

import re
from typing import Any

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
STATUSES = ("confirmed", "provisional", "insufficient")
EXTERNAL_KINDS = ("external_dependency", "kafka", "redis", "network", "capacity_limit")
SUBMIT_TOOL_NAME = "submit_rca"

SUBMIT_TOOL = {
    "name": SUBMIT_TOOL_NAME,
    "description": (
        "조사를 끝내고 근본 원인 결론을 제출한다. 한 번만 호출한다. "
        "causes.target 은 도구 응답에서 직접 관측한 대상 UUID 여야 하고, support_refs 는 도구 응답의 refs 값이어야 한다. "
        "관측 계측 밖의 원인(외부 의존, 브로커, 네트워크)은 external_causes 에 boundary_target(그 영향이 처음 관측된 내부 대상 UUID)과 함께 적는다."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": list(STATUSES), "description": "confirmed=직접 인과 관측, provisional=영향 실측 2개 이상이나 직접 인과 미관측, insufficient=근거 부족"},
            "summary": {"type": "string", "description": "결론 요약 2~4문장 (관측 근거 인용)"},
            "causes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "target": {"type": "string", "description": "원인 대상 UUID"},
                        "mechanism": {"type": "string", "description": "무엇이 어떻게 장애를 일으켰는가"},
                        "support_refs": {"type": "array", "items": {"type": "string"}, "description": "근거 ref 목록 (도구 응답 refs 값 그대로)"},
                    },
                    "required": ["target", "mechanism", "support_refs"],
                },
            },
            "external_causes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "external:<이름> 형식의 외부 원인 식별자"},
                        "kind": {"type": "string", "enum": list(EXTERNAL_KINDS)},
                        "name": {"type": "string"},
                        "boundary_target": {"type": "string", "description": "외부 영향이 처음 관측된 내부 대상 UUID"},
                        "evidence_refs": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["id", "kind", "name", "boundary_target", "evidence_refs"],
                },
            },
        },
        "required": ["status", "summary", "causes"],
    },
}


# Most candidates one answer may name (causes + external_causes). The largest golden root set is 3
# (two internal origins + one external); more candidates can only hedge (2026-10-01).
MAX_CANDIDATES = 3


def validate_result(args: dict[str, Any]) -> dict[str, Any]:
    """Return the normalised result or raise ValueError with recovery information."""
    problems: list[str] = []
    status = str(args.get("status", ""))
    if status not in STATUSES:
        problems.append(f"status must be one of {STATUSES}, got {status!r}")
    causes_in = args.get("causes")
    if not isinstance(causes_in, list):
        problems.append("causes must be a list (may be empty only when status=insufficient)")
        causes_in = []
    causes: list[dict[str, Any]] = []
    for index, cause in enumerate(causes_in):
        if not isinstance(cause, dict):
            problems.append(f"causes[{index}] must be an object")
            continue
        target = str(cause.get("target", "")).strip()
        if not UUID_RE.match(target):
            problems.append(f"causes[{index}].target must be a target UUID observed through the tools, got {target!r}")
        refs = [str(ref) for ref in cause.get("support_refs", []) if str(ref).strip()]
        if not refs:
            problems.append(f"causes[{index}].support_refs must cite at least one tool ref")
        causes.append({"target": target.lower(), "mechanism": str(cause.get("mechanism", "")).strip(), "support_refs": refs})
    external: list[dict[str, Any]] = []
    for index, cause in enumerate(args.get("external_causes") or []):
        if not isinstance(cause, dict):
            problems.append(f"external_causes[{index}] must be an object")
            continue
        identifier = str(cause.get("id", "")).strip()
        if not identifier.startswith("external:"):
            problems.append(f"external_causes[{index}].id must start with 'external:'")
        kind = str(cause.get("kind", ""))
        if kind not in EXTERNAL_KINDS:
            problems.append(f"external_causes[{index}].kind must be one of {EXTERNAL_KINDS}")
        boundary = str(cause.get("boundary_target", "")).strip()
        if not UUID_RE.match(boundary):
            problems.append(f"external_causes[{index}].boundary_target must be an internal target UUID")
        refs = [str(ref) for ref in cause.get("evidence_refs", []) if str(ref).strip()]
        if not refs:
            problems.append(f"external_causes[{index}].evidence_refs must cite at least one tool ref")
        external.append({"id": identifier, "kind": kind, "name": str(cause.get("name", "")).strip(), "boundary_target": boundary.lower(), "evidence_refs": refs})
    if status != "insufficient" and not causes and not external:
        problems.append("a confirmed/provisional answer needs at least one cause or external cause")
    if len(causes) + len(external) > MAX_CANDIDATES:
        problems.append(f"causes + external_causes may name at most {MAX_CANDIDATES} candidates in total, got {len(causes) + len(external)} "
                        "(origins only; affected targets on the propagation path belong in summary)")
    if problems:
        raise ValueError("submit_rca rejected: " + "; ".join(problems))
    return {"status": status, "summary": str(args.get("summary", "")).strip(), "causes": causes, "external_causes": external}
