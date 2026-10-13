# RCA 하네스 구조

2026-10-13 소스 기준. [HTML 다이어그램](rca-harness-architecture.html) · [시작하기](../README.md)

## 조사 흐름

```text
관측 알람 + 시간 범위 → seed
  → Python student 조사 루프 ↔ 모델 API / Codex 연결
  → MCP stdio → Go 관측 도구 23종 → PG / CH / VM
  → 관측 응답·근거 refs → 다음 조사 또는 submit_rca
  → trajectory / result / score → 기전 채점·보고서·학습 데이터
```

정본은 `src/rca_lab/mcp/student.py`, `tools/rca-mcp/tools/registry.go`입니다.
`submit_rca`와 structured 정책의 `read_observation`은 Python 하네스 도구로, Go 23종 수에 포함하지 않습니다.
모델 공급자는 실험별로 선택합니다. Muse 전용 문법 강제는 모든 모델에 적용하는 공통 규칙이 아닙니다.

## 데이터와 도구

| 조회 | 도구 | 주 데이터 원천 |
|---|---|---|
| 데이터 종류·대상·관계 | `describe_data_sources`, `search_targets`, `describe_target`, `expand_topology`, `get_data_coverage` | PG 자산·K8s, CH 관측, VM 수집 근거 |
| 지표·기준선·또래 비교 | `scan_metrics`, `read_timeseries`, `compare_peers` | VM + PG 대상 정보 |
| 로그·이벤트·변경 | `sample_logs`, `list_events`, `list_changes`, `search_host_data` | CH + PG, 일부 VM |
| 트레이스 | `get_trace_summaries`, `get_trace_spans`, `get_trace_activity`, `breakdown_endpoints` | CH |
| DB | `db_slow_queries`, `db_blocking` | CH |
| 호스트·K8s | `get_processes`, `get_process_snapshot`, `get_k8s_state`, `get_cluster_projections` | CH + PG |
| 네트워크 | `get_snmp_traps` | CH |

도구 응답은 `status`, `findings`, `refs`, 조회 범위·제약을 담습니다. `no_data`는 장애가 없다는 뜻이 아닙니다.
입력은 알람과 시간 범위로 만들며, 정답 메타데이터는 채점 영역에 둡니다. blind 필터만으로 누출 방지가 완성되는 것은 아닙니다.

<!-- TOOL-CATALOG:START -->
## 도구별 사용법

Go 관측 도구 **23종**, Python 결론 제출 **1종**, structured 정책 전용 원문 조회 **1종**입니다. legacy에서는 `read_observation`을 제공하지 않습니다.

예시의 UUID·시간·근거 참조는 실제 조회에서 얻은 값으로 바꿉니다.

### `describe_data_sources`

어떤 관측 데이터를 조회할 수 있는지 먼저 확인한다.

- 제공 조건: Go 공통
- 주요 입력: 필수 없음. domain 선택: inventory, metrics, logs, events, traces, database, process, network, kubernetes.
- 반환 정보: 데이터셋별 주요 필드, 시간·대상 축, 담당 도구와 제약.
- 구현: [tools/rca-mcp/tools/datasources.go](../tools/rca-mcp/tools/datasources.go)

```json
{
  "domain": "traces"
}
```

### `search_targets`

이름·주소·유형으로 대상 UUID를 찾는다.

- 제공 조건: Go 공통
- 주요 입력: 필수 없음. query: 이름·표시명·UUID·주소의 대소문자 무관 부분 문자열; type: 정확한 대상 유형. limit 기본 50(1~100), offset 기본 0(최대 1,000,000).
- 반환 정보: 연결된 인벤토리 스냅샷의 대상 목록과 다음 페이지 위치. 등록 여부만으로 실행·건강 상태를 판단할 수 없다.
- 구현: [tools/rca-mcp/tools/searchtargets.go](../tools/rca-mcp/tools/searchtargets.go)

```json
{
  "query": "worker",
  "type": "server",
  "limit": 50
}
```

### `get_process_snapshot`

사건 시간창의 프로세스 원본 스냅샷을 조회한다.

