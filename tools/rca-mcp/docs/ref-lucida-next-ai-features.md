---
title: Lucida Next AI 기능 참조
status: Active
owner: project
last_reviewed: 2026-07-07
tags:
  - reference
  - lucida-next
  - ai
summary: ../lucida-next의 Chat과 RCA를 제외한 AI 기능, API, 런타임, 판단 로직을 코드 기준으로 정리합니다.
---

# Lucida Next AI 기능 참조

이 문서는 `../lucida-next`의 AI 기능 중 Chat과 RCA 분석 파이프라인을 제외한
기능을 코드 기준으로 정리한다. 단순 화면 목록이 아니라 실제 판단 로직,
fallback, 안전 게이트, 저장소 경계까지 함께 기록한다.

## 범위

포함 범위:

- AI Dashboard 위젯 생성과 resolve
- Now 운영 요약과 Runbook 준비
- Detect 조회 API와 Observer 탐지기
- stream, forecast, log, trace, change, alarm 탐지 로직
- eventcluster의 topology 기반 이벤트 묶음
- incident judge의 incident 승격/드롭 판정
- Memory/KEDB/KDB의 임베딩, 벡터 검색, 큐레이션
- Agents/Manager의 coverage planner, detection policy, agent health, model registry

제외 범위:

- `/ai/chat` 화면
- `backend/services/ai/features/chat/*`
- RCA 실행, 조회, 리포트, 진행 상태
- `backend/services/ai/features/operator/rca/*`
- `frontend/web/src/components/ai/rca/*`

`incident judge`는 RCA가 아니다. judge는 event cluster를 incident로 올릴지
판정하고, RCA는 이미 incident인 건의 원인을 분석한다. 그래서 이 문서는
judge를 포함하고 RCA 분석 로직은 제외한다.

## 전체 구조

AI 화면 모드는
[`AiModeNav.tsx`](../../lucida-next/frontend/web/src/pages/ai/AiModeNav.tsx)를
기준으로 Dashboard, Now, Detect, Memory, Chat, Agents로 나뉜다. 이 문서는
Chat을 제외한 나머지 모드를 다룬다.

주요 백엔드 entrypoint:

| Entry | 코드 | 책임 |
|---|---|---|
| manager | [`cmd/manager/main.go`](../../lucida-next/backend/services/ai/cmd/manager/main.go) | Manager API, Memory/KEDB/KDB API, KEDB curator, coverage monitor, memory cleanup |
| observer | [`cmd/observer/main.go`](../../lucida-next/backend/services/ai/cmd/observer/main.go) | Detect/Now 조회 API, 6종 탐지 worker |
| operator | [`cmd/operator/main.go`](../../lucida-next/backend/services/ai/cmd/operator/main.go) | eventcluster, incident judge, now brief, runbook, execution tracker |
| dashboard | [`features/dashboard/module.go`](../../lucida-next/backend/services/ai/features/dashboard/module.go) | schema catalog, WidgetSpec generation/resolve, dashboard config |
| manager module | [`features/manager/module.go`](../../lucida-next/backend/services/ai/features/manager/module.go) | coverage, agent configs, health, detection policies, model registry |

AI 로직은 LLM 호출만으로 구성되지 않는다.

| 유형 | 사용 위치 |
|---|---|
| 통계/시계열 모델 | stream anomaly, forecast, trace anomaly |
| 로그 템플릿/규칙/조건부 LLM | log anomaly |
| 룰 기반 정규화 | change detect, alarm bridge |
| 그래프 connected component | eventcluster |
| LLM + hard gate | incident judge, now brief, dashboard generation, runbook fallback |
| 벡터 임베딩/검색 | KEDB/KDB, log semantic template grouping |
| 정책/커버리지 룰 | coverage planner, detection policies |

## AI Dashboard

목적은 운영자가 자연어 또는 preset으로 원하는 관점을 입력하면 서버가 안전한
`WidgetSpec[]`을 생성하고, 이를 저장소별 조회 계획으로 변환해 위젯 데이터를
만드는 것이다.

주요 코드:

- [`generation_v2_service.go`](../../lucida-next/backend/services/ai/features/dashboard/service/generation_v2_service.go)
- [`generation_stream_service.go`](../../lucida-next/backend/services/ai/features/dashboard/service/generation_stream_service.go)
- [`resolve_service.go`](../../lucida-next/backend/services/ai/features/dashboard/service/resolve_service.go)
- [`dashboardspec`](../../lucida-next/backend/services/ai/shared/dashboardspec)

