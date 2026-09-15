---
title: Lucida Next 데이터 수집 참조
status: Active
owner: project
last_reviewed: 2026-07-22
tags:
  - reference
  - lucida-next
  - data-collection
  - schema
summary: lucida-next의 수집 대상, 저장 경로, 스키마, 예시 데이터를 정리한 참조 문서.
---

# Lucida Next 데이터 수집 참조

## 문서 상태

- 대상 저장소: `../lucida-next`
- 기준일: 2026-07-07
- 근거: `../lucida-next`의 README, 아키텍처 문서, 수집기 매핑 CSV, PostgreSQL DDL,
  ClickHouse DDL, collector/ingest 구현 코드
- 제외 범위: RCA, incident/operator, eventcluster, KEDB 판단 로직
- 갱신 조건: `lucida-next`의 DDL, 수집기 매핑, collector 저장 경로, ingest 분기, 보관
  정책이 바뀔 때만 갱신한다.

이 문서는 `lucida-next`가 어떤 데이터를 수집하고, 어떤 식별자로 정규화하며, 어느
저장소에 어떤 스키마로 적재하는지 한 곳에서 판단할 수 있게 만든 참조 문서다. 예시
데이터는 실제 운영 DB 덤프가 아니라 DDL과 코드 경로를 기준으로 구성한 대표 형태다.

> 이 문서는 source capability 참조이며 현재 배포·활성·실관측 상태를 보장하지 않는다.
> RCA 설계에서는 소스, 배포 image, 적용 schema, 실데이터를 교차 검증한
> [Lucida Next RCA 데이터 인벤토리](ref-lucida-next-rca-data-inventory.md)를 함께 사용한다.

## 한 페이지 요약

Lucida Next는 OpenTelemetry 중심의 풀스택 옵저버빌리티 플랫폼이다. 수집 대상은 서버,
애플리케이션, 데이터베이스, 네트워크, Kubernetes, 가상화, 스토리지, 설비, 외부 APM/관제
시스템이다.

저장소 역할은 명확히 분리된다.

| 저장소 | 정본 역할 | 대표 데이터 | 보관 기준 |
|---|---|---|---|
| VictoriaMetrics | 메트릭 시계열 | `sms.*`, `dpm.*`, `kcm.*`, `facility.*`, OTel metrics | vmsingle 전역 7일 |
| ClickHouse | 분석성 로그/트레이스/이벤트/원본 레코드 | OTLP trace/log, syslog, SNMP trap, NetFlow, WPM, DPM session/topSQL | 테이블별 TTL |
| PostgreSQL | 운영/관리 메타데이터 | target, collector, policy, credential, inventory, topology, catalog | 명시 TTL 없음 |
| Redpanda | 스트림 버퍼 | `lucida.metrics`, `lucida.logs`, `lucida.traces` | 스트림 설정 의존 |
| Valkey | 캐시/세션 | Query/UI/API 캐시성 데이터 | 런타임 설정 의존 |

기본 파이프라인은 두 평면으로 나뉜다.

| 평면 | 경로 | 의미 |
|---|---|---|
| Telemetry plane | Collector/OTel Collector -> Redpanda -> ingest -> VM/ClickHouse | 수집 데이터 적재 |
| Operation/control plane | Query/API/Collector -> PostgreSQL, pg_notify, OpAMP, long-poll | 정책, 설정, 자격증명, 작업, 상태 |

주요 근거:

- 전체 저장소 역할과 현재 구현 구조:
  [아키텍처.md](../../lucida-next/docs/02-아키텍처/아키텍처.md:52),
  [아키텍처.md](../../lucida-next/docs/02-아키텍처/아키텍처.md:107)
- 에이전트/컬렉터 통신 구조:
  [에이전트-컬렉터-통신.md](../../lucida-next/docs/02-아키텍처/에이전트-컬렉터-통신.md:10)
- 수집기 매핑:
  [lucida-collector-mapping.csv](../../lucida-next/lucida-collector-mapping.csv:1)
- VM 스키마:
  [스키마_VM.md](../../lucida-next/docs/02-아키텍처/스키마/스키마_VM.md:13)
- PostgreSQL 스키마 범위:
  [스키마_PG.md](../../lucida-next/docs/02-아키텍처/스키마/스키마_PG.md:6)
- 보관 정책:
  [보관정책.md](../../lucida-next/docs/02-아키텍처/보관정책.md:113)

## 공통 식별자 모델

Lucida Next의 모든 수집 데이터는 다음 식별자 중 일부를 가진다. 메트릭은 라벨로,
ClickHouse 데이터는 컬럼 또는 Map 속성으로, PostgreSQL 데이터는 컬럼으로 표현된다.

| 식별자 | 위치 | 의미 | 예시 |
|---|---|---|---|
| `target_id` | VM label, CH column/attribute, PG FK | 관제 대상의 Lucida 내부 UUID | `018f4b5e-...` |
| `collector_id` | VM label, CH column/attribute, PG FK | 수집기 인스턴스 UUID | `col-...` 또는 UUID |
| `collector_kind` | VM label, PG `collectors.kind` | 수집기 유형 | `polestar_sms`, `polestar_dpm` |
| `resource_kind` | PG resource inventory | target 하위 리소스 종류 | `filesystem`, `interface`, `pod`, `vm` |
| `resource_key` | PG resource inventory | 같은 kind 안에서 유일한 리소스 키 | `/`, `eth0`, `default/nginx-0` |
| `host_target_id` | PG/CH 일부 테이블 | 서버 하위 리소스의 부모 서버 target | `018f-host-...` |
| `parent_target_id` | PG resource tables | 계층형 리소스 부모 target | node -> pod, host -> vm |
| `cluster_target_id` | KCM PG resource tables | Kubernetes cluster target | `018f-k8s-...` |
| `vcenter_target_id` | VMM PG resource tables | vCenter target | `018f-vcenter-...` |
| `db.system` / `db_system` | VM label, OTLP resource attr | DB 엔진 | `postgresql`, `oracle`, `clickhouse` |
| `service.name` / `service_name` | OTLP/CH | 애플리케이션 서비스명 | `checkout-api` |
| `host.name` / `host_name` | OTLP/CH | 호스트명 | `web-01` |

### `targets`

`targets`는 관제 대상의 루트 엔티티다.

| 컬럼 | 타입/성격 | 설명 |
|---|---|---|
| `id` | UUID PK | target 식별자 |
| `type` | text | 서버, DB, 네트워크, Kubernetes, 애플리케이션 등 대상 유형 |
| `name` | text | 내부 이름 |
| `display_name` | text | 화면 표시명 |
| `address` | text | IP, hostname, endpoint 등 |
| `status` | text | 상태 |
| `group_id` | UUID nullable | 그룹 |
| `discovery` | varchar(64) | 발견 경로/원천 |
| `tags` | text[] 또는 jsonb 계열 | 태그 |
| `labels` | jsonb | 라벨 |
| `meta` | jsonb | 도메인별 확장 메타 |
| `created_at`, `updated_at` | timestamp | 생성/수정 시간 |

예시:

```json
{
  "id": "018f0000-1111-7222-8333-aaaaaaaa0001",
  "type": "server",
  "name": "web-01",
  "display_name": "web-01",
  "address": "10.10.1.21",
  "status": "active",
  "group_id": null,
  "discovery": "polestar_sms",
  "tags": ["prod", "linux"],
  "labels": {"env": "prod", "role": "web"},
  "meta": {"os": "Rocky Linux", "primary_ip": "10.10.1.21"},
  "created_at": "2026-07-07T00:00:00Z",
  "updated_at": "2026-07-07T00:05:00Z"
}
```

근거:
[002_targets_collectors_policies.sql](../../lucida-next/database/ddl/postgres/002_targets_collectors_policies.sql:31)

### `target_identities`

외부 시스템의 식별자와 Lucida `target_id`를 연결한다.

| 컬럼 | 설명 |
|---|---|
| `target_id` | Lucida target |
| `system` | 외부 식별 체계. 예: `sms`, `snmp`, `dpm`, `vcenter` |
| `external_id` | 외부 ID. 예: agent hostname, device IP, DB instance key |
| `external_meta` | 원천별 보조 정보 |