- 제공 조건: Go 공통
- 주요 입력: 필수 target, from, to. proc_key: 정확한 UInt64 문자열 또는 pid 필터. sort_by=cpu|memory|threads(기본 cpu), top_n 기본 10·최대 50, offset 기본 0.
- 반환 정보: 프로세스 키로 결합한 CPU/RSS/상태, PID·PPID·사용자·명령행과 페이지 정보.
- 구현: [tools/rca-mcp/tools/processsnapshot.go](../tools/rca-mcp/tools/processsnapshot.go)

```json
{
  "target": "00000000-0000-4000-8000-000000000001",
  "from": "2026-10-13T00:00:00Z",
  "to": "2026-10-13T00:15:00Z"
}
```

### `get_trace_summaries`

저장된 오류 체인·호출 경로 요약에서 대표 trace를 찾는다.

- 제공 조건: Go 공통
- 주요 입력: 필수 kind=error_chain|path, target(정확한 UUID 또는 서비스명), from, to. signature 정확일치 선택; top_n 기본 20(1~100), offset 기본 0(최대 1,000,000).
- 반환 정보: 창과 겹치는 요약, 원래 요약 창의 건수, exemplar_trace_ids와 페이지 정보. 건수는 교집합 시간만의 건수가 아니다.
- 구현: [tools/rca-mcp/tools/tracesummary.go](../tools/rca-mcp/tools/tracesummary.go)

```json
{
  "kind": "error_chain",
  "target": "00000000-0000-4000-8000-000000000001",
  "from": "2026-10-13T00:00:00Z",
  "to": "2026-10-13T00:15:00Z"
}
```

### `get_trace_spans`

대표 trace의 span 원문을 따라 호출·실패 경로를 확인한다.

- 제공 조건: Go 공통
- 주요 입력: 필수 trace_id, from, to. limit 기본 50(1~100), offset 기본 0(최대 1,000,000).
- 반환 정보: 원본 span과 attributes/events/links. duration_ns는 정밀도를 보존한 십진 문자열. 부모 누락은 창 밖·미계측 때문일 수 있다.
- 구현: [tools/rca-mcp/tools/tracespans.go](../tools/rca-mcp/tools/tracespans.go)

```json
{
  "trace_id": "0123456789abcdef0123456789abcdef",
  "from": "2026-10-13T00:00:00Z",
  "to": "2026-10-13T00:15:00Z"
}
```

### `get_trace_activity`

직전 같은 길이의 시간창과 처리량을 비교해 사라진 작업·메시지 흐름을 찾는다.

- 제공 조건: Go 공통
- 주요 입력: 필수 target, from, to(시간창 1초~24시간). topic, consumer_group 정확일치 선택; bucket_seconds 1~3600, 창당 최대 240버킷(기본 60초 이상 자동); limit 기본 20(1~50), offset 기본 0.
- 반환 정보: 서비스·span 종류·작업·topic·consumer group별 현재/기준선 버킷과 감소·소멸 관측. 브로커 backlog나 전달 보장을 뜻하지 않는다.
- 구현: [tools/rca-mcp/tools/traceactivity.go](../tools/rca-mcp/tools/traceactivity.go)

```json
{
  "target": "00000000-0000-4000-8000-000000000001",
  "from": "2026-10-13T00:00:00Z",
  "to": "2026-10-13T00:15:00Z"
}
```

### `search_host_data`

호스트 syslog 또는 기록된 소켓 연결을 검색한다.

- 제공 조건: Go 공통
- 주요 입력: 필수 kind=syslog|connections, from, to. 공통 host·query 선택. syslog 전용 app·severity_max(0~7), connections 전용 target·process·state·direction(in|out)·remote·local_port·remote_port(0~65535). limit 기본 50(1~200), offset 기본 0(최대 1,000,000).
- 반환 정보: 원본 필드·내용 refs, 다음 페이지와 일치 건수 하한. 모드에 맞지 않는 필터는 거부한다.
- 구현: [tools/rca-mcp/tools/hostobservations.go](../tools/rca-mcp/tools/hostobservations.go)