현재 흐름:

1. FE가 prompt 또는 preset을 보낸다.
2. v3 generation은 `SchemaCatalogForPermissions`로 사용자 권한에 맞는 catalog를 만든다.
3. LLM 호출은 structured output 방식이다. 서버가 JSON schema를 넘기고
   `WidgetSpec[]` 형태를 강제한다.
4. LLM 응답은 registry로 검증한다.
5. JSON parsing 또는 spec 검증 실패 시 self-repair를 1회 수행한다.
6. 그래도 유효 spec이 없으면 fallback으로 전환한다.
7. resolve 단계는 spec을 다시 validate한 뒤 `Translate`로 `QueryPlan`을 만든다.
8. QueryPlan kind에 따라 PostgreSQL, ClickHouse, VictoriaMetrics, Qdrant executor를 호출한다.
9. rows를 `stat`, `bars`, `timeseries`, `items` 등 widget data로 변환한다.

안전 경계:

- Dashboard는 raw query agent가 아니다.
- LLM이 raw SQL/MetricsQL을 만들더라도 Dashboard 서버는 실행하지 않는다.
- 실행 쿼리는 `dashboardspec.Registry.Translate`가 만든 QueryPlan에서만 나온다.
- `dataset`, `metric`, `field`, `agg`, `viz`는 catalog whitelist를 통과해야 한다.
- 권한 없는 source의 WidgetSpec은 `filterSpecsByPermissions`에서 제거된다.

주요 API:

| API | 설명 |
|---|---|
| `GET /dashboard/schema-catalog` | 허용 dataset, metric, field, viz catalog |
| `POST /dashboard/generate-v3` | catalog 기반 WidgetSpec 생성 |
| `POST /dashboard/generate-v3-stream` | streaming generation |
| `POST /dashboard/widgets/resolve` | WidgetSpec을 QueryPlan으로 변환해 데이터 조회 |
| `GET/PUT /dashboards/{id}` | dashboard config 저장/복원 |

## Detect와 Observer 탐지기

Observer entrypoint는 6종 탐지 worker를 실행한다. 코드 기준은
[`cmd/observer/main.go`](../../lucida-next/backend/services/ai/cmd/observer/main.go)다.

실행되는 탐지 축:

- stream-anomaly
- trace-anomaly
- forecast
- log-anomaly
- change-detect
- alarm-bridge

### Stream anomaly

주요 코드:

- [`detector.go`](../../lucida-next/backend/services/ai/features/observer/streamanomaly/service/detector.go)
- [`params.go`](../../lucida-next/backend/services/ai/features/observer/streamanomaly/service/params.go)
- [`runner`](../../lucida-next/backend/services/ai/features/observer/streamanomaly/runner)

현재 로직:

- 시리즈별 인메모리 상태를 유지한다.
- baseline은 Holt/EWMA 계열 level/trend와 rolling band를 사용한다.
- 요일×시간 168 bucket, 시간대 24 bucket으로 반복 패턴을 분리한다.
- 이상 후보는 주로 `level_shift`로 생성된다.
- 과거 설계에 있던 단변량 IForest/HSTrees context anomaly는 기본 비활성화되어 있다.
- `params.go`는 context anomaly flag를 운영 검증 전까지 off로 sanitize한다.
- band half-width는 threshold×std, tail q99, materiality floor를 결합한다.
- near-zero, low-count, bursty series의 과탐을 줄이기 위해 tiny std floor,
  Poisson counting noise floor, spike regime, log band, re-anchor를 둔다.
- severity는 단순 절대 z값이 아니라 발행 경계 대비 초과 비율로 매핑한다.
- 에피소드가 오래 지속되면 re-anchor로 새 정상 상태를 받아들여 close/reopen churn을 줄인다.

핵심 게이트:

| 게이트 | 목적 |
|---|---|
| materiality floor | 통계적으로만 튄 미세 변화 억제 |
| tiny std floor | 분산 0에 가까운 gauge의 z 폭주 방지 |
| count noise floor | near-zero 카운트류 과탐 방지 |
| direction band | lower-better/higher-better metric 방향성 반영 |
| re-anchor | 장기 레벨 변화가 새 정상일 때 baseline 재정착 |
| coverage gate | `ai_coverages` 대상만 runner가 처리 |