예시:

```json
{
  "target_id": "018f0000-1111-7222-8333-aaaaaaaa0001",
  "system": "sms",
  "external_id": "web-01",
  "external_meta": {"agent_version": "8.0.0", "primary_mac": "00:11:22:33:44:55"}
}
```

근거:
[002_targets_collectors_policies.sql](../../lucida-next/database/ddl/postgres/002_targets_collectors_policies.sql:59)

### `external_systems`

Datadog, Whatap, New Relic, Dynatrace, Zabbix 같은 외부 API 연계 시스템을 표현한다.

| 컬럼 | 설명 |
|---|---|
| `name` | 외부 시스템 이름 |
| `kind` | 연동 종류 |
| `endpoint` | API endpoint |
| `auth_encrypted` | 암호화된 인증 정보 |
| `sync_interval_s` | 동기화 주기 |
| `enabled` | 활성화 여부 |
| `last_sync_*` | 마지막 동기화 상태/시간/오류 |
| `config` | 연동별 설정 |

예시:

```json
{
  "name": "datadog-prod",
  "kind": "datadog",
  "endpoint": "https://api.datadoghq.com",
  "auth_encrypted": "<aes256gcm>",
  "sync_interval_s": 300,
  "enabled": true,
  "last_sync_status": "success",
  "config": {"site": "datadoghq.com", "domains": ["server", "application"]}
}
```

근거:
[002_targets_collectors_policies.sql](../../lucida-next/database/ddl/postgres/002_targets_collectors_policies.sql:70)

### `collection_policies`

수집 주기, 타임아웃, 재시도, 저장 모드 같은 정책 정본이다.

| 컬럼 | 설명 |
|---|---|
| `name` | 정책명 |
| `collector_kind` | 적용 수집기 kind |
| `domain` | 서버/DB/네트워크 등 도메인 |
| `enabled` | 활성화 여부 |
| `builtin`, `default` | 내장/기본 정책 여부 |
| `interval_seconds` | 수집 주기 |
| `timeout_seconds` | 타임아웃 |
| `retry_count` | 재시도 횟수 |
| `params` | collector별 세부 설정 |

예시:

```json
{
  "name": "sms-default-30s",
  "collector_kind": "polestar_sms",
  "domain": "server",
  "enabled": true,
  "builtin": true,
  "default": true,
  "interval_seconds": 30,
  "timeout_seconds": 10,
  "retry_count": 1,
  "params": {"metrics": true, "process_top_n": 50}
}
```

근거:
[002_targets_collectors_policies.sql](../../lucida-next/database/ddl/postgres/002_targets_collectors_policies.sql:89)

### `collectors`

대상별 수집기 인스턴스와 수집 상태를 저장한다.

| 컬럼 | 설명 |
|---|---|
| `target_id` | 관제 대상 |
| `kind` | `polestar_sms`, `polestar_dpm`, `polestar_nms` 등 |
| `enabled` | 활성화 여부 |
| `config` | 평문 설정. secret 제외 권장 |
| `config_encrypted` | 암호화된 secret 포함 설정 |
| `collection_policy_id` | 정책 |
| `external_system_id` | 외부 연계 기반 수집일 때 |
| `storage_mode` | raw/rollup 등 저장 모드 |
| `last_collected_at` | 마지막 수집 시간 |
| `last_collect_status` | 마지막 수집 상태 |
| `last_collect_error` | 마지막 오류 |

예시:

```json
{
  "target_id": "018f0000-1111-7222-8333-aaaaaaaa0001",
  "kind": "polestar_sms",
  "enabled": true,
  "config": {"endpoint": "agent-push", "host": "web-01"},
  "config_encrypted": null,
  "collection_policy_id": "018f0000-2222-7222-8333-bbbbbbbb0001",
  "external_system_id": null,
  "storage_mode": "raw",
  "last_collected_at": "2026-07-07T00:05:30Z",
  "last_collect_status": "success",
  "last_collect_error": null
}
```

근거:
[002_targets_collectors_policies.sql](../../lucida-next/database/ddl/postgres/002_targets_collectors_policies.sql:109)

Secret이 들어가는 `config_encrypted`는 AES-256-GCM으로 암호화한다. 문서상 secret 필드는
`community`, v3 password류, `password`, `api_key`, `token`, `private_key`,
`client_secret`, CLI password류가 포함된다.

근거:
[에이전트-컬렉터-통신.md](../../lucida-next/docs/02-아키텍처/에이전트-컬렉터-통신.md:132)

## 수집기 매핑 카탈로그

`lucida-collector-mapping.csv`는 화면/도메인과 수집기 kind의 기준표다.

| 도메인 | 대표 L2 | collector service | `collectors.kind` | 설정 분기 |
|---|---|---|---|---|
| Server | Polestar SMS | `lucida-collector-sms` | `polestar_sms` | SMS agent |
| Server | OTel Host/eBPF/ICMP/SNMP/IPMI/Redfish | OTel Collector | `opentelemetry` 계열 | receiver별 |
| Server | Datadog/Whatap/New Relic/Dynatrace/Zabbix | connector external | 외부 연계 kind | API |
| Database | Polestar DPM | `lucida-collector-dpm` | `polestar_dpm` | `engine` |
| Database | OTel PostgreSQL/Oracle/MySQL/MSSQL/Redis/ClickHouse/Kafka | OTel Collector | `opentelemetry` 계열 | receiver별 |
| Network | Polestar NMS | `lucida-collector-nms` | `polestar_nms` | SNMP |
| Network | Polestar TMS | `lucida-collector-tms` | `polestar_tms` | NetFlow/sFlow/IPFIX |
| Network | Polestar SYSLOG | `lucida-collector-syslog` | `polestar_syslog` | Syslog |
| Kubernetes | KCM | `lucida-collector-kcm` | `polestar_kcm` | cluster |
| Application | Polestar APM/WPM/WebURL | `collector-apm`, `collector-wpm`, `collector-weburl` | APM/WPM/WebURL kind | app/synthetic |
| Storage | SRM/NetApp/Pure/Datadog | storage collector/connector | storage kind | vendor |
| Facility | FMS SNMP/Modbus/BMS | `lucida-collector-fms` | `polestar_fms` | protocol |
| Virtualization | VMM/AHV/vCenter/NSX/vSAN | `lucida-collector-vmm` 또는 OTel/external | `polestar_vmm` 등 | platform |

근거:
[lucida-collector-mapping.csv](../../lucida-next/lucida-collector-mapping.csv:1)

## VictoriaMetrics 메트릭 스키마

VM에는 메트릭만 저장한다. ClickHouse에는 메트릭 테이블을 만들지 않는다고 DDL에
명시되어 있다.

근거:
[001_init.sql](../../lucida-next/database/ddl/clickhouse/001_init.sql:86)

### 기본 형태

```json
{
  "__name__": "sms.cpu.utilization",
  "target_id": "018f0000-1111-7222-8333-aaaaaaaa0001",
  "collector_kind": "polestar_sms",
  "host_name": "web-01",
  "cpu": "0",
  "mode": "user",
  "value": 42.3,
  "timestamp_ms": 1783382730000
}
```

| 요소 | 설명 |
|---|---|
| `__name__` | 메트릭명. dot 표기를 유지한다. |
| resource labels | `target_id`, `collector_kind`, `db_system`, `host.name`, `k8s.*` 등 allowlist 기반 |
| data-point labels | 인터페이스명, 상태, device, SQL key 등 데이터 포인트 속성 |
| value | double 또는 integer 계열 |
| timestamp | ms 정밀도 시계열 timestamp |

### 주요 allowlist 라벨

| OTel resource attribute | VM label |
|---|---|
| `lucida.target_id` | `target_id` |
| `lucida.collector_kind` | `collector_kind` |
| `lucida.storage.raw|min1|min5|hour1|day1` | `storage_*` |
| `db.system` | `db_system` |
| `host.id`, `host.arch` | `host_id`, `host_arch` |
| `os.type`, `os.name`, `os.version`, `os.description` | `os_*` |
| `k8s.cluster.name`, `k8s.node.name`, `k8s.namespace.name` | `cluster_name`, `node_name`, `namespace_name` |
| `k8s.pod.name`, `k8s.container.name` | `pod_name`, `container_name` |
| `process.pid`, `process.executable.name`, `process.owner` | `process_*` |

