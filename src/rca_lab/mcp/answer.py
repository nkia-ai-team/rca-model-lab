"""Final-answer contract shared by teacher and student: the `submit_rca` tool."""

from __future__ import annotations

import copy
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



def _submit_tool_r2() -> dict:
    """Revision 2 wording of submit_rca (2026-10-19; see prompts._EXTERNAL_RULE_R2): only origins go into causes /
    external_causes, the condition that exposed a defect goes into that cause's mechanism."""
    tool = copy.deepcopy(SUBMIT_TOOL)
    tool["description"] = (
        "조사를 끝내고 근본 원인 결론을 제출한다. 한 번만 호출한다. "
        "causes.target 은 도구 응답에서 직접 관측한 대상 UUID 여야 하고, support_refs 는 도구 응답의 refs 값이어야 한다. "
        "causes·external_causes 에는 결함이나 한도가 시작된 발원지만 적는다. "
        "external_causes 는 발원지가 관측 계측 밖(외부 의존, 브로커, 네트워크)에 있고 그 실패가 경계에서 관측될 때만 "
        "boundary_target(그 영향이 처음 관측된 내부 대상 UUID)과 함께 적는다. "
        "결함을 드러나게 한 계기·조건(부하·트래픽 급증, 계측 밖 클라이언트의 요청·조작)은 해당 cause 의 mechanism 에 쓴다."
    )
    tool["inputSchema"]["properties"]["causes"]["items"]["properties"]["mechanism"]["description"] = (
        "무엇이 어떻게 장애를 일으켰는가 (결함을 드러나게 한 계기·조건 포함)")
    return tool


SUBMIT_TOOL_R2 = _submit_tool_r2()


def _submit_tool_r3() -> dict:
    """Revision 3 wording (2026-10-21; see prompts._EXTERNAL_RULE_R3): the starting change ("발단") and the exposing
    condition are named apart; load / traffic is never the starting change."""
    tool = copy.deepcopy(SUBMIT_TOOL)
    tool["description"] = (
        "조사를 끝내고 근본 원인 결론을 제출한다. 한 번만 호출한다. "
        "causes.target 은 도구 응답에서 직접 관측한 대상 UUID 여야 하고, support_refs 는 도구 응답의 refs 값이어야 한다. "
        "causes·external_causes 에는 결함이나 한도가 시작된 발원지만 적는다. "
        "external_causes 는 발원지가 관측 계측 밖(외부 의존, 브로커, 네트워크)에 있고 그 실패가 경계에서 관측될 때만 "
        "boundary_target(그 영향이 처음 관측된 내부 대상 UUID)과 함께 적는다. "
        "mechanism 에는 발단(결함을 만든 변경·사건, 주체가 관측되지 않으면 미관측이라고)과 결함을 드러낸 조건(부하·트래픽)을 구분해 쓴다."
    )
    tool["inputSchema"]["properties"]["causes"]["items"]["properties"]["mechanism"]["description"] = (
        "발단(결함을 만든 변경·사건)이 무엇이고 어떻게 장애를 일으켰는가, 그리고 그것을 드러낸 조건")
    return tool


SUBMIT_TOOL_R3 = _submit_tool_r3()


def submit_tool(revision: int = 1) -> dict:
    if revision not in (1, 2, 3):
        raise ValueError(f"unknown prompt revision {revision}")
    return {1: SUBMIT_TOOL, 2: SUBMIT_TOOL_R2, 3: SUBMIT_TOOL_R3}[revision]

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