산출 이벤트는 `metric_anomaly`다. evidence에는 observed value, baseline/band,
z score, method, rolling band가 들어간다.

### Forecast

주요 코드:

- [`forecast.go`](../../lucida-next/backend/services/ai/features/observer/forecast/service/forecast.go)
- [`ttm.go`](../../lucida-next/backend/services/ai/features/observer/forecast/service/ttm.go)
- [`holtwinters.go`](../../lucida-next/backend/services/ai/features/observer/forecast/service/holtwinters.go)
- [`intermittent.go`](../../lucida-next/backend/services/ai/features/observer/forecast/service/intermittent.go)
- [`metric_registry.go`](../../lucida-next/backend/services/ai/features/observer/forecast/repository/metric_registry.go)

현재 로직:

- 기본 champion은 TTM zero-shot ONNX다.
- TTM이 불가능하거나 variant가 강제되면 Holt-Winters, TSB/Croston 계열 fallback/challenger를 쓴다.
- `resolvePolicy`가 metric registry와 name metadata로 안전한 forecast policy를 만든다.
- unknown metric은 임의 threshold로 발화하지 않고 `ErrUnsupportedMetric`으로 skip한다.
- `%`/ratio 정책은 raw counter/bytes와 비교되지 않도록 sanity ceiling을 둔다.
- 현재값이 이미 임계에 닿은 경우 forecast 이벤트를 발화하지 않는다.
- lag-1 autocorrelation이 유의하게 음수인 평균회귀 톱니 series는 anti-persistent로 skip한다.
- 예측선이 임계와 교차하는 첫 시점을 point 사이 선형 보간으로 ETA 계산한다.
- confidence band는 evidence이고, 발행 조건은 점추정 예측선의 임계 교차다.
- `%` 정책의 예측값과 band는 [0,100]으로 clamp한다.

주요 skip/error:

| 코드 | 의미 |
|---|---|
| `ErrUnsupportedMetric` | 안전한 policy를 만들 수 없는 metric |
| `ErrUnitMismatch` | `%`/ratio 임계와 raw scale이 맞지 않음 |
| `ErrAntiPersistentSeries` | 반지속 series라 trend forecast 부적합 |
| `ErrInsufficientData` | 학습/예측 데이터 부족 |

산출 이벤트는 `forecast_alert`다.

### Log anomaly

주요 코드:

- [`detector.go`](../../lucida-next/backend/services/ai/features/observer/loganomaly/service/detector.go)
- [`drain.go`](../../lucida-next/backend/services/ai/features/observer/loganomaly/service/drain.go)
- [`eventtime_rate.go`](../../lucida-next/backend/services/ai/features/observer/loganomaly/service/eventtime_rate.go)
- [`tier3.go`](../../lucida-next/backend/services/ai/features/observer/loganomaly/service/tier3.go)
- [`tier3async.go`](../../lucida-next/backend/services/ai/features/observer/loganomaly/service/tier3async.go)
- [`semanticcache.go`](../../lucida-next/backend/services/ai/features/observer/loganomaly/service/semanticcache.go)

현재 로직은 3-tier pipeline이다.

Tier 1은 룰이다.

- whitelist/probe류는 drop한다.
- blacklist, panic, OOM, fatal 등은 즉시 이벤트다.
- ERROR 빈도 기반 이벤트를 만든다.
- blacklist는 기본 hard-fire다.
- `BlacklistToTier3`가 켜지고 Tier3가 가능할 때만 LLM 정밀판정으로 라우팅한다.
- Tier3가 불가능하면 안전하게 hard-fire로 fallback한다.

Tier 2는 Drain + event-time rate다.

- 로그 body를 Drain template으로 일반화한다.
- ERROR 이상의 신규 template은 `novel_template` 후보/이벤트가 될 수 있다.
- WARN/INFO 신규 template은 기본적으로 바로 이벤트가 아니라 Tier3 후보 성격이다.
- 기존 template 급증은 고정 window count가 아니라 event timestamp 기반 interval/rate로 본다.
- short/long EWMA rate, ADWIN-lite drift, Gamma-Poisson posterior predictive tail test를 통과해야 `rate_spike`가 된다.
- 경계는 `target + log_stream + template` 단위로 분리된다.