근거:
[스키마_VM.md](../../lucida-next/docs/02-아키텍처/스키마/스키마_VM.md:80)

### MetricKind와 rollup

| 항목 | 현재 기준 |
|---|---|
| Gauge | 지원 |
| Counter | 지원 |
| Histogram | 현재 경로에서는 skip 대상 |
| raw 보관 | vmsingle 전역 7일 |
| rollup | raw -> 1m -> 5m -> 1h -> 1d |
| 대표 group | `PER_TARGET`, `PER_INSTANCE`, `PER_IF`, `IF_CANON`, `HOST_AGG` |

근거:
[스키마_VM.md](../../lucida-next/docs/02-아키텍처/스키마/스키마_VM.md:57),
[스키마_VM.md](../../lucida-next/docs/02-아키텍처/스키마/스키마_VM.md:137),
[보관정책.md](../../lucida-next/docs/02-아키텍처/보관정책.md:132)

### 메트릭 예시

서버 SMS:

```json
[
  {
    "__name__": "sms.cpu.utilization",
    "target_id": "018f0000-1111-7222-8333-aaaaaaaa0001",
    "collector_kind": "polestar_sms",
    "host_name": "web-01",
    "cpu": "total",
    "mode": "system",
    "value": 18.7,
    "timestamp_ms": 1783382730000
  },
  {
    "__name__": "sms.memory.utilization",
    "target_id": "018f0000-1111-7222-8333-aaaaaaaa0001",
    "collector_kind": "polestar_sms",
    "host_name": "web-01",
    "value": 73.2,
    "timestamp_ms": 1783382730000
  },
  {
    "__name__": "sms.network_interface.link_alive",
    "target_id": "018f0000-1111-7222-8333-aaaaaaaa0001",
    "collector_kind": "polestar_sms",
    "interface": "eth0",
    "value": 1,
    "timestamp_ms": 1783382730000
  }
]
```

DPM:

```json
{
  "__name__": "dpm.postgresql.database.buffer_hit_ratio",
  "target_id": "018f0000-3333-7222-8333-cccccccc0001",
  "collector_kind": "polestar_dpm",
  "collector_id": "018f0000-3333-7222-8333-cccccccc1001",
  "db_system": "postgresql",
  "database": "appdb",
  "value": 99.41,
  "timestamp_ms": 1783382730000
}
```

Kubernetes KCM:

```json
{
  "__name__": "kcm.pod.cpu.usage",
  "target_id": "018f0000-4444-7222-8333-dddddddd0001",
  "cluster_id": "018f0000-4444-7222-8333-dddddddd1001",
  "namespace": "default",
  "pod": "checkout-7df7d7f9b6-x8n2k",
  "container": "checkout",
  "value": 0.42,
  "timestamp_ms": 1783382730000
}
```

Facility FMS:

```json
{
  "__name__": "facility.ups.battery.charge_percent",
  "target_id": "018f0000-5555-7222-8333-eeeeeeee0001",
  "collector_kind": "polestar_fms",
  "facility_type": "ups",
  "snmp_index": "1",
  "value": 96,
  "timestamp_ms": 1783382730000
}
```

## ClickHouse 스키마

### 보관 정책

| 테이블 | 데이터 | TTL |
|---|---|---:|
| `otel_traces_local` | OTLP span | 3일 |
| `otel_logs_local` | OTLP log | 7일 |
| `agg_service_golden_signals` | 서비스 골든 시그널 집계 | 90일 |
| `snmp_traps_local` | SNMP trap | 90일 |
| `host_connections` | 서버 연결 스냅샷 | 7일 |
| `lucida_events_local` | 원자 이벤트 | 30일 |
| `wpm_xlog_local` | WPM transaction/xlog | 30일 |
| `wpm_xlog_profile_local` | WPM profile | 30일 |
| `wpm_interaction_local` | WPM service interaction counter | 30일 |
| `wpm_summary_local` | WPM summary | 30일 |
| `wpm_text_local` | WPM text payload | 3650일 |
| `dpm_session_local` | DB session log | 행별 `retention_days`, 기본 90일 |
| `dpm_topsql_local` | DB top SQL log | 행별 `retention_days`, 기본 90일 |
| `dpm_ch_query_event_local` | ClickHouse DPM query event | 행별 `retention_days`, 기본 14일 |
| `dpm_ch_storage_inventory_local` | ClickHouse DPM storage inventory | 행별 `retention_days`, 기본 30일 |
| `dpm_ch_replica_health_local` | ClickHouse DPM replica health | 행별 `retention_days`, 기본 30일 |
| `netflow_records_local` | NetFlow/sFlow/IPFIX | 30일 |
| `lucida_logs_local` | Lucida normalized logs | 7일 |
| `syslog_local` | Syslog original row | 90일 |

근거:
[보관정책.md](../../lucida-next/docs/02-아키텍처/보관정책.md:113)

### `otel_traces_local`

OTLP span 정본 테이블이다.

| 컬럼 | 설명 |
|---|---|
| `timestamp`, `end_timestamp` | span 시작/종료 |
| `trace_id`, `span_id`, `parent_span_id` | trace 식별자 |
| `trace_state` | W3C trace state |
| `span_name`, `span_kind` | span 이름/종류 |
| `status_code`, `status_message` | 상태 |
| `duration_ns` | span duration |
| `service_name`, `service_version`, `service_namespace` | 서비스 |
| `deployment_env` | 배포 환경 |
| `host_name` | 호스트 |
| `k8s_namespace`, `k8s_pod_name`, `k8s_node_name` | Kubernetes context |
| `span_attributes`, `resource_attributes` | Map 속성 |
| `events`, `links` | JSON 문자열 |
| `scope_name`, `scope_version`, `otel_schema_url` | instrumentation scope |

예시:

```json
{
  "timestamp": "2026-07-07T00:05:30.123Z",
  "trace_id": "9f5a2e9f7a3c4a4bb9b7f8b6c5d4e3f2",
  "span_id": "4f3e2d1c0b9a8877",
  "parent_span_id": "0000000000000000",
  "span_name": "GET /api/orders",
  "span_kind": "SERVER",
  "status_code": "OK",
  "duration_ns": 18300000,
  "service_name": "checkout-api",
  "deployment_env": "prod",
  "host_name": "app-01",
  "k8s_namespace": "default",
  "k8s_pod_name": "checkout-7df7d7f9b6-x8n2k",
  "span_attributes": {"http.method": "GET", "http.route": "/api/orders"},
  "resource_attributes": {"lucida.target_id": "018f0000-6666-7222-8333-ffffffff0001"}
}
```

근거:
[001_init.sql](../../lucida-next/database/ddl/clickhouse/001_init.sql:8)

### `otel_logs_local`

OTLP log 정본 테이블이다.

| 컬럼 | 설명 |
|---|---|
| `timestamp`, `observed_timestamp` | event/observed 시각 |
| `trace_id`, `span_id`, `trace_flags` | trace 연계 |
| `severity_text`, `severity_number` | 심각도 |
| `body` | 로그 본문 |
| `service_name`, `service_version`, `service_namespace` | 서비스 |
| `deployment_env`, `host_name` | 환경/호스트 |
| `k8s_namespace`, `k8s_pod_name`, `k8s_node_name`, `k8s_container_name` | Kubernetes context |
| `log_attributes`, `resource_attributes` | Map 속성 |
| `scope_name`, `scope_version`, `otel_schema_url` | instrumentation scope |

예시:

```json
{
  "timestamp": "2026-07-07T00:05:31.000Z",
  "observed_timestamp": "2026-07-07T00:05:31.050Z",
  "trace_id": "9f5a2e9f7a3c4a4bb9b7f8b6c5d4e3f2",
  "span_id": "4f3e2d1c0b9a8877",
  "severity_text": "ERROR",
  "severity_number": 17,
  "body": "payment provider timeout",
  "service_name": "checkout-api",
  "host_name": "app-01",
  "log_attributes": {"logger": "payment", "error.type": "TimeoutError"},
  "resource_attributes": {"lucida.target_id": "018f0000-6666-7222-8333-ffffffff0001"}
}
```