```json
{
  "kind": "connections",
  "from": "2026-10-13T00:00:00Z",
  "to": "2026-10-13T00:15:00Z"
}
```

### `get_cluster_projections`

캡처에 기록된 이벤트의 클러스터·desired version 할당을 확인한다.

- 제공 조건: Go 공통
- 주요 입력: 필수 from, to. cluster 정확일치 선택; limit 기본 50·최대 200.
- 반환 정보: event_id, cluster_id, desired_version, assigned_at와 refs; 반환 한도 초과 여부.
- 구현: [tools/rca-mcp/tools/clusterprojection.go](../tools/rca-mcp/tools/clusterprojection.go)

```json
{
  "from": "2026-10-13T00:00:00Z",
  "to": "2026-10-13T00:15:00Z"
}
```

### `scan_metrics`

대상의 모든 지표를 기준선과 비교해 조사할 신호를 찾는다.

- 제공 조건: Go 공통
- 주요 입력: 필수 target, from, to. baseline_from·baseline_to는 함께 선택; 기본 기준선은 인시던트 첫 증상 직전 60분.
- 반환 정보: disappeared(사라짐), appeared(등장), shifted(robust z 이탈), steady(건수)와 후속 조회할 지표명.
- 구현: [tools/rca-mcp/tools/scan.go](../tools/rca-mcp/tools/scan.go)

```json
{
  "target": "00000000-0000-4000-8000-000000000001",
  "from": "2026-10-13T00:00:00Z",
  "to": "2026-10-13T00:15:00Z"
}
```

### `read_timeseries`

지표의 시간적 모양과 대상 간 이상 시작 순서를 비교한다.

- 제공 조건: Go 공통
- 주요 입력: 필수 targets 배열, metrics 배열, from, to. 대상×지표 최대 8조합. group_by 선택 시 지표 1개·라벨 상위 5+other. baseline_from·baseline_to 선택(기본 발단 직전 60분). from/to는 RFC3339 또는 now 계열 상대 표기.
- 반환 정보: 버킷 값, 기준선 대비 onset, 시계열 간 선후 타임라인, 알려진 한도 대비 사용률.
- 구현: [tools/rca-mcp/tools/read.go](../tools/rca-mcp/tools/read.go)

```json
{
  "metrics": [
    "observed_metric_name"
  ],
  "targets": [
    "00000000-0000-4000-8000-000000000001"
  ],
  "from": "2026-10-13T00:00:00Z",
  "to": "2026-10-13T00:15:00Z"
}
```

### `sample_logs`

로그 종류를 먼저 파악하고 특정 종류의 원문을 읽는다.

- 제공 조건: Go 공통
- 주요 입력: 필수 mode=map|grep, target, from, to. map: baseline_from·baseline_to 선택(기본 발단 직전 60분). grep: template_id 또는 query, severity_min 선택, context_lines 0~5, limit 기본 30·최대 100.
- 반환 정보: map: 기준선 미관측·급증·소멸·최빈 종류, 총량 변화와 레벨 분포. grep: 원문과 같은 스트림의 전후 맥락.
- 구현: [tools/rca-mcp/tools/sample.go](../tools/rca-mcp/tools/sample.go)

```json
{
  "mode": "map",
  "target": "00000000-0000-4000-8000-000000000001",
  "from": "2026-10-13T00:00:00Z",
  "to": "2026-10-13T00:15:00Z"
}
```

### `list_events`

이상감지·외부 알람·K8s 이벤트를 시간 순서로 확인한다.

- 제공 조건: Go 공통
- 주요 입력: 필수 target, from, to. 시간은 RFC3339 또는 now/now-15m. K8s 클러스터 UUID는 전체 namespace, 승격 리소스 UUID는 해당 리소스를 조회한다.
- 반환 정보: 이상감지 에피소드의 발단·해소·지속, 외부 알람, Normal/Warning K8s 이벤트와 묶음 요약.
- 구현: [tools/rca-mcp/tools/listevents.go](../tools/rca-mcp/tools/listevents.go)

```json
{
  "target": "00000000-0000-4000-8000-000000000001",
  "from": "2026-10-13T00:00:00Z",
  "to": "2026-10-13T00:15:00Z"
}
```