Tier 3는 조건부 LLM이다.

- 모든 로그를 LLM에 보내지 않는다.
- exact cache, semantic cache, deterministic sampling, 조건부 escalation을 통과한 경우만 호출한다.
- 기본 모델명은 params 기준 `gemma4-26b-a4b-it-fp8`이다.
- sample rate 기본은 10%다.
- async Tier3가 있으면 LLM 호출은 consume path 밖의 worker pool에서 수행된다.
- 첫 등장은 Stage B/Tier2 결과로만 처리하고, LLM 판정은 cache에 쌓여 재등장부터 반영된다.
- Qdrant semantic template index는 라벨 없는 grouping/history metadata 용도이며 정상/비정상 판정권은 없다.

산출 이벤트는 `log_anomaly`다. 주요 reason은 `blacklist`, `freq`,
`novel_template`, `rate_spike`, `llm_classified`, `periodic_absent`,
`template_profile_shift`다.

### Trace anomaly

주요 코드:

- [`detector.go`](../../lucida-next/backend/services/ai/features/observer/traceanomaly/service/detector.go)
- [`distance.go`](../../lucida-next/backend/services/ai/features/observer/traceanomaly/service/distance.go)
- [`chain.go`](../../lucida-next/backend/services/ai/features/observer/traceanomaly/service/chain.go)
- [`baseline.go`](../../lucida-next/backend/services/ai/features/observer/traceanomaly/service/baseline.go)
- [`online.go`](../../lucida-next/backend/services/ai/features/observer/traceanomaly/service/online.go)

현재 로직:

- OTel raw span 자체의 패턴 이상을 본다.
- p99/error_rate 같은 단일 수치는 stream anomaly 영역이고, trace anomaly는
  span duration 분포, error chain, novel path를 본다.
- analysis unit 기본은 route다.
- 현재 window span을 route/service/operation 단위로 그룹핑한다.
- baseline duration distribution과 현재 distribution의 거리를 계산한다.
- 지원 거리에는 KL, Wasserstein, KS 등이 있다.
- online ADWIN 설정은 full-resolution path에서는 Wasserstein으로 대체된다.
- tail-sampling 보정을 위해 관측 span 수 기준을 sampling rate로 조정한다.
- baseline 또는 현재 표본이 부족하면 skip한다.
- distribution shift는 latency가 느려진 방향일 때만 이벤트화한다.
- error-chain은 parent-child 에러 전파를 본다.
- novel-path는 이전에 없던 호출 경로를 본다.
- 같은 route에서 여러 reason이 동시에 나오면 하나로 합치고 error_chain을 primary로 우선한다.

산출 이벤트는 `trace_anomaly`다. reason은 `distribution_shift`,
`error_chain`, `novel_path`다.

### Change detect

주요 코드:

- [`detect.go`](../../lucida-next/backend/services/ai/features/observer/changedetect/service/detect.go)
- [`params.go`](../../lucida-next/backend/services/ai/features/observer/changedetect/service/params.go)

현재 로직:

- ML/LLM이 아니라 룰 기반 변경 이벤트 정규화다.
- 입력은 patch, config, inventory, policy 변경 메시지다.
- class toggle이 꺼져 있으면 무시한다.
- event id는 source, target_ref, occurred_at 기반 안정 ID다.
- seen map으로 중복 webhook을 흡수한다.
- target별 최근 변경을 grace period 동안 influence tag로 보관한다.
- influence tag는 이후 normalizer/judge 상관에 쓰일 수 있다.
- dedup map은 TTL과 max entries로 정리된다.

산출 이벤트는 `change`이고 severity 기본값은 `info`다.

### Alarm bridge

주요 코드:

- [`normalize.go`](../../lucida-next/backend/services/ai/features/observer/alarmbridge/service/normalize.go)
- [`rules.go`](../../lucida-next/backend/services/ai/features/observer/alarmbridge/service/rules.go)
- [`gradefilter.go`](../../lucida-next/backend/services/ai/features/observer/alarmbridge/service/gradefilter.go)
- [`policygate.go`](../../lucida-next/backend/services/ai/features/observer/alarmbridge/service/policygate.go)

현재 로직:

- 플랫폼 알람 또는 coverage event를 `external_alarm` 이벤트로 정규화한다.
- 플랫폼 알람 payload는 backend alarm notifier의 PascalCase JSON 계약을 직접 매핑한다.
- producer가 event_id를 주지 않아 platform alarm은 발생마다 UUID를 만든다.
- coverage event는 source/ext id 기반으로 안정 ID를 만든다.
- severity/status/original severity를 attributes에 보존한다.
- 원본 알람 severity와 target grade/policy gate로 통과 여부를 판단한다.
- firing일 때만 value를 넣고 resolved value=0을 실제 측정값으로 오인하지 않게 한다.

산출 이벤트는 `external_alarm`이다.

## EventCluster

주요 코드와 설계:

- [`DESIGN.md`](../../lucida-next/backend/services/ai/features/operator/eventcluster/DESIGN.md)
- [`service`](../../lucida-next/backend/services/ai/features/operator/eventcluster/service)
- [`runner`](../../lucida-next/backend/services/ai/features/operator/eventcluster/runner)

핵심 결정:

- 점수 기반 clustering이 아니다.
- topology edge policy로 만든 connected component를 시간 가드 안에서 boolean union한다.
- eventcluster는 topology map을 만들지 않고 `/api/v1/topology/graph`를 on-demand로 읽는다.

흐름:

1. detector가 ClickHouse `lucida_events_local`에 event를 저장한다.
2. 저장 성공 후 `event.saved`에 event_id를 publish한다.
3. eventcluster worker가 event_id batch를 받아 ClickHouse에서 event row를 읽는다.
4. Redpanda를 사용할 수 없으면 ClickHouse polling fallback으로 `cluster_id` 없는 event를 조회한다.
5. batch 기준 `asOf=max(event.occurred_at)`로 topology graph를 조회한다.
6. event target을 canonical topology node로 resolve한다.
7. union 허용 edge만 connected component를 만든다.
8. 같은 component이고 edge kind별 time window 안이면 같은 cluster로 묶는다.
9. PostgreSQL `event_clusters`에 저장하고 ClickHouse event `cluster_id`를 태깅한다.
10. stale cluster를 close하고 lifecycle outbox를 만든다.
11. closed cluster는 incident judge의 입력이 된다.

edge 정책:

| Edge | 성격 |
|---|---|
| `apm_call`, `apm_db` | union 가능 |
| `host_socket` | 조건부 union |
| `k8s_service_pod`, `k8s_workload_pod` | guarded union |
| `apm_host`, `network_link`, `service_map`, `collector` | 기본 context only |
| `identity` | resolver only |

안전 장치:

- freshness guard
- fanout guard
- max events per cluster
- storm guard
- close_after
- merge_wait 동안 closed cluster 재비교 후 finalized

## Incident Judge

주요 코드:

- [`judge.go`](../../lucida-next/backend/services/ai/features/operator/incident/judge/judge.go)
- [`llm.go`](../../lucida-next/backend/services/ai/features/operator/incident/judge/llm.go)
- [`judge_repo.go`](../../lucida-next/backend/services/ai/features/operator/incident/repository/judge_repo.go)
- [`judge_runner.go`](../../lucida-next/backend/services/ai/features/operator/incident/runner/judge_runner.go)

판정 순서:

1. input hash 생성
2. cheap gate
3. floor signal 확인
4. LLM pass1 triage 또는 conservative fallback
5. post-processing
6. promote일 때만 pass2 title narration
7. repository가 promote/drop/hold를 PostgreSQL에 기록

cheap gate:

- 명백한 비격상은 LLM 없이 drop한다.
- 단, floor signal이 있거나 기존 활성 incident 재판정이면 cheap gate를 건너뛴다.

floor signal:

- 가용성 영향, SLA, 보안, 절대값 위험 등 코드가 중대하다고 보는 신호다.
- floor가 있으면 LLM이 drop/hold를 내도 promote로 뒤집고 최소 critical을 보장한다.

LLM 사용:

- pass1은 triage JSON을 structured output으로 받는다.
- LLM이 nil이면 fallback으로 간다.
- 4xx/bad request는 compact 재시도 후 fallback한다.
- parse 실패는 fallback한다.
- 5xx, timeout, network 오류는 floor가 없으면 `ErrLLMDeferred`로 보류한다.
- promote 확정 건에만 pass2 title narration을 수행한다.