근거:
[001_init.sql](../../lucida-next/database/ddl/clickhouse/001_init.sql:53)

### `agg_service_golden_signals`

서비스 단위 집계 테이블이다.

| 컬럼 | 설명 |
|---|---|
| `minute` | 집계 분 |
| `service_name` | 서비스 |
| `req_count` | 요청 수 |
| `error_count` | 오류 수 |
| `duration_sum_ms` | duration 합계 |
| `tdigest` | duration 분포 |
| `log_error_count`, `log_warn_count` | 로그 심각도 집계 |

예시:

```json
{
  "minute": "2026-07-07T00:05:00Z",
  "service_name": "checkout-api",
  "req_count": 1280,
  "error_count": 12,
  "duration_sum_ms": 48320.5,
  "log_error_count": 7,
  "log_warn_count": 31
}
```

근거:
[001_init.sql](../../lucida-next/database/ddl/clickhouse/001_init.sql:95)

### `snmp_traps_local`

SNMP trap 원본과 varbind를 저장한다.

| 컬럼 | 설명 |
|---|---|
| `received_at` | 수신 시각 |
| `target_id`, `collector_id` | 장비/수집기 |
| `source_ip` | trap 송신 IP |
| `trap_oid`, `trap_type`, `enterprise_oid` | trap 식별 |
| `generic_trap`, `specific_trap` | SNMP v1 trap 코드 |
| `varbinds` | varbind Map |
| `raw_pdu` | 원본 PDU |
| `listen_port`, `listen_proto` | 수신 포트/프로토콜 |
| `if_index` | linkUp/linkDown 등 인터페이스 index |

예시:

```json
{
  "received_at": "2026-07-07T00:06:00Z",
  "target_id": "018f0000-7777-7222-8333-aaaaaaaa1001",
  "collector_id": "018f0000-7777-7222-8333-aaaaaaaa2001",
  "source_ip": "10.20.0.10",
  "trap_oid": "1.3.6.1.6.3.1.1.5.3",
  "trap_type": "linkDown",
  "enterprise_oid": "1.3.6.1.4.1.9",
  "generic_trap": 2,
  "specific_trap": 0,
  "varbinds": {"ifIndex": "12", "ifDescr": "Gi1/0/12"},
  "listen_port": 10162,
  "listen_proto": "udp",
  "if_index": 12
}
```

근거:
[003_snmp_traps.sql](../../lucida-next/database/ddl/clickhouse/003_snmp_traps.sql:1)

### `host_connections`

서버 네트워크 연결 스냅샷이다. `/proc/net` 기반 스냅샷이며 eBPF 플로우 byte 카운터가
아니다.

| 컬럼 | 설명 |
|---|---|
| `timestamp` | 수집 시각 |
| `target_id` | 서버 target |
| `host_name` | 호스트명 |
| `local_addr`, `local_port` | 로컬 endpoint |
| `remote_addr`, `remote_port` | 원격 endpoint |
| `direction` | inbound/outbound 등 |
| `process` | 프로세스명 |
| `state` | TCP state |

예시:

```json
{
  "timestamp": "2026-07-07T00:05:30Z",
  "target_id": "018f0000-1111-7222-8333-aaaaaaaa0001",
  "host_name": "web-01",
  "local_addr": "10.10.1.21",
  "local_port": 443,
  "remote_addr": "10.10.2.51",
  "remote_port": 52784,
  "direction": "inbound",
  "process": "nginx",
  "state": "ESTABLISHED"
}
```

근거:
[008_host_connections.sql](../../lucida-next/database/ddl/clickhouse/008_host_connections.sql:28)

### `lucida_events_local`

원자 이벤트 저장 테이블이다. 이 문서에서는 RCA/incident 판단 흐름이 아니라 일반
이벤트 원천 저장소로만 다룬다.

| 컬럼 | 설명 |
|---|---|
| `event_id` | 이벤트 ID |
| `occurred_at` | 발생 시각 |
| `detector` | 감지 주체 |
| `event_kind` | 이벤트 종류 |
| `severity` | 심각도 |
| `target_id`, `service_name`, `domain` | 대상/서비스/도메인 |
| `source_id`, `cluster_id` | 원천/군집 식별자 |
| `attributes` | 정규화 속성 |
| `raw` | 원본 |

예시:

```json
{
  "event_id": "evt-20260707-000001",
  "occurred_at": "2026-07-07T00:05:30Z",
  "detector": "collector-tms",
  "event_kind": "exporter_stale",
  "severity": "warning",
  "target_id": "018f0000-7777-7222-8333-aaaaaaaa1001",
  "domain": "network",
  "source_id": "10.20.0.10",
  "attributes": {"exporter_ip": "10.20.0.10", "flow_type": "netflow"},
  "raw": {"message": "netflow exporter became stale"}
}
```

근거:
[008_lucida_events.sql](../../lucida-next/database/ddl/clickhouse/008_lucida_events.sql:6)

### WPM 테이블

WPM은 direct-to-storage 경로가 있으며 ClickHouse와 VictoriaMetrics에 직접 쓴다.

| 테이블 | 핵심 컬럼/내용 |
|---|---|
| `wpm_xlog_local` | transaction/xlog 원본, `end_time`, `txid`, `elapsed_ms`, status, hash 기반 service/context |
| `wpm_xlog_profile_local` | xlog별 profile 세부 단계 JSON |
| `wpm_interaction_local` | service-to-service interaction counter |
| `wpm_summary_local` | summary 집계 |
| `wpm_text_local` | hash -> text dictionary, 긴 보관 |

예시:

```json
{
  "table": "wpm_xlog_local",
  "end_time": "2026-07-07T00:05:30Z",
  "target_id": "018f0000-8888-7222-8333-bbbbbbbb1001",
  "collector_id": "018f0000-8888-7222-8333-bbbbbbbb2001",
  "txid": 100000001,
  "service_hash": 123456,
  "elapsed_ms": 431,
  "status": 200
}
```

근거:
[008_wpm.sql](../../lucida-next/database/ddl/clickhouse/008_wpm.sql:9),
[clickhouse.go](../../lucida-next/backend/services/collector-wpm/store/clickhouse.go:1)

### DPM ClickHouse 테이블

`dpm_session_local`과 `dpm_topsql_local`은 DB 공통 세션/TopSQL 로그성 데이터다.

| 테이블 | 핵심 컬럼 |
|---|---|
| `dpm_session_local` | `timestamp`, `target_id`, `collector_id`, `engine`, session id/user/db/state/wait/body/attributes |
| `dpm_topsql_local` | `timestamp`, `target_id`, `collector_id`, `engine`, `sql_id`, `sql_hash`, severity/body/attributes |

예시:

```json
{
  "table": "dpm_topsql_local",
  "timestamp": "2026-07-07T00:05:30Z",
  "target_id": "018f0000-3333-7222-8333-cccccccc0001",
  "collector_id": "018f0000-3333-7222-8333-cccccccc1001",
  "engine": "postgresql",
  "sql_id": "pg:9fd1",
  "sql_hash": "9fd11db5",
  "severity_text": "INFO",
  "body": "SELECT * FROM orders WHERE status = $1",
  "log_attributes": {"calls": "310", "mean_ms": "12.8"},
  "retention_days": 90
}
```

ClickHouse 엔진 자체를 DPM으로 수집할 때는 전용 테이블을 쓴다.

| 테이블 | 핵심 컬럼 |
|---|---|
| `dpm_ch_query_event_local` | query id, fingerprint, query kind, user, duration, rows/bytes, memory, exception, profile events, cluster/shard/replica |
| `dpm_ch_storage_inventory_local` | database/table/partition/part, rows/bytes/marks, disk, TTL |
| `dpm_ch_replica_health_local` | database/table/replica, queue/delay/readonly/session flags |

예시:

```json
{
  "table": "dpm_ch_query_event_local",
  "timestamp": "2026-07-07T00:05:30Z",
  "target_id": "018f0000-3333-7222-8333-cccccccc2001",
  "collector_id": "018f0000-3333-7222-8333-cccccccc3001",
  "event_type": "QueryFinish",
  "query_id": "8e5809f2-7ec9-4d2c-8d5d",
  "sql_fingerprint": "select count() from otel_logs_local where service_name = ?",
  "query_kind": "Select",
  "user_name": "readonly",
  "current_database": "lucida",
  "query_duration_ms": 842,
  "read_rows": 1200000,
  "read_bytes": 184000000,
  "memory_usage": 536870912,
  "exception_code": 0,
  "profile_events": {"SelectedRows": "1200000"},
  "cluster": "default",
  "shard": "1",
  "replica": "1",
  "retention_days": 14
}
```

근거:
[009_dpm_session_topsql.sql](../../lucida-next/database/ddl/clickhouse/009_dpm_session_topsql.sql:18),
[010_dpm_clickhouse.sql](../../lucida-next/database/ddl/clickhouse/010_dpm_clickhouse.sql:17)

### `netflow_records_local`

NetFlow/sFlow/IPFIX 레코드 저장 테이블이다.

| 컬럼 | 설명 |
|---|---|
| `received_at` | 수신 시각 |
| `target_id`, `collector_id` | exporter/collector |
| `exporter_ip` | exporter IP |
| `flow_type` | netflow/sflow/ipfix |
| `src_addr`, `dst_addr`, `src_port`, `dst_port` | 5-tuple 일부 |
| `protocol` | L4 protocol |
| `bytes`, `packets` | traffic counters |
| `in_iface`, `out_iface` | interface index |
| `tcp_flags` | TCP flags |
| `sampling` | sampling rate |

예시:

```json
{
  "received_at": "2026-07-07T00:05:30Z",
  "target_id": "018f0000-7777-7222-8333-aaaaaaaa1001",
  "collector_id": "018f0000-7777-7222-8333-aaaaaaaa3001",
  "exporter_ip": "10.20.0.10",
  "flow_type": "netflow",
  "src_addr": "10.10.1.21",
  "dst_addr": "10.30.0.15",
  "src_port": 52784,
  "dst_port": 443,
  "protocol": 6,
  "bytes": 183920,
  "packets": 214,
  "in_iface": 12,
  "out_iface": 4,
  "tcp_flags": 24,
  "sampling": 1
}
```

근거:
[012_netflow_records.sql](../../lucida-next/database/ddl/clickhouse/012_netflow_records.sql:8)

### `lucida_logs_local`

collector가 만든 정규화 로그 테이블이다. syslog/trap/flow event 등 도메인 로그가
OTLP 로그 형태로 변환되어 들어갈 수 있다.

| 컬럼 | 설명 |
|---|---|
| `timestamp`, `observed_timestamp` | 발생/관측 시간 |
| `target_id` | 대상 |
| `collector_kind` | 수집기 kind |
| `event_source`, `event_level` | 이벤트 원천/레벨 |
| `trace_id`, `span_id` | trace 연계 |
| `severity_text`, `severity_number` | 심각도 |
| `body` | 본문 |
| `service_*`, `host_*`, `k8s_*` | context |
| `log_attributes`, `resource_attributes` | Map 속성 |

예시:

```json
{
  "timestamp": "2026-07-07T00:06:00Z",
  "target_id": "018f0000-7777-7222-8333-aaaaaaaa1001",
  "collector_kind": "polestar_syslog",
  "event_source": "syslog",
  "event_level": "warning",
  "severity_text": "WARNING",
  "body": "Interface Gi1/0/12 changed state to down",
  "host_name": "switch-01",
  "log_attributes": {"facility": "local7", "app_name": "LINK"},
  "resource_attributes": {"source_ip": "10.20.0.10"}
}
```

근거:
[014_lucida_logs.sql](../../lucida-next/database/ddl/clickhouse/014_lucida_logs.sql:68)

### `syslog_local`

Syslog 원본 정규화 테이블이다.

| 컬럼 | 설명 |
|---|---|
| `received_at` | 수신 시각 |
| `receiver_target_id`, `collector_id` | receiver/collector |
| `device_target_id` | 등록 장비 target |
| `registered` | 등록 장비 매칭 여부 |
| `match_domain` | 매칭 도메인 |
| `source_ip` | 송신 IP |
| `facility`, `severity`, `severity_text` | syslog severity |
| `hostname`, `app_name`, `proc_id`, `msg_id` | RFC 필드 |
| `structured_data`, `message`, `raw` | 본문/원본 |
| `encoding`, `listen_port`, `listen_proto` | 수신 정보 |

예시:

```json
{
  "received_at": "2026-07-07T00:06:00Z",
  "receiver_target_id": "018f0000-7777-7222-8333-aaaaaaaa4001",
  "collector_id": "018f0000-7777-7222-8333-aaaaaaaa5001",
  "device_target_id": "018f0000-7777-7222-8333-aaaaaaaa1001",
  "registered": true,
  "match_domain": "network",
  "source_ip": "10.20.0.10",
  "facility": 23,
  "severity": 4,
  "severity_text": "warning",
  "hostname": "switch-01",
  "app_name": "LINK",
  "proc_id": "1234",
  "msg_id": "IFDOWN",
  "message": "Interface Gi1/0/12 changed state to down",
  "raw": "<188>1 2026-07-07T00:06:00Z switch-01 LINK 1234 IFDOWN - Interface Gi1/0/12 changed state to down",
  "listen_port": 514,
  "listen_proto": "udp"
}
```

근거:
[017_syslog.sql](../../lucida-next/database/ddl/clickhouse/017_syslog.sql:129)

## PostgreSQL 도메인 스키마

### SMS inventory

`collector_sms_inventory`는 서버 target 단위의 최신 인벤토리 스냅샷이다. 메트릭과
로그는 별도 저장소에 저장되고, 이 테이블은 장비 속성/인벤토리 중심이다.

| 컬럼 | 설명 |
|---|---|
| `target_id` | 서버 target PK |
| `hostname` | 호스트명 |
| `os`, `os_version`, `os_patch_level`, `kernel` | OS 정보 |
| `cpu_model`, `cpu_sockets`, `cpu_cores`, `cpu_threads` | CPU 구성 |
| `mem_total_bytes`, `disk_total_bytes` | 총 메모리/디스크 |
| `nic_count`, `primary_ip`, `primary_mac` | 네트워크 기본 정보 |
| `agent_version`, `agent_arch` | SMS agent 정보 |
| `meta` | 확장 메타 |
| `created_at`, `updated_at` | 시간 |

예시:

```json
{
  "target_id": "018f0000-1111-7222-8333-aaaaaaaa0001",
  "hostname": "web-01",
  "os": "linux",
  "os_version": "Rocky Linux 9.4",
  "os_patch_level": "9.4",
  "kernel": "5.14.0-427.el9.x86_64",
  "cpu_model": "Intel Xeon Gold",
  "cpu_sockets": 2,
  "cpu_cores": 16,
  "cpu_threads": 32,
  "mem_total_bytes": 68719476736,
  "disk_total_bytes": 1099511627776,
  "nic_count": 4,
  "primary_ip": "10.10.1.21",
  "primary_mac": "00:11:22:33:44:55",
  "agent_version": "8.0.0",
  "agent_arch": "amd64",
  "meta": {"virtualized": true}
}
```

근거:
[019_collector_sms_inventory.sql](../../lucida-next/database/ddl/postgres/019_collector_sms_inventory.sql:11)

### SMS process/service/session/docker snapshots

| 테이블 | PK/유니크 | 주요 컬럼 | 의미 |
|---|---|---|---|
| `collector_sms_process_snapshot` | `(target_id, pid)` | `name`, `ppid`, `username`, `status`, `cmdline`, `create_time`, `updated_at` | top process snapshot |
| `collector_sms_services_snapshot` | `(target_id, name)` | `description`, `load_state`, `active_state`, `sub_state`, `unit_type`, `enabled` | systemd/service snapshot |
| `collector_sms_netstat_snapshot` | `(target_id, seq)` 성격 | protocol/local/foreign/state/pid/process/collected_at | net session snapshot |
| `collector_sms_docker_snapshot` | `(target_id, seq)` 성격 | container id/name/image/state/health/cpu/memory/pids/restart/uptime | Docker container snapshot |