### `describe_target`

대상 정체·소속·하위 자원·한도를 확인한다.

- 제공 조건: Go 공통
- 주요 입력: 필수 target. from 선택(기본 첫 증상 60분 전), to 선택(기본 now); RFC3339 또는 상대 시간.
- 반환 정보: 유형·이름·주소·상태, 호스트/K8s/서비스그룹 소속, 하위 요약, 지표별 limit와 창 내 변화.
- 구현: [tools/rca-mcp/tools/describe.go](../tools/rca-mcp/tools/describe.go)

```json
{
  "target": "00000000-0000-4000-8000-000000000001"
}
```

### `list_changes`

사건 전후 변경을 전역에서 찾아 배포·구성 변경과 증상을 연결한다.

- 제공 조건: Go 공통
- 주요 입력: 필수 없음. from 기본 발단-24시간, to 기본 인시던트 창 끝(없으면 now). target은 필터가 아닌 정렬 힌트. limit 기본 40(접기 후), include_audit_activity 기본 false.
- 반환 정보: 정책·수집기·구성·자산·계정 변경, K8s rollout 스펙 차이와 scale 전후 수. 자원 limit 변경 시 새 pod 사용량 피크도 제공.
- 구현: [tools/rca-mcp/tools/listchanges.go](../tools/rca-mcp/tools/listchanges.go)

```json
{
  "from": "2026-10-13T00:00:00Z",
  "to": "2026-10-13T00:15:00Z"
}
```

### `compare_peers`

한 대상만 이탈했는지 또래도 함께 이탈했는지 확인한다.

- 제공 조건: Go 공통
- 주요 입력: 필수 target, metric 정확한 이름, from, to. baseline_from·baseline_to 선택(기본 발단 직전 60분).
- 반환 정보: 또래 집합 근거, 각자의 기준선 대비 이탈, 미관측 또래. 지표 값의 크기만으로 좋고 나쁨을 판정하지 않는다.
- 구현: [tools/rca-mcp/tools/comparepeers.go](../tools/rca-mcp/tools/comparepeers.go)

```json
{
  "metric": "observed_metric_name",
  "target": "00000000-0000-4000-8000-000000000001",
  "from": "2026-10-13T00:00:00Z",
  "to": "2026-10-13T00:15:00Z"
}
```

### `expand_topology`

앱의 의존 관계와 서버에 배치된 서비스를 양방향으로 탐색한다.

- 제공 조건: Go 공통
- 주요 입력: 필수 target. from 기본 발단, to 기본 인시던트 끝(없으면 now). hops 기본 2·최대 3(hosted_on은 홉 미소모), limit 기본 400. edge_kinds는 출력만 필터링하며 탐색은 모든 종류를 사용한다.
- 반환 정보: 호출·DB·계측 밖 의존·hosted_on·FDB·LLDP 간선과 관측 근거. 서버→서비스 탐색도 간선 방향은 앱→호스트로 보존. 모호한 pod/node 매핑은 제외·표시한다.
- 구현: [tools/rca-mcp/tools/expandtopology.go](../tools/rca-mcp/tools/expandtopology.go)

```json
{
  "edge_kinds": [
    "hosted_on"
  ],
  "hops": 1,
  "target": "00000000-0000-4000-8000-000000000001",
  "from": "2026-10-13T00:00:00Z",
  "to": "2026-10-13T00:15:00Z"
}
```

### `db_blocking`

DB 잠금 사건과 장시간 세션·클라이언트별 대기 상태를 조사한다.

- 제공 조건: Go 공통
- 주요 입력: 필수 없음. db: 부모 database UUID(생략하면 DB 전체 개관). from/to 기본 인시던트 창. max_events 기본 20.
- 반환 정보: 누가 누구를 막았는지, 루트 세션·경합 객체·시간, 상위 세션 SQL, 클라이언트별 상태·대기 추이. 전체 세션 수 시계열은 read_timeseries로 조회.
- 구현: [tools/rca-mcp/tools/dbblocking.go](../tools/rca-mcp/tools/dbblocking.go)