재판정 원칙:

- 활성 incident는 자동 drop/hold 하지 않는다.
- severity는 monotonic하게 내리지 않는다.
- 종결은 사람 또는 명시적 close 흐름에서 처리한다.

보수 fallback:

- floor가 있으면 promote/critical
- floor가 없으면 drop
- 과거 "critical anomaly면 룰로 promote" 방식은 폐지되어 있다.

## Now와 Runbook

### Now brief

주요 코드:

- [`nowbrief/module.go`](../../lucida-next/backend/services/ai/features/operator/nowbrief/module.go)
- [`nowbrief/service`](../../lucida-next/backend/services/ai/features/operator/nowbrief/service)

동작:

- active incident와 forecast를 읽어 운영 상태 요약을 만든다.
- LLM이 있으면 brief를 생성한다.
- 입력 또는 LLM 실패는 화면 전체를 막지 않고 fallback/degraded로 처리한다.
- bounded wait와 cache/max age 설정이 있다.

### Runbook

주요 코드:

- [`runner.go`](../../lucida-next/backend/services/ai/features/operator/runbook/service/runner.go)
- [`matcher.go`](../../lucida-next/backend/services/ai/features/operator/runbook/service/matcher.go)
- [`generator.go`](../../lucida-next/backend/services/ai/features/operator/runbook/service/generator.go)
- [`execution`](../../lucida-next/backend/services/ai/features/operator/execution)

현재 로직:

1. runbook 없는 incident를 가져온다.
2. incident당 runbook이 이미 있으면 skip한다.
3. 최근 manual 결과가 cooldown 안이면 LLM/매칭도 하지 않고 skip한다.
4. matcher가 catalog 후보를 먼저 찾는다.
5. catalog 매칭이 없으면 KEDB/LLM fallback이 가능하다.
6. 생성/매칭 결과가 manual이면 runbook row를 만들지 않고 cooldown에만 기록한다.
7. runbook을 만들 때는 `incident_runbooks` snapshot으로 저장한다.
8. 승인 전 step은 execution worker가 실행하지 않는다.

안전 원칙:

- LLM 생성 runbook도 `approval=pending`, `auto_exec=false`가 기본 안전선이다.
- 실행 허가는 `POST /runbooks/{id}/steps/{n}/approve`가 유일한 경로다.
- manual case에 rejected row를 남기지 않는다. LLM 복구나 설정 변경 후 재시도 가능성을 보존하기 위해 in-memory cooldown만 쓴다.

## Memory, KEDB, KDB

### KEDB incident vector

주요 코드:

- [`synthesize.go`](../../lucida-next/backend/services/ai/features/memory/kedb/service/synthesize.go)
- [`runner.go`](../../lucida-next/backend/services/ai/features/memory/kedb/service/runner.go)
- [`curator.go`](../../lucida-next/backend/services/ai/features/memory/kedb/service/curator.go)

현재 로직:

- incident close/feedback 흐름에서 `kedb_pending`이 생긴다.
- curator가 pending incident를 읽어 KEDB entry를 합성한다.
- vector embedding에 쓰는 텍스트는 `IncidentSignatureText`다.
- RCA 전문, runbook 전문, operator feedback 전문은 vector text에 넣지 않는다.
- 상세 정보는 PostgreSQL detail/hydration path에 남긴다.
- point id는 incident UUID 그대로 사용한다. 재적재는 duplicate가 아니라 upsert다.
- payload는 redaction을 거쳐 Qdrant에 들어간다.

`IncidentSignatureText` 구성:

- incident_id
- title
- severity
- domain
- service
- summary, title과 다를 때
- signals

실패 처리:

| 실패 | 동작 |
|---|---|
| embedder nil/down | upsert 안 함, curated 표시 안 함, retry 기록 |
| Qdrant nil/down | upsert 안 함, retry 기록 |
| publisher 실패 | Qdrant/curated는 rollback하지 않음 |
| no feedback | closed incident는 자동 ingest 가능 |

### KDB 운영문서

주요 코드:

- [`service.go`](../../lucida-next/backend/services/ai/features/memory/kdb/service/service.go)
- [`cmd/manager/main.go`](../../lucida-next/backend/services/ai/cmd/manager/main.go)