프로세스 예시:

```json
{
  "target_id": "018f0000-1111-7222-8333-aaaaaaaa0001",
  "pid": 1234,
  "name": "nginx",
  "ppid": 1,
  "username": "nginx",
  "status": "S",
  "cmdline": "nginx: worker process",
  "create_time": "2026-07-06T23:00:00Z",
  "updated_at": "2026-07-07T00:05:30Z"
}
```

Docker 예시:

```json
{
  "target_id": "018f0000-1111-7222-8333-aaaaaaaa0001",
  "seq": 1,
  "container_id": "a1b2c3d4e5f6",
  "name": "checkout-api",
  "image": "registry.example.com/checkout:2026.07.07",
  "state": "running",
  "status": "Up 3 hours",
  "health": "healthy",
  "compose_project": "shop",
  "compose_service": "checkout",
  "cpu_percent": 4.2,
  "mem_usage_bytes": 402653184,
  "mem_limit_bytes": 1073741824,
  "mem_percent": 37.5,
  "pids": 32,
  "restart_count": 0,
  "uptime_seconds": 10800,
  "created_epoch": 1783370000,
  "collected_at": "2026-07-07T00:05:30Z"
}
```

근거:
[019_collector_sms_process_snapshot.sql](../../lucida-next/database/ddl/postgres/019_collector_sms_process_snapshot.sql:65),
[019_collector_sms_services_snapshot.sql](../../lucida-next/database/ddl/postgres/019_collector_sms_services_snapshot.sql:90),
[083_collector_sms_net_sessions.sql](../../lucida-next/database/ddl/postgres/083_collector_sms_net_sessions.sql:106),
[089_collector_sms_docker.sql](../../lucida-next/database/ddl/postgres/089_collector_sms_docker.sql:145)

### `server_resources`

서버 target 하위의 리소스 target 또는 리소스 인벤토리를 정규화한다.

| 컬럼 | 설명 |
|---|---|
| `target_id` | 리소스 target |
| `host_target_id` | 부모 서버 target |
| `parent_target_id` | 상위 리소스 |
| `resource_kind` | `cpu`, `memory`, `filesystem`, `interface`, `gpu`, `netsessions`, `containers` 등 |
| `resource_key` | 리소스 유니크 키 |
| `meta` | 리소스별 속성 |
| `created_at`, `updated_at` | 시간 |

예시:

```json
{
  "target_id": "018f0000-1111-7222-8333-aaaaaaaa0101",
  "host_target_id": "018f0000-1111-7222-8333-aaaaaaaa0001",
  "parent_target_id": "018f0000-1111-7222-8333-aaaaaaaa0001",
  "resource_kind": "filesystem",
  "resource_key": "/",
  "meta": {"device": "/dev/sda2", "fstype": "xfs", "mountpoint": "/"}
}
```

근거:
[069_server_resources.sql](../../lucida-next/database/ddl/postgres/069_server_resources.sql:28)

### DPM configuration and SQL metadata

DPM은 수집 설정, DB config snapshot, SQL text/plan dedup, custom SQL metric metadata를
PostgreSQL에 저장한다.

| 테이블 | 의미 |
|---|---|
| `collector_dpm_pg_config` | PostgreSQL 인스턴스 config snapshot |
| `collector_dpm_pg_database_config` | database별 config snapshot |
| `db_sql_text` | SQL text dedup 저장 |
| `db_sql_plan` | plan hash별 plan 저장 |
| custom SQL metadata tables | 사용자 정의 SQL metric template/deployment/revision/runtime health |

SQL text 예시:

```json
{
  "resource_id": "018f0000-3333-7222-8333-cccccccc0001",
  "sql_key": "pg:9fd11db5",
  "engine": "postgresql",
  "normalized_sql": "SELECT * FROM orders WHERE status = $1",
  "created_at": "2026-07-07T00:05:30Z"
}
```

custom SQL metric deployment 예시:

```json
{
  "template_key": "pg_locks_by_mode",
  "target_id": "018f0000-3333-7222-8333-cccccccc0001",
  "enabled": true,
  "interval_seconds": 60,
  "metric_prefix": "dpm.custom.postgresql",
  "labels": ["database", "lock_mode"],
  "runtime_status": "success"
}
```

근거:
[051_collector_dpm_config.sql](../../lucida-next/database/ddl/postgres/051_collector_dpm_config.sql:24),
[064_db_sql_text.sql](../../lucida-next/database/ddl/postgres/064_db_sql_text.sql:83),
[140_dpm_custom_sql_metadata_schema.sql](../../lucida-next/database/ddl/postgres/140_dpm_custom_sql_metadata_schema.sql:122)

### Metric catalog

메트릭 카탈로그는 VM `__name__`과 리소스 타입, 라벨, 단위, value type, rollup 여부를
관리한다.

| 테이블/컬럼 | 의미 |
|---|---|
| `resource_types` | 리소스 타입 정의 |
| `metric_definitions.metric_key` | VM `__name__` |
| `labels` | 허용/기대 라벨 |
| `unit` | 단위 |
| `metric_type` | gauge/counter 등 |
| `value_type` | `gauge`, `counter_up`, `counter_down` |
| `delta_op`, `raw_unit` | delta 변환/원본 단위 |
| `collector_kind` | 수집기 kind |
| `source_spec`, `origin` | 원천/정의 출처 |

예시:

```json
{
  "metric_key": "sms.cpu.utilization",
  "resource_type": "server",
  "labels": ["target_id", "host_name", "cpu", "mode"],
  "unit": "percent",
  "metric_type": "gauge",
  "value_type": "gauge",
  "collector_kind": "polestar_sms",
  "origin": "builtin",
  "enabled": true
}
```

근거:
[148_metric_catalog.sql](../../lucida-next/database/ddl/postgres/148_metric_catalog.sql:35),
[149_metric_definitions_value_type.sql](../../lucida-next/database/ddl/postgres/149_metric_definitions_value_type.sql:96),
[151_metric_definitions_delta_conversion.sql](../../lucida-next/database/ddl/postgres/151_metric_definitions_delta_conversion.sql:124)

### Kubernetes KCM resource targets

KCM은 Kubernetes 리소스를 target 하위 리소스로 승격/관리한다.

| 컬럼 | 설명 |
|---|---|
| `target_id` | Kubernetes resource target |
| `cluster_target_id` | cluster target |
| `parent_target_id` | namespace/node/deployment 등 부모 |
| `resource_kind` | `node`, `namespace`, `service`, `hpa`, `deployment`, `statefulset`, `daemonset`, `replicaset`, `job`, `cronjob`, `pod`, `container`, `ingress`, `pvc`, `pv`, `storageclass`, `configmap` |
| `resource_key` | namespace/name 또는 cluster scope key |
| `meta` | labels, annotations, owner refs 등 |

예시:

```json
{
  "target_id": "018f0000-4444-7222-8333-dddddddd0101",
  "cluster_target_id": "018f0000-4444-7222-8333-dddddddd0001",
  "parent_target_id": "018f0000-4444-7222-8333-dddddddd0201",
  "resource_kind": "pod",
  "resource_key": "default/checkout-7df7d7f9b6-x8n2k",
  "meta": {
    "namespace": "default",
    "name": "checkout-7df7d7f9b6-x8n2k",
    "node": "worker-01",
    "labels": {"app": "checkout"}
  }
}
```

근거:
[141_kcm_resource_targets.sql](../../lucida-next/database/ddl/postgres/141_kcm_resource_targets.sql:108),
[resources.go](../../lucida-next/backend/services/collector-kcm/store/resources.go:177)

### VMM vCenter resources

VMM은 vCenter 하위 리소스를 target/resource로 관리한다.

| 컬럼 | 설명 |
|---|---|
| `target_id` | resource target |
| `vcenter_target_id` | vCenter target |
| `parent_target_id` | 상위 host/datastore/network 등 |
| `resource_kind` | `host`, `vm`, `datastore`, `network`, `host_cpu`, `host_memory`, `host_storage`, `host_network`, `vm_cpu`, `vm_memory`, `vm_disk`, `vm_network` |
| `resource_key` | vCenter managed object key 등 |
| `meta` | power state, guest, datastore, cluster 등 |

