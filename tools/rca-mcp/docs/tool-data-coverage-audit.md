---
title: 도구-수집 데이터 커버리지 1차 감사
status: Draft
last_reviewed: 2026-09-08
---

# 범위

이 문서는 `/data/eval-cases`에 보존된 캡처 데이터와 현재 RCA 도구 표면을
정적으로 대조한 1차 감사다. 에이전트의 호출 전략이나 RCA 정답률은 범위에
포함하지 않는다. 다음 단계에서 대표 케이스를 복원해 응답 보존 여부를
확인한다.

# 확인된 표면

- 캡처 케이스 46개, 고유 `scenario_id` 36개
- 모든 케이스에 ClickHouse 데이터셋 13종이 존재한다.
- 현재 공개 `Toolset`은 `tools/registry.go`에 등록된 15개다.
- 응답은 공통 봉투, `refs`, 관측 범위, 결측·절단 표식을 사용한다.

# 정적 매핑

## 캡처 실물 확인

Parquet 메타데이터를 직접 읽은 결과, 46개 케이스 모두에서 13개 데이터셋이
존재했고 행 수가 0인 데이터셋은 없었다. 중앙값 행 수는 다음과 같다.

| 데이터셋 | 중앙 행 수 | 주요 정보 |
|---|---:|---|
| `otel_traces_local` | 573,098 | span·부모관계·duration·서비스·속성 |
| `lucida_logs_local` | 279,842 | 구조화 로그·trace/span 연결·Kubernetes 속성 |
| `process_snapshot` | 392,585 | PID·CPU·메모리·상태·생성시각 |
| `host_connections` | 48,857 | 프로세스·주소·포트·방향·소켓 상태 |
| `syslog_local` | 72,081 | 장비·facility·severity·원문 메시지 |
| `dpm_session_local` | 13,953 | 엔진·SQL ID·세션 본문 |
| `dpm_topsql_local` | 7,852 | 엔진·SQL ID·TopSQL 본문 |
| `trace_error_chains_local` | 10 | 경로별 오류 사슬·exemplar trace |
| `trace_path_signatures_local` | 373 | 경로 signature·빈도·exemplar trace |
| `event_cluster_projection_local` | 6,378 | 클러스터·desired version·할당시각 |
| `lucida_events_local` | 6,360 | 탐지 이벤트·episode·evidence |
| `kcm_events_local` | 109 | Kubernetes 이벤트 |
| `process_meta` | 23,165 | PID·명령행·사용자 |

따라서 “캡처에 데이터가 없어서 도구가 못 쓴다”는 설명은 현재 전체 캡처에
대해서는 성립하지 않는다. 데이터는 모두 존재하며, 다음 질문은 도구가 이를
노출·요약·연결하는지다.

| 캡처 데이터 | 현재 연결이 확인된 도구 | 1차 판정 |
|---|---|---|
| `lucida_logs_local` | `sample_logs`, `get_data_coverage` | 연결됨 |
| `lucida_events_local` | `list_events`, `get_data_coverage` | 연결됨 |
| `kcm_events_local` | `list_events` | 연결됨 |
| `otel_traces_local` | `expand_topology`, `breakdown_endpoints` | 연결됨 |
| `dpm_session_local` | `db_blocking`, `db_slow_queries` | 연결됨 |
| `dpm_topsql_local` | `db_slow_queries` | 연결됨 |
| 메트릭 저장소(VictoriaMetrics) | `scan_metrics`, `read_timeseries`, `compare_peers`, `get_processes` 등 | 연결됨 |
| `event_cluster_projection_local` | 직접 조회 경로 미확인 | 검증 필요 |
| `process_meta` | 직접 조회 경로 미확인 | 검증 필요 |
| `process_snapshot` | 직접 조회 경로 미확인 | 검증 필요 |
| `syslog_local` | 직접 조회 경로 미확인 | 검증 필요 |
| `trace_error_chains_local` | 직접 조회 경로 미확인 | 검증 필요 |
| `trace_path_signatures_local` | 직접 조회 경로 미확인 | 검증 필요 |
| `host_connections` | 직접 조회 경로 미확인; topology 구현은 CLIENT span 경로를 사용 | 검증 필요 |