현재 로직:

- 파일 업로드를 document와 chunk로 파싱한다.
- OCR/HWP extractor를 설정할 수 있다.
- 표/이미지는 media interpreter가 운영문서 검색에 유용한 설명으로 요약한다.
- chunk별 `EmbeddingText`를 embedding한다.
- vector point는 chunk fingerprint와 payload를 포함한다.
- ingest 실패 시 새 vector rollback, stale vector 삭제 같은 정합 처리를 한다.
- 검색은 vector search와 keyword search를 결합한다.
- hybrid 모드는 reciprocal rank fusion 성격의 boost와 lexical exact match boost를 사용한다.
- 낮은 품질 chunk, placeholder, 중복 chunk를 metadata로 분류하고 검색 품질에 반영한다.

검색 필터:

- `source_key`
- `corpus_key`
- `product_area`
- `visibility`
- `document_id`

## Agents와 Manager

### Coverage planner

주요 코드:

- [`planner.go`](../../lucida-next/backend/services/ai/features/manager/service/coverageplanner/planner.go)
- [`runner.go`](../../lucida-next/backend/services/ai/features/manager/runner/runner.go)

현재 로직은 LLM 없는 룰 기반이다.

처리:

1. 모든 target을 순회한다.
2. target의 enabled collector와 등급을 읽는다.
3. 등급 미지정은 order 2로 fallback한다.
4. `auto_scope`에 따라 tier를 정한다.
5. `collection_common_policies`에서 해당 tier/kind의 metric을 가져온다.
6. 실제 신호가 도착한 metric만 매핑한다.
7. 기존 mapping이 있으면 건드리지 않는다. `operator_override`를 보존한다.
8. `template_auto_apply=true`일 때만 `source=template`, `enabled=true`로 upsert한다.

`auto_scope`:

| 값 | 동작 |
|---|---|
| `golden` 또는 미지정 | tier-lite, stream-anomaly only |
| `grade-tier` | 1등급=전체+forecast, 2등급=핵심+표준, 3등급=golden |
| `all` | 전체 1등급 지표 + forecast |

### Coverage monitor

주요 코드:

- [`monitor.go`](../../lucida-next/backend/services/ai/features/manager/service/coveragemonitor/monitor.go)
- [`store_pg.go`](../../lucida-next/backend/services/ai/features/manager/service/coveragemonitor/store_pg.go)

현재 로직:

- enabled coverage의 기대 signal이 timeout 동안 오지 않으면 `signal_lost` 성격의 coverage event를 만든다.
- VictoriaMetrics metric signal은 batch query로 조회한다.
- log/trace signal은 ClickHouse `otel_*_local` last-at로 본다.
- target 변화도 감시한다.
- agent heartbeat freshness를 보고 `agent_up` self metric을 emit한다.
- `agent_configs.enabled=false`면 cycle skip한다.

### Agent health

주요 코드:

- [`agent_health.go`](../../lucida-next/backend/services/ai/features/manager/service/agent_health.go)

상태 계산:

| 조건 | 상태 |
|---|---|
| config disabled | `disabled` |
| runtime reader 없음 | `configured` |
| self metric store 오류 | reason만 추가 |
| sample up <= 0 | `down` |
| last_seen 15분 초과 | `down` |
| last_seen 5분 초과 | `stale` |
| fresh sample | `healthy` |
| required data source missing/disabled | `degraded` |

`params.enabled`는 health gate가 아니다. `agent_configs.enabled` 컬럼이
single source of truth다.

### Detection policies

주요 코드:

- [`detection_policies.go`](../../lucida-next/backend/services/ai/features/manager/controller/detection_policies.go)
- [`store/detection_policies.go`](../../lucida-next/backend/services/ai/features/manager/store/detection_policies.go)
- [`detectionPolicies.ts`](../../lucida-next/frontend/web/src/api/ai/detectionPolicies.ts)

정책 종류:

| policy_kind | 의미 |
|---|---|
| `item` | metric/log/trace 항목 정책 |
| `severity` | alarm-bridge 등급 정책 |
| `change_class` | change-detect 변경 클래스 정책 |

배포:

- include/exclude target selector를 받아 구체 target으로 전개한다.
- `ai_coverages`를 upsert한다.
- `operator_override`는 보존한다.
- deploy result에는 `deployed`, `skipped_override`, `removed`,
  `skipped_no_metric` 등이 포함된다.