예시:

```json
{
  "target_id": "018f0000-9999-7222-8333-cccccccc0101",
  "vcenter_target_id": "018f0000-9999-7222-8333-cccccccc0001",
  "parent_target_id": "018f0000-9999-7222-8333-cccccccc0201",
  "resource_kind": "vm",
  "resource_key": "vm-4231",
  "meta": {"name": "checkout-vm-01", "power_state": "poweredOn", "guest_os": "linux"}
}
```

근거:
[106_vcenter_resources.sql](../../lucida-next/database/ddl/postgres/106_vcenter_resources.sql:208),
[reconcile.go](../../lucida-next/backend/services/collector-vmm/store/reconcile.go:69)

### Network resource inventory

네트워크 리소스는 target으로 승격하지 않는 last-known inventory 테이블도 가진다.

| 컬럼 | 설명 |
|---|---|
| `target_id` | 네트워크 장비 target |
| `resource_kind` | `network_interface`, `cpu`, `memory`, `module`, `fan`, `power`, `temperature`, `slb_virtual`, `slb_physical` |
| `resource_key` | `network_interface`는 interface name 또는 `eth{ifIndex}` fallback, 그 외 종류는 SNMP index |
| `display_name` | 표시명 |
| `meta` | vendor/OID 기반 속성 |
| `collected_at`, `updated_at` | 시간 |

예시:

```json
{
  "target_id": "018f0000-7777-7222-8333-aaaaaaaa1001",
  "resource_kind": "network_interface",
  "resource_key": "Gi1/0/12",
  "display_name": "Gi1/0/12",
  "meta": {"ifType": "ethernetCsmacd", "ifSpeed": 1000000000, "adminStatus": "up"},
  "collected_at": "2026-07-07T00:05:30Z"
}
```

근거:
[141_network_resource_inventory.sql](../../lucida-next/database/ddl/postgres/141_network_resource_inventory.sql:280)

## 도메인별 수집 경로

### OpenTelemetry 계열

| 항목 | 수집 데이터 | 경로 | 저장 |
|---|---|---|---|
| Trace | spans, events, links, resource/span attributes | OTel Collector -> Redpanda `lucida.traces` -> ingest | ClickHouse `otel_traces_local` |
| Log | log body, severity, trace context, resource/log attributes | OTel Collector -> Redpanda `lucida.logs` -> ingest | ClickHouse `otel_logs_local` 또는 `lucida_logs_local` |
| Metric | OTel ResourceMetrics | OTel Collector -> Redpanda `lucida.metrics` -> ingest | VictoriaMetrics |
| Agent control | RemoteConfig, health, policy | OpAMP WS `/v1/opamp`, PostgreSQL, pg_notify | PostgreSQL |

OpAMP과 SMS agent는 별도 제어 채널이다. OpAMP은 token 기반 WebSocket과 RemoteConfig를
사용하고, 정책 변경은 `collection_policy_changed` 알림으로 fan-out된다.

근거:
[에이전트-컬렉터-통신.md](../../lucida-next/docs/02-아키텍처/에이전트-컬렉터-통신.md:49),
[ingest/main.go](../../lucida-next/backend/services/ingest/main.go:498),
[metrics.go](../../lucida-next/backend/services/ingest/consumer/metrics.go:32)

### Server: Polestar SMS

SMS agent는 HTTP push와 operation long-poll을 사용한다.

| API/데이터 | 내용 | 저장 |
|---|---|---|
| enrollment | `enr_` enrollment token으로 `agk_` agent key 발급 | PostgreSQL target/identity/collector |
| `POST /metrics` | CPU, memory, swap, load, filesystem, disk, network, process, IIS, RTT | VM direct 또는 Redpanda -> ingest -> VM |
| `POST /processes` | top process snapshot | PostgreSQL `collector_sms_process_snapshot` |
| `POST /docker` | Docker container snapshot + metrics | PostgreSQL `collector_sms_docker_snapshot`, VM |
| `POST /net-sessions` | netstat/session snapshot | PostgreSQL snapshot, ClickHouse `host_connections`, VM count metric |
| `POST /inventory` | OS/CPU/memory/disk/NIC/agent info | PostgreSQL `collector_sms_inventory`, `targets.meta` |
| `POST /api/log-events` | log monitor event | Redpanda `lucida.logs` -> ClickHouse |
| `POST /api/custom-monitor-*` | custom monitor data/event | VM 또는 logs |
| `GET /api/agent/operations/{host}` | 35초 long-poll 작업 수신 | PostgreSQL operations, Redpanda ops |

근거:
[에이전트-컬렉터-통신.md](../../lucida-next/docs/02-아키텍처/에이전트-컬렉터-통신.md:91),
[collector-sms/main.go](../../lucida-next/backend/services/collector-sms/cmd/collector-sms/main.go:148),
[agent_ingest.go](../../lucida-next/backend/services/collector-sms/handler/agent_ingest.go:65),
[pg.go](../../lucida-next/backend/services/collector-sms/store/pg.go:301)

### Database: Polestar DPM

`collector-dpm`은 PostgreSQL 설정과 암호화된 자격증명을 읽고 엔진별 poller로 분기한다.

| 엔진 | 수집 데이터 | 저장 |
|---|---|---|
| PostgreSQL | connection, transaction, cache, WAL, replication, session, top SQL, config | VM, CH DPM tables, PG config/sql metadata |
| Oracle/RAC | session, top SQL, wait, memory, plan, ASM/RAC metrics | VM, CH DPM tables |
| MySQL/MariaDB | session, top SQL, memory, instance delta | VM, CH DPM tables |
| MSSQL | DMV 기반 session/top SQL/memory/db gauge | VM, CH DPM tables |
| Tibero | session, top SQL, memory, plan | VM, CH DPM tables |
| CUBRID | CMS/broker/instance/process/session/volume | VM, CH DPM tables |
| ClickHouse | query event, storage inventory, replica health | VM, CH `dpm_ch_*` |
| Kafka/Redpanda | broker/topic/partition health | VM |

공통 resource attributes:

```json
{
  "lucida.target_id": "018f0000-3333-7222-8333-cccccccc0001",
  "lucida.collector_id": "018f0000-3333-7222-8333-cccccccc1001",
  "lucida.collector_kind": "polestar_dpm",
  "db.system": "postgresql"
}
```

근거:
[collector-dpm/main.go](../../lucida-next/backend/services/collector-dpm/main.go:255),
[poll.go](../../lucida-next/backend/services/collector-dpm/dbpoll/poll.go:95),
[poll.go](../../lucida-next/backend/services/collector-dpm/dbpoll/poll.go:300),
[clickhouse_dpm.go](../../lucida-next/backend/services/ingest/writer/clickhouse_dpm.go:52)

### Network: NMS, Trap, TMS, Syslog

| 수집기 | 데이터 | 저장 |
|---|---|---|
| `collector-nms` | SNMP polling metric, availability, FDB, neighbor, vendor/custom OID, netcli config | VM, PostgreSQL inventory/topology |
| `collector-nms` trap receiver | SNMP trap 원본, varbind, linkUp/linkDown ifIndex | ClickHouse `snmp_traps_local`, Redpanda `lucida.logs` |
| `collector-tms` | NetFlow/sFlow/IPFIX records | ClickHouse `netflow_records_local` |
| `collector-tms` exporter monitor | exporter new/stale/resumed event | Redpanda `lucida.logs` |
| `collector-syslog` | syslog 원본 | ClickHouse `syslog_local`, Redpanda `lucida.logs` |

근거:
[collector-nms/main.go](../../lucida-next/backend/services/collector-nms/main.go:138),
[receiver.go](../../lucida-next/backend/services/collector-nms/trap/receiver.go:277),
[receiver.go](../../lucida-next/backend/services/collector-tms/flow/receiver.go:82),
[logs_producer.go](../../lucida-next/backend/services/collector-tms/flow/logs_producer.go:1),
[schema.go](../../lucida-next/backend/services/collector-syslog/syslog/schema.go:10)

