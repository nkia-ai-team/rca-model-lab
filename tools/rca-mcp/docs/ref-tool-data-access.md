---
title: 도구 계약의 데이터 원천 매핑
status: Active
owner: project
last_reviewed: 2026-07-15
tags:
  - rca
  - agent
  - tools
  - lucida-next
summary: 도구 계약 13종+topology가 칠 저장소·테이블·식별 체계의 지도 — 스키마(DDL·수집 참조) 기준. 조회 설계(무엇을 어떻게 가져올지)는 우리 도구 계약에서 도출하며 lucida-next의 기존 조회 코드는 참조하지 않는다.
---

# 도구 계약의 데이터 원천 매핑

[도구 계약](spec-agent-tools.md) §7 열린 결정 "도구 구현의 데이터
소스 매핑"의 답이다. 스키마 상세는
[데이터 수집 참조](ref-lucida-next-data-collection.md)를 본다.

## 1. 참조 범위 원칙

이 문서가 기록하는 것은 **저장소의 구조**다 — 어느 데이터가 어느
저장소·테이블에 어떤 식별자로 있는가. 이것은 DDL·아키텍처 문서로
검증 가능한 물리적 사실이고, 어느 조사자든 피할 수 없는 의존이다.

**기록하지 않는 것: lucida-next 기존 RCA 코드의 조회 방식.** 기존
코드에는 "로그는 WARN 이상만", "트레이스는 1초 초과만" 같은 조사
취향이 스며 있다 — 그것을 복제하면 우리 agent가 기존 RCA의 시야를
물려받는다. 무엇을 얼마나 어떤 필터로 가져올지(쿼리 설계)는 우리
도구 계약의 원칙(응답 봉투, 요약+참조, 없음 3구분)에서 처음부터
도출한다. (구조 설계의 "lucida-next RCA 파이프라인 불참조" 원칙을
데이터 접근 코드까지 확장 — 2026-07-15 확정.)

출처가 코드 관찰뿐이어서 스키마·실 데이터로 **재검증이 필요한**
항목은 ⚠︎ 표시를 달았다.

## 2. 저장소와 접속

| 저장소 | 정본 역할 | 접속 |
| --- | --- | --- |
| VictoriaMetrics | 메트릭 시계열 전량 (`target_id` 라벨) | Prometheus 호환 HTTP API (`/api/v1/query_range` 등), 기본 `http://victoriametrics:8428`, 무인증 |
| ClickHouse | 로그·트레이스·이벤트·DPM·trap·연결 (`target_id` 컬럼) | native 9000, 기본 DB `lucida` |
| PostgreSQL | targets 메타·변경 이력·코호트·인시던트(topology 박제 포함) | DSN 주입 |

Kafka(Redpanda)는 수집 경로 전용 — 조회와 무관. 도구 구현은 세
저장소의 클라이언트를 직접 든다(별도 레포이므로 당연하고,
lucida-next 내부에서도 서비스 간 store 패키지 공유를 막는 경계가
있어 테이블 직독이 관례다).

## 3. 도구별 데이터 원천

조회 설계(필터·요약·상위 N 기준)는 전부 우리 몫이다. 아래는 "어디에
있는가"만이다.