“직접 조회 경로 미확인”은 불필요하다는 판정이 아니다. 보조 데이터인지,
다른 원천으로 대체되는지, 필요한데 도구가 빠진 것인지 대표 케이스에서
원자료와 결과를 대조해야 한다.

# 구성상 주의점

코드에는 `discover_signals`, `get_metric_series`, `get_metric_dimensions`가
구현되어 있지만 현재 공개 `Toolset`에는 `scan_metrics`, `read_timeseries`
중심의 새 표면이 등록되어 있다. 구현된 도구와 모델에 공개된 도구의 세대
차이를 문서와 함께 정리해야 한다.

또한 응답에는 상위 N개, 또래 판정 상한, 그룹별 절단, VM 응답 바이트 상한이
있다. 각 절단은 대부분 표식되지만, 실제 캡처에서 핵심 근거가 절단되는지는
실행 검증이 필요하다.

# 다음 검증

대표적으로 DB 락, 외부 API, 메시징 또는 Kubernetes 사례를 골라 같은 시간창을
복원한다. 각 데이터셋에서 직접 추출한 기준 행과 도구 응답을 비교해 다음을
기록한다.

`원자료 필드 → 도구 응답 필드 → 값/단위/시간창 보존 → 절단·누락 → 영향`

그 결과에 따라 새 도구, 기존 도구 응답 확장, 의도적 미노출을 구분한다.

# 시나리오 계열별 1차 우선순위

메타데이터의 시나리오 제목을 기준으로 36개 시나리오를 조사 질문이 비슷한
계열로 묶었다. 이는 원인 정답 채점이 아니라 어떤 캡처 원천과 도구 연결을
먼저 검증할지 정하는 분류다.

| 계열 | 대표 시나리오 | 먼저 필요한 데이터 | 현재 정적 상태 | 우선순위 |
|---|---|---|---|---|
| DB lock / blocking | F01, F06, F08, F15-G | `dpm_session_local`, `dpm_topsql_local`, trace·호출 경로 | `db_blocking`·`db_slow_queries`·trace 도구 연결 | 높음 |
| 외부 호출 지연·429 | F01-H, F06-P/R, F08-H, F19 | CLIENT span 속성·duration·오류, 로그 | `breakdown_endpoints`·`sample_logs` 연결; path/error 요약 미사용 가능성 | 높음 |
| 메시지·outbox·consumer | F04, F18, F23 | PRODUCER/CONSUMER span, 토픽·consumer group, 이벤트 | `otel_traces_local`은 사용하지만 사전 계산 path/error 요약은 미확인 | 높음 |
| Kubernetes·OOM·readiness | F05, F09, F12, F15-P/T1, F16, F17, F25 | `kcm_events_local`, process snapshot/meta, K8s 상태, 로그 | K8s 상태·이벤트 도구 일부 연결; process Parquet 직접 경로 미확인 | 높음 |
| 호스트·네트워크·장비 | F05-P, F10, F15-P | `process_snapshot`, `process_meta`, `host_connections`, `syslog_local` | 프로세스는 VM 지표, SNMP는 별도; 캡처 원천 직접 사용 미확인 | 중간 |
| 기능적·무음 실패 | F04-H, F14-P, F17-P, F18-P, F23-R | 이벤트·로그·outbox/ledger 상태·변경 이력 | 수집은 존재하나 도구 결과가 기능적 성공과 실제 처리 완료를 연결하는지 미확인 | 높음 |
| 다중 원인·재발 | F15, F15-R, F15-T1 | 위 계열의 독립 신호와 시간·episode 연결 | 단일 도구 응답의 다중 원인 표현 여부 실행 검증 필요 | 높음 |

이 분류에서 특히 우선할 것은 메시지 계열과 기능적·무음 실패다. 이 계열은
HTTP 성공만으로는 장애를 설명할 수 없고, `PRODUCER → topic → CONSUMER`,
outbox·ledger 처리 상태, 이벤트·로그의 시간 연결이 필요하다. 원자료에는
이를 만들 수 있는 필드가 있으므로, 도구가 실제로 연결하는지 확인할 가치가
크다.

# 컬럼-응답 매핑 1차 판정

아래 판정은 도구 소스의 SQL·응답 구조를 기준으로 했다. `파생·부분`은 원자료
필드가 집계나 제한된 요약으로만 나타나는 경우다.