```json
{
  "from": "2026-10-13T00:00:00Z",
  "to": "2026-10-13T00:15:00Z"
}
```

### `db_slow_queries`

수집된 top-SQL의 지연과 기준선 대비 악화를 확인한다.

- 제공 조건: Go 공통
- 주요 입력: 필수 db. from/to 기본 인시던트 창. top_n 기본 10; sort=total_ms(기본)|ratio_p50|latency_p50|latency_max|execs. full_text_for 최대 3개 sql_key, plan_for 최대 2개. exclude_self_monitoring 기본 false.
- 반환 정보: SQL별 집계·직전 24시간 자기 기준선 비교, SQL 본문·실행계획 존재와 선택된 상세.
- 구현: [tools/rca-mcp/tools/dbslowqueries.go](../tools/rca-mcp/tools/dbslowqueries.go)

```json
{
  "db": "00000000-0000-4000-8000-000000000001",
  "from": "2026-10-13T00:00:00Z",
  "to": "2026-10-13T00:15:00Z"
}
```

### `breakdown_endpoints`

서비스 내부의 어떤 진입·처리·외부 호출 구간이 느리거나 실패하는지 분해한다.

- 제공 조건: Go 공통
- 주요 입력: 필수 target(서비스명 또는 UUID). from/to 선택(기본 인시던트 창). sections=entry|step|egress(기본 모두), top_n 기본 20·최대 50(구획별).
- 반환 정보: HTTP·메시지 소비·스케줄러·프레임워크·나가는 호출의 구획별 지연·실패와 직전 창 비교.
- 구현: [tools/rca-mcp/tools/breakdownendpoints.go](../tools/rca-mcp/tools/breakdownendpoints.go)

```json
{
  "target": "00000000-0000-4000-8000-000000000001",
  "from": "2026-10-13T00:00:00Z",
  "to": "2026-10-13T00:15:00Z"
}
```

### `get_data_coverage`

관측이 없는 이유를 수집 상태와 실제 저장소 건수로 점검한다.

- 제공 조건: Go 공통
- 주요 입력: 필수 targets 배열, from, to.
- 반환 정보: 대상별 수집기 등록·최근 성공/실패와 저장소별 실제 관측 수. 현재 수집 상태가 사건 창의 연속 수집을 보증하지 않으며 0건은 원인 배제 근거가 아니다.
- 구현: [tools/rca-mcp/tools/meta.go](../tools/rca-mcp/tools/meta.go)

```json
{
  "targets": [
    "00000000-0000-4000-8000-000000000001"
  ],
  "from": "2026-10-13T00:00:00Z",
  "to": "2026-10-13T00:15:00Z"
}
```

### `get_processes`

호스트 지표에서 리소스를 많이 쓰는 프로세스를 찾는다.

- 제공 조건: Go 공통
- 주요 입력: 필수 host(server UUID), from, to. sort_by=cpu|memory|io_read|io_write(기본 cpu), top_n 기본 5.
- 반환 정보: VictoriaMetrics에서 읽은 프로세스명·PID별 상위 리소스 사용량. 모든 프로세스 인벤토리는 아니다.
- 구현: [tools/rca-mcp/tools/server_network.go](../tools/rca-mcp/tools/server_network.go)

```json
{
  "host": "00000000-0000-4000-8000-000000000001",
  "from": "2026-10-13T00:00:00Z",
  "to": "2026-10-13T00:15:00Z"
}
```

### `get_snmp_traps`

네트워크 장비가 보낸 직접적인 trap 신호를 확인한다.

- 제공 조건: Go 공통
- 주요 입력: 필수 device(network UUID), from, to.
- 반환 정보: 시간창의 linkDown 등 SNMP trap과 인용 가능한 관측.
- 구현: [tools/rca-mcp/tools/server_network.go](../tools/rca-mcp/tools/server_network.go)

```json
{
  "device": "00000000-0000-4000-8000-000000000001",
  "from": "2026-10-13T00:00:00Z",
  "to": "2026-10-13T00:15:00Z"
}
```

### `get_k8s_state`

