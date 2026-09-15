---
title: RCA agent 도구 계약
status: Deprecated
owner: project
last_reviewed: 2026-07-28
tags:
  - rca
  - agent
  - tools
  - contract
summary: RCA agent 조사자에게 노출되는 조회 도구 계약 — 설계 원칙, 응답 봉투, 공통·도메인 특화 도구 목록, 깊이 참조 목록, 단계별 도구 배치.
---

# RCA agent 도구 계약

> **대체 안내 (2026-07-28).** 도구 표면의 정본은 이제
> [도구 표면 재설계](spec-tool-redesign.md)다 — 표면 초판이 검수를 통과해
> 확정(2026-07-24)되면서 예고대로 이 문서를 대체했다. 이 문서는 규칙 중심
> 시기(18종 표면)의 기록으로 남긴다.
>
> 과도기 주의: 구현은 도구를 하나씩 교체 중이다(scan_metrics ·
> read_timeseries · sample_logs 완료). 아직 교체되지 않은 자리는 이 문서의
> 옛 도구가 임시 표면으로 남아 있으므로, **현재 배선의 정본은 코드**
> (`tools.Toolset`)다. 봉투 원칙(§2)과 레드팀 기록(§8)처럼 새 설계가
> 계승한 내용은 spec-tool-redesign에 다시 근거로 인용돼 있다.

이 문서는 [구조 설계](spec-agent-design.md)의 조사자가 데이터를 조회하는
도구 계약을 정의한다. 도구는 조사 깊이의 상한을 결정한다 — agent는
도구가 반환하는 것보다 깊이 내려갈 수 없다. 따라서 이 계약의 차원
필드 목록이 구조 설계 §6.3 깊이 의무의 참조 목록이다.

모든 도구는 lucida-next가 실제 수집하는 데이터
([데이터 수집 참조](ref-lucida-next-data-collection.md)) 위에 정의된다.
수집되지 않는 데이터를 가정한 도구는 없다.

## 1. 설계 원칙