| 우리 도구 | 저장소 | 원천 테이블/라벨 | 비고 |
| --- | --- | --- | --- |
| `discover_signals` | PG+VM | PG `metric_definitions`(metric_key·labels·unit·value_type·delta_op = 의미 카탈로그) + VM 시계열 존재·기준선 대비 스캔 | 신설(계약 §3 메타 도구) |
| `get_data_coverage` | PG+VM/CH | PG `collectors`(상태·오류)·`collection_policies` + 저장소별 실제 관측 범위 대조 | 신설 — no_data_reason 판별 재료 |
| ~~`get_runtime_connections`~~ | CH | `host_connections` | **폐지(2026-07-30, 재설계 §12.4)** — 앱 프로세스 0건이라 "계측 밖 의존"을 못 냈다. 몫은 `expand_topology`의 `apm_client_peer`가 승계 |
| `get_target_meta` | PG | `targets` (id, type, name, display_name, meta) | type enum은 §4 |
| `get_metric_series` | VM | `{__name__="<metric>", target_id="<uuid>"}` | 지표별 산식 주의 §5-1 |
| `get_metric_dimensions` | VM | 동일 시계열의 차원 라벨: `process_executable_name`·`process_pid`·`snmp_index`·`pod_name`·`interface` 등 | 라벨 allowlist는 수집 참조 문서 |
| `get_events` | CH | `lucida_events_local` (occurred_at, detector, event_kind, severity, target_id, domain, attributes) | 알람 이력은 별도로 PG `alert_history` ⚠︎ |
| `list_changes` | PG | `policy_deployments` + `collectors` + `change_history` 세 테이블 — **같은 사건을 다른 단위로 중복 기록**하므로 사건 단위 교차 접기(spec-tool-redesign §10.5) | `targets.updated_at`은 변경 신호 아님 §5-3. `policy_deployments`는 이력이 아니라 **현재 상태 표** — 과거 부착 상태 복원 불가 §10.2-3 |
| `get_logs` | CH | `lucida_logs_local` (severity_text/number, body, service_name, target_id, log_attributes) | 정본 테이블 주의 §5-2 |
| `compare_peers` | PG+VM | 또래 집합: `asset_tree_members`(kind='service') ∩ 같은 `targets.type`, 그룹 **소속이 없을 때만** `targets.type` 전체 fallback(약함) → 또래 전원의 VM 시계열을 현재·기준선 두 창으로 range 조회 | 판정은 각자 자기 기준선 대비 이탈(spec-tool-redesign §11.2). `targets.status`로 또래를 거르지 않음 — 생존자 편향 §11.4 |
| `get_trace_breakdown` | CH | `otel_traces_local` (trace_id, span_id, parent_span_id, service_name, duration_ns, span_attributes) | 구간 분해는 span 트리(parent_span_id)로 직접 계산 |
| `get_slow_endpoints` | CH | `otel_traces_local`의 `span_name` 축 (보조: `agg_service_golden_signals` 분 단위 집계) | |
| `db_blocking` | CH | `dpm_session_local` (engine, body JSON, `log_attributes['is_blocking']`) | **engine 8종 계약(생산자 소스 정본, 2026-07-30 — 스펙 §13.1)**: 막힘 키 PG `blockingPid` / Oracle·Tibero `blockingSession` / MySQL·MariaDB·CUBRID `blockingTid` / MSSQL `blockingSid`. sentinel은 CUBRID만 `-1`이나 세션 id가 양수라 판별식 `> 0`이 둘을 흡수. 세션 id는 `pid`/`sid`/`tid`. 공통 축 `is_blocking`은 "내가 남을 막는다"로 막힘 키와 뜻이 달라 루트를 직접 표시. **ClickHouse는 공통 축·막힘 키 모두 없음(판단 불가)**. `blockingPid`는 `pg_blocking_pids()` 배열 첫 원소만 보존 — 짝은 구조적으로 불완전. `relNames`(경합 relation)는 PG만 |
| `db_slow_queries`(§14) | CH+PG | `dpm_topsql_local` (sql_id, sql_hash, body, attributes) + SQL 본문 사전 PG `db_sql_text` (resource_id, sql_key) | |
| `get_processes` | VM+PG | VM `sms.process.*`/`process.*` (process_* 라벨) + PG `collector_sms_process_snapshot` | PG 스냅샷은 최신 상태뿐(시계열 아님) |
| `get_snmp_traps` | CH | `snmp_traps_local` (received_at, trap_oid, trap_type, varbinds, if_index) | 화면용은 `lucida_logs_local`에 통합되지만 원본은 이 테이블 |
| `get_k8s_state` | VM+PG | pod 상태 = VM `kcm.pod.*` 38종 (`container_state_running/waiting/terminated`, `container_crash_loop_back_off`, `container_oom_killed`, `container_restart_count` 등; cluster/node/deployment/namespace/service/workload 계층도 `kcm.*`에 존재) + KCM 리소스 계층 PG `kcm_resource_targets` | 레드팀이 발견한 원천 상충을 실환경(AP 119)에서 해소(2026-07-15): CH에 `otel_metrics_gauge_local` 없음(UNKNOWN_TABLE) — 코드 관찰이 틀렸고 수집 참조 문서("CH에 메트릭 테이블 없음, VM 저장")가 맞음 |
| `expand_topology` | CH+PG | CH `otel_traces_local`(앱↔앱·앱→DB·고아 CLIENT) + PG `kcm_resources`(파드→노드) + `network_fdb_hosts`/`network_neighbors` | **`get_topology` 자리 교체(§12)** — 박제가 아니라 원천 즉석 조립. 박제(`incidents.topology`)는 seed 브리핑 몫으로 남음 |

1차 유보 도구 재료(계약 §6): WPM=`wpm_*` 테이블,
netflow=`netflow_records_local`, DB plan/config=`db_sql_plan`·
`collector_dpm_pg_config`, 알람 생명주기=PG `alert_history` ⚠︎.
(`host_connections`는 2026-07-15 `get_runtime_connections`로 승격했다가 2026-07-30 폐지 — 실측상 앱 프로세스 0건.)