클러스터 또는 승격 리소스의 pod 이상 상태를 확인한다.

- 제공 조건: Go 공통
- 주요 입력: 필수 target(K8s 클러스터 또는 승격 리소스 UUID), from, to.
- 반환 정보: 시간창 내 재시작 증가, CrashLoopBackOff, OOMKilled, waiting pod 상태.
- 구현: [tools/rca-mcp/tools/k8s.go](../tools/rca-mcp/tools/k8s.go)

```json
{
  "target": "00000000-0000-4000-8000-000000000001",
  "from": "2026-10-13T00:00:00Z",
  "to": "2026-10-13T00:15:00Z"
}
```

### `submit_rca`

조사를 끝내고 원인 대상·기전·근거를 구조화해 제출한다.

- 제공 조건: Python 공통
- 주요 입력: 필수 status=confirmed|provisional|insufficient, summary, causes 배열. cause마다 target(UUID), mechanism, support_refs 필수. external_causes 선택: id(external: 접두사), kind, name, boundary_target, evidence_refs 필수. 내부+외부 후보 합계 최대 3.
- 반환 정보: 검증된 최종 결과 또는 필드·UUID·근거 등 검증 오류. confirmed/provisional은 원인 후보가 필요하며 refs는 실제 도구 관측을 인용한다.
- 구현: [src/rca_lab/mcp/answer.py](../src/rca_lab/mcp/answer.py)

```json
{
  "status": "insufficient",
  "summary": "현재 관측만으로 원인을 확정하기 어렵다.",
  "causes": []
}
```

### `read_observation`

축약 응답에서 생략된 저장 원문을 필요한 범위만 다시 읽는다.

- 제공 조건: structured only
- 주요 입력: 필수 observation_id. json_pointer 선택(RFC6901, 빈 문자열은 루트); offset 기본 0(문자 단위), limit 기본 4,000·최대 12,000.
- 반환 정보: 선택한 JSON 필드 또는 원문 페이지, offset/next_offset·완료 여부와 출처. 실행별 저장소의 실제 원문만 읽으며 새 백엔드 조회는 하지 않는다.
- 구현: [src/rca_lab/mcp/observations.py](../src/rca_lab/mcp/observations.py)

```json
{
  "observation_id": "obs_example",
  "json_pointer": "/findings/0",
  "offset": 0,
  "limit": 4000
}
```

<!-- TOOL-CATALOG:END -->
## 서버에서 서비스 찾기

1. `search_targets`로 서버 이름/IP를 UUID로 확정합니다.
2. `expand_topology(target=<서버 UUID>, from=…, to=…)`에서 역방향 `hosted_on` 연결로 서비스 후보를 찾습니다. 간선 자체는 기존처럼 앱 → 호스트 방향입니다.
3. 서비스의 추가 의존 관계는 반환된 대상 ID로 후속 조회합니다.
4. 실행 프로세스와 연결 포트는 `get_process_snapshot`·`search_host_data(kind="connections")`에서 확인합니다.

직접 관계는 `targets.meta.host_target_id`가 가리키는 **application → server** 유형을 검증해 연결합니다. 이 경로는 트레이스가 없어도 동작하며 시간 근거는 `current_inventory;not_historical`입니다. CH·파드 조회가 실패해도 직접 확인된 관계는 보존하고 실패한 원천을 degraded로 표시합니다.

추가 관측 관계는 시간창 내 트레이스의 `service`·`host_name`을 PG K8s 파드 이름 → `data.nodeName` → 호스트 `resource_key`와 연결합니다. `lucida.target_id`로 서비스 UUID를 확인하며 미확정 서비스는 `service:<이름>` 노드로 남깁니다. 중복 파드/노드 이름은 모호한 연결을 만들지 않도록 제외합니다. `hosted_on` 연결은 APM hop을 소모하지 않으며 기존 크기 제한·truncated 표기를 유지합니다.

시간 근거는 `span_in_window;pod_placement=current_inventory`입니다. 트레이스는 지정 시간창이지만 파드 배치는 현재 자산 스냅샷이므로 과거 배치까지 보증하지 않습니다.

