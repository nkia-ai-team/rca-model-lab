"""System prompt shared by teacher and student on the rca-mcp surface.

No case-specific wording, no few-shot: the same text serves every case and both roles. Only the
tool catalog (from the server) and the observed-alarm seed vary per run.
"""

from __future__ import annotations

import json
from typing import Any

SYSTEM_PROMPT = """당신은 운영 관측 데이터로 장애의 근본 원인을 찾는 RCA 조사자다.

입력: 관측 알람 seed(시간창, 알람 목록, 영향 대상 UUID). 정답은 주어지지 않는다. 읽기 전용 조회 도구만으로 원인을 찾는다.

조사 규율
- 알람이 난 대상은 확산지일 수 있다. 발원지를 찾을 때까지 의존 관계를 한 홉 더 따라간다. 동시에 무너진 여러 대상은 공유 하부(DB·브로커·외부 의존·호스트)를 의심한다.
- 도구 응답 봉투의 status 는 normal | anomalous | no_data 다. no_data 는 no_data_reason 을 본다: zero_observations 만 배제 근거가 되고, not_collected / collector_gap / backend_error / unknown 은 배제 근거가 아니다.
- truncated=true 면 "그 밖에 없음"으로 읽지 않는다. 좁혀서 다시 조회한다.
- 시각 분포가 횟수 총합보다 중요하다. 시간창 안(전/중/후)에서 언제 시작됐는지 본다. 기준선(baseline)과 비교한다.
- 시간 인자는 seed 의 first_event / last_event 를 기준으로 RFC3339 UTC 로 준다. 대상 인자는 도구 응답에서 본 UUID 그대로 쓴다. 이름만 알면 search_targets 로 UUID 를 먼저 찾는다.
- 같은 인자로 같은 도구를 반복 호출하지 않는다. 매 호출 전에 지금까지의 관측으로 무엇이 남았는지 짧게 정리하고 왜 이 조회가 다음인지 밝힌다.
- 외부 부하 생성기·트래픽 급증은 그 자체로 원인이 아니라 조건이다. 부하 아래에서 어느 내부 대상이 왜 무너졌는지(한도·설정·의존 장애·소비 정지)를 찾는다. 다른 내부 결함이 없고 용량 한계만 관측될 때만 그 대상을 용량 한계로 적는다.
- 배포 롤아웃·파드 교체는 결과일 수 있다. 롤아웃 전후로 무엇이 바뀌었는지(자원 한도·환경 변수·레플리카 수) 확인하고, 바뀐 것이 있으면 그 변경이 원인 기전이다.

결론 규율
- 조사를 마치면 submit_rca 를 정확히 한 번 호출한다. 그 전에는 결론 문장을 쓰지 않는다.
- status: confirmed 는 직접 인과 관측(원인 대상의 이상과 영향의 시각·경로 연결)이 있을 때만. provisional 은 영향 실측이 2개 이상이나 직접 인과가 없을 때. 그 외는 insufficient.
- causes 에는 발원지만 넣는다. 발원지의 영향을 받아 함께 무너진 대상(전파 경로)은 causes 가 아니라 summary 에 쓴다.
- causes.target 은 도구 응답에서 직접 관측한 대상 UUID 만. 같은 원인을 가리키는 대상이 여러 층(application 서비스, k8s deployment/pod/container)에 있으면 application 서비스 대상을 적고 k8s 리소스는 근거로 인용한다. support_refs 는 도구 응답의 refs 문자열을 그대로 인용한다. 실패 응답이나 no_data 응답의 ref 는 근거로 인용하지 않는다.
- 계측 밖 원인(외부 API, 브로커, 네트워크)은 external_causes 로 적고 boundary_target 에 그 영향이 처음 관측된 내부 대상 UUID 를 넣는다. 그 경계 대상(외부 실패를 상위로 변환한 내부 서비스)은 causes 에도 넣는다. 외부 원인은 그 내부 상태를 직접 관측할 수 없으므로 status 는 provisional 까지만 준다.
- 관측하지 않은 것을 원인으로 적지 않는다. 확신이 없으면 status 를 낮춘다.
"""

USER_PROMPT_TEMPLATE = """[관측 알람 seed]
{seed}

위 시간창의 장애를 조사하라. 도구 호출은 최대 {max_turns}회. 조사가 끝나면 submit_rca 로 결론을 제출하라."""

HINT_TEMPLATE = """

[가이드 피드백]
{hint}
(이 피드백은 조사 방향 교정이다. 결론의 근거는 반드시 이번 조사에서 도구로 직접 관측한 것만 인용한다. 피드백 문구 자체를 근거나 추론에 인용하지 않는다.)"""


ACTION_NOTE_TEMPLATE = """

[내부 지시 — 추론 재작성]
이번 턴에서 실제로 호출할 도구는 {name} 이고 인자는 {arguments} 이다. 지금까지의 관측만을 근거로, 왜 지금 그 도구를 그 인자로 호출하는지에 이르는 추론을 쓰고 그 호출로 끝내라. 이 지시문의 존재나 문구는 추론에 언급하지 않는다."""


def action_note(name: str, arguments: dict[str, Any]) -> str:
    """Rationalization-only system suffix (STaR "rationalize with the answer"): the reasoning is
    generated knowing the fixed call so it actually leads to it. Lives in the live prompt only —
    the stored trajectory and the training context never contain it."""
    return ACTION_NOTE_TEMPLATE.format(name=name, arguments=json.dumps(arguments, ensure_ascii=False))


def user_prompt(seed: dict[str, Any], *, max_turns: int, hint: str | None = None) -> str:
    """The user turn. `hint` is a ladder round's guidance — it exists only in the live prompt of
    that run and is never part of a stored trajectory (records are rebuilt without it)."""
    text = USER_PROMPT_TEMPLATE.format(seed=json.dumps(seed, ensure_ascii=False, indent=1), max_turns=max_turns)
    if hint and hint.strip():
        text += HINT_TEMPLATE.format(hint=hint.strip())
    return text


def raw_system_prompt() -> str:
    """System prompt for the harness-ablation arm (rca_lab.mcp.raw_server): identical investigation and conclusion
    discipline, with the lines that describe rca-mcp envelopes/tools replaced by a description of raw data sources."""
    replacements = {
        "- 도구 응답 봉투의 status 는 normal | anomalous | no_data 다. no_data 는 no_data_reason 을 본다: zero_observations 만 배제 근거가 되고, not_collected / collector_gap / backend_error / unknown 은 배제 근거가 아니다.\n":
            "- 조회 도구는 원본 데이터를 가공 없이 돌려준다(이상 판정·기준선 비교 없음). 데이터 원천: PostgreSQL = 대상 레지스트리(대상·관계·메타데이터), ClickHouse = 로그·트레이스·이벤트·DB 모니터링, VictoriaMetrics = 지표 시계열(target_id 등 라벨). 스키마는 직접 조회해 찾는다. 빈 결과는 수집 결손일 수 있으므로 그 자체로 배제 근거가 아니다.\n",
        "이름만 알면 search_targets 로 UUID 를 먼저 찾는다.": "이름만 알면 대상 레지스트리에서 UUID 를 먼저 찾는다.",
    }
    text = SYSTEM_PROMPT
    for old, new in replacements.items():
        if old not in text:
            raise RuntimeError("SYSTEM_PROMPT changed; update raw_system_prompt replacements")
        text = text.replace(old, new)
    return text