| 데이터셋 | 중요 컬럼/관계 | 담당 도구 응답 | 판정 | 검토 포인트 |
|---|---|---|---|---|
| `otel_traces_local` | parent/child, service, span kind, duration, status, span attributes | `breakdown_endpoints`, `expand_topology`의 경로·구획·refs | 파생·부분 | topic·consumer group·임의 span attributes가 조사 결과에 충분히 남는지 |
| `trace_error_chains_local` | chain signature/depth/count, exemplar trace | 직접 도구 없음 | 미연결 | 이미 계산된 오류 사슬을 별도 결과로 노출할 필요 |
| `trace_path_signatures_local` | path signature/count, exemplar trace | 직접 도구 없음 | 미연결 | 대규모 raw trace 재집계 대신 경로 후보로 활용 가능 |
| `lucida_logs_local` | body, severity, trace/span, service, k8s·host 속성 | `sample_logs`, `get_data_coverage` | 직접·부분 | 샘플링으로 동일 사건의 대표 로그가 빠지는지 |
| `syslog_local` | device, facility, severity, message, raw | 직접 도구 없음 | 미연결 | 네트워크·장비 시나리오에서 `sample_logs`와 분리 필요 |
| `lucida_events_local` | detector, episode, evidence, transition, severity | `list_events`, `get_data_coverage` | 직접·부분 | episode 전체와 evidence 원문이 절단되지 않는지 |
| `kcm_events_local` | object, reason, event type, count, timestamps | `list_events` | 직접·부분 | Kubernetes 상태 도구와 시간순 상관 연결 필요 |
| `dpm_session_local` | engine, sql_id/hash, severity, body, timestamp | `db_blocking`, `db_slow_queries` | 직접·부분 | blocking 관계·SQL text·세션 시점이 같은 finding에 연결되는지 |
| `dpm_topsql_local` | engine, sql_id/hash, body, timestamp | `db_slow_queries` | 직접·부분 | TopSQL이 ranking만 남기고 SQL 본문·차원을 잃지 않는지 |
| `process_snapshot` | pid/ppid, name, state, cpu, RSS, threads | 직접 도구 없음; `get_processes`는 VM 경유 | 미연결 후보 | 캡처 시점의 프로세스 상태를 재현하는 경로 필요 |
| `process_meta` | pid, name, user, cmdline | 직접 도구 없음 | 미연결 | CPU 상위 결과와 명령행·소유자를 결합할 수 있는지 |
| `host_connections` | local/remote addr·port, process, direction, state | 직접 도구 없음; topology는 trace 중심 | 미연결 후보 | 계측 밖 의존·소켓 장애에서 실제 연결 근거가 사라짐 |
| `event_cluster_projection_local` | cluster, desired version, assigned_at | 직접 도구 없음 | 미연결 후보 | 배포·클러스터 할당 시나리오에서 변경 원인 확인 필요 |

## 우선순위가 높은 결함 후보

1. **오류·경로 사전 계산 데이터 미노출** — `trace_error_chains_local`과
   `trace_path_signatures_local`은 응답 크기를 줄이면서 대표 trace까지 제공하는
   조사 친화적 데이터인데, 현재 공개 도구에서 직접 사용되지 않는다. 다만
   `expand_topology`는 raw trace에서 PRODUCER→CONSUMER 및 caller-only 경로를
   파생해 반환하므로 메시징 계보 자체는 미연결이 아니라 **대체 구현**이다.
2. **프로세스 스냅샷과 메타 결합 부재** — 캡처에는 PID·CPU·RSS·명령행·사용자가
   모두 있지만, 현재 프로세스 도구는 VM 지표 중심이다. 과거 캡처 시점의 프로세스
   정체를 재현하지 못할 수 있다.
3. **syslog와 소켓 관측의 분리** — 네트워크·장비 장애에서 원문 syslog와
   `host_connections`가 모두 존재하지만, 현재 도구 조합만으로 이 둘을 같은
   finding에 묶을 수 있는지 불분명하다.