같은 서버라는 이유만으로 서비스 간 호출 관계를 만들지 않습니다. 수집된 배치 관계와 관측 근거를 구분해야 합니다.
서버 조회는 실시간 SSH 탐색이 아니며 미수집·미등록 서비스까지 완전하게 열거하지 않습니다.

## 컨텍스트 정책

정본: `context.py`, `observations.py`, `student.py`.

| 정책 | 모델에 전달하는 응답 | 이전 응답 | 원문 재조회 |
|---|---|---|---|
| `legacy` | 설정 길이까지 앞부분 절단; 기본 6,000자 | 최근 8개를 제외하면 기본 1,200자까지 절단 | 같은 도구를 좁은 범위로 다시 호출; 별도 원문 reader 없음 |
| `structured-v1` | 전체 응답을 보관하고 크기 제한 JSON 봉투 전달 | 구조를 유지한 축약 | `read_observation` |
| `structured-v2` | 전체 응답을 보관하고 필드·누락 정보를 전달 | 이미 전달된 v2 관측 봉투를 유지 | `read_observation` |

`read_observation`은 현재 조사에 등록된 `observation_id`만 받습니다. 파일 경로를 받아 읽는 도구가 아닙니다.
`json_pointer`로 필드를 고르고 `offset`·`limit`·`next_offset`으로 페이지를 읽습니다.
보관된 “원문”은 **도구가 반환한 전체 응답**이지, 데이터베이스 전체나 도구 내부 limit으로 제외된 행까지 뜻하지 않습니다.
따라서 원문 저장만으로 중요한 증거가 반드시 조회·추론된다는 보장은 없습니다.
이전 `reasoning_content` 제거 등 롤링 문맥 정책은 추론·추론 재작성·학습에서 공유합니다.

## 예산·결론·평가

- `max_turns`, `max_prompt_tokens`, `max_tokens`는 별도 설정입니다. `max_prompt_tokens=0`은 강제 제출을 유발하는 프롬프트 예산만 해제합니다.
- 입력+출력의 물리적 상한은 모델 서버 컨텍스트입니다. 초과 시 처리는 정책별로 다르며 무제한 입력이 되지 않습니다.
- `submit_rca`는 상태·요약·원인 대상·기전·근거 refs를 제출합니다. 관측하지 않은 대상/근거 등 계약 위반은 검증 오류로 반환합니다.
- 원인 대상 채점과 기전 채점을 분리합니다. 기전 정답률 비교에는 채점자·정답 계약도 고정해야 합니다.
- 평가 결과에는 모델/토크나이저 revision, 양자화, thinking, 추측 디코딩, MCP 바이너리 해시, 정책, 예산, 서버 컨텍스트, 반복 수, split을 기록합니다.
- API 시간 기준 tok/s와 도구 포함 전체 풀이 tok/s는 다릅니다. 계측 정의가 다른 수치를 직접 비교하지 않습니다.

## 학습과 제품 연결

교사 풀이 → 근거·기전 채택 → 필요 시 무힌트 추론 재작성 → 학습 데이터 export → SFT 순서입니다.
턴별 학습 예제는 해당 시점까지의 문맥과 현재 assistant 출력을 사용합니다. 턴 단위 손실이 에피소드를 임의로 6턴만 남긴다는 뜻은 아닙니다.
학습·검증·봉인셋은 시나리오 단위로 분리하며, 검증셋으로 선택한 체크포인트를 최종 평가합니다.

Go MCP 도구는 제품 RCA에서도 재사용할 수 있는 경계입니다. 다만 이 저장소의 격리 캡처 복원·평가 루프는 제품 배포 자체가 아닙니다.
제품 연결 시 인증·테넌트 범위·시간 범위·읽기 권한·도구 응답 계약을 제품의 실행 경계에서 보장해야 합니다.

**최신 소스와 실행 바이너리는 별개입니다.** 고정 바이너리를 쓰는 진행 중 평가는 소스 수정으로 바뀌지 않습니다.
새 기능을 적용하려면 빌드·검증 후 새 실행 이름과 바이너리 해시로 시작하고 이전 결과와 구분합니다.