1. **Read-only 전용.** 조회 도구만 존재한다. 상태를 바꾸는 도구
   (재시작·설정 변경·조치)는 계약에 없다 — 목적 문서 비목표("운영자
   검토 없이 자동 조치 없음")를 도구 수준에서 강제한다.
2. **저장소가 아니라 질문 단위.** 도구 이름과 인자는 조사 질문("누가
   누구를 블로킹하나")을 표현한다. 어느 저장소(VM/CH/PG)를 치는지는
   구현 뒤에 숨긴다. 수집 스택이 바뀌어도 계약은 유지된다.
3. **깊이를 계약에 명시.** 각 도구의 응답 스키마에 차원 필드(sql_id,
   프로세스명, 인터페이스, pod명 …)를 명시한다(§5).
4. **응답은 LLM 소비형: 요약 + 참조.** raw 데이터를 프롬프트에 넣지
   않는다. 응답은 판단용 요약(기준선 대비·추세·상위 N)과 evidence
   ref(원본 참조 키)의 쌍이다. 드릴다운은 같은 도구를 좁은 범위로
   재호출한다.
5. **"없음"을 세 가지로 구분.** 모든 응답은 정상 관측 / 이상 관측 /
   데이터 없음(미수집)을 구분한다. 완결성 원리의 "이행 불가 기록"과
   출력 계약 `data_coverage.gaps`/`ruled_out` 구분의 전제다.

## 2. 응답 봉투

모든 도구는 다음 봉투로 응답한다. status 3값은 유지하고(프롬프트·
장부 SourceStatus가 이 3값을 승계 — 하위 호환) 2026-07-15 레드팀
검토(§8)로 세부 필드를 확장했다.

```yaml
status: normal | anomalous | no_data  # 원칙 5
no_data_reason:                       # no_data일 때 필수 — 배제 근거의 품질
  zero_observations | not_collected | collector_gap | ttl_expired | unknown
assessment_basis: string              # normal|anomalous 판정의 기준 서술
                                      # (예: "기준선 창 대비", "한도 대비 %")
summary: string                       # LLM 소비형 요약 한 문단
findings:                             # 구조화 관측(도구별 스키마, 차원 필드 포함)
  - ...                               # 각 finding은 자기 refs를 결합해 든다 —
    refs: []                          # 어느 관측을 어느 원본이 지지하는지 추적
observed_range: {from, to}            # 실제 관측이 존재한 범위 — TTL·수집
                                      # 주기 때문에 요청 window와 다를 수 있다
truncated: bool                       # 상위 N 잘림 — true면 "그 밖에 없음"으로
                                      # 오인 금지 (조사자 프롬프트 v2/v5에 반영).
                                      # 격자(§9)에서는 제3값 "부분 관측"이 된다
                                      # — 프롬프트 규범만으로는 부족(설계 §15.4 ③)
refs: []                              # 봉투 수준 참조(재현 쿼리 등)
```

- **no_data는 하나가 아니다.** `zero_observations`(조회 성공, 관측
  0건 — 가장 강한 배제 근거) / `not_collected`(이 대상엔 원래 미적용)
  / `collector_gap`(수집기 장애 — **배제 근거로 쓰면 안 됨**) /
  `ttl_expired`(보관 초과 삭제) / `unknown`. 구분 못 하면
  `ruled_out`과 `missing_evidence`의 경계가 무너진다. 판별 재료는
  `get_data_coverage`(§3).
- **시간 의미론.** window는 UTC, `[from, to)` 반개구간. 관측의
  시간은 event time 기준(수집·적재 시각과 구분 — 봉투의
  observed_range도 event time). 기준선 비교가 필요한 도구는
  `baseline_window` 인자를 별도로 받는다(생략 시 직전 동일 길이 창).
  조회 실패(오류)는 no_data가 아니라 오류다.
- **오류 응답은 복구 정보를 포함해야 한다.** 인자 검증 실패 시 유효한
  값 목록(또는 근접 후보)을 함께 돌려준다. spike 3(구조 설계 §10)에서
  복구 정보 없는 오류는 동일 호출 무한 반복을 만들었고, 유효 target_id
  목록을 담은 오류는 즉시 자가 수정을 만들었다 — 루프 생존 조건이다.
- **모든 봉투는 refs를 보증한다 (2026-07-16 no_data → 2026-07-20
  전체로 일반화).** 봉투의 모든 관측은 인용 가능해야 한다 — "조회했으나
  없음"(no_data)뿐 아니라 "trap 0건 = 미발생"·지표 인벤토리 요약 같은
  normal 관측도 조사자의 근거가 된다. 인용할 ref가 없으면 모델이 ref를
  지어내고 근거 ref 검증 가드(프롬프트 문서 §6.6 ①)에 막힌다(재생
  주행 실측 — discover_signals normal 봉투). 도구 구현이 refs를 채우지
  않은 봉투는 조립 지점(`tools.Toolset`의 `withEnvelopeRef`)이
  `tool:<이름>:<인자 compact JSON>` ref를 보증한다(형식은 §7 ref 포맷
  열린 결정에 종속된 임시).
- `refs`의 포맷(원본 저장소 주소 체계)은 열린 결정(§7).

## 3. 공통 도구 (도메인 무관)

앞 세 개는 **메타 도구**다(2026-07-15 신설, §8 레드팀 검토) — 조사
공간의 발견(`discover_signals`), 관측의 증명(`get_data_coverage`),
계측 밖 의존의 발견(`get_runtime_connections`). 도메인 도구를 늘리기
전에 이 축이 먼저다: 조사자가 지표 이름을 모르면 조회 도구는 부를 수
없고, 커버리지를 모르면 no_data를 배제 근거로 쓸 수 없고, topology에
없는 의존은 미조사로조차 기록되지 않는다.

| 도구 | 인자 | 답하는 질문 | 차원 |
| --- | --- | --- | --- |
| `discover_signals` | target, window, top_n | 이 대상에서 무엇이 이상한가 — 지표 인벤토리(이름·단위·산식 의미 포함) + 기준선 대비 이상 상위 N | metric명 |
| `get_data_coverage` | targets (목록), window | 이 대상·시간창에 어떤 데이터가 수집되고 있(었)나 — 수집기 상태·관측 범위·결손 사유 | 데이터 종류별 coverage |
| `get_runtime_connections` | host, window | 이 서버가 실제로 누구와 통신하나 — topology 밖 런타임 의존 발견 | 원격 endpoint, 프로세스, TCP state |
| `get_target_meta` | targets (목록, 일괄) | 이 대상은 무엇인가 (이름·도메인·서비스) | — |
| `get_metric_series` | target, metric, window | 이 지표가 기준선 대비 어떤가 — metric 이름은 `discover_signals`가 알려준 것을 쓴다 | — |
| `get_metric_dimensions` | target, metric, dim_label, window, top_n | 이 집계 지표를 누가 끌어올렸나 | process / interface / pod … |
| `get_events` | target, window | 이 대상에 어떤 이벤트가 있었나 | — |
| ~~`get_changes`~~ → `list_changes` | target?, from?, to? | 뭐가 바뀌었나 — **창 전역 반환, 대상은 정렬 힌트**(재설계 §10) | 사건(kind, at, scope, correlation, targets) |
| `get_logs` | target, window, filter? | 로그에 뭐가 나타났나 (레벨·템플릿 분포 + 대표 라인) | 로그 템플릿 |
| ~~`get_cohort`~~ → `compare_peers` | target, metric, from, to | 혼자 이탈인가, 또래도 같이 이탈인가 — **판정은 각자 자기 기준선 대비**(재설계 §11) | verdict(alone/shared/self_not_deviating/undecidable) + 또래 근거·분모 |

## 4. 도메인 특화 도구

| 도메인 | 도구 | 인자 | 답하는 질문 | 차원 |
| --- | --- | --- | --- | --- |
| application | `get_trace_breakdown` | service, window | 지연이 내 코드에서 나나, 어느 하위 호출에서 나나 | 호출 구간(하위 서비스/DB/외부) |
| application | `get_slow_endpoints` | service, window, top_n | 어느 엔드포인트가 느리거나 에러 나나 | endpoint |
| database | ~~`get_db_sessions`~~ → `db_blocking` | db, window(생략 가능) | 누가 누구를 블로킹하나 (세션 볼륨은 read_timeseries로 분리 — 옴니버스 금지) | 사건별 루트 세션·막힌 세션 수·경합 객체 (spec-tool-redesign.md §13) |
| database | ~~`get_slow_queries`~~ → `db_slow_queries` | db, window, top_n, sort | 어느 쿼리가 느린가 | 자리 교체(spec-tool-redesign §14) |
| server | `get_processes` | host, window, sort_by, top_n | 어느 프로세스가 리소스를 먹나 | 프로세스명/pid |
| network | `get_snmp_traps` | device, window | 장비가 직접 원인 신호(linkDown 등)를 보냈나 | trap 종류 |
| kubernetes | `get_k8s_state` | target, window | pod 재시작·스케줄링·리소스 상태는 | pod/container |

토폴로지 보조: `get_topology(target, hops)` — seed의 topology
subgraph가 잘려 있을 때 인접을 확장 조회한다.

네트워크 인터페이스별 분해는 별도 도구가 아니라
`get_metric_dimensions(dim_label="interface")`로 흡수한다 — 프로세스/
pod 분해와 같은 질문 형태이며, 도구 수를 줄이는 것이 조사자의 선택
부담을 줄인다.

## 5. 깊이 참조 목록

구조 설계 §6.3 깊이 의무가 참조하는 **원인 유형별 필수 probe family**
목록이다(2026-07-15 개정 — "도구가 존재하면 시도"가 아니라 "family가
충족됐는가"로, §8). 채택 가설의 원인 도메인에 대해 아래 도구를
시도하지 않았으면 `confirmed`를 보류하고, 도구가 아예 없는 도메인
(현재 virtualization)은 의무 계산기가 unsupported로 기록해
`confirmed`를 차단한다.

| 원인 도메인 | 지목 가능한 최소 단위 | 도구 |
| --- | --- | --- |
| database | sql_id, session, 대상 테이블 | `db_blocking`, `db_slow_queries` |
| server | 프로세스 | `get_processes`, `get_metric_dimensions(process)` |
| application | endpoint, 하위 호출 구간 | `get_slow_endpoints`, `get_trace_breakdown` |
| network | 인터페이스, trap 종류 | `get_metric_dimensions(interface)`, `get_snmp_traps` |
| kubernetes | pod/container | `get_k8s_state`, `get_metric_dimensions(pod)` |
| virtualization | (1차 유보 — §6) | — |

이 표의 차원 값은 **종류 예시**다. 차원 식별자의 정본 표기(예:
PostgreSQL의 `sql_id` vs `sql_hash`, 인터페이스의 `ifIndex` vs 이름)는
실제 수집 스키마 기반의 **canonical dimension registry**(신설 예정,
평가·시나리오·테스트베드와 공유)를 정본으로 따른다. registry 없이
문서마다 식별자를 하드코딩하면 golden 채점과 도구 응답이 어긋난다.

## 6. 도구 배치 — 누가 언제 쓰나

도구 계약(무엇이 있나)과 배치(누가 쓰나)는 분리한다.

| 단계 | 사용 주체 | 도구 |
| --- | --- | --- |
| [1] Triage | **파이프라인 코드 (LLM 아님)** | `get_target_meta` — 도메인 해석(증상·후보 도메인)의 정본. topology 노드의 `Domain`은 보조(메타 미해석 시 fallback). lucida-next도 도메인 선택은 targets 조회(`TargetMeta`)로 한다 (2026-07-13 확인) |
| [2] 변경 조회 | **파이프라인 코드 (LLM 아님)** | typed 변경 조회(`tools.NewChangesFunc`) — 모든 인시던트에서 무조건, 증상 대상 + topology 인접에 대해 결정론 호출. **LLM 표면 `list_changes`와 별개 조회다**(대상 지목 계약 유지 — 재설계 §10.9 이월) |
| [3] 1차 조사 | 조사자(LLM) | **`discover_signals`로 시작**(대상마다 무엇이 이상한지 발굴 — 지표 이름을 아는 유일한 경로) + 공통 도구 (events, cohort, trace_breakdown, runtime_connections로 계측 밖 이웃 탐색) |
| [5] 검증 루프 | 조사자(LLM) | 전체 — 선택은 가설의 검증 계획·반증 조건이 유도 |
| 도메인 전문가 도입 시(2차) | 각 전문가 | 자기 도메인 특화 도구 + 공통 도구. §4의 도메인 열이 배정표가 된다 |

1차 유보 도구: WPM(웹 URL 응답), netflow, virtualization 계열 —
수집은 존재하나 테스트베드 시나리오가 요구할 때 추가한다. 도구 추가는
계약에 행을 늘리는 것이라 저비용이다.

## 7. 열린 결정

- **ref 포맷.** evidence ref가 원본(CH row, VM 쿼리, PG row)을
  가리키는 주소 체계. 채점 하네스·UI의 ref 해석 계약과 함께 정한다.
- **인자 검증 규칙.** target_id가 seed/topology에 실존하는지, window가
  분석 시간창을 벗어나는지 등의 코드 레벨 검증과 오류 응답 형식.
- **요약 통계의 구체 스키마.** `findings`의 도구별 필드 확정 —
  뼈대 구현에서 mock으로 먼저 고정하고 실 구현 시 확정.
- **no_data_reason 판별 규칙.** 봉투의 결손 사유 5값을 실제로 어떻게
  구분하나 — `get_data_coverage` 구현(수집기 상태·정책·TTL 대조)과
  함께 정한다.
- **assessment(normal|anomalous) 판정 규칙.** 기준선 창 산정·임계
  방식 — 도구가 결론을 선반영하므로 assessment_basis에 근거 서술을
  강제하되, 판정식 자체의 정본은 구현 시 확정. **우선순위 상승
  (2026-07-21, 설계 §15.4 ②):** 격자 칸의 "정상"은 그 대상·관점을
  용의선상에서 빼므로, 판정식이 틀리면 거짓 안심을 대량 생산한다 —
  미뤄도 되는 결정에서 격자의 전제 조건으로 격상. 현 구현의 임시
  판정식(±50% 등)이 첫 실측 대상.
- ~~**도구 구현의 데이터 소스 매핑.**~~ 해소(2026-07-15) —
  [ref-tool-data-access.md](ref-tool-data-access.md). 저장소·테이블·
  식별 체계의 지도(스키마 기준). 조회 설계는 이 계약에서 도출하며
  lucida-next 기존 조회 코드는 참조하지 않는다(불참조 원칙을 데이터
  접근 코드까지 확장).

## 8. 검토 기록 — 충분성 레드팀 (2026-07-15)

"이 도구 목록이 실전 RCA에 충분한가"를 자체 분석 + Codex(gpt-5.6-sol)
교차 레드팀으로 검토했다(원문:
`.omc/artifacts/ask/codex-…-2026-07-15T01-41-19-456Z.md`). 합의된
핵심: **맹점은 도구 수가 아니라 "조사 공간을 발견하고 그 공간이
실제로 관측됐는지 증명하는 메타 도구"의 부재**였다.

반영(P0):

1. 메타 도구 3종 신설(§3) — `discover_signals` /
   `get_data_coverage` / `get_runtime_connections`.
2. 응답 봉투 확장(§2) — no_data 사유 5구분, assessment_basis,
   finding별 refs 결합, observed_range, truncated, 시간 의미론.
3. 완결성 규칙 수정(구조 설계 §6.3) — 깊이 의무를 "차원 도구가
   존재하면"에서 "원인 유형별 필수 probe family 충족"으로. 도구
   부재는 의무 소멸이 아니라 `confirmed` 차단 사유.

유보(기존 원칙대로 시나리오가 요구할 때):

- 도메인 드릴다운 확장 — WPM/WebURL, netflow 5-tuple, DB plan/config
  비교, K8s YAML history diff, BMC/Redfish, virtualization 관계 도구,
  알람 생명주기·과거 인시던트 검색(피드백(b)·past_case와 묶임).
- trace exemplar 검색(`get_trace(trace_id)` 류), 다중 대상 batch 비교.

이 검토가 남긴 원칙: **도구가 없는 영역은 조사 의무에서 조용히
빠지면 안 된다** — 계약에 없는 도메인·데이터는 의무 계산기에서
명시적 unsupported로 남아 confirmed를 막는다.

## 9. 격자 칸 배정표 (2026-07-21, 설계 §15.4 ①)

[3] 커버리지 격자(설계 §15.2)의 관점 칸을 어느 도구 호출이 채우는가.
이 표가 없으면 discover_signals는 지표 세계만 훑으므로 "한 번 돌리고
다 봤다" 착각이 구조적으로 재발한다. 구현 거울:
`pipeline/grid.go`의 `gridAssign`.

| 관점 | 채우는 도구 | 비고 |
| --- | --- | --- |
| metrics (골든 시그널 묶음) | `discover_signals`(일괄 주력), `get_metric_series`, `get_metric_dimensions`, `get_cohort` | 골든 시그널 4종 개별 분류는 지표명→시그널 매핑이 필요해 후속 확장(§7 판정식과 함께) |
| events | `get_events`, `get_snmp_traps`, `get_k8s_state` | 이산 사실 관점 |
| logs | `get_logs` | |

격자 밖 도구: topology·meta·runtime_connections(대상 축 발견용),
드릴다운 계열(trace·DB·프로세스 — [5] 감별·깊이 의무 몫), 변경([2]가
결정론 담당). v1 격자는 채움/빈 칸만 계산하고 **비게이트 계측**이다
— 첫 스모크 실측: 6대상×3관점에서 빈 칸 10(discover 일괄 후 비이상
대상의 events·logs 건너뜀). 빈 칸 율 축적 후 보충 라운드·truncated
제3값·특성화 계산(전부/일부·혼자/또래)의 승격을 결정한다.