4. **메시징 속성의 응답 손실 가능성** — 라이브 `expand_topology`에서
   `span_kinds=PRODUCER→CONSUMER`와 소비 시간 주의사항은 확인했다. 다만 topic·
   consumer group 원문 속성은 응답에 나타나지 않아, 계보는 보존되지만 운영자가
   topic 단위로 드릴다운하는 기능은 별도 검토가 필요하다.
5. **기능적 성공과 처리 완료의 연결 부족** — HTTP 200, outbox·ledger·KCM 이벤트가
   서로 다른 원천에 있는데, 현재 도구가 “요청 성공”과 “후속 처리 완료”를 한 응답에서
   비교하는 계약은 확인되지 않는다.

## 확정 전 필요한 검증

위 후보는 정적 근거에 따른 우선순위다. 실제 결함으로 확정하려면 대표 캡처에서
원자료 기준 행을 뽑아 도구 결과와 비교해야 한다. 비교 기준은 값 자체뿐 아니라
시간창, 단위, 대상 식별자, exemplar/ref 재현성, 절단 표식까지 포함한다.

# 데이터 노출 축

시나리오에 직접 등장하지 않는 데이터도 놓치지 않기 위해 별도의
`Data Exposure Matrix`를 둔다. 아래 표는 현재 캡처 스키마와 도구 계약을
기준으로, 데이터가 도구에서 탐색 가능한 정도를 평가한 것이다.

| 데이터 계열 | 캡처 정보 | 탐색 표면 | 상태 | 일반 조사 가치 |
|---|---|---|---|---|
| 메트릭 | 대상·지표·값·차원·기준선 | `scan_metrics`, `read_timeseries`, `compare_peers` | 직접 노출 | 매우 높음 |
| 로그 | 본문·severity·trace/span·서비스/호스트 속성 | `sample_logs` | 직접·샘플링 | 매우 높음 |
| 감지/운영 이벤트 | episode·evidence·전이·K8s 이벤트 | `list_events` | 직접·합성 | 매우 높음 |
| 트레이스 원본 | parent/child·kind·duration·attributes | `breakdown_endpoints`, `expand_topology` | 파생·부분 | 매우 높음 |
| DB 세션/TopSQL | 세션·blocking·SQL key·본문·엔진 | `db_blocking`, `db_slow_queries` | 직접·집계 | 매우 높음 |
| 대상/토폴로지 | 정체·소속·관계·변경 | `describe_target`, `expand_topology`, `list_changes` | 직접·파생 | 매우 높음 |
| 프로세스 스냅샷 | PID·PPID·CPU·RSS·상태·명령행 | `get_processes`(VM 경유) | 부분·과거 스냅샷 미연결 | 높음 |
| 소켓 연결 | local/remote 주소·포트·프로세스·상태 | 직접 표면 미확인 | 미연결 후보 | 높음 |
| syslog | 장비·facility·severity·원문 | 직접 표면 미확인 | 미연결 후보 | 높음 |
| trace 사전 계산 요약 | 오류 사슬·경로 signature·exemplar | 직접 표면 미확인 | 미연결 후보 | 높음 |
| 클러스터 projection | cluster·desired version·할당시각 | 직접 표면 미확인 | 미연결 후보 | 중간 |

이 표는 시나리오에 사용됐는지와 관계없이, 수집된 데이터가 조사 표면에
노출되는지를 평가한다.

# 교차 판정과 우선순위

두 매트릭스를 교차해 다음 우선순위를 사용한다.

| 우선순위 | 조건 | 현재 후보 |
|---|---|---|
| P0 | 여러 시나리오 계열에 필요하고 데이터는 있으나 도구에서 미연결 | trace error/path summary, process snapshot/meta, host connections |
| P1 | 특정 계열의 핵심 증거이며 현재 응답에서 일부만 보존 | messaging attributes, syslog, DB SQL·세션 연결 |
| P1 | 결측 원인이 모호해 잘못된 배제 가능성이 있음 | `get_processes`의 `no_data_reason=unknown` |
| P2 | 현재 시나리오에서는 영향이 작지만 미래 조사 확장성이 있음 | event cluster projection, 추가 syslog 탐색 |

우선순위는 “시나리오에 몇 번 등장했는가”만으로 정하지 않는다. 다음 네 값을
함께 본다.

`데이터 존재성 × 일반 조사 가치 × 미노출 정도 × 잘못된 배제 위험`