### Kubernetes: KCM

KCM은 stream metric을 `kcm.*` VM metric으로 만들고, Kubernetes resource를 PostgreSQL
리소스 target으로 reconcile한다.

| 데이터 | 저장 |
|---|---|
| cluster/node/pod/container/workload metrics | VictoriaMetrics |
| resource target inventory | PostgreSQL KCM resource tables |
| YAML history | PostgreSQL history table. node는 history 제외 |

대표 metric 예시:

```json
{
  "__name__": "kcm.node.memory.usage",
  "target_id": "018f0000-4444-7222-8333-dddddddd0301",
  "cluster_id": "018f0000-4444-7222-8333-dddddddd0001",
  "node": "worker-01",
  "value": 12884901888,
  "timestamp_ms": 1783382730000
}
```

근거:
[metric.go](../../lucida-next/backend/services/collector-kcm/handler/metric.go:1),
[metric.go](../../lucida-next/backend/services/collector-kcm/handler/metric.go:127),
[resources.go](../../lucida-next/backend/services/collector-kcm/store/resources.go:195)

### Virtualization: VMM/vCenter

VMM은 vCenter/host/VM/datastore/network 단위 metric을 만들고 PostgreSQL resource target을
reconcile한다. poweredOff/template VM은 제한된 snapshot 중심으로 취급된다.

| 데이터 | 저장 |
|---|---|
| vCenter inventory and health | PostgreSQL target/resource, VM metric |
| host CPU/memory/storage/network | VM |
| VM CPU/memory/disk/network | VM |
| datastore/network metrics | VM |

대표 metric 예시:

```json
{
  "__name__": "vmm.vm.cpu.usage",
  "target_id": "018f0000-9999-7222-8333-cccccccc0101",
  "vcenter_target_id": "018f0000-9999-7222-8333-cccccccc0001",
  "vm": "checkout-vm-01",
  "value": 23.4,
  "timestamp_ms": 1783382730000
}
```

근거:
[metrics.go](../../lucida-next/backend/services/collector-vmm/vmware/metrics.go:14),
[metrics.go](../../lucida-next/backend/services/collector-vmm/vmware/metrics.go:71),
[106_vcenter_resources.sql](../../lucida-next/database/ddl/postgres/106_vcenter_resources.sql:179)

### Facility: FMS

FMS는 SNMP/Modbus/BMS 계열 설비 데이터를 `facility.*` 메트릭으로 만든다.

| 프로토콜 | 데이터 | 저장 |
|---|---|---|
| SNMP | UPS, temperature, fan, power, sensor OID | VM |
| Modbus | UPS, leak, temp/humidity, fire, pump, chiller 등 | VM |

대표 metric 예시:

```json
{
  "__name__": "facility.temphum.temperature_celsius",
  "target_id": "018f0000-5555-7222-8333-eeeeeeee0002",
  "collector_kind": "polestar_fms",
  "facility_type": "temphum",
  "sensor": "rack-a-01",
  "value": 24.8,
  "timestamp_ms": 1783382730000
}
```

근거:
[snmp.go](../../lucida-next/backend/services/collector-fms/fms/snmp.go:1),
[modbus.go](../../lucida-next/backend/services/collector-fms/fms/modbus.go:1),
[modbus.go](../../lucida-next/backend/services/collector-fms/fms/modbus.go:119)

### WebURL and WPM

`collector-weburl`은 HTTP/HTTPS synthetic probe metric을 만든다. WPM은 웹 성능 xlog,
profile, interaction, summary, text를 ClickHouse에 직접 쓰고 performance counter를 VM에
remote-write한다.

WebURL 예시:

```json
{
  "__name__": "weburl.ttfb.duration",
  "target_id": "018f0000-8888-7222-8333-bbbbbbbb0001",
  "url": "https://shop.example.com/health",
  "status_code": "200",
  "value": 83,
  "timestamp_ms": 1783382730000
}
```

근거:
[poll.go](../../lucida-next/backend/services/collector-weburl/weburl/poll.go:1),
[poll.go](../../lucida-next/backend/services/collector-weburl/weburl/poll.go:87),
[victoria.go](../../lucida-next/backend/services/collector-wpm/store/victoria.go:16)

### BMC/Redfish

BMC collector는 Redfish로 system/chassis inventory와 sensor 성격 데이터를 수집한다.

예시 inventory:

```json
{
  "target_id": "018f0000-aaaa-7222-8333-dddddddd0001",
  "vendor": "Dell",
  "model": "PowerEdge R760",
  "serial": "ABC1234",
  "firmware": "1.2.3",
  "bmc_address": "10.40.0.11"
}
```

근거:
[poll.go](../../lucida-next/backend/services/collector-bmc/redfish/poll.go:21),
[poll.go](../../lucida-next/backend/services/collector-bmc/redfish/poll.go:73)

## 직접 저장 예외

정본 경로는 Redpanda -> ingest -> 저장소지만, 일부 수집기는 운영상 direct write를
사용한다.

| 수집기 | 직접 저장 | 이유/성격 |
|---|---|---|
| `collector-sms` | VM direct remote-write, PostgreSQL inventory/snapshot, ClickHouse host connections | agent push 처리와 상태 갱신 |
| `collector-wpm` | ClickHouse WPM tables, VM remote-write | WPM 전용 구조화 데이터 |
| `collector-nms` trap | ClickHouse trap insert + logs 발행 | trap 원본 보존 |
| `collector-syslog` | ClickHouse syslog insert + logs 발행 | syslog 원본 보존 |
| DPM ClickHouse writer | `dpm_*` 전용 ClickHouse tables | DB 세션/TopSQL/query event의 구조화 저장 |

근거:
[collector-sms/main.go](../../lucida-next/backend/services/collector-sms/cmd/collector-sms/main.go:148),
[clickhouse.go](../../lucida-next/backend/services/collector-wpm/store/clickhouse.go:1),
[receiver.go](../../lucida-next/backend/services/collector-nms/trap/receiver.go:277),
[schema.go](../../lucida-next/backend/services/collector-syslog/syslog/schema.go:35),
[clickhouse_dpm_ch.go](../../lucida-next/backend/services/ingest/writer/clickhouse_dpm_ch.go:30)

## 포트와 운영 접점

현재 local/all-in-one 기준 주요 접점이다.

| 서비스 | 포트 | 용도 |
|---|---:|---|
| Query API | 18080 | API/query |
| OpAMP | 4320, 14320 | agent control |
| OTLP gRPC | 14317 | OTel ingest |
| OTLP HTTP | 14318 | OTel ingest |
| SNMP trap | 10162 | trap receiver |
| collector-sms | 8090, 8190 | SMS HTTP/mTLS |
| collector-kcm gRPC | 7575 | KCM stream |
| VictoriaMetrics | 18428, 8428 | metrics |
| ClickHouse | 18123, 19000 | HTTP/native |
| PostgreSQL | 15432 | metadata |
| Redpanda | 19092 | Kafka API |
| Valkey | 16379 | cache |

근거:
[아키텍처.md](../../lucida-next/docs/02-아키텍처/아키텍처.md:189)

## 문서 갱신 체크리스트

이 문서는 `lucida-next` 기준의 데이터 수집/저장 계약 문서로 유지한다. 다음 변경이 생기면
갱신한다.

- `database/ddl/postgres` 또는 `database/ddl/clickhouse`에 수집 데이터 테이블/컬럼이
  추가, 삭제, 의미 변경된 경우
- `docs/02-아키텍처/스키마`의 VM/PG/CH 저장 원칙이 바뀐 경우
- `lucida-collector-mapping.csv`의 도메인, service, `collectors.kind`, config 분기가
  바뀐 경우
- collector가 Redpanda 경로에서 direct write로 바뀌거나 반대로 변경된 경우
- ingest의 topic consumer, writer, table 분기가 바뀐 경우
- 보관 정책 TTL이 바뀐 경우

반대로 화면 문구, README 표현, RCA 계열 판단 로직만 바뀐 경우에는 이 문서를 수정하지
않는다.