## 4. 식별 체계 — 인자 검증의 전제

- **target_id는 순수 UUID다.** PG `targets.id`, VM `target_id` 라벨,
  CH `target_id` 컬럼 모두 동일. 접두 규칙 없음 — 프롬프트 점검
  (spike)의 `svc:…` 표기는 mock 전용이었다.
- **접두가 있는 곳은 topology 노드 ID뿐**: `target:<uuid>` /
  `service:<name>` / `host:<name>` / `db:<engine>` (인시던트 박제
  topology JSON에서 확인 가능). target 없는 가짜 노드(`db:oracle`)의
  실제 대상 해소는 도구 구현의 몫(예: DPM 테이블에서 engine으로
  역조회).
- **호스트 귀속**: 하위 리소스 target은
  `targets.meta->>'host_target_id'`로 부모 서버에 귀속된다. 이벤트류
  조회는 `target_id = X OR host_target_id = X` 패턴이 필요 ⚠︎ (귀속
  분포는 실 데이터로 확인).
- targets.type 실측 enum: 루트 = `server` `application` `database`
  `network` `kubernetes` `web_service`, 하위·특수 = `server_resource`
  `network_resource` `kubernetes_resource` `virtualization_resource`
  `monitor_group` 등.

## 5. 데이터의 물리적 성질 (조회 설계와 무관하게 참인 것)

1. **지표 저장 형태**: `system.cpu.utilization`은 state 차원(idle/
   user/…)으로 저장된다 — 전체 avg는 무의미하고 idle 제외 산식이
   필요. memory도 state 차원, disk/network io는 저장 단계에서 이미
   델타(rate 재적용 = 이중미분). 정본 확인처는 PG 메트릭 카탈로그
   (`metric_definitions`: labels, value_type, delta_op).
2. **로그 정본은 `lucida_logs_local`.** `otel_logs_local`은 컷오버
   (ADR-016) 후 적재 중단 — 읽으면 0행.
3. **`targets.updated_at`은 변경 이력이 아니다** ⚠︎ — 상태 갱신·
   reconcile로도 bump된다(코드 관찰, 실 데이터로 재검증). 변경
   이력은 §3의 세 테이블이 원천. **`collectors.updated_at`도 같은
   함정이다**(실측 2026-07-15, AP 119): 매 수집 폴마다
   `last_collected_at`과 동일 시각으로 bump — 수집기의 변경 신호는
   `created_at`(등록)뿐이고, 설정 변경은 `change_history`(카테고리
   '수집기')가 담당한다.
3b. **`change_history`의 대상 귀속은 이름 문자열뿐이다**(실측
   2026-07-15): target_id 컬럼이 없고 `detail`은 대부분 `{}` —
   `name` 컬럼(대상의 name/display_name/address와 일치)으로 잇는다.
   이름 변경·삭제된 대상은 귀속이 끊길 수 있다. `policy_deployments`
   는 `target_ref`가 대상 UUID(현재 `target_kind='target'`뿐)라 정확.
4. **트레이스 status 값은 short-form** (`'ERROR'`/`'OK'`/`'UNSET'`) ⚠︎
   — 실 데이터 샘플로 확정.
5. **TTL 창**: 트레이스 3일, 로그 7일, VM raw 7일 (보관정책 문서).
   오래된 인시던트 재조사는 원본이 없다 — 평가의 캡처/재생
   ([spec-eval-data-capture](spec-eval-data-capture.md))이 이 문제를
   푸는 축.
6. **VM 쿼리 제약** ⚠︎: 서로 다른 시계열 집합의 이항 연산이
   "duplicate time series" 오류를 낼 수 있어 분리 조회 후 조립이
   안전하다(코드 관찰 — 실험으로 재검증).

## 6. ref 포맷 열린 결정에 주는 재료

도구 계약 §7의 "ref 포맷"에 대해, 원천별 자연 키는:

- VM: 쿼리 재현 파라미터 (`__name__` + `target_id` + window + step)
- CH: 테이블 + 정렬 키 (예: `otel_traces_local`의 trace_id/span_id,
  `dpm_session_local`의 timestamp+target_id)
- PG: 테이블 + PK (예: `change_history.id`, `db_sql_text.sql_key`)

캡처/재생 평가에서도 같은 키로 원본을 찾을 수 있어야 하므로, ref
포맷 확정은 캡처 스펙의 저장 키와 함께 정한다.