이 기준으로 P0 후보를 먼저 확인하고, 실제 사용 예가 확인되면 기존 도구 응답
확장과 새 도구 추가 중 비용이 낮은 쪽을 선택한다.

# 6단계 산출물 상태

1. 전체 데이터셋·주요 컬럼 인벤토리: 완료
2. 데이터별 조회 가능성 매핑: 완료(정적 코드 기준)
3. 응답 손실·필터 한계 확인: 부분 완료(라이브 응답 일부 확인)
4. 시나리오별 필수 데이터와 교차: 완료(계열 수준)
5. 시나리오 밖 일반 조사 기능 검토: 완료(데이터 노출 축 추가)
6. 도구 추가·응답 확장 우선순위: 완료(P0~P2 초안)

남은 일은 매핑 자체가 아니라 P0/P1 후보의 대표 데이터셋에 대해 실제 원자료와
도구 결과를 나란히 비교해 확정하는 것이다.

# 실행 검증 상태 (2026-09-08)

대표 `case-f01-h-v3-48ec8e62` 복원을 시도했다. 복원 스크립트 자체는 실행됐지만,
공유 환경에서 `38123`, `38428`, `45432` 포트가 이미 사용 중이라 격리 DB를
기동할 수 없었다. 기존 컨테이너는 건드리지 않고 시도한 컨테이너와 네트워크는
정리했다. 따라서 이 문서의 현재 결과는 정적 매핑까지이며, 실제 도구 응답과
원자료의 값 대조는 격리 포트가 확보된 실행에서 이어야 한다.

`192.168.230.118`의 Kubernetes `polestar` 네임스페이스에서 PostgreSQL,
ClickHouse, VictoriaMetrics 서비스가 실행 중임을 확인했고, 포트포워딩 후
현재 체크아웃의 `cmd/tool`에 연결했다. `list`, `get_data_coverage`,
`list_events` 호출은 성공했다. 이 실행은 라이브 클러스터 데이터에 대한
연결·응답 봉투 검증이며 `/data/eval-cases`의 특정 캡처를 복원한 결과가
아니다. 따라서 도구 연결성은 확인했지만 시나리오별 값 보존 판정은 캡처
시간창에 맞춘 별도 실행이 필요하다.

P1 실행 검증에서 `db_slow_queries`는 PostgreSQL 59개 폴을 집계하고 32개 SQL 중
3개를 반환했다. 각 finding에 엔진, SQL key, SQL 본문, 단위(ms), 기준선, IO,
실행 횟수, ref가 포함되어 DB 도구는 원자료 보존성이 양호했다. `db_blocking`도
59개 폴 전체를 확인하고 0건을 `verified` 배제 근거로 반환했다.

반면 `get_k8s_state`는 실제 Kubernetes 대상에서 `no_data_reason=unknown`을
반환했다. `get_processes`와 같은 패턴으로, KCM 수집 부재·해당 창의 관측 없음·
지표 미등록을 응답만으로 구분하지 못한다. 따라서 결측 사유 분류는 P1 공통
응답 개선 항목으로 확정한다.

# P1 개선 backlog와 검증 기준

## P1-1 결측 사유 세분화

대상: `get_processes`, `get_k8s_state` 및 같은 VM/KCM 계열 도구.

현재 문제는 `unknown` 하나로 다음 상태를 모두 표현한다는 것이다.

- 수집기 미등록 또는 비활성
- 수집기는 정상이나 창 내 관측 0
- 지표/라벨 미등록
- 백엔드 조회 실패
- 대상 유형상 해당 원천을 조회하지 않음

기대 응답은 최소한 `not_collected`, `zero_observations`, `metric_unregistered`,
`backend_error`, `not_applicable`을 구분하고, 각 분류에 `next_step`을 붙이는
것이다. 검증은 동일 대상에서 시간창과 대상 유형을 바꿔 각 분류가 결정론적으로
나오는지 확인한다.

## P1-2 메시징 원문 드릴다운

대상: `expand_topology`, `breakdown_endpoints`.

현재 계보와 producer/consumer 방향은 제공되지만 topic·consumer group 원문이
응답에 없다. `otel_traces_local`의 messaging 관련 속성이 존재할 때 edge 또는
finding에 `topic`, `consumer_group`, `producer_service`, `consumer_service`를
보존해야 한다. 속성이 없을 때는 빈 문자열 대신 `unavailable`을 사용한다.