### Model registry

주요 코드:

- [`models.go`](../../lucida-next/backend/services/ai/features/manager/controller/models.go)
- [`model_registry.go`](../../lucida-next/backend/services/ai/features/manager/store/model_registry.go)

API는 존재하지만 제품 문서상 Agents 모델 탭은 Phase 2/deprecated 성격이다.
이 문서에서는 운영 기능으로 존재하는 API만 기록한다.

## 주요 API 맵

| 영역 | API |
|---|---|
| Dashboard | `/dashboard/schema-catalog`, `/dashboard/generate-v3`, `/dashboard/generate-v3-stream`, `/dashboard/widgets/resolve`, `/dashboards/{id}` |
| Now/Detect | `/now-brief`, `/now-health`, `/incidents`, `/incidents/{id}`, `/events`, `/events/{id}`, `/clusters`, `/forecasts`, `/metric-series` |
| Runbook | `/automation/runbooks`, `/automation/runbooks/{id}`, `/automation/runbooks/{id}/match-preview`, `/runbooks/{id}/approve`, `/runbooks/{id}/steps/{n}/approve` |
| Memory/KEDB | `/kedb`, `/kedb/{id}`, `/ai/kedb-match`, `/kedb-pending`, `/kedb-pending/{id}/retry` |
| KDB | `/kdb/uploads`, `/kdb/documents`, `/kdb/documents/{id}/file`, `/kdb/search` |
| Agents/Manager | `/ai-coverages`, `/agent-configs`, `/agent-health`, `/ai-detection-policies`, `/models`, `/model-bindings` |

기계 가독 API 계약은
[`ai-api-v1.yaml`](../../lucida-next/api/openapi/ai-api-v1.yaml)을 우선한다.

## 코드 확인 체크리스트

이 문서 작성 시 확인한 주요 코드 앵커:

| 기능 | 확인 파일 |
|---|---|
| Dashboard generation/resolve | `features/dashboard/service/generation_v2_service.go`, `resolve_service.go`, `module.go` |
| Stream anomaly | `features/observer/streamanomaly/service/detector.go`, `params.go` |
| Forecast | `features/observer/forecast/service/forecast.go`, `policy.go`, `ttm.go`, `holtwinters.go`, `intermittent.go` |
| Log anomaly | `features/observer/loganomaly/service/detector.go`, `tier3.go`, `tier3async.go`, `semanticcache.go`, `chat_classifier.go`, `params.go` |
| Trace anomaly | `features/observer/traceanomaly/service/detector.go`, `distance.go`, `chain.go` |
| Change detect | `features/observer/changedetect/service/detect.go` |
| Alarm bridge | `features/observer/alarmbridge/service/normalize.go`, `gradefilter.go`, `policygate.go` |
| Event cluster | `features/operator/eventcluster/DESIGN.md`, `service/*`, `runner/*` |
| Incident judge | `features/operator/incident/judge/judge.go`, `llm.go`, `repository/judge_repo.go` |
| Now brief | `features/operator/nowbrief/module.go`, `service/*` |
| Runbook | `features/operator/runbook/service/runner.go`, `matcher.go`, `generator.go` |
| KEDB | `features/memory/kedb/service/synthesize.go`, `runner.go`, `curator.go` |
| KDB | `features/memory/kdb/service/service.go` |
| Coverage planner/monitor | `features/manager/service/coverageplanner/planner.go`, `coveragemonitor/monitor.go`, `runner/runner.go` |
| Agent health | `features/manager/service/agent_health.go` |

## 주의사항

- 이 문서는 `../lucida-next`의 현재 코드 흐름을 참조한다.
- 일부 `../lucida-next/docs/AI/*` 설계 문서에는 목표 설계나 과거 상태가 남아 있을 수 있다.
- 구현 상태를 판단할 때는 코드와 테스트, OpenAPI 계약을 우선한다.
- Dashboard는 LLM 기반이지만 raw query agent가 아니라 catalog-backed WidgetSpec 생성기다.
- KEDB vector는 incident signature 중심이다. RCA/feedback/runbook 전문을 vector text에 넣지 않는 현재 코드 결정은 검색 오염을 줄이기 위한 경계다.