검증은 F04 계열 캡처에서 raw trace의 속성과 응답의 동일 필드를 대조한다.

## P1-3 프로세스 스냅샷 경로

대상: `get_processes`.

현재 VM 실시간 지표 중심이라 Parquet의 `process_snapshot`·`process_meta`에
있는 과거 PID, PPID, 명령행, 사용자, 생성시각을 직접 재현하지 못할 수 있다.
기존 VM 결과를 유지하되 캡처·재현 모드에서 snapshot을 선택할 수 있는지 검토한다.

검증은 CPU/RSS 상위 결과의 PID와 snapshot/meta의 PID·명령행·사용자가 일치하는지,
시간창 밖 프로세스가 섞이지 않는지를 확인한다.

## P1-4 사전 계산 trace 요약 사용 여부

대상: `trace_error_chains_local`, `trace_path_signatures_local`.

현재 raw trace 파생 결과와 사전 계산 테이블의 중복·차이를 측정한다. 사전 계산
요약이 더 빠르거나 exemplar ref를 안정적으로 제공하면 topology/breakdown의
보조 입력으로 승격한다. raw 파생과 의미가 다르면 두 결과의 출처를 응답에
분리해서 표시한다.

검증은 같은 서비스·시간창에서 경로 수, 오류 사슬 수, exemplar trace 일치율,
조회 시간, 절단 여부를 비교한다.

# 원자료 추가 확인

F04 캡처 두 건의 `otel_traces_local`을 직접 스캔했다. 두 케이스 모두
`PRODUCER`·`CONSUMER` span이 존재했고, 메시징 관련 속성 문자열을 가진 행도
각각 107,864건과 104,742건이었다. 따라서 topic·consumer group 정보를
원자료에서 사용할 수 있다는 점은 확인됐다. 현재 `expand_topology`가
producer→consumer 방향과 계보를 반환하므로, 남은 문제는 계보 부재가 아니라
**topic·consumer group 원문을 최종 finding에 보존하지 않는 응답 손실**이다.

프로세스 데이터도 F05-P와 F05-R 캡처에서 `target_id + proc_key` 결합을
확인했다. F05-P는 snapshot 380,156행·meta 22,728행에서 4,633개의 공통
프로세스 키가 있었고, F05-R은 snapshot 387,868행·meta 22,964행에서
4,865개의 공통 키가 있었다. 즉 PID·CPU/RSS/상태와 명령행·사용자 정보를
과거 캡처 안에서 결합할 수 있다. 현재 `get_processes`가 VM 시계열만 사용해
이 결합을 반환하지 않는 것은 실제 데이터 노출 공백으로 P1을 유지한다.

`syslog_local`과 `host_connections`도 F05-P·F10-H·F15-P 캡처에서 직접 확인했다.
syslog는 케이스별 약 70,000행이며 hostname·app_name·severity·원문 message가
있지만 샘플 행의 `receiver_target_id`·`device_target_id`가 비어 있었다. 따라서
현재 target UUID 중심 도구로는 syslog를 대상에 안전하게 귀속하기 어렵고,
host/시간/앱 기준 검색 표면이 필요하다. 반대로 `host_connections`는 케이스별
약 43,000~56,000행이고 target_id, host, remote 주소·포트, 방향, 프로세스,
상태가 채워져 있어 소켓 데이터 자체는 대상 귀속이 가능하다. 다만 현재
`expand_topology`는 이 원천을 앱 소유권에 사용할 수 없다고 명시하므로,
소켓을 직접 조회하는 별도 진단 표면이 필요한지 시나리오 검증 대상으로 남긴다.

마지막으로 사전 계산 요약과 cluster projection을 확인했다. F04-H의
`trace_error_chains_local`은 오류 사슬 10행, `trace_path_signatures_local`은
경로 369행이며, 각 행에 chain/path signature, count, exemplar trace ID가
있었다. F04-R도 각각 10행·369행이었다. 이는 대량 raw trace를 다시 접지 않고
오류·경로 후보와 재현용 trace를 제공할 수 있는 완성된 조사 원천이다. 현재
도구가 raw trace 파생으로 일부 기능을 대체하더라도, 이 테이블을 직접 조회할
수 있는 경량 드릴다운은 P1 후보로 유지한다.

`event_cluster_projection_local`도 F04-H 6,314행, F08-H 6,769행, F15-G
6,568행으로 event·cluster·desired_version·assigned_at이 채워져 있었다.
배포·클러스터 할당 변화를 확인할 때 유용하지만, 현재 `list_changes`와
`list_events`가 이 projection을 직접 사용한다는 근거는 없어 P2로 확정한다.

# 구현 진행

모델이 시나리오에 없는 데이터도 발견할 수 있도록 `describe_data_sources`를
공개 Toolset에 추가했다. 이 정적 카탈로그 도구는 8개 조사 도메인과 캡처
데이터셋, 주요 필드, 시간·대상·관계 축, 담당 도구, 제한사항을 구조화해
반환한다. 백엔드가 없어도 동작하며 `catalog:captured-data:v1` ref를 제공한다.
`tools/degrade_test.go`의 backend 표면 일관성에도 추가했고 전체 `go test ./...`
가 통과했다.

추가로 라이브 대상 `commerce-pricing`에 `breakdown_endpoints`를 호출했다.
entry 2·step 4·egress 3행을 모두 반환했고 `spans_total=1232`,
`spans_unique=1232`, 각 행의 trace ref와 `observed_range`를 제공했다. 응답은
구획별 절단 없이 반환됐지만, `limits`에 ORM/JDBC 중복과 section 합산 금지,
CLIENT 목적지 복원 한계를 명시했다. 같은 시간창의 `get_processes`는
`no_data_reason=unknown`을 반환했는데, 이는 프로세스 지표 부재와 수집기
문제를 구분하지 못한 상태로 `get_data_coverage`를 다음 단계로 안내했다.

이후 `get_processes`와 `get_k8s_state`의 빈 결과 분기를 구현으로 보강했다.
대상 메트릭 이름을 추가 확인해 전체 미수집은 `not_collected`, 대상 메트릭만
없으면 `metric_unregistered`, 해당 메트릭이 있으나 창에 값이 없으면
`zero_observations`로 분류하고, 보조 조회 자체가 실패하면 `unknown`을
유지한다. 기존 호환 테스트와 전체 테스트가 통과했다.

`search_host_data`도 공개 Toolset에 추가했다. `syslog` 모드는 target UUID가
비어도 host·app·본문·시간창으로 검색하고, `connections` 모드는 target·host·
remote·process·state·시간창으로 소켓 관측을 검색한다. 두 원천 모두 limit·
truncated·refs·observed_range를 반환해 합성 RCA 궤적의 근거로 사용할 수 있다.

`get_cluster_projections`를 추가해 `event_cluster_projection_local`도 직접
조회할 수 있게 했다. 이제 캡처 13개 데이터셋 모두가 카탈로그와 최소 하나의
읽기 도구에 연결된다. 새 도구들은 공통 봉투, 시간창, limit/truncated,
scope, refs를 사용한다.

## 최종 런타임 검증

`192.168.230.118`의 `polestar` 서비스에 포트포워딩해 새 도구를 실제 호출했다.
`describe_data_sources`, `search_host_data(connections)`,
`get_cluster_projections`, `get_process_snapshot`, `get_trace_summaries(path)`가
모두 정상 응답했다. 첫 trace summary 호출은 path 테이블에 없는
`chain_depth`를 공통 SELECT에 넣어 `http_4xx`가 났고, 라이브 스키마를
`DESCRIBE TABLE`로 확인해 error_chain에서만 선택하도록 수정한 뒤 재호출이
성공했다. 전체 `go test ./...`도 다시 통과했다.

추가로 `get_process_snapshot`을 공개 Toolset에 구현했다. 이 도구는 캡처된
`process_snapshot`과 `process_meta`를 `target_id + proc_key`로 결합해 PID·PPID·
CPU·RSS·상태·threads·user·cmdline·create_time을 반환한다. 라이브 VM 지표를
조회하는 `get_processes`와 분리해 과거 캡처 재현과 합성 궤적 생성에 사용할 수
있도록 했다. CH backend 의존성과 degrade 표면을 등록했고 전체 테스트가
통과했다.
