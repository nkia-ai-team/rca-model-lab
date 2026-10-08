---
title: 도구 표면 재설계 — 하향식 설계 작업 문서
status: Active
owner: project
last_reviewed: 2026-07-28
tags:
  - rca
  - tools
  - design
summary: 재설계 D9·D10을 뼈대로 조사자의 진단 질문 목록에서 출발해, 각 질문을 lucida-next 실제 수집 스키마와 정합성을 따지며 도구로 묶어가는 하향식 설계. 표면 초판 확정(2026-07-24)으로 spec-agent-tools.md를 대체한 도구 표면의 정본이다. 도구별 구현 설계는 구현 순서대로 진행 중.
---

# 도구 표면 재설계 — 하향식 설계 작업 문서

## 목적과 방식

기존 도구 표면(18종, [spec-agent-tools.md](spec-agent-tools.md))의 결함 목록에서
출발하는 상향식 패치는 테스트베드 케이스 2건에 과적합할 위험이 있어 기각했다
(2026-07-23 합의). 대신 **하향식**으로 간다:

1. **요구에서 출발** — 재설계 결정([adr-rca-agent-redesign.md](adr-rca-agent-redesign.md))
   D9(조사자 하나 + 상황별 메뉴)·D10(코드=눈, LLM=머리)을 뼈대로, 조사자가 걸음마다
   던질 진단 질문 목록을 세운다. (이 문서 §1 — 완료)
2. **질문 ↔ 원천 짝짓기** — 질문 하나하나를 lucida-next의 실제 수집 스키마와
   대조한다: 답하는 질문 → 원천(테이블/쿼리) → 식별자 정합성(무엇으로 겨누고,
   응답의 id가 다음 도구의 인자로 이어지는가). 연결성 문제(topology 간극 —
   [backlog-tool-legibility.md](backlog-tool-legibility.md) 참조)를 설계 시점에
   잡기 위한 단계다.
3. **도구로 묶기** — 질문들을 D9의 메뉴 구도(일상 공구 + 도메인별 상황 메뉴,
   어느 순간에도 8~10개)로 접는다.
4. **검수** — 다 그린 뒤 프론티어 궤적 2건(F04-R·F01-R)과 청구서 T1~T8로
   "이 표면이었으면 어디서 막혔을까"를 대조한다. 청구서는 설계 의제가 아니라
   검수 문제지다.

## 1. 조사자의 진단 질문 목록 (v2 — 2026-07-23 합의)

v1(세션 초안)을 Codex 교차검토(SRE/DBA/NetOps 관행 근거)로 보강했다. 주요 반영:
자리매김(영향) 동작 신설, 차원 쪼개기를 '보기'로 이동, 이벤트/알람과 변경 분리,
현재 유효 한도 질문 신설, '확정'을 '지목'으로 낮추고 인과 고리 검증을 분리.

**일곱 동작은 순서가 아니라 분류다.** 실제 조사는 보기→잇기→보기→비교처럼
왕복한다(D6의 걸음 구조는 유지 — 한 걸음 = 질문 하나).

### Ⅰ. 자리매김 — 조사를 증상에 정박시킨다

1. 증상은 무엇이고, 누가 얼마나 영향받고 있으며, **지금도 계속되는가?**

### Ⅱ. 찾기 — 조사 공간에서 대상을 손에 쥔다

2. 이 환경에 어떤 대상들이 있나? (유형·도메인·이름으로 열거/검색 — 범위는
   관제 인벤토리 전체, 인시던트 topology에 갇히지 않는다)
3. 이 대상은 무엇인가? (정체 — 이름·유형·주소·소속)
4. 이 대상의 **현재 유효한 설정·한도**는? (max_connections, K8s limits, 큐 크기,
   replica 수 — 11번 양적 대조의 전제)

### Ⅲ. 보기 — 한 대상의 상태를 읽는다

5. 이 대상, 지금 무엇이 이상한가? (지표 발굴 — 뭐가 있는지 모르는 상태에서
   이상 후보를 받는다)
6. 이 지표, 평소 대비 어떤가? (시계열 — 언제부터, 얼마나)
7. 이 이상은 **어느 차원에 집중됐나?** (프로세스/엔드포인트/pod 분해 — 같은 대상
   안의 국소화. 결과가 다른 대상을 가리키는 순간부터가 '잇기'다)
8. 로그에 무엇이 나타났나?
9. 어떤 이벤트·알람이 있었나? (알람은 2차 파생 신호 — 로그·원본 이벤트와 증거
   무게를 구분한다)
10. **무엇이 바뀌었나?** (배포·설정 변경 — 원인 선행사건 후보라 9와 무게가 다르다)

### Ⅳ. 비교 — 이상을 맥락에 놓는다

계산은 코드가 한다(D10 — 조사자는 raw 숫자를 눈대중하지 않는다). 단 비교쌍
선택은 조사자의 몫이다.

11. 이 수치가 물리적으로 말이 되나? (rate vs 한도·capacity — 4번이 읽어온 유효
    한도가 분모)
12. 혼자 이상인가, 또래도 같이 이상한가?
13. 둘 중 누가 먼저 움직였나? (선후·onset — 비교할 두 신호는 조사자가 고르고,
    onset·오차범위 계산은 코드가 한다)

### Ⅴ. 잇기 — 대상에서 대상으로 넘어간다

14. 이 대상은 누구와 연결돼 있나? (지도가 아는 의존 — topology)
15. 실제로는 누구와 통신하나? (지도가 모르는 의존 — 소켓 관측.
    topology 간극의 우회로)

### Ⅵ. 지목 — 최소 단위까지 내려간다 (도메인별 — 상황 메뉴 자리)

16. (DB) 누가 누구를 블로킹하나 / 어느 쿼리가 느린가 / 커넥션이 포화인가 —
    서로 다른 갈림길이므로 옴니버스로 묶지 않는다
17. (서버) 어느 프로세스가 먹나 · (K8s) pod에 무슨 일이 · (네트워크) 장비가
    직접 신호를 보냈나 … 도메인마다 최소 지목 단위까지 내려가는 질문

### Ⅶ. 검증 — 지목이 확정이 되려면 (D7·D8의 질문 형태)

18. 지목한 원인에서 증상까지 **중간 고리가 실제로 관측됐는가?** (wait·retry·
    backlog가 있었나, 원인이 먼저였나, 회복도 같이 움직였나)

### 목록에 넣지 않은 것 (성격이 다름)

- **"이 값(또는 '없음')을 믿어도 되는가?"** — 평시엔 모든 도구 응답에 자동
  부착되는 품질 속성(관측 구간·표본 수·수집기 상태·measured-zero/no-sample
  구분). 수상할 때만 독립 질문으로 승격. → 응답 봉투 설계의 몫.
- **"어디를 아직 안 봤나?"** — 질문이 아니라 보드가 늘 보여주는 상태
  (D5 discovery frontier). → 보드 설계의 몫.

## 2. 질문 ↔ lucida-next 원천 짝짓기 (2026-07-23 실측)

조사 방법: 병렬 3갈래로 case-f08-h 복원 DB(PG/CH/VM) 읽기전용 실조회 + 현
도구 코드·DDL 대조 + 119 실측 참조(인벤토리 문서). **캡처 두께 주의** — 이
복원 세트에서 `dpm_session`/`dpm_topsql`/`process_snapshot`(테이블 자체는
`collector_sms_*` 계열)/`kcm_events`/`snmp_traps`/`netflow`/wpm은 0행이라,
해당 항목은 DDL과 119 참조로 보강했고 아래에 명시한다.

### 2.0 관통 발견 — 도구 설계에 공통으로 들어가는 사실 5건

1. **한도(capacity)의 정본 원천은 설정 스냅샷이 아니라 한도성 gauge 메트릭이다.**
   `kcm.container.cpu_limit`(케이스 창 832 series)·`kcm.pod.mem_limit`(715) 등
   K8s 전 계층 limit/request, `dpm.*.session.max_session`, `apm.jdbc.max_connections`,
   `sms.network_interface.link_speed`가 VM에 실존하고, 파생 지표
   (`kcm.pod.cpu_utilization_by_limit`)까지 있다. 반면 설정 스냅샷 테이블은
   구멍이 많다 — `collector_dpm_pg_config`에 max_connections 컬럼이 없고,
   `kcm_resources.yaml`은 containers를 null로 지워 저장(28/28). → Q4·Q11은
   "한도성 메트릭(1차) + 설정 스냅샷(보조)" 혼합으로 설계한다.
2. **grade 라벨 팬아웃**: 한 target·metric의 naked selector가 grade UUID
   13~14종으로 복제 매치된다(참조 문서 라벨 목록에 없던 존재). `avg by(__name__)`
   류 집계는 grade를 혼합 평균한다 — 모든 VM 조회는 grade 처리 규칙이 필요하다.
3. **차원 라벨의 실명과 연쇄 규칙**: 실존 라벨은 프로세스 `name`+`pid`,
   인터페이스 `device`, pod `pod`+`pod_id`(+namespace/node_id/cluster_id),
   container `container`+`container_id`. 문서 예시(process_executable_name/
   interface/pod_name)는 전부 빗나감. **id계열 라벨(pod_id·node_id·container_id)만
   다음 도구 인자로 이어진다** — 도구 응답은 표시명이 아니라 id 라벨을 우선
   반환해야 한다.
4. **수집 편차가 실재한다**: kafka 계열 메트릭이 F04-R 당시 환경엔 카탈로그
   0건이었으나 f08-h 캡처엔 157개 이름(랙 실데이터 포함)이 실존. 같은 지표도
   환경·시점에 따라 있고 없다 — coverage 판정(Q의 "믿어도 되는가" 자동 부착)이
   전제 조건이다.
5. **원천이 도구보다 풍부한 곳이 반복 확인됨**: 이벤트의 `transition`(open/close)·
   `episode_id`·`duration_ms`·`peak_severity`, 로그의 INFO 이하 레벨, VM의
   range query(버킷 시계열). 현 도구가 안 읽을 뿐 스키마는 지원한다.

### 2.1 Ⅰ 자리매김 (Q1)

- **원천**: `incidents`(main_target_id/main_metric/main_event_id) +
  `incidents.blast_radius` jsonb(services[], target_ids[]) + `incidents.
  recovery_status`(partial/resolved)·status·closed_at + `incident_members`
  (recovered·resolved_at·stable_since). 영향·지속 모두 seed 계약에 실려 온다.
- **연쇄**: main_target_id·blast_radius.target_ids[]·main_event_id → 대상/이벤트 조회.
- **간극**: 전부 **박제 시점** 상태. "지금도 계속되는가"의 실시간 재판정은
  main_metric을 현재 창으로 VM 재조회해야 한다. 이벤트에서 직접 읽기는 반쪽
  (종료 이벤트 표식 비표준).

### 2.2 Ⅱ 찾기 (Q2~Q4)

- **Q2 열거/검색**: `targets`(659행)의 type(enum: server/application/database/
  network/kubernetes/web_service + *_resource류)·name·display_name·address·
  status로 필터 열거 재료 완비. **현 도구의 UUID-only 제약은 스키마 한계가
  아니라 도구 표면 한계** — find_targets는 스키마 공사 없이 성립한다.
  간극: group_id는 전 행 NULL(죽은 필드).
- **Q3 정체·소속**: targets 컬럼으로 이름·유형·주소는 충분. 소속은 분산 —
  호스트 귀속 `targets.meta->>'host_target_id'`+`server_resources`, K8s 귀속
  `kcm_resource_targets`(cluster_target_id·parent_target_id), 서비스그룹
  `asset_tree_members`(kind=service). 도구가 이들을 합성해야 한다.
- **Q4 현재 한도**: §2.0-1 참조 — 한도성 메트릭이 1차 원천. 남는 간극:
  PG 서버측 max_connections는 스냅샷·메트릭 어디에도 직접값이 없고
  `session_utilization`(비율)로 간접 판정.

### 2.3 Ⅲ 보기 (Q5~Q10)

- **Q5 지표 발굴**: `metric_definitions`(2,002건 — metric_key=VM `__name__`,
  unit 1989건 채움, value_type gauge/counter_up, delta_op) + VM 집계. 간극:
  change_score 미정규화·episodic gauge 오도(판정식 문제), grade 팬아웃(§2.0-2),
  `.bands`(562종)·`.raw` 변종이 인벤토리를 부풀림 — 발굴 응답에서 접어야 한다.
  - **이상감지 이벤트(stream-anomaly)는 Q5를 대체하지 못한다(2026-07-24 실측).**
    탐지기는 `ai_coverages` 화이트리스트에 등록된 (대상,지표)만 채점하고
    (runtime.go covered()), 그 목록의 기본 scope=`golden`(3등급 tier-lite만 —
    coverageplanner), kafka·apm류 누적 카운터는 delta-poll 게이트 의존. 캡처
    실측(07-21 장애 창): VM 실존 ~1,965종/52대상 중 이상 이벤트가 덮은 건
    113종/27대상 — kafka(157종 실재)·apm·postgresql·system·process 계열 0건.
    F04(consumer lag)·F01(pool pending)의 원인 지표가 정확히 이 사각지대.
    판정도 EWMA z-score 문턱(기본 4.0) 통과분만. → 이상감지 이벤트는 Q9의
    재료(push 기록)이고, Q5는 조사자가 겨눈 대상의 전 지표 pull 대조로 별개.
- **Q6 시계열**: VM `/api/v1/query_range`로 정렬 버킷 시계열 실측 성공(두 대상
  나란히, 동일 타임스탬프). **창 요약만 주는 현 상태는 조회 설계 한계**(vm.go에
  RangeQuery 헬퍼 자체가 없음).
- **Q7 차원 분해**: §2.0-3 참조. endpoint 차원은 VM 라벨에 없음 — 트레이스
  (span_name) 축이 담당.
- **Q8 로그**: CH `lucida_logs_local`(severity_text/number·body·target_id·
  host_target_id 이중 귀속). **INFO 이하도 원천에 저장됨** — 현 도구의 WARN+
  템플릿 제한은 도구 한계. 레벨 불문 raw N줄 표본은 스키마상 지원.
- **Q9 이벤트·알람**: CH `lucida_events_local`. 이벤트/알람 구분 =
  detector('alarm-bridge')·event_kind('external_alarm')·class. 지속/해소 정본
  신호 = `transition`(open/close) + episode_id·duration_ms·peak_severity.
  현 도구는 이 필드들을 안 읽고 severity!='cleared'로 우회 — 재설계에서 정본
  필드 사용.
- **Q10 변경**: `policy_deployments`(target_ref=UUID, 정확) + `collectors`
  (created_at=등록) + `change_history`. 수집 변경은 change_history category로만.
  **정정(2026-07-29, §10.2 실측):** ①change_history의 귀속 재료는 "name
  문자열뿐"이 아니다 — `detail.target_id` 24행, `name` 칸이 대상 UUID
  문자열인 23행이 실재해 **정확 귀속 경로는 5개**다(§10.2-1). ②
  `policy_deployments`는 이력이 아니라 **현재 상태 표**이고 재배포가 기존
  링크를 삭제·삽입하므로 **과거 부착 상태는 복원 불가**(§10.2-3). ③원천
  3곳은 독립이 아니라 **같은 사건을 다른 단위로 중복 기록**한다(§10.5).

### 2.4 Ⅳ 비교 (Q11~Q13)

- **Q11 양적 대조**: rate 쪽 재료는 metric_definitions에 있음(value_type·
  delta_op·raw_unit·unit). 분모(한도)는 §2.0-1. 간극: "높을수록 나쁜가/0의
  의미" 같은 판정 방향은 카탈로그에 없음.
- **Q12 또래**: `asset_tree_members`(kind=service 그룹 3개, 크기 12/7/6)에서
  동일 type 필터 — 그룹 있으면 견고. **fallback(같은 type 전체)은 도메인
  혼재라 약함** — 또래 근거(그룹 vs fallback)를 응답에 명시해야 한다.
- **Q13 선후(onset)**: range query로 두 대상 버킷 시계열 나란히 뽑기 실쿼리
  검증 완료(`avg by(target_id)`가 grade 팬아웃도 흡수). onset 계산 도구는
  미구현일 뿐 원천은 지원. 주의: metric=source time, event/log=fallback
  time이라 지표↔이벤트 onset 대조는 정밀도 한계.

### 2.5 Ⅴ 잇기 (Q14~Q15)

**박제 topology 생성 경로 실측 (2026-07-24, lucida-next 소스 추적).** 인시던트의
`incidents.topology`는 전역 topology 테이블의 복사가 아니다 — **전역 topology는
물리 테이블이 아예 없다.** query 서비스가 요청 시점에 즉석 조립(`query/store/
topology_store.go`)하고, ai 서비스 eventcluster가 그걸 받아 클러스터 anchor 기준
**1-hop BFS**(기본값, env `EVENTCLUSTER_TOPOLOGY_SNAPSHOT_HOPS`)로 오려
`event_clusters.correlation_meta`에 저장, 인시던트 격상 시 `incidents.topology`로
복사한다(`ai/store/incidents_judge.go:645`). 간선별 원천:

| kind | 원천 |
|---|---|
| apm_call | CH `otel_traces_local` — 같은 trace의 parent↔child span 조인 |
| apm_db | CH `otel_traces_local` — `db.system` 속성 있는 CLIENT span |
| access_host | PG `network_fdb_hosts` (FDB+ARP, collector-nms) |
| network_link | PG `network_neighbors` (LLDP/CDP, collector-nms) |
| event_cluster | 원천 없음 — 실간선 0일 때 멤버를 star로 잇는 합성 overlay |

추가 사실: (a) **freshness 창** — apm 계열 600s, network 계열 3600s 내 관측
간선만 박제됨 → 호출이 뜸했던 실의존은 시간창에서 탈락(간극 4번째 꼴).
(b) edge kind는 코드에 **16종 정의**되어 있고(k8s_*·host_socket·ebpf_* 등),
5종만 보이는 건 이 환경의 데이터 실재 문제지 설계 한계가 아니다. (c)
`db:<engine>` 문자열은 원천 결손 — span `db.system`에 target UUID가 애초에
없다. lucida-next 자체 RCA도 별도 resolve를 시도한다(`rca/service/dependency.go`).
(d) **원천이 전부 캡처에 실재** → 박제를 읽는 대신 우리 조건(홉·시간창·조립
규칙)으로 직접 조립 가능. → §3.0 아래 Ⅴ 방침 선결정 참조.

- **Q14 topology 끝점 resolve**: apm_db 문자열 끝점(`db:<engine>`)의 구제
  경로 — 박제가 버린 필드가 원자료(otel db-client span)에 생존: `db.name`
  (commerce/fooddelivery/…)·`server.address`·port·`db.sql.table`. 단 **하드
  FK는 없다**: target_identities에 database 행 0, workload_db_connection_profiles
  0행 미적재. server.address(컨테이너 호스트명) ↔ targets.address(IP)라 직접
  매칭도 실패 — db.name↔target.name 부분일치라는 연성 조인만 가능. →
  resolve는 휴리스틱 + 확신도 표시로 설계하거나, 수집측 브리지 적재를 청구.
  **정정(2026-07-29 실측 → 2026-07-30 3자 검토로 등급 하향) 2건:**
  ①**apm 간선에 대상 UUID가 실린다** — span `resource_attributes`의
  `lucida.target_id` 99%(90,059/90,940). **단 계약이 아니라 배포 규약이다** —
  설치 스크립트는 강제 upsert하지만 공통 Go 초기화는 선택 병합이고
  (`backend/pkg/observability/otel.go:169,196`) otel-collector는 trace
  파이프라인에 주입하지 않는다(`deploy/config/otel-collector/config.yaml:158`).
  미주입 서비스는 조용히 빠지므로 coverage 표기 대상(§12.2).
  ②**앱→DB는 "부분일치 연성 조인만"이 아니다** — `db_resource` 대상의 typed
  meta(`resource_kind`/`resource_key`/`parent_target_id`)로 `db.name`을 이어
  부모 UUID를 얻는다(PG·MySQL 성공, Oracle PDB·Redis는 생산자가 의도적으로
  미생성). **단 이 행들은 정본상 "미승격·탐색 전용"이라 계약이 아니다** —
  등급은 환경 관측(§2.9-1 청구 유지). 이름 문자열 파싱은 쓰지 않는다.
  server.address 직접 매칭 실패는 맞다(0건 일치 재확인).
- **Q15 소켓 관측**: host_connections의 process가 rootless shim으로 뭉개짐
  확정(rootlesskit 130만 행). PID·container_id 컬럼 부재로 실프로세스 식별
  불가 — 유일 우회는 local_port(well-known)·remote_addr↔targets.address 대조.
  **정정(2026-07-29, §12.1-3 실측):** 현 캡처는 뭉개짐 이전에 **비어 있다** —
  전 기간 448,691행 중 대상 5개·프로세스 13종이 전부 인프라(kcm-agent·
  k3s-server·kubelet·kube-proxy·flanneld·sshd·containerd)이고 **앱 프로세스
  0건**. 위 우회책조차 적용할 재료가 없다(행 수는 TTL 롤링이라 "전 기간"
  프레이밍은 부정확 — 재현 374,573). → expand_topology는 소켓을 넣지
  않는다(§12.4). 승격 조건은 스키마 청구 해결이 아니라 **실행 시 app-owner
  coverage > 0**이다.

### 2.6 Ⅵ 지목 (Q16~Q17)

- **Q16 DB**: `dpm_session_local`은 블로킹/wait/세션id가 전용 컬럼이 아니라
  `log_attributes` Map + body에 engine별 키(PG blockingPid / Oracle
  blockingSession / MySQL blockingTid)로 들어 있음 — 도구가 engine 분기 흡수
  필요. sql_id → `db_sql_text`(sql_key 조인, resource_id는 비-FK) → 본문,
  `db_sql_plan`(plan_json). 커넥션 포화는 세션 집계 or VM postgresql.db.
  connections.*. 이 캡처엔 dpm 0행(DDL·119 보강).
- **Q17 서버/K8s/네트워크**: 서버 프로세스 = VM `sms.process.*`(name+pid 라벨,
  2,490 series — 단 SMS 관측 호스트 한정, pod 내부 프로세스 없음). K8s =
  `kcm_resource_targets`(pod/container가 target으로 실재) + kcm.* 지표.
  **kcm_events(OOMKill 등 reason)는 이 캡처 0행**(119엔 있음). 네트워크 =
  nms.*/snmp 지표는 실존하나 **snmp_traps는 119 포함 실관측 미확인** —
  "장비 직접 신호" 질문은 현재 어느 환경에서도 데이터가 없다.

### 2.7 Ⅶ 검증 (Q18)

중간 고리 관측 가능성을 두 실패 케이스로 실측:

- **F04형(consumer 정지→backlog→지연)**: `kafka.consumer.records_lag`(topic·
  partition·client_id 라벨, 312 series)=backlog 그 자체, `last_poll_seconds_ago`
  =정지 감지, producer측 retry/buffer 지표까지 실존. otel PRODUCER/CONSUMER
  span(offset·consumer.group)으로 계보·offset 진행 조립 가능. → 중간 고리
  검증 가능(단 수집 편차 주의 — §2.0-4).
- **F01형(락→풀 고갈→실패)**: `db.client.connections.pending_requests`
  (pool_name 라벨)=대기큐, `timeouts`·usage/max, 서버측 postgresql.db.
  connections.waiting. 간극: 이 캡처에선 testbed DB 서버측 waiting 미수집이라
  서버측 확증은 클라이언트 지표 의존, 락 자체(dpm blocking)는 미포집.
  - **정정(2026-07-24 실측): dpm 부재는 수집 갭이 아니라 캡처 갭.** 119엔
    DPM이 테스트베드 DB 3종(pg/mysql/oracle)을 수집 중이고(dpm_session 24h
    11.8만 행), **캡처 창(07-21 06:30~08:00)과 동일 창에도 7,392행 실재** —
    격리 캡처 DB만 0행. 케이스 덤프가 dpm 테이블을 안 담는 것(F02-R 기각
    사유와 같은 뿌리). 라이브 조사라면 blocking 확정 계층 관측 가능. 평가
    세션 전달거리: 캡처 스크립트에 dpm 테이블 포함. 설계 함의는 불변 —
    관측 가능성은 환경·캡처마다 다르므로 coverage 판별이 전제(§2.0-4).

### 2.8 §2가 도구 묶기(§3)에 넘기는 결론

- find_targets(Q2)·범용 시계열/onset(Q6·Q13)·raw 로그 표본(Q8)·이벤트 정본
  필드(Q9)는 **원천이 이미 지원 — 도구만 만들면 됨**.
- 한도 조회(Q4·Q11 분모)는 한도성 메트릭 패턴으로 **범용화 가능**.
- topology 끝점 resolve(Q14)·host_connections 프로세스(Q15)·change_history
  귀속(Q10)은 **원천 결손 — 휴리스틱+확신도 설계 또는 수집측 청구**.
- snmp trap(Q17 네트워크)·K8s 이벤트는 **데이터 실재부터 확인 필요**(수집측).

### 2.9 수집측 청구 목록 (담당자 전달용, 2026-07-24)

도구 설계로 우회는 하되 근본 해결은 수집 쪽에 있는 항목. 우회책은 확신도가
낮으므로 수집이 해결되면 도구의 휴리스틱을 걷어낸다.

1. **앱→DB 연결의 대상 UUID 다리 (Q14, 청구 T2와 동일 뿌리).**
   - 문제(전달문): 애플리케이션의 DB 호출 트레이스에는 `db.system`(엔진)·
     `db.name`(논리 DB명)·`server.address`만 실리고 관제 대상 UUID가 없다.
     그래서 topology의 apm_db 간선이 실제 DB 대상 UUID가 아니라
     `db:postgresql` 같은 엔진 문자열로 끝난다(apm_call 간선이 양끝 UUID인
     것과 대조). DB 대상 자체는 인벤토리에 등록돼 있지만 앱과 잇는 데이터가
     없다 — `target_identities`에 database identity 미적재,
     `workload_db_connection_profiles` 미적재, span의 server.address(컨테이너
     호스트명)와 targets.address(IP)는 형식이 달라 주소 매칭 불가. 이 때문에
     RCA가 "이 앱이 쓰는 그 DB"로 건너가 세션·락·슬로우쿼리 조사를 겨냥할
     수 없다.
   - 요청(우선순위): ①`workload_db_connection_profiles` 적재가 미구현인지/꺼진
     건지/계획 있는지 확인(살리면 최선) ②대안: `target_identities`에 database
     identity(엔진·주소·논리DB명) 적재 ③최소선: topology 조립 시 apm_db 간선
     target에 해소된 target_id 동봉.
   - 완료 판정: 앱 UUID에서 **추측 없이** 그 앱이 쓰는 DB target UUID 획득 가능.
   - **청구 유지 — 축소 철회(2026-07-30, §12.1 정정 2. 3자 검토로 뒤집힘):**
     `db_resource` 대상의 typed meta(`resource_kind`/`resource_key`/
     `parent_target_id`)로 PG·MySQL이 풀리는 것은 사실이나, **이는 생산자
     계약이 아니다** — 정본 `targets저장표준.md` §7.1 도메인 표가 이 도메인을
     `데이터베이스(dpm/db 폴링) → ❌ 미승격(설계 — VM 메트릭 스냅샷 파생),
     DBDpmResourceSnapshot 탐색 전용`으로 명시한다. 표준이 강제되면 사라질 수
     있는 표시용 스냅샷이다. Oracle(PDB 대신 tablespace만)·Redis(엔진 switch
     분기 없음)가 빠지는 것도 **생산자의 의도적 분기**다
     (`query/store/database_dpm_resources_store.go:44,49`). → 본 청구(하드
     브리지)는 그대로 유효하고, 현 경로는 우회책으로만 쓴다.
   - 해결 전 우회(내부용) = db.name↔target name 연성 조인 + 확신도 표시(명명
     관습 우연에 의존 — 관습 다르면 깨짐, 논리DB명은 서버 특정 못 함, redis류는
     db.name 자체 없음). 근거 실측은 §2.5 Q14.
2. **host_connections 실소유자 기록 (Q15).**
   - 문제(전달문): rootless 컨테이너 환경에서는 컨테이너의 소켓 통신이
     `rootlesskit` 중계 프로세스를 거치므로, `host_connections`의 process
     필드가 실제 서비스가 아니라 rootlesskit으로 기록된다. 테이블에 PID·
     container_id 컬럼도 없어 실소유자로 거슬러 갈 수 없다. 결과적으로
     "어느 호스트가 어느 호스트:포트와 통신 중"까지만 보이고 "어느 컨테이너/
     서비스가"는 식별 불가 — 소켓 관측으로 topology에 없는 실제 의존을
     밝히는 용도가 반쪽이 된다.
   - 요청: ①`host_connections`에 `container_id`(가능하면 실 PID·이름) 컬럼 추가
     ②수집 시 채우기(방식은 수집기 몫 — 예: 컨테이너 netns 내부 관측, 소켓
     inode→cgroup/컨테이너 매핑). 스키마 변경 수반.
   - 완료 판정: 행 하나에서 소켓 소유 컨테이너(나아가 관제 대상) 식별 가능.
   - 해결 전 우회(내부용) = local_port(well-known)·remote_addr↔targets.address
     대조뿐. 근거 실측은 §2.5 Q15.
3. (기존 §2.8 항목) k8s 이벤트·snmp trap 데이터 실재 확인, change_history의
   target_id 귀속 — **1급 컬럼 신설 청구**다(현재는 `detail.target_id`가
   일부 카테고리에만 실리고 `구성 대상`·`수집기`·`정책 템플릿`은 name
   문자열뿐이라 rename/삭제 시 끊김. 실측 §10.2-1). 청구 3건 추가
   (2026-07-29, §10 설계 중 발견):
   - **`action` 컬럼을 실제로 채워라.** 파괴 작업인 `SESSION_KILL`이
     action 미기록으로 남아(생산자 소스 실측, §10.4) 소비자가 "변경 여부"를
     이 컬럼으로 판별할 수 없다. 완료 판정: 모든 writer가 action 기록.
   - **정책 배포에 `deployment_batch_id`(또는 감사 detail에 template IDs).**
     현재 감사 detail은 개수만 담아 change_history↔policy_deployments
     연결이 영구 추정이다(§10.5-3). 나아가 **append-only 배포 이벤트
     원장**이면 과거 부착 상태 복원 불가(§10.2-3)까지 함께 해결된다.
   - **자산 트리 감사에 revision 또는 내용 해시.** 현재 detail은 멤버
     *개수*만이라 "내용이 같은 저장"을 판정할 수 없다(§10.5-5).
4. **지표 카탈로그에 한도 짝 관계 필드 (Q11 — 우선순위 낮음).**
   - 문제(전달문): 사용량 지표와 그 한도 지표(예: DB 커넥션 사용↔풀 최대,
     컨테이너 메모리 사용↔limit)가 카탈로그에 서로 무관한 항목으로 존재해,
     소비자가 "무엇으로 나눠야 사용률인가"를 각자 하드코딩해야 한다. 또
     OTel 표준 수신 지표(db.client.connections.* 등 통과 지표)는 카탈로그에
     미등재라 단위 정보 자체가 시스템에 없다.
   - 요청: ①카탈로그(프론트 .ts 정본)에 한도 짝 관계 필드(짝 지표 key +
     비교 시 라벨 처방, 예: state=used만) ②통과 지표의 카탈로그 등재.
   - 완료 판정: 카탈로그 조회만으로 임의 사용량 지표의 한도 짝·비교 방법
     획득 가능.
   - 해결 전 우회(내부용) = read_timeseries의 검증된 짝 고정표(§6.3, 6짝
     한정·확장은 평가 실측 발동 조건부). **우선순위 낮음**: 한도성 지표는
     본질적으로 소수(실측 1,962종 중 ~20종)라 표 유지비가 낮고, 표 확장
     필요가 실증되기 전엔 재촉할 이유 없음.

## 3. 도구로 묶기

### 3.0 묶기 규율 (2026-07-24 합의)

질문→도구 매핑은 1:1이 아니다. 접기 전에 규율을 고정한다:

1. **도구 수는 입력이 아니라 출력이다.** D9의 8~10개는 "한 순간에 보이는 메뉴
   크기"지 전체 도구 수의 상한·목표가 아니다. §1의 질문이 18개인 것과 기존
   표면이 18종인 것은 우연의 일치일 뿐, 어느 쪽도 새 도구 수를 구속하지 않는다.
2. **도구 하나마다 근거 필수.** 어떤 질문(들)을 답하는지, 왜 응답 봉투·보드·
   범용 도구 조합으로는 안 되는지를 함께 적는다.
3. **도구로 안 만드는 기준.** (a) 다른 도구의 응답 봉투에 자동 부착되는 품질
   속성, (b) 보드가 상시 보여주는 상태, (c) 범용 도구 조합 + 코드 후처리(D10)로
   답이 나오는 것 — 은 독립 도구로 만들지 않는다.
4. **집합은 열려 있다.** §4 검수(프론티어 궤적 F04-R·F01-R + 청구서 T1~T8)와
   이후 평가 주행이 도구 추가·삭제의 정본 경로다. 여기서 그린 표면은 초판이지
   확정판이 아니다.

**Ⅴ(잇기) 방침 선결정 (2026-07-24 합의)** — 상세 설계는 순서대로 하되 방침만
먼저 고정한다: 잇기는 인시던트 박제(`incidents.topology`)를 읽지 않고 **원천에서
직접 조립한다**(§2.5 실측 — 원천이 캡처에 전부 실재, 홉·시간창을 우리가 정하고
Kafka 계보·동거 등 조립 규칙도 추가 가능). 형태는 반반이다: **조사 시작 시점의
지도(anchor 주변)는 seed가 코드로 미리 조립해 브리핑에 실어주고**(도구 아님 —
규율 3(b)), **조사 도중 새 대상 주변으로의 확장만 도구**로 만든다(대상·홉·시간창
파라미터 — 어디로 튈지 몰라 사전 계산 불가, CH 조인+휴리스틱이라 조사자 합성
불가 = 규율 3의 예외 아님). `db:<engine>` 끝점은 원천 결손이라 조립으로도 안
풀림 — 휴리스틱 resolve + 확신도 표시는 여전히 필요. 박제는 "증상 시점의 그림"
참고 자료로 seed에 유지한다.

### 3.1 Ⅰ 자리매김 — 도구 0개 + 설계 배정 2건 (2026-07-24 합의)

Q1은 새 도구를 만들지 않는다:

- **증상·영향**: `incidents`(main_target_id·main_metric·blast_radius) +
  `incident_members`(recovered·resolved_at)는 조사 시작 시 **seed 계약으로
  이미 주어지는** 정보다 — 규율 3(b), 보드/초기 브리핑의 몫.
- **"지금도 계속되는가"**: 실체는 "main_metric을 현재 창으로 VM 재조회" =
  범용 시계열 도구(Q6)를 seed의 main_metric에 겨누는 동작 — 규율 3(c), 조합.

배정 2건:

1. seed 브리핑에 blast_radius·recovery_status·incident_members 요약 필수
   포함 (보드 설계 몫).
2. 시계열 도구는 "현재 창" 조회를 지원할 것 (Q6 요건에 흡수).

검수 이월: "영향이 **번지고 있는가**"(blast_radius가 박제 이후 확대)가 또래
열거+시계열 조합으로 커버되는지 — §4에서 F04-R 궤적으로 확인.

### 3.2 Ⅱ 찾기 — 도구 2개 (2026-07-24 합의)

**① `find_targets` (Q2) — 명부 검색.**

- 뒤지는 건 딱 하나, PG `targets`(관제 대상 명부 659행). type·이름·주소 필터로
  열거/검색해 UUID를 손에 쥔다. **데이터 검색이 아니다** — 임의 대상의 지표·
  로그·트레이스를 훑는 능력은 없고, 데이터는 쥔 대상에 Ⅲ 도구를 하나씩 겨눠야만
  보인다. 최악의 남용이 명부 659행 훑기라 범위 폭발의 표면적이 없다.
- 왜 도구인가: 검색어가 조사자의 가설에서 나오는 능동 동작 — 봉투·보드로 대체
  불가. 근거 실측: F08-H wrong 3겹 중 "seed 밖 원인", F01-R의 `db:postgresql`
  → "database 대상 나열(5건)" 경로 부재(T1).
- 범위 규정: 조사 경계는 인시던트 topology가 아니라 **관제 인벤토리**다(§2.5 —
  박제는 1-hop·시간창 탈락이 있는 좁은 그림이라 경계로 삼으면 F01형에서 원인
  도달 자체가 막힘). 단 기본 걸음은 여전히 topology 따라가기(잇기)고, 잇기가
  원천 조립으로 바뀌면(Ⅴ 방침) 지도 수선 수요 대부분이 그쪽에 흡수돼
  find_targets는 가설 지목 검색만 남는다.
- 응답 규율: 표시명이 아니라 **id 우선 반환**(§2.0-3) — 다음 도구 인자로 연쇄.
- **구현 보류 (2026-07-28 합의, snmp trap과 같은 지위):** 자격 사건 2건을
  재점검한 결과 비상구 몫 대부분이 Ⅴ 잇기에 이미 흡수돼 있다 — F01형
  문자열 결손은 expand_topology의 연성 resolve가, 계측 밖 의존은 소켓
  관측 간선이 커버. 남는 몫은 "통신 흔적조차 없는 원인"뿐인데 실측이
  없다. 설계(위 문단들)는 유지하되 구현하지 않는다. **발동 조건:
  실주행에서 잇기·연성 resolve로 원인 대상에 못 닿는 실패가 실측되면
  그때 구현.**

**② `describe_target` (Q3+Q4) — 정체·소속·현재 한도.**

- 정체(이름·유형·주소)는 targets 컬럼, 소속은 원천 합성 — 호스트 귀속
  `meta->>'host_target_id'`, K8s 귀속 `kcm_resource_targets`, 서비스그룹
  `asset_tree_members` (+`server_resources` — §2.2 실측 원천 4번째, §9 결정 1).
  한도 섹션은 "한도성 메트릭(1차) + 설정 스냅샷(보조)" 혼합(§2.0-1 — 단
  스냅샷은 §9에서 원천 실측 부재로 보류). 합성을 조사자에게 미루면 매 케이스
  같은 휴리스틱 반복 — D10대로 코드 몫.
- Q4(한도)를 별도 도구로 안 빼는 이유: 대상을 쥐는 순간 한도가 같이 손에
  있어야 Q11 양적 대조의 분모로 이어지고, 독립 도구면 "한도를 안 본" 실패
  모드(F01형)가 남는다. describe 응답에 기본 포함이면 빼먹을 수 없다.
- 봉투 배정: PG max_connections처럼 직접값이 없는 한도는 간접값
  (`session_utilization` 등)에 "직접값 아님" 표식 — 응답 봉투 확신도 몫.
- **`as_of`(조사창 시점) 지원 (§4 검수 반영, Codex 지적):** 사후 조사에서
  "현재 한도"로 인시던트 당시 값을 나누면 그 사이 배포·설정 변경으로 한도가
  바뀐 경우 오답. 한도 섹션은 incident-time 값·current 값·둘 사이 변경 여부를
  구분해 반환한다. **→ §9 결정 3으로 재구성됨** — current 재조회 없이 "창
  하나 + 창 내 변화 1급 + `current_not_checked` 명시"로 형태 변경(취지
  유지). 이 불릿의 3분법 원문은 역사 기록으로만 남는다.

### 3.3 Ⅲ 보기 — 도구 5개 (2026-07-24 합의)

**Q7 갈림 기준 (합의):** 분해(차원 쪼개기)는 질문의 명사(프로세스냐 pod냐)가
아니라 원천·동작으로 가른다 — **같은 시계열 원천 안의 라벨 분해면 시계열 도구의
파라미터, 다른 원천이 필요하면 Ⅵ 지목으로 이월.** 프로세스(`name`+`pid`)·pod
(`pod`+`pod_id`)·컨테이너는 VM 라벨에 실존(§2.0-3)하므로 전자, 엔드포인트 분해는
VM에 라벨이 없고 트레이스(span_name) 축이라 후자(Ⅵ APM 메뉴). 이에 따라 Ⅵ의
"(서버) 어느 프로세스가 먹나"류는 대부분 ②의 파라미터로 커버되고, Ⅵ에는 라벨
분해로 안 되는 것(예: K8s 이벤트 — 재시작·OOMKilled)만 남는다.

**① `scan_metrics` (Q5) — 지표 발굴.**

- 뭐가 있는지 모르는 상태에서 "이 대상, 지금 무엇이 이상한가"의 이상 후보를
  받는다. 원천: `metric_definitions`(2,002건) + VM 집계(§2.3).
- 왜 도구인가: 어느 대상을 훑을지가 조사자의 가설에서 나오는 능동 동작 —
  보드가 미리 계산해 둘 수 없다(seed 앵커 주변은 브리핑 몫이지만, 조사 도중
  새로 쥔 대상의 발굴은 사전 계산 불가). 이상감지 이벤트(stream-anomaly)로
  대체 불가 — 화이트리스트×감도 문턱을 통과한 push 기록일 뿐(§2.3 Q5 실측:
  캡처 창에서 113/1,965종, 원인 계열 kafka·apm 0건).
- 설계 요건: `.bands`(562종)·`.raw` 변종 접기(인벤토리 부풀림 방지), grade
  팬아웃 처리(§2.0-2), 이상 판정식은 현 change_score의 결함(미정규화·episodic
  gauge 오도) 수선을 전제 — 판정식 상세는 구현 설계 몫.

**② `read_timeseries` (Q6+Q7) — 시계열 + 차원 분해.**

- Ⅲ의 중추. 원천: VM `/api/v1/query_range` — 원천 지원은 실측 완료(§2.3),
  vm.go에 헬퍼만 부재. 창 요약만 주는 현 상태를 대체한다.
- 파라미터: 대상(복수 — Q13 선후 비교의 재료로 두 대상 나란히)·지표·시간창
  (**"현재 창" 지원 필수** — §3.1 배정 2)·분해 라벨(위 갈림 기준 — process/
  pod/container 등 §2.0-3 실명 라벨).
- Q7을 별도 도구로 안 빼는 이유: 집계 라벨만 다른 같은 조회 — 규율 3(c).
- 응답 규율: id계열 라벨 우선 반환(§2.0-3), `avg by(target_id)`류 집계로 grade
  팬아웃 흡수(§2.0-2).

**③ `sample_logs` (Q8) — 로그 지도 + grep 드릴다운 (2026-07-28 구현 설계
확정 — 사용자 문답 + Codex 검토, 원문 .omc/artifacts/ask/codex-…07-28T01-09-58).**

- 원천: CH `lucida_logs_local`. 레벨 제한 철폐(현 도구의 WARN+ 제한은 도구
  한계 — §2.3, F04 유일 미확정 원인). 귀속: target_id/host_target_id 이중.
- **표본 정책을 두지 않는다** — "최신 N줄"·"시간 균등"류는 도구의 추측
  (표본 편향)이라 기각. 대신 scan→read와 동형인 2단계 깔때기:
  `mode: map | grep` (명시 파라미터 — 암묵 전환은 모델에게 숨은 문법).
- **map 모드**: 템플릿(숫자·16진 정규화) 그룹핑을 **존재 4구획**으로 —
  기준선 미관측(신규 등장)/급증/**소멸**(기준선엔 있었는데 창에서 사라짐 —
  로그 끊김 자체가 단서)/최빈. 구획별 quota(단일 정렬은 startup 신규
  홍수가 저빈도 원인 로그를 밀어냄). scan_metrics 존재 4분류와 동형 =
  조사자가 새로 배울 문법 없음.
  - 신규성 표시는 판정이 아니라 사실만: `not_seen_in_baseline`/`seen`/
    `baseline_unknown` 3값 + 양 창 계수(기준선 0회·현재 12회꼴). "신규"
    단정 금지 — 도구가 아는 건 "기준선 창에서 미관측"뿐.
  - 각 그룹에 `template_id` 부여 — grep 모드가 그대로 받는다(26B가
    정규화 문구에서 검색어를 재조립하는 의미 변환 제거, "다음 호출 인자
    복사 가능" 원칙).
  - 레벨 분포는 유지하되 **판정 없는 요약 메타로 강등** — 현행 "ERROR
    존재=anomalous" 판정 폐기(상시 ERROR와 사건성 ERROR 구분 불가).
- **grep 모드**: `template_id` 또는 검색어(부분 일치)로 raw 줄 시간순 N줄 +
  매치 전후 맥락 줄(grep -C, 같은 귀속 스트림 안에서만 — 남의 로그가
  문맥으로 섞이는 것 방지, 줄마다 before/match/after 표시). severity
  하한·시간창 필터. total_matches·truncated 정직 표기.
- 유보(발동 조건부 — 실주행 실측 시 승격): cursor 페이지네이션(시간창
  좁히기가 대체), regex 매칭, 비밀정보 마스킹·untrusted 표식(평가 환경은
  자체 테스트베드라 위험 낮음 — 한계로 기록).

**④ `list_events` (Q9) — 이벤트·알람.**

- 원천: CH `lucida_events_local` + 대상이 K8s면 `kcm_events_local` 합성
  (§3.6 합의 — 출처 표식·coverage 표식 필수). **정본 필드 사용**: 지속/해소 =
  `transition`(open/close)+`episode_id`·`duration_ms`·`peak_severity`
  (현 도구의 severity!='cleared' 우회 폐기), 이벤트/알람 구분 =
  detector·event_kind·class.
- 응답 규율: 이벤트/알람 구분을 응답에 명시 — 알람은 2차 파생 신호라 로그·원본
  이벤트와 증거 무게가 다르다(§1 Q9). 이상감지 이벤트(stream-anomaly류)도
  화이트리스트×감도 통과분의 push 기록임을 구분(§2.3 Q5 실측).
- seed와의 역할 분담(왜 도구인가): seed의 이벤트 목록은 eventcluster가 anchor
  1-hop·묶기 창 안에서 이 인시던트로 묶은 것의 **격상 시점 박제**다. 조사 중
  새로 쥔 대상(seed 밖 — F08-H 실측), 묶기 창 밖(전조·"이 알람 평소에도
  울리나" baseline), 격상 이후 진행(닫혔나·새로 열렸나)은 pull 조회가 필요 —
  Ⅴ 방침(시작 지도=seed, 확장=도구)과 동일 구조.

**⑤ `list_changes` (Q10) — 변경.**

- 원천 3곳 합성: `policy_deployments`(target_ref=UUID, 정확)·`collectors`
  (created_at=등록)·`change_history`(귀속 재료가 카테고리마다 다름 — 응답에
  확신도 표시. 근본 해결은 수집측 청구 §2.9-3).
  **정정(2026-07-29, §10 구현 설계):** 원천 3곳은 독립이 아니라 **같은 사건을
  다른 단위로 중복 기록**하므로 나열이 아니라 **사건 단위 교차 접기**가
  필요하다(§10.5). change_history 귀속도 name 문자열뿐이 아니다(§10.2-1).
- Q9와 안 합치는 이유: 변경은 원인 선행사건 후보라 증거 무게가 다르고(§1
  Q10), 원천도 다르다(PG vs CH). 묶으면 "무엇이 바뀌었나"를 안 물은 실패
  모드(F08-H 변경 미수집)가 이벤트 나열에 파묻힌다.

검수 이월: 엔드포인트 분해의 Ⅵ APM 메뉴 배치는 §3.6(Ⅵ 지목)에서 확정,
scan_metrics 판정식은 §5에서 확정(2026-07-24 — 존재 4분류+robust z). list_events 활용률 우려(사용자,
07-24 — F08-H의 도구 선택 편중 실측 있음)는 커버리지 격자(§15.2)의 이벤트
관점 사용률로 실측하고, 바닥이면 프롬프트 처방·통폐합을 규율 4 경로로 결정.

### 3.4 Ⅳ 비교 — 도구 1개 + 봉투 부착 2건 (2026-07-24 합의)

**갈림 기준 (합의):** D10(계산은 코드, 조사자는 raw 숫자 눈대중 금지)을 어디에
둘지는 **비교쌍을 누가 아는가**로 가른다 — **코드가 비교쌍을 스스로 알면 응답
봉투 부착(자동, 빼먹기 불가), 조사자가 골라야 하면 도구(능동 호출).** 부착의
장점은 F01형 실패("한도를 안 봄")의 원리적 차단 — describe_target 한도 기본
포함(§3.2)과 같은 논리.

**Q11 (값 vs 한도) → read_timeseries 봉투 부착.**

- 조회 지표에 짝 한도가 있으면(커넥션↔max, pod mem↔limit 등) 코드가 "한도
  대비 %"를 계산해 부착. 재료: 한도성 gauge 메트릭(§2.0-1)+metric_definitions
  value_type·delta_op(counter는 rate 변환 후 비교).
- 한계 명시: 지표↔한도 짝짓기 규칙표는 구현 설계 몫(검수 이월), "높을수록
  나쁜가/0의 의미" 판정 방향은 카탈로그에 없음(§2.4) — 봉투는 계산만, 판정은
  조사자.

**Q13 (선후·onset) → read_timeseries 봉투 부착.**

- 복수 대상·지표 조회 시(②의 기존 능력) 코드가 각 시계열의 onset과 상호
  선후·시차를 계산해 부착 — 조사자는 "A가 B보다 N분 먼저"라는 사실만 받는다.
- 정밀도 표식: metric=source time, event/log=fallback time(§2.4) — 지표↔이벤트
  선후 대조엔 한계 표식. onset 판정 알고리즘(임계/change point)은 D10에서도
  deferred — 구현 설계 몫.

**Q12 (또래) → 독립 도구 `compare_peers`.**

- 또래 열거(asset_tree_members 서비스그룹 우선, 없으면 같은 type 전체
  fallback — §2.4) + 전원의 같은 지표를 나란히 계산해 대조.
- 왜 도구인가: 어떤 대상의 또래를 볼지는 조사 흐름에서 나오는 능동 결정 —
  매 응답 자동 수행은 비싸고 시끄럽다. 실증은 가장 강함: 프론티어 두 주행
  모두 결정적 사살이 또래 비교에서 나옴(F04-R "peer lag 0, shipping만 1087" /
  F01-R "peer pending 0, inventory만 20" — ADR D5 정련, cohort는 load-bearing
  primitive).
- 응답 규율: 또래 근거(그룹 vs fallback) 명시 — fallback은 도메인 혼재라 약한
  비교(§2.4).

**파생값 보수화 (§4 검수 반영, 독립 검토 2건 공통 지적):** D10은 "코드
계산값 = 신뢰"를 학습시키므로 자동 부착된 파생값이 틀리면 raw보다 위험한
고신뢰 오류가 된다. 부착 계약에 명시:

- 한도% — 짝짓기는 fuzzy matching 금지, **검증된 짝 목록(allowlist) + 단위·
  차원·시간 정합 검사**. 조건 하나라도 안 맞으면 비율을 만들지 않고
  `capacity_match=unknown`으로 끝낸다. 간접값(session_utilization류)은 직접
  max처럼 취급 금지(§3.2 표식과 동일).
- onset — 단일 시각이 아니라 **구간(onset_interval)+방법+확신도**로 반환,
  두 시계열의 불확실 구간이 겹치면 선후 판정은 반드시 `indeterminate`.
  sparse/episodic gauge의 첫 표본을 onset으로 오인하는 경로 차단(§2.3 Q5의
  episodic 오도와 같은 뿌리).
- 파생 부착값에도 coverage와 동급의 **확신도/유효성 표식** — 계산 못 하면
  침묵이 아니라 "왜 못 했는지"를 남긴다.

검수 이월: 지표↔한도 짝짓기 규칙표·onset 알고리즘은 구현 설계에서 상세화.

### 3.5 Ⅴ 잇기 — 도구 1개 (2026-07-24 합의)

방침은 §3.0 선결정 그대로: 시작 지도(anchor 주변)는 seed가 코드로 조립해
브리핑에 싣고(도구 아님), **조사 도중 새 대상 주변으로의 확장만 도구로 만든다.**
박제(`incidents.topology`)는 "증상 시점의 그림" 참고 자료로만.

**`expand_topology` (Q14+Q15) — 대상 주변 연결 조립.**

- 파라미터: 대상·홉 수·시간창. 원천에서 즉석 조립(§2.5 — 호출 간선은 CH
  otel_traces, 네트워크 간선은 PG network_fdb_hosts/network_neighbors, 소켓
  간선은 CH host_connections). 박제 대비 이득: 시간창을 조사자가 정하므로
  freshness 창(600s/3600s)에서 탈락한 뜸한 실의존을 창 확대로 되살릴 수 있고,
  홉도 1-hop 고정이 아니다.
  **정정(2026-07-29, §12.1-4 실측):** 창 확대 이득은 **재현되지 않았다** —
  간선쌍이 600s=24 · 1h=24 · 24h=25로, 24시간까지 넓혀야 1개 는다. 이 논거는
  쓰지 않는다. 남는 이득은 ①**조사 중 새로 쥔 대상 주변**(박제는 격상 시점·
  anchor 1-hop 고정이라 아예 없음) ②홉 고정 아님.
- **Q15(소켓 관측)를 별도 도구로 안 빼고 접는다 (합의).** ①조사자의 질문은
  "이 대상, 누구와 연결돼 있나" 하나 — 원천 구분은 우리 사정. ②나누면 소켓
  쪽 미호출 실패 모드가 생기는데, 소켓 관측의 존재 이유가 "지도에 없는 의존
  찾기"라 지도가 멀쩡해 보일 때야말로 필요 — 그 판단을 조사자에게 미루면
  정확히 필요한 순간에 빠진다. ③신뢰 등급 혼합 우려는 간선별 원천 표식이
  해결. ④비용 미미 — host_connections가 줄 수 있는 게 호스트↔호스트:포트
  수준뿐(§2.5 rootlesskit 실측)이라 간선 몇 개 추가 수준.
  **뒤집힘(2026-07-30, §12.4 — 논거는 3자 검토로 한 차례 교체됨):**
  소켓 간선을 **넣지 않는다.** 최초 논거였던 "앱 프로세스 0건이라 숨은 의존
  찾기를 구조적으로 수행 불가"는 **반증됐다** — 숨은 의존(미계측 외부 호출)은
  호출자 CLIENT span이 더 정확히 담는다(§12.3 `apm_client_peer`, 실측
  `testbed-external-pg-mock` 236회/h). 확정 논거는 ①소켓이 줄 수 있는
  호스트↔호스트:포트는 앱 소유권이 없어 의존 간선이 못 되고 ②그 몫을
  `apm_client_peer`가 더 정확히 수행한다는 것이다. 승격 조건도 "§2.9-2 해결"이
  아니라 **실행 시 app-owner coverage > 0**(데이터 관측)으로 건다. 기존
  `get_runtime_connections`도 같은 이유로 자리 교체 대상(§12.4·§12.10).
- 응답 규율: **간선마다 원천 표식 + 확신도** — 트레이스 관측(실호출)/NMS
  장비 관측/소켓(호스트 수준 추정, 낮음)/합성(event_cluster류)을 구분. 표시명
  아니라 id 우선(§2.0-3).
  **정정(2026-07-30, §12.2·§12.6):** ①소켓 등급은 삭제(원천 미채택 — §12.4)
  ②합성(event_cluster)은 **조립하지 않는다**(원천 관측이 아님) ③"확신도"
  단일 값이 아니라 **3축 분리**(`resolution_status`·`match_basis`·`confidence`)
  — §10 provenance 규율 상속 ④간선 종류에 `apm_client_peer`(한쪽 관측) 신설.
- `db:<engine>` 끝점: 조립으로도 UUID 안 나옴(원천 결손, §2.5) — db.name↔
  대상 이름 연성 조인 휴리스틱 resolve + 확신도 표시. **정정(2026-07-30):
  이름 조인이 아니라 `db_resource` typed meta 경로를 쓴다**(§12.1 정정 2).
  단 계약이 아니므로 등급은 환경 관측이고, `db:<engine>` 같은 **가짜 노드
  ID는 만들지 않는다**(§12.2). 수집측 청구 §2.9-1
  해결 시 걷어냄. 소켓 간선의 호스트 수준 한계도 §2.9-2 해결 시 격상.
- 조립 규칙은 열려 있음(§3.0): Kafka 계보·동거(같은 호스트) 등은 검수·평가
  주행이 요구를 실증하면 규율 4 경로로 추가.

검수 이월: 홉·시간창 기본값, 간선 확신도 등급 체계는 구현 설계에서 상세화.
seed 시작 지도의 조립 범위(홉·창)는 보드/브리핑 설계 몫.

### 3.6 Ⅵ 지목 — 상황 메뉴 도구 3개 + 배정 2건 + 보류 1건 (2026-07-24 합의)

Ⅵ은 범용 동작이 아니라 **도메인별 상황 메뉴**(D9 — 그 도메인이 이야기에
들어올 때만 노출). 도메인별 결정:

**DB 메뉴 — 도구 2개.** §1 Q16의 "옴니버스 금지" 그대로 갈림길별 분리:

- **`db_blocking`** — 누가 누구를 블로킹하나. 원천 `dpm_session_local`
  (블로킹 키가 engine별로 다름 — PG blockingPid/Oracle blockingSession/MySQL
  blockingTid, §2.6 — 도구가 engine 분기 흡수).
- **`db_slow_queries`** — 어느 쿼리가 느린가. dpm_topsql → sql_id →
  `db_sql_text`(본문)·`db_sql_plan`(플랜)까지 연쇄.
- 커넥션 포화는 도구 안 만듦 — read_timeseries(connections 지표)+Q11 한도
  부착 조합이 답(규율 3(c)).

**APM 메뉴 — 도구 1개.** **`breakdown_endpoints`** — 어느 엔드포인트가
느린가/에러인가(§3.3 이월분 — VM 라벨에 endpoint 차원이 없어 트레이스
span_name 축, 원천이 달라 read_timeseries 파라미터로 못 접은 것).
**span_kind 범위 명시 (§4 검수 반영, 3자 공통 지적):** SERVER만이 아니라
**CONSUMER·PRODUCER·INTERNAL span 포함** — "엔드포인트"가 아니라 "처리
구간"의 분해다. 메시지 구동 서비스는 SERVER span이 /actuator/health뿐이라
(F04-R step 4 실측), SERVER 한정이면 프론티어 케이스 하나의 사각지대를
정의상 재생산한다. 이 명시로 T2의 APM 절반(consumer 처리 구간 분해)이
해소된다.

**서버 — 도구 0개.** "어느 프로세스가 먹나"는 read_timeseries의 프로세스
라벨 분해(§3.3 Q7 기준)가 답. sms.process.*는 SMS 관측 호스트 한정·pod 내부
프로세스 없음(§2.6) — 커버리지 한계는 봉투 몫.

**K8s — 도구 0개, ④ list_events에 원천 합성 (합의).** "pod에 무슨 일이"
(OOMKilled·재시작 reason)의 원천 `kcm_events_local`은 별도 테이블이지만,
조사자의 질문은 "이 대상에 무슨 이벤트가 있었나"로 ④와 같다 — 대상이
K8s(pod/container/node)면 list_events가 kcm_events도 합쳐 반환(출처 표식).
⑤의 변경 3원천 합성, §3.5의 소켓 접기와 같은 논리(나누면 미호출 실패 모드).
kcm_events는 이 캡처 0행·119엔 실재(§2.6) — coverage 표식 필수.

**네트워크 — 보류.** snmp trap("장비가 직접 신호를 보냈나")은 119 포함 어느
환경에서도 실관측 미확인(§2.6) — 데이터 실재 확인(§2.9-3)이 먼저, 도구는
확인 후 규율 4 경로로. nms.*/snmp 지표 자체는 범용 도구(Ⅲ)로 커버.

검수 이월: K8s 합성이 ④ 응답을 어지럽히지 않는지(출처 섞임), APM 메뉴에
breakdown_endpoints 하나로 충분한지(F08-H의 trace_breakdown·slow_endpoints
미사용 이력 참조)는 §4 검수·평가 주행에서 확인.

### 3.7 Ⅶ 검증 — 도구 0개 + 배정 3건 (2026-07-24 합의)

Q18(중간 고리가 실제로 관측됐는가)은 새 도구를 만들지 않는다:

- **검증 = 기존 도구의 재사용.** 중간 고리의 재료는 전부 지표·로그다(§2.7
  실측 — backlog=consumer lag, 대기큐=pending_requests, 정지=last_poll…).
  검증 동작은 Ⅲ·Ⅳ 도구를 "중간 고리를 겨냥해" 다시 쓰는 것 — 규율 3(c).
- **선후·동반 회복은 Q13 부착이 답한다.** "원인이 먼저였나, 회복도 같이
  움직였나"는 §3.4 onset 자동 계산 그대로.
- **검증의 강제는 도구 표면 몫이 아니다.** 확정 문턱(D7 — 검증됨 화살표
  사슬 없이 확정 불가)과 예측표(§15.3 — probe 전 예상 선언)가 담당.

배정 3건:

1. **관측 불가 판별은 봉투 coverage 몫** — measured-zero(봤는데 없음, 배제
   근거)와 no-sample/collector_gap(안 보여서 없음, 근거 아님)의 구분이 검증
   단계에서 특히 치명적(§2.7 F01형: 클라이언트 지표는 실재, 확정 계층은 캡처
   갭 — 정정 문단 참조. 관측 가능성은 환경·캡처마다 다름 §2.0-4).
2. **중간 고리 관측 수단 없음 → 확정 불가 + 종료 사유 typed(수단 소진) +
   missing evidence 기록** — D7(멈춤≠확정)과의 접점. 존재하지 않는 데이터를
   무한히 파지 않고 품위 있게 끝낸다.
3. 검증 겨냥을 돕는 재료(원인 유형별 중간 고리 후보 — F04형 backlog·retry,
   F01형 대기큐·timeout)는 도구가 아니라 프롬프트/의무 계산기(깊이 처방) 몫.

**§3 완주.** 표면 요약: 상시 8개(find_targets·describe_target·scan_metrics·
read_timeseries·sample_logs·list_events·list_changes·compare_peers) +
확장 1(expand_topology) + 상황 메뉴 3(db_blocking·db_slow_queries·
breakdown_endpoints) = **도구 12개**, 봉투 부착 2(한도%·onset), 보류 1(snmp
trap). D9의 "한 순간 8~10개" 구도와 정합(상황 메뉴는 도메인 진입 시에만
노출). 다음: §4 검수(프론티어 궤적 F04-R·F01-R 재조·청구서 T1~T8 대조).

## 4. 검수 — 프론티어 궤적 재조 + 청구서 T1~T8 대조 (2026-07-24)

방식: 자체 검수 + 독립 검토 2건(별도 Claude 세션·Codex gpt-5.6-sol — 원문
`.omc/artifacts/ask/codex-tmp-…2026-07-24T04-10-31….md`). **3자 총평 일치:
골격(12도구 구성) 유지, 수정은 정의 보강·배정 결정 수준 — 구성 변경(도구
추가/삭제/재분할) 요구 없음.** 주의: 3자 모두 같은 궤적 2건을 증거로 읽었다 —
독립 검증 3개가 아니라 같은 증거의 세 번 읽기이며, 그 2건은 테스트베드
장애주입("한 놈만 고장"으로 설계된 세계)이다. 이 표본 한계가 §4.3의 채택/
유보 갈림을 결정했다. 궤적 원본은 휘발성 스크래치패드에서
[frontier-trajectory-f04r.md](frontier-trajectory-f04r.md) ·
[frontier-trajectory-f01r.md](frontier-trajectory-f01r.md)로 보존.

### 4.1 청구서 T1~T8 판정

| 항목 | 판정 | 근거 |
|---|---|---|
| T1 타깃 디렉터리 | 해소 | find_targets — F01의 "database 대상 나열" 경로 그 자체 |
| T2 확정 도구 겨냥 | 해소(조건부) | DB 절반: find_targets+연성 resolve(확신도 — 아래 기대치 주의). APM 절반: §3.6 span_kind 명시로 해소 |
| T3 cohort 1급 | 해소(긴장 기록) | compare_peers 도구. "자동 공급" 요구와의 긴장은 §4.3-1로 |
| T4 랭킹 신뢰화 | 요건 확정 | scan_metrics 설계 요건에 명시. 판정식 자체는 §4.3-3 순서 보증 |
| T5 버킷 시계열 | 해소 | read_timeseries = query_range·복수 대상, onset 부착 |
| T6 zero/no-sample | 해소 | 봉투 coverage 몫 명시(§3.7 배정 1) — enum 계약은 구현 설계 |
| T7 레벨 불문 로그 | 해소 | sample_logs raw N줄 — F04 유일 미확정 원인이 닫힘 |
| T8 오류 복구 정보 | 해소(규율 신설) | 아래 공통 규율 — §3 개별 도구가 아니라 표면 전체 계약 |

**T8 공통 규율 (검수 반영):** ①모든 도구 오류·no_data에 복구 정보(유효 id
후보·다음 수 안내) 필수 — 계약 §2 원칙의 재확인이며, find_targets가 생겨
후보 재료도 확보됨. ②**호출 가능 id 불변식**: 도구가 반환한 id는 다음 도구
인자로 그대로 들어간다. 휴리스틱 resolve 산물은 확정 id와 섞지 않고
`candidate`로 typed 구분(`db:<engine>` 같은 미해소 끝점은 `unresolved`).

**DB 겨냥 기대치 (검수 기록):** db_blocking·db_slow_queries의 겨냥은
§2.9-1(앱→DB UUID 다리) 해결 전까지 연성 조인 확신도에 의존한다 — F01형
시나리오의 출시 초기 실히트율은 이 한계 안이다. 수집측 해결 시 격상.

### 4.2 궤적 재조 — 새 표면으로 재주행하면

- **F01-R**: 실주행에서 4스텝을 소모하고 포기한 막힘(DB UUID 미획득 →
  확정 도구 조준 불가)이 find_targets 한 번으로 뚫리고, db_blocking으로
  blocking SQL까지 도달 가능 — provisional이 confirmed로 상승했을 주행
  (단 dpm 캡처 갭 정정 §2.7 참조 — 라이브면 가능, 격리 캡처는 덤프 보강
  필요). 마찰 6건 전부 대응 존재.
- **F04-R**: sample_logs로 18:31/18:38 INFO 본문(container pause/resume)을
  읽어 유일 미확정(정확 트리거)을 닫을 수 있고, breakdown_endpoints의
  span_kind 명시로 consumer 처리 구간 dead-end(step 4)가 해소된다.
  §3.1 검수 이월("번지고 있는가")도 확인 — step 14의 send_rate 팬아웃이
  정확히 compare_peers 동작이라 또래 열거+시계열 조합으로 커버됨.
- 미커버로 남는 것: F04의 메시징 계보 넓히기(§4.3-2 유보), K8s 합성
  혼잡도·list_events 활용률(궤적 2건에 K8s·이벤트 사용 데이터 없음 —
  §3.3 이월대로 커버리지 격자 실측).

### 4.3 후보 기록 — 채택하지 않고 발동 조건을 명시한 것

표본 한계(궤적 2건, 장애주입 세계) 위에 일반 규칙을 세우지 않는다 — 규율
4(표면 변경의 정본 경로 = 평가 주행)를 따른다.

1. **cohort 자동 공급 (3자 검수 최상위 지적).** 긴장은 실재: 청구서 증류
   문단은 T3를 "코드가 선제 계산·노출"하라 요구하고, §3.4는 능동 도구로
   결정했다 — 소형 모델이 compare_peers를 떠올리지 못하면 두 궤적의
   결정타를 놓친다. 준비된 처방 2개: (a)의무 규칙 — "1순위 가설의 의심
   대상에 또래가 존재하면(서비스그룹 동료 or 같은 type ≥2) 확정 전
   compare_peers 1회 필수, 또래 없으면 면제 기록" — 가설 의미 분류 없이
   기계적 사실로만 판별, 기존 의무 계산기 구조 재사용. (b)scan_metrics
   응답에 경량 또래 플래그 부착 — 또래 선정·지표 선택·비용의 설계 숙제
   수반. **발동 조건**: 평가 주행에서 조사자가 cohort를 안 부르고 확정
   또는 오답에 이르는 사례 실측 시 (a)부터 적용. 계측은 커버리지 격자
   (§15.2)의 compare_peers 사용률로 — 추가 계측 불요.
2. **메시징(Kafka) 계보 조립 규칙.** 증거가 F04 1건뿐 — 과적합 강등
   (2026-07-23) 과거와 같은 뿌리라 §3.5 유보 유지. 원자료 실재는 확인됨
   (PRODUCER/CONSUMER span·messaging.* 속성, §2.5). **발동 조건**: 평가
   주행에서 메시징 경로 넓히기가 rate 상관 추론으로 헤매는 사례 실측 시
   expand_topology 간선 종류로 추가(otel messaging.* 표준 속성 기반 —
   Kafka 특정 아닌 메시징 일반으로).
3. **scan_metrics 이상 판정식 — 순서 보증.** 독립 검토 2건 모두 "구현
   이월이 아니라 지금 확정"을 요구(두 궤적 첫 수를 오도한 load-bearing
   요소, 소형 모델은 회복 못 할 수 있음). 판정식 세부는 실데이터 실험이
   필요해 문서 합의로 확정 불가 — 대신 **구현 설계 착수 시 1순위**로
   못박는다: change_score 정규화·episodic gauge 표식이 도구 구현 첫 작업.
4. **메뉴 수 관찰.** 상시 8+확장 1에 DB 2+APM 1 동시 노출 시 12개로 D9의
   "한 순간 8~10"을 넘는다(Codex 지적 — 앱→DB 조사는 흔한 경로). 실해는
   도구 선택 오류율로 드러나므로 평가 주행 계측 후 우선순위/교체 검토.

### 4.4 검수 결론

**표면 초판 확정 — 구현 진행 가능.** 12도구 구성은 문제지(T1~T8·궤적
마찰 13건)를 통과했고, 검수가 만든 변경은 §3 본문의 정의 보강 3건
(describe_target as_of·봉투 파생값 보수화·breakdown_endpoints span_kind)과
공통 규율 2건(T8·기대치), 후보 기록 4건이 전부다. 다음: 구현 설계
(§4.3-3 순서 보증 포함 — scan_metrics 판정식부터), 기존 18도구 계약
(spec-agent-tools.md)과의 이행 경로는 구현 설계에서.

## 5. 구현 설계 — scan_metrics 이상 판정식 (2026-07-24 합의)

§4.3-3 순서 보증(구현 설계 착수 1순위)의 이행. 방식 = F04-R 케이스
(case-f04-r-3814ac35, 주입 18:29:44~18:39:14 UTC — shipping consumer 중단)를
전용 격리 세트 `eval-f04r-toolspike`(VM 38428/CH 38123/PG 45432)에 복원해
현행 판정식의 실패를 실측하고, 후보를 같은 데이터에서 검증하며 하나씩 합의.

### 5.1 현행 판정식의 실측 실패 2건

현행(discover.go): 직전 동일 길이 창 평균 대비 상대 변화
`|cur−base|/max(|base|,1e-9)`, 기준선에 없으면 +Inf("신규 등장").

1. **인접 기준선의 오염.** 진범이 보이는 창(18:36~18:41, lag 895)의 직전
   창이 하필 장애 구간(replica 0 = 무데이터) → 192개 지표 전부 +Inf 동점
   "신규 등장" → 진범 `records_lag_max`가 **알파벳 순서에 밀려 86등/192**.
2. **소멸 비대칭.** 주입 중 창의 진짜 신호는 시계열 소멸(APM 요청 지표
   8종 증발 = 처리 중단)인데, 현행은 등장만 +Inf로 잡고 **소멸은 보고
   자체가 없음**(cur에 없으면 조용히 누락).

### 5.2 확정 판정식 — 존재 4분류 + robust z

점수 하나가 아니라 **존재 분류가 먼저**다. 지표별로:

| 분류 | 조건 | 처리 |
|---|---|---|
| disappeared | 기준선 有, 현재 창 無 | 점수 없음 — §5.5 판별 재료 부착 |
| appeared | 기준선 無, 현재 창 有 | 점수 없음 — §5.4 지속률·episodic 태그 |
| shifted | 양쪽 有, robust z ≥ 문턱 | z 내림차순 상위 N |
| steady | 양쪽 有, z < 문턱 | 건수만 |

- **robust z** = `|med_cur − med_base| / max(1.4826·MAD_base, 5%·|med_base|,
  1%·scale, ε)`. median/MAD(스파이크 오염 내성), 분모 바닥값이 "평소 출렁임
  0" 지표의 0-나눗셈 폭발(F01 마찰 (c))을 막는다. 정렬은 uncapped z,
  표시만 캡 — 캡 동점 안에서 순위가 다시 알파벳이 되는 것 방지(실측:
  캡 정렬 시 lag 14등 → uncapped 정렬 시 7등).
- **counter는 버킷 increase 후 비교**(metric_definitions.value_type
  counter_*). 현행의 counter raw 평균(*_total류 전부 무의미)을 대체.
  리셋(음수 증가분)은 0 처리.
- `.bands`·`.raw` 변종은 인벤토리·판정 모두에서 접는다(§3.3 ①).

### 5.3 기준선 창 — 인시던트 전 고정, 코드가 잡는다

- 기본 = **seed first_event 직전 lookback**(도구는 인시던트 바인딩으로
  생성되므로 코드가 안다 — 조사자 계산 불요). 인접 창 방식은 §5.1-1로 기각.
- lookback 기본 **60분**: 10분(≈10표본, 폴링 60s)은 통계보다 **주기
  포착**이 약점 — 배치·GC 등 "평소에도 튀는" 지표의 주기가 안 담기면
  오탐. 60분이면 둘 다 완화(실측: 5분/10분 기준선의 판정 결과 동일 —
  큰 이탈 감지는 표본 수에 둔감, z 50~100대).
- 재생(캡처) 환경은 가용 구간으로 자동 축소 + **실사용 구간·표본 수를
  응답에 명시**, 표본 미달 시 "기준선 빈약" 표식(파생값 보수화).
- 조사자 override = baseline_window 인자(봉투 v2 기존 자리). 탐지 지연이
  큰 장애("직전 60분도 이미 병듦")는 코드가 못 가르므로 조사자 몫.
- 하루 주기(새벽 배치 등)는 60분으로도 못 담는 **명시적 천장** —
  계절성 primitive는 D10 미정 항목에 이미 유보.
- 전달거리(평가 세션): 케이스 캡처의 장애 전 구간 10분 → 60분 요청.

### 5.4 appeared — 줄 세우지 않는다

지표 등장 = 대부분 "사건이 방금 일어난 흔적"(rebalance_latency는 재조인이
있어야 태어난다). 단위가 섞여 크기 비교가 무의미하므로 **순위 경쟁 없이**
전부 표시(상한 N + truncated), 항목마다 기계적 사실 3개: **지속률**(현재
창 버킷 중 표본 비율)·단위·대표값. 지속률 낮으면(<30%, 임시) **episodic
태그** — F04 첫 수 오도(rebalance "신규 등장" 1등 → 원인 오인)가 "복구
시점에 재조인 반짝"이라는 정확한 서사로 바뀐다(실측: appeared 8종 전부
2/11 버킷 = episodic).

### 5.5 disappeared — 판별하지 않고 판별 재료를 붙인다

"끊김"의 두 의미(대상 정지 vs 수집 결손)를 도구가 단정하면 §4의 "파생값
고신뢰 오독" 위험 그 자체다. 기계적 사실 2개만 부착:

1. **범위**: partial(일부 계열만, 나머지 지표 계속 — 같은 에이전트에서
   일부만 끊길 수집 사유는 없으므로 "그 컴포넌트가 멈춤" 강한 신호) vs
   total(전 지표 — 애매).
2. **동시성**(total일 때 자동, 쿼리 1방): 같은 창 다른 대상들의 방출
   여부 → `isolated`(이 대상만 전멸 — 대상 측 사건 개연성. 실측: 장애 중
   shipping 0 시리즈/나머지 51 of 52 정상) vs `widespread`(다수 동시 전멸 —
   수집 결손 의심, **배제 근거 사용 금지** 표식). no_data_reason의
   zero_observations/collector_gap 구분과 같은 사상.

최종 판별("죽은 게 맞나")은 조사자가 list_events(pod Killing/Scaled)·
sample_logs로 — 도구는 "isolated total 소멸"까지만 말한다.

### 5.6 grade 팬아웃 — 첫 접기 단계로 `max without(grade)`

원천 확인(ingest/writer/grade_label.go): grade = **알람 등급 ID**. vmalert
materialized 룰이 `{grade=<id>}`로 대상을 고르므로 ingest가 대상의 배포
등급마다 시리즈를 복사 — **설계상 값 동일 복제**다(전수 실측: 3,401
시리즈 → grade 접기 269 고유, 값 불일치 0). 처방: 모든 VM 조회의 첫
단계 = `max without(grade)`(부분 표본에도 안전 — avg는 가정 깨질 때
조용히 왜곡) + grade 간 값 불일치 발견 시 경고 로그(가정 감시). 부수
효과: 시리즈 13분의 1. grade 접은 뒤 남는 실차원(partition 등)은
scan_metrics 수준에선 지표당 집계(방식 응답 명시), 분해는 read_timeseries
파라미터 몫(§3.3 Q7 갈림 유지).

### 5.6.1 구현 실측 2건 (2026-07-24, Go 구현 중 발견 — tools/scan.go)

1. **PromQL 집계의 `__name__` 소실 함정.** `max without(grade)`류 집계는
   지표 이름을 버려서, 라벨셋이 같은 **서로 다른 지표**(disk.io vs
   disk.operations, 둘 다 device=vda)를 한 그룹으로 합쳐버린다(라이브
   실측: 이 함정으로 grade 불일치 "153건" 거짓 양성 — 실제 0건).
   `keep_metric_names` 수식자는 현행 VM 버전 미지원, raw rollup만 이름
   유지(475/475). **처방: grade 접기·지표당 집계를 쿼리가 아니라 코드가
   한다**(raw 시리즈 수신 → grade 제외 라벨셋 그룹의 버킷별 max + 같은
   버킷 값 불일치 정확 계수 → 지표당 버킷별 avg). D10(코드가 눈)과
   정합, 별도 감시 쿼리도 불요해짐.
2. **간헐 방출 지표의 소멸 오인.** 기준선(60분)이 현재 창보다 길면
   원래 가끔만 찍히는 지표가 disappeared로 잡힌다 — 라이브 실측:
   소멸 27건 **전부**가 `lucida:*:*1h`(시간당 1회 물질화 recording
   rule, 기준선 존재율 1/121). §5.5 철학대로 단정 대신 재료 부착:
   **기준선 존재율**을 항목마다 붙이고, 존재율 < 30%(episodic 문턱
   재사용)면 intermittent 태그 + 보수 문구. 표시 순서는 상시 방출이던
   것(진짜 신호)이 먼저 — 캡 안에서 간헐이 자리를 먹지 않게.

### 5.7 검증 결과와 임시 수치

같은 W_drain 창 재실측(기준선 = 인시던트 전 10분): disappeared 1 /
appeared 8(전부 episodic — 재조인 계열) / shifted 88 중 **진범
records_lag_max 7등**(z=90.9), 상위 20 전부 실제 장애 관련(catch-up 소비
폭증·producer 서지·rebalance), 오도 지표 0. 주입 중 창: disappeared 8
(APM 요청 계열 = 처리 중단을 그대로 서술) / shifted 2.

**F01-R 교차 검증 통과 (2026-07-24, case-f01-r-a598db24 — DB hot row 락,
F04와 무관 도메인).** 주입 창(06:41:46~06:43:52) × 기준선(t1 직전 10분):
- 진범 inventory: **appeared 유일 항목 = db.client.connections.timeouts**
  (타임아웃이 처음 발생해야 태어나는 지표 — "사건의 흔적" 의미론 그대로) +
  shifted 8개 전부 장애 서사(p95 5.8ms→8.9s, error_rate 등장, DB 응답
  859ms, thread 3배, **pending_requests z=5.6 > 문턱 3**). 잡음 0.
- 음성 대조(무관 shipping): appeared/disappeared 0, 약한 shifted 6
  (kafka 배치 크기 출렁임, z 3~7) — 오탐 홍수 없음.
- 피해자(order): 영향 서사 정확(p99 70ms→7.4s, error_rate 0→64,
  자기 풀 pending 7.3) — 원인 대상과 피해 대상 모두에서 읽을 만한 그림.

**임시 수치(교차 검증 후 유지)**: z 문턱 3(진범 pending z=5.6 — 5로
올리면 경계라 3 유지), 표시 캡 20, 버킷 30s, episodic 30%, lookback 60m.
운영 조정은 평가 주행 축적 후. 구현 메모: appeared 항목이 counter면
대표값은 누적치가 아니라 increase로. 재현: 세션 스크래치패드
rank_current.py/rank_candidate.py, 격리 세트 폐기는 `docker compose -f
replay/eval-dbs.compose.yml -p eval-f01r-toolspike down -v`(f04r 세트는
검증 후 폐기 완료).

부수 발견: restore.sh가 CH 초기화 레이스(readiness의 SELECT 1 통과 시점에
lucida DB 미생성)로 DDL 전패 후 `set -e` 중단할 수 있음 — **수정 완료
(2026-07-24)**: readiness를 lucida DB 존재 확인으로 강화(실측 창 ~2.8초)
+ DDL 파일당 3회 재시도(entrypoint가 init 후 임시 서버를 재기동하는 두
번째 다운 창 흡수). 신규 CH 기동에서 DDL 32테이블 전패→전승 검증.

## 6. 구현 설계 — read_timeseries (2026-07-26 합의 완료)

§3.3-②의 구현 설계. 결정 4개(응답 형태·파라미터·Q11 한도 부착·Q13
onset 부착) 전부 합의 — 다음은 Go 구현.

### 6.1 응답 형태 — 다운샘플 버킷 포함 (결정 1, 합의)

창 요약(현행 get_metric_series)이 아니라 **버킷 값 배열 + 코드 파생값**을
준다. 역할 분담: 모양 독해(계단/반짝/추세)는 조사자, 산술은 코드 —
프론티어 실증(F04 "lag 1087 지속은 3s pause로 불가능"은 모양 독해에서
나옴)이 근거. 요약만 주는 안은 모양-서술 판정식(미검증 코드)이 새로
필요해져 기각, 옵션 스위치 안은 활용률 위험(F08-H 편중 실측)으로 기각.

### 6.2 파라미터 (결정 2, 합의)

- `targets`(복수 — Q13 선후 재료)·`metrics`(복수) — **조합 ≤ 8**, 초과는
  오류+쪼개기 안내. 근거: 8시리즈×40버킷이 26B 컨텍스트 상한선, 선후
  비교는 2~3 대상이면 충분(프론티어 실측).
- 버킷: 원본 30s(§5.7 공유), 시리즈당 **표시 40버킷 상한** — 초과 시 균등
  병합(gauge avg, counter increase 합)하고 `bucket_seconds` 명시.
- `group_by`(분해 라벨, §3.3 Q7 갈림): 지정 시 지표 1개 제한, 라벨 값
  top 5 + other 합산 + truncated.
- 시간창: `to:"now"`·상대 표기(`now-15m`) 허용(§3.1 "현재 창" 배정 이행).
  재생 환경은 관측 없음으로 정직하게 드러남.
- 기준선: scan_metrics와 동일(자동 = first_event 직전 60분, override,
  재생 clamp 표식) — 코드 공유.

### 6.3 Q11 한도 부착 — 검증된 짝 고정표, 보험 성격 (결정 3, 합의)

**성격 규정(사용자 문답으로 확정): 필수 기능이 아니라 보험** — 없어도
도구는 완전 동작(조사자가 한도를 직접 조회 가능, describe_target 한도
섹션 §3.2 중복 안전망). 존재 이유는 소형 모델이 한도를 볼 생각을 안
하는 실패 모드(F01형)의 구조적 차단 하나. 한도는 **모르는 게 기본
상태**(1,962종 중 한도성 gauge ~20종, Metaspace·limit 안 건 컨테이너처럼
있어야 할 곳도 없을 수 있음) — 짝이 표에 있고 한도가 창 내 관측될 때만
붙는다.

**짝 원천 조사(2026-07-26, 코드+수집 양면):**
- 이름 규칙 자동 짝짓기 기각 — `_max` 접미사의 절반은 한도가 아니라 관측
  최대값(records_lag_max=F04 진범 지표, fetch_size_max 등). 기계 판별
  불가, 오짝은 "자신 있는 오답"(§3.4 보수화의 근거 그 자체).
- 카탈로그(metric_definitions) 대조 불가 확정 — 정본은 프론트 카탈로그
  .ts→SeedMetricCatalog upsert이며 **수집 등록(CollectorKind) 지표만
  시드**. db.client.connections.*는 레포 정의 없는 OTel javaagent 통과
  지표(레포 주석 "정의서 미등재(HikariCP semconv)")라 단위 정보가 시스템
  어디에도 없음. 단위 표기도 가족별 상이(B/bytes/mCPU — 짝 내부는 일관).
- 독립 확증: lucida 코드 8곳이 이미 같은 짝을 인지(collector-db poll.go가
  active↔max를 한 쌍으로 동시 생산, 알람 정책 ">=70% max_connections",
  KCM usage/limit/capacity 구조체, DPM session_utilization=current/max×100
  등). state 라벨은 OTel semconv이며 lucida 자신도 memory에서 used만 골라
  씀 — state=used 처방과 일치.
- 한도의 정적성 실측: 캡처 창 32분 내 값 변화 시리즈 0(3짝 raw 검증,
  changes()는 착시) → 창 내 last 사용 안전.

**짝 고정표 v1 (전항 실측 검증, 2026-07-24~26 격리 F01-R 세트):**

| 사용측(+필터) | 한도측 | 조인 라벨 | 잉여 라벨 처방 | 단위(내장) |
|---|---|---|---|---|
| db.client.connections.usage{state="used"} | db.client.connections.max | pool_name(+process_pid) | state 필터 | connections |
| apm.agent.otel.java.jvm.memory.used | …jvm.memory.limit | jvm_memory_pool_name+type | limit 없는 풀(실측 Metaspace)=unknown | B |
| system.memory.usage{state="used"} | system.memory.limit | (대상 단일) | state 필터 | B |
| kcm.container.cpu_usage | kcm.container.cpu_limit | container_id | — | mCPU |
| kcm.container.mem_usage | kcm.container.mem_limit | container_id | — | B |
| postgresql.db.connections.active | postgresql.db.connections.max | (인스턴스) | db_name 합산 후 비교 | connections |
| dpm.oracle.tablespace.used | dpm.oracle.tablespace.max | tablespace | — | bytes |

- **단위는 표에 내장**(카탈로그 부재 대응) — 카탈로그에 값이 있으면 보조
  대조(불일치→unknown+사유), 없으면 표가 정본.
- 런타임 검사(하나라도 실패 시 비율 없이 `capacity_match:"unknown"`+사유):
  ①단위(내장값 vs 카탈로그, 있을 때만) ②차원(처방 적용 후 조인 키 맞물림)
  ③시간(한도가 조회 창 내 관측 — 오래된 한도로 계산 금지).
- 이미-비율 지표는 짝 계산 대신 "직접 읽으라" 안내: system.{memory,cpus,
  filesystems}.utilization(cpus는 lucida 파생 집계 실측),
  kcm.pod.*_utilization_by_*, dpm.*.session_utilization,
  dpm.oracle.tablespace.max_utilization.
- 부착 형태: 해당 finding에 `capacity:{percent, limit_value, limit_metric,
  matched_on}` — 계산만, 판정 없음(§3.4).
- **확장은 발동 조건부**(§4.3 패턴): 평가 주행에서 "한도 미확인 오답"
  실측 시에만 행 추가. 근본 해결은 §2.9-4 카탈로그 청구(우선순위 낮음),
  이행 시 이 표를 걷어낸다. v1은 gauge↔gauge만(counter 짝은 검증된 것
  없음 — delta_op 실재 확인했으나 §3.0 "안 만드는 기준"대로 미수록).

### 6.4 Q13 onset 부착 — scan 부품 재사용 + 구간 반환 (결정 4, 합의 2026-07-26)

**비교 집합 = 조사자의 호출이 정한다.** 한 호출에 담긴 대상×지표 조합
(≤8)이 비교 집합 — "무엇을 나란히 놓을까"는 가설에서 나오는 능동 선택
(조사자), 놓인 것들의 산술은 코드(D10 분담, §3.4 갈림 기준의 적용).
쌍별 행렬이 아니라 **onset 시간 정렬 목록**(타임라인) 하나로 부착.
조사자가 지표 전체를 훑는 구조가 아님 — scan_metrics 깔때기(전체 1,962종
→ 대상당 100~200종은 코드 계산 → 이상 후보 ~10개 → 가설 지목 1~2개)가
선행하고, read의 인자 어휘는 scan 응답에서 나온다(사용자 문답 확인).

**알고리즘 — scan_metrics의 검증된 부품 재사용**(기준선 60m·robust z·30s
버킷·counter increase — 새 판정식 없음, scan의 shifted와 척도 일관):
1. 버킷별 z(기준선 median/MAD, §5.2 분모 바닥값 공유).
2. onset = 첫 지속 이탈 — z≥3 첫 버킷 + 후속 2버킷 중 과반 z≥3(단발
   스파이크 오인 방지).
3. 반환은 시각 아닌 **구간**: [마지막 정상 관측 버킷, 첫 이탈 관측 버킷]
   — 관측 갭만큼 구간이 넓어지는 게 정직한 표현(§3.4 보수화).
4. 선후: 두 구간 비겹침일 때만 "A가 N~M초 먼저", 겹치면 indeterminate.
5. 계산 포기(침묵 아닌 사유): 간헐 방출(존재율<30%, scan episodic 문턱
   공유 — 첫 표본=발단 오인 차단) / 창 시작부터 이미 z≥3 = "onset이 창
   밖(이전)" 표기(조사자에게 창 확장 신호) / 기준선 빈약 = confidence low.

부착 형태: 시계열별 `onset:{interval, method:"sustained z>=3",
confidence}` + 응답 말미 onset 정렬 타임라인(겹침엔 indeterminate 명시).

## 7. 이행 경로 — 과도기 표면 배선 (2026-07-27 합의)

구 18도구(spec-agent-tools.md) → 신 12도구는 한 번에 갈아끼우지 않는다.
구현된 신 도구부터 **자리 교체(병존 금지)**로 Toolset에 넣고, 과도기
표면으로 26B 첫 주행을 해서 나머지 얇은 도구들의 구현 순서를 실측으로
정한다(설계-구현 교차 방식의 연장).

### 7.1 교체 원칙 — 자리 교체, 병존 금지 (합의)

역할이 겹치는 구 도구는 신 도구 배선과 동시에 뺀다:

| 들어옴 (신) | 빠짐 (구) | 근거 |
|---|---|---|
| scan_metrics | discover_signals | Q5 역할 동일(이상 후보 깔때기) |
| read_timeseries | get_metric_series, get_metric_dimensions | 값 읽기 + 차원 분해는 group_by가 흡수(§3.3 Q7) |

근거: 26B의 익숙한 도구 선택 편중(F08-H metric_series 16회/타 도구 0회
실측)이 병존 시 재현되면 신 도구 실측 자체가 무산된다 — 미배선을
유지했던 "구/신 혼재 방지"와 같은 논리. 구 표면 discover_signals의
인벤토리 열거 역할을 scan_metrics 응답(.bands 접기·steady 건수)이 충분히
대체하는지는 단정하지 않고 첫 주행 관찰 항목으로 둔다.

### 7.2 배선 (구현 완료, 2026-07-27)

- `tools.Toolset(stores, incidentID, firstEvent)` — firstEvent 파라미터
  추가(신 도구 기준선 자동 창 [firstEvent-60m, firstEvent)의 기준점,
  §5.3). cmd/rca는 seed의 FirstEventAt을 그대로 넘긴다.
- `cmd/tool`에 `-first-event <RFC3339>` 플래그 추가(미지정 시 now —
  라이브 탐색용).
- 과도기 표면 = **17종**: 신 2(scan_metrics·read_timeseries) + 구 15
  (coverage·runtime_connections·target_meta·events·changes·logs·cohort·
  trace_breakdown·slow_endpoints·db_sessions·slow_queries·processes·
  snmp_traps·k8s_state·topology).
- 갱신(2026-07-28): sample_logs ← get_logs(§3.3-③), list_events ←
  get_events(§8) 자리 교체로 현재 **신 4 + 구 13 = 17종**. list_events의
  K8s 이벤트 합성으로 get_k8s_state의 이벤트 몫이 흡수됐으나
  get_k8s_state는 pod 상태 지표 조회가 남아 유지(§8 자리 교체 문단).
  격자 events 관점 배정(§15.2)·중복 호출 가드 안내 문구의
  get_events 참조도 치환.
- 갱신(2026-07-29): **describe_target ← get_target_meta**(§9) 자리
  교체로 현재 **신 5 + 구 12 = 17종**. 동반 변경: ①일괄 스크리닝
  몫은 Examiner 브리핑이 흡수 — TriageResult.TargetNames 신설,
  조사 대상 목록이 UUID 나열에서 {target_id, type, name} 행으로
  (§9 결정 4) ②내부 typed batch(`TargetMetaFunc`)는 유지(LLM 표면과
  무관 — cmd/rca 자체 SQL) ③get_processes 안내 문구의 구 이름 치환,
  registry 머리 주석 정정 ④uuidRe·targetTypeCounts는 describe.go로
  승계, 구 파일 삭제. 격자 배정 불요(관점 도구 아님 — get_target_meta도
  미배정이었음).
- 갱신(2026-07-29): **list_changes ← get_changes**(§10) 자리 교체로 현재
  **신 6 + 구 11 = 17종**. 동반 변경: ①`Toolset`이 `lastEvent`를 추가로
  받는다(기본 창 `[firstEvent-24h, lastEvent]`의 재료 — cmd/rca·cmd/react는
  seed의 `LastEventAt`, cmd/tool은 `-last-event` 플래그, 미지정 시 now 폴백)
  ②파이프라인 [2]의 typed 자리(`NewChangesFunc`)는 유지하되 **조회를 공유하지
  않는다** — changes.go는 typed 전용으로 축소, 봉투 도구는 listchanges.go가
  승계 ③`llm/mockworld_test.go` 목 이름·pipeline/changes.go 주석·
  ref-tool-data-access.md·spec-agent-tools.md 구 이름 치환. 격자 배정 불요
  (관점 3열에 변경 없음 — 변경 관점은 [2] 담당).
- 구 이름 참조 치환 4곳(빠진 도구를 가리키던 코드 — 배선의 강제 귀결):
  Examiner 프롬프트 지표 시작 규칙(discover_signals→scan_metrics, 정독은
  read_timeseries — 문안 재검증은 첫 주행이 겸함), 커버리지 격자 칸
  배정표(§15.2 — metrics 관점 3구명→scan·read 2명), **깊이 의무 family
  (server·network·kubernetes의 get_metric_dimensions→read_timeseries —
  방치 시 존재하지 않는 도구 시도를 요구해 해당 도메인 confirmed가
  구조적으로 막혔음)**, 중복 호출 가드의 안내 문구. Investigator
  프롬프트는 도구 이름 미참조로 무사.
- 배선 검증(격리 F01-R 세트 eval-f01r-scanimpl): scan_metrics가 §5.7 기지
  결과 재현(appeared 유일 db.client.connections.timeouts), read_timeseries
  한도 부착이 주입 창 내 HikariPool-1 10/10=100% 재현. 주입 종료 후로
  창을 넓히면 percent 0 — "창 내 last 값" 기준의 정직한 회복 표현(버그
  아님, 실측 확인).

### 7.3 다음 — 26B 첫 주행 (남음)

과도기 표면으로 자격 케이스 재생 주행(cmd/rca), Opus 궤적(F04-R·F01-R)과
나란히 비교: 도구 선택 분포(편중 재발 여부)·격자 빈 칸 율·갈림길
사살/넓히기 수행 여부(단일 ReAct 유지 결정의 실측 조건, 2026-07-24 합의).
주행 실측이 나머지 신 도구(describe_target·list_changes·compare_peers·
expand_topology·상황 메뉴 3 — sample_logs·list_events는 구현 완료,
find_targets는 §3.2 발동 조건부 보류)의 구현 순서를 정한다. 유의: 신규 도구용 프롬프트 문안(Examiner의
discover_signals 시작 규칙 등 구 이름 참조)은 주행 전 점검 필요.

### 7.4 재주행 결과 (2026-07-30) — 도구를 11개 만들어도 궤적은 안 바뀐다

신 도구 11종 배선 완료 후 §7.3을 수행했다. **평가 케이스로는 못 했다** —
24개 케이스 전부 `golden.rca.json`이 없고, 캡처 창 안에 인시던트가 0건이다
(실측 4케이스: v3 둘의 인시던트 테이블이 07-24에서 끊기는데 창은 07-25,
v2 둘도 각각 51분·6시간 앞에서 끊긴다. 주입 구간의 인시던트가 격상되기 전에
덤프가 떠진 것으로 보인다). 그래서 07-27 첫 주행과 같은 방식인 **라이브 119
인시던트**로 갔다(`6b8e87c9`, 07-30 04:29~05:50, 139멤버 — 구현 중
breakdown_endpoints가 독립적으로 잡아낸 gateway 409 급증 사건이다).

| 항목 | 07-27 기준선(신 4종) | 07-30 재주행(신 11종) |
|---|---|---|
| 소요 | 2m46s | 2m41s |
| 도구 호출 | 35회 / 7종 | 26회 / **4종** |
| 최다 도구 집중도 | scan_metrics 51.4% | scan_metrics **76.9%** |
| 미호출 신 도구 | 9종 | **똑같이 9종** |
| 격자 빈칸 | 27/39 = 69.2% | 38/57 = **66.7%** |
| events·logs 관점 | 0/13 | **0/19** |
| 결론 | provisional(H2 채택) | **insufficient**(원인 미확정) |

호출된 것은 `scan_metrics` 20 · `read_timeseries` 3 · `get_data_coverage` 2 ·
`get_processes` 1이 전부다. 배선 문제가 아니다 — 모델이 시스템 프롬프트에 없는
`get_processes`를 불렀으므로 Toolset 전체가 노출돼 있었다.

**이로써 §3.2의 발동 조건(미호출 + 오답 동시 실측)이 충족됐다.**

**원인 진단과 그 검증.** Examiner 프롬프트(`llm/examiner.go`)가 "지표·이벤트·
로그 세 관점을 덮어라"라고 요구하면서 **지표 도구 이름만** 준다 —
`list_events`·`sample_logs`는 프롬프트 어디에도 없다(격자 배정표에는 둘 다
등록돼 있으니 부르기만 하면 칸이 채워진다). 관점별 도구 이름을 넣어 재주행하니
행동이 즉시 바뀌었다:

| | 이름 없을 때 | 이름 준 뒤 |
|---|---|---|
| scan_metrics | 20 | 19 |
| list_events | **0** | **19** |
| sample_logs | **0** | **19** |

19대상 × 3관점 = **57칸 전부 시도**. 빈칸을 만든 것은 모델의 판단이 아니라
프롬프트의 결손이었다.

**그리고 그 즉시 다음 벽이 나왔다 — 커버하면 컨텍스트가 터진다.**
`HTTP 400: maximum context length 131072 tokens, prompt contains at least
123073 input tokens`로 [3] examine이 죽었다. Examiner가 한 대화에 봉투 57개를
그대로 쌓기 때문이다:

| 도구 | 호출 | 응답 합 | 평균 | 최대 |
|---|---|---|---|---|
| scan_metrics | 19 | 101,869자 | 5,361 | 15,534 |
| sample_logs | 19 | 98,265자 | 5,171 | 9,799 |
| list_events | 19 | 81,371자 | 4,282 | 10,510 |
| 합계 | 57 | **281,505자** | | |

→ **이전의 빈칸 69%는 "아껴 쓴 결과"가 아니라 이 벽을 우연히 피해 가고 있던
상태였다.** 도구를 더 만드는 것으로는 이 자리를 못 넘는다 — 단일 ReAct 하나에
도구 16종을 주고 한 대화에 전 대상 × 전 관점을 쌓는 **구조**의 문제다.
다음 주제는 도구가 아니라 **에이전트 구조**다(2026-07-30 사용자 판단).

미착수 선택지(다음 세션): ①훑기용 축약 봉투(대상당 5.4k자 → 1~2k자, 무엇을
버릴지가 도구 계약 결정) ②Examiner를 대상 단위 서브 대화로 분할 ③대상 수 제한
(커버 포기라 §7.3의 물음과 어긋남).

> **후속 확정(2026-07-31)**: 이 자리의 답은 선택지 ①~③의 조합이 아니라
> **가설 구동 전환**으로 합의됐다 — [3]을 결정론 스크리닝 배터리로 교체하고
> 정밀 조사는 경쟁 가설이 요구하는 증거만. 정본은
> **spec-agent-structure.md** (외부 리서치 3방향 + Codex 비판 검토 +
> blind 3자 설계 패널 경유).

**이 절의 프롬프트 실험은 트리에 남기지 않았다** — 지금 넣으면 주행이 400으로
죽기 때문이다. 재현용 문안은 위 표와 이 문단이 정본이고, `pipeline/grid.go`의
`breakdown_endpoints` 관점 배정(§15 설계에서 빠뜨렸던 결정 — 원천은 트레이스지만
내놓는 것은 처리 구간별 지연·실패율이므로 지표 관점)만 반영했다.

## 8. 구현 설계 — list_events (2026-07-28 합의)

§3.3-④의 구현 설계. 결정 5개(응답 형태·이벤트/알람 구분·K8s 합성·
파라미터·0건 해석)를 사용자 문답으로 하나씩 확정했다. 원천 실측은
격리 세트 eval-f01r-samplelogs(CH :38123 — lucida_events_local 758행,
kcm_events_local 53행)로 확인: 정본 필드(transition open/close ·
episode_id · duration_ms · peak_severity · class · reason) 실재,
kcm_events는 클러스터 target UUID 귀속 + 주인공은
namespace/object_kind/object_name 문자열.

**결정 1 — 응답은 에피소드 단위로 접는다.** 같은 `episode_id`의
open/close 행을 한 사건으로 접어 [발단, 해소] 구간·지속시간·
peak_severity로 표현한다(§3.3-④의 "severity!='cleared' 우회 폐기"
이행). 창 안에 close가 없으면 "**창 안에서** 해소 기록 없음"으로
표기한다 — "진행 중" 단정은 창 밖 close를 놓치면 거짓이 되므로,
read_timeseries의 "onset이 창 밖" 표기와 같은 정직성 원칙. 창 확장
재호출은 조사자 몫.

3자 검토(아래 검토 기록) 보강 — 접기 세부 규칙:

- **전이별 필드 출처 비대칭(실측)**: `duration_ms`·`peak_severity`는
  close 행에만 산다(open 412행 전부 0·빈값 / close 345행 전부 채워짐).
  peak = close.peak_severity, 없으면 **open.severity 폴백**; duration은
  close 있을 때만, 없으면 "미상"(**0 출력 금지** — 빈 peak를 "경미"로
  오독하는 경로 차단).
- **경계 상태 4값**: `complete`(짝 완결) / `opened_before_window`
  (close만 창 안 — 발단은 `close_t − duration_ms`로 역산해
  `derived_open_at` + "발단 창 밖(역산)" 표식. gap≡duration 실측
  일치 검증됨) / `closed_after_window`(창 안 해소 기록 없음) /
  `identity_missing`(아래 zero episode).
- **다중 close**(실측 9건)는 최신 occurred_at 채택. open 중복은 실측
  0건이라 규칙 불요.
- **zero episode_id·빈 transition 행은 접기 제외** — 평면 단건으로
  반환(`unpaired`). alarm-bridge가 정확히 이 경우(writer가 에피소드
  필드 미기록, lucida-next 소스 추적 확인)라, 접으면 서로 다른 알람이
  한 사건으로 뭉친다. FIRING/RESOLVED는 attributes.status로 보완만.

**결정 2 — 성격 딱지로 증거 무게를 구분한다.** 각 사건에
`기록(fact)`(K8s 이벤트 등 실제 일어난 일) / `감지(detection)`
(stream/log/trace-anomaly — 화이트리스트×감도 통과분의 2차 신호,
§2.3 Q5 실측) / `외부알람(external)`(alarm-bridge) 딱지를 붙이고
summary에서도 분리 집계한다. 감지 딱지에는 "감지 신호는 단서 —
원본(지표·로그) 확인은 scan_metrics·sample_logs 몫" 안내를 붙여
확정으로 건너뛰는 다리를 끊는다(규칙 강제가 아니라 legibility로
유도 — D5).

보강: 판별자는 **detector × 출처 테이블의 완전 매핑표**다 — `class`
컬럼은 실측 상수(757행 전부 'anomaly')라 판별자가 아니다.
stream/log/trace-anomaly·forecast·change-detect(io-contract 정본
detector 전체) → 감지 / alarm-bridge → 외부알람 / kcm 출처 → 기록.
매핑에 없는 신규 detector는 감지로 폴백하되 detector 원문을 노출.
'기록' 딱지 설명에는 "발생 **기록**이지 원인 확정이 아님"을 포함
(Killing이 있었다 ≠ 그게 원인이다).

**결정 3 — K8s 합성은 대상 정체에 따라 세 갈래.** PG 명부로 대상
유형을 판별해 ① 클러스터 대상 → 해당 UUID의 kcm_events 전부 +
일반 이벤트 합성 ② pod/container/노드 대상 → 이름 매칭 분만 합성
③ 비K8s 대상 → kcm 테이블 미조회. 합성분은 `source: kcm` 출처
표식(성격 딱지와 별개), 이름 매칭 분은 확신도 표식(§3.3-⑤
list_changes name 귀속과 같은 관례 — 연결 신뢰도를 숨기지 않는다).

보강 — 매칭·중복·구획 규칙:

- **매칭 키 고정**: `(cluster_target_id, lower(kind), namespace,
  object_name 정확 일치)` — 이름 단독 매칭은 kind·namespace 경계를
  잃는다(실측: PG kind='pod' vs KCM kind='Pod' — case 정규화 필수).
  유형 판별의 PG 정본은 `targets.type`이 아니라(pod·container·node가
  전부 kubernetes_resource) `resource_kind`+`resource_key`
  (namespace/name)다.
- **container 대상**(§3.6 합의 범위)은 `resource_key`
  (namespace/pod/container)에서 부모 pod를 얻어 Pod 이벤트에 연결,
  `match_basis: parent_pod` 표식. KCM 이벤트에 UID가 없어 이름
  재사용은 완전 해소 불가 — 낮은 확신도 표식 유지.
- **재푸시 dedup**: kcm 원시 행은 사건 수가 아니라 누적 count의
  재푸시 스냅샷(실측 53행=실제 8그룹, lucida-next 화면 조회 선례도
  dedup) — `(kind, name, namespace, reason)` 묶음 + `max(count)` 후
  반환.
- **클러스터 조회는 namespace 구획**: 무관 namespace(모니터링 스택
  자신 등, 실측 23%)가 섞이므로 namespace별 묶음으로 반환하고
  인시던트 대상 namespace를 표식. **임의 제외는 하지 않는다**(system
  namespace가 진범인 케이스를 막지 않기 위해).

**결정 4 — 파라미터는 target·from·to 3개 유지.** "평소에도
울리나"(baseline)는 별도 인자 없이 창을 옮긴 재호출로 —
read_timeseries와 동일 철학(선례 일관·인자 최소화·소형 모델 오용
방지). baseline 자동 비교는 실주행에서 그 질문이 실제로 반복 실측될
때 봉투 부착으로 승격하는 발동 조건부 후보로만 둔다. top N 잘림 +
truncated 표식은 공통 규율대로.

보강: 시간 인자는 read_timeseries의 유연 파서(`now-15m` 허용)를
**재사용** — 선례라 강조해놓고 문법이 다르면 소형 모델이 상대 표기를
복사해 오류를 만든다(현행 get_events는 strict RFC3339였음). 잘림은
접기·dedup **후** 적용하고 정렬은 발단 최신순, 성격 구획별
반환/전체 건수를 메타로 노출(peak 정렬은 미해소 사건의 빈 peak가
먼저 잘리는 함정이라 기각).

**결정 5 — 0건은 normal이되 문구가 한정을 명시한다.** detector 상시
동작 전제로 0건은 "감지 시도 있었고 걸린 것 없음"의 진짜
관측(no_data 아님, 현행 유지). 단 summary에 "이상감지는 감시
목록(화이트리스트) 안만 본다 — 무혐의 뜻이 아니며 원본 확인은
scan_metrics·sample_logs 몫"을 명시해 false normal(용의선상 조기
제외)을 차단한다(§2.3 Q5 사각지대 실측이 근거). K8s 합성을 건너뛴
경우(유형 판별 실패)는 0건과 섞지 않고 "K8s 조회 시도 안 함"을 따로
표시한다(조용한 누락 금지).

보강(경량 수용): 원천별(일반 이벤트/kcm) **조회 시도 여부**를 응답에
표시한다 — "K8s 조회 안 함"의 일반화. Codex의 원천별 coverage·TTL
전면 구조화(observed_range·zero_meaning per source) 제안은 취지
정합하나 지금은 과설계로 판단, 실주행에서 TTL 경계 오독이 실측되면
승격하는 발동 조건부 후보로 기록.

**검토 기록 (2026-07-28, 3자):** 자체 문답(결정 5개 확정) 후 독립
Claude 서브에이전트(격리 CH 실측 대조 수행)와 Codex(gpt-5.6-sol,
lucida-next 소스 추적 수행) 교차 검토. 양쪽 모두 골격 5결정 "방향
정합, 통과" — 보강 8건 반영(위 각 결정의 보강 문단): 전이별 필드
비대칭·close-only 역산·zero episode 분리(양측 일치)·namespace
구획(양측 일치)·container 매칭·재푸시 dedup·detector 매핑·시간 파서
통일. Codex의 원천별 coverage 전면 구조화는 경량 수용(결정 5 보강).
Codex 원문: `.omc/artifacts/ask/codex-rca-docs-spec-tool-redesign-md-8-*-2026-07-28T03-59-*.md`.

자리 교체(§7.1): list_events가 들어오면 get_events가 빠진다. K8s
이벤트 합성으로 get_k8s_state의 이벤트 몫 일부도 흡수하나,
get_k8s_state 자체는 pod 상태 지표 조회가 남으므로 유지 — 완전
대체는 read_timeseries의 kcm.* 라벨 분해가 담당(§3.6 K8s 도구 0
합의)할 때 재평가.

## 9. 구현 설계 — describe_target (2026-07-28 합의)

§3.2-②의 구현 설계. 결정 5개(소속 합성·한도 섹션·한도 시점·인자
형태·0건/미등록)를 사용자 문답으로 하나씩 확정했다. 결정 2는 문답
중 사용자 요청으로 3자 검토(독립 Claude 서브에이전트 + Codex
gpt-5.6-sol)를 먼저 거쳐 수정 채택했다(검토 기록은 아래).

**결정 1 — 소속 합성은 "위로는 전부, 아래로는 개수+유형만".** 상위
소속은 **4원천**(`meta->>'host_target_id'` · `server_resources` ·
`kcm_resource_targets` · `asset_tree_members` — §2.2 실측 목록,
초안의 "3원천"은 `server_resources` 누락 축약이었음) 전부 합성.
하위는 목록 폭발 방지를 위해 개수+유형 요약 기본.

3자 검토(아래 검토 기록) 보강 — 합성 규칙:

- **관계는 typed로, 평평한 목록 금지**: 네 원천은 성격이 다른
  관계다(호스트 귀속/서버 리소스/K8s 부모 체인/서비스그룹 멤버십).
  행마다 `relation_kind`(host | direct_parent | cluster |
  service_group)·원천·확신도를 붙인다 — 소형 모델이 첫 줄만 읽고
  아무 원천이나 "부모"로 삼는 것 방지.
- **self-reference 제외**: 최상위 대상은 `host_target_id=self`가
  주입될 수 있다(lucida-next DDL 159_targets_meta_contract 명시,
  Codex 실측) — 자기 자신은 상위에서 제외하고 `self` 표식만.
- **원천 간 불일치는 감지·표식**: meta 호스트와 KCM 부모가 다르면
  둘 다 반환하되 `conflict` 표식 — 어느 쪽이 정답인지 도구가
  판정하지 않는다(연결 신뢰도를 숨기지 않는다, §8 결정 3 관례).
- **K8s 부모 체인은 루트(클러스터)까지 따라가되** 깊이 한도(체인
  실측 최대 4: container→pod→node→cluster)와 사이클 가드, 미등록
  부모는 `missing` 표식.
- **원천별 조회 시도 여부를 반환**(checked / not_applicable /
  error) — §8 결정 5 보강("K8s 조회 안 함" 표시)의 일반화. 소속은
  **현재 스냅샷**임을 응답에 명시(당시 소속 이력은 없다 — 정직).
- **유형 판별은 §8 결정 3 규칙 상속**: PG 정본은 `targets.type`이
  아니라 `resource_kind`+`resource_key`, kind case 정규화 — 같은
  명부를 읽는 두 도구가 규칙 두 벌을 갖지 않는다.
- **하위 id 문턱**: 하위가 소수(≤10)면 유형 요약에 UUID 목록 동반,
  초과 시 유형별 개수 + `truncated` 표식. 근거 — "필요 시 UUID로
  재호출"이 성립하려면 UUID 획득 경로가 있어야 하는데 find_targets
  보류·expand_topology 미구현 상태라 §2.0-3 id 연쇄 규율이 여기서
  끊긴다(양 검토 일치 지적).

**결정 2 — 한도 섹션은 % 없는 "한도 카탈로그"다 (3자 검토 후 수정
채택).** 원제안(read_timeseries 짝 표 재사용 + 사용 % 부착)을 검토
결과로 축소했다:

- **카탈로그만**: 대상에서 관측되는 지표 중 짝 고정표(§6.3,
  tools/capacity.go 7짝)가 아는 것을 usage·limit **양방향** 탐색
  (limit만 관측돼도 행 반환 — Codex 보강), 행 = 짝 지표 이름 +
  limit 값 + 관측 여부. **% 계산은 하지 않는다** — 근거 2겹:
  ①`joinCapacity`의 창 내 last 기준은 넓은 스크리닝 창에서 피크를
  놓친다(F01-R 실측: 주입 종료 후 창이면 HikariPool 100%가
  사라짐 — "68% 여유"가 포화 배제를 유발) ②두 도구의 창이 달라
  같은 지표에 % 이중값이 대화에 돈다(`ratioMetricHints` 주석이
  도구 안에서 막은 위험을 도구 사이에서 다시 여는 꼴). 정독·%는
  read_timeseries 몫임을 응답에 안내. §3.2-②의 "Q11 분모로
  이어진다" 취지는 유지된다 — 목적은 조사자가 한도를 *볼 생각을
  하게* 만드는 것이고, 지표 이름이 손에 쥐어지면 달성된다.
- **전수성 착시 차단(고정 고지)**: 개별 지표마다 "모름"을 나열하면
  폭발하고(관측 지표 100~200종), 침묵하면 현행 `attachCapacity`의
  nil과 같다. 대신 **집계 문장 + 섹션 고정 고지** — "관측 지표
  N종 중 한도 짝을 아는 것 k종. 이 표는 검증분 7짝뿐 — 여기
  없다고 한도가 없는 게 아니다." (일괄 나열은 read_timeseries에는
  없던 전수성 착시 — 목록에 없으면 한도 무관으로 배제 — 를 새로
  만든다는 양측 일치 지적.)
- **간접값 표식은 표 스키마 확장과 함께**: 현행 `capacityPairs`에
  원천 등급 필드가 없다. exporter가 보고한 설정 한도(kcm limit류)와
  분모 불명 파생 비율은 증거 성격이 다르므로 표에 원천 등급을
  추가하고 행에 표식. 검증 항목 1건 동반: 표의
  `postgresql.db.connections.max`가 §2.2가 "직접값 없음"이라 한
  서버측 max_connections와 동일값인지 실측 확인 — 다르면 그 행은
  간접값 강등. **→ 확인 완료(2026-07-29 구현 중)**: AP 119 서버
  `SHOW max_connections`=200 = 메트릭 값 200 — 직접값 확정
  (`reported_config`). §2.2 Q4의 "직접값 없음" 간극 기록은 스냅샷
  기준의 옛 관찰 — DPM 수집이 보고하고 있었다.
- **구현 경계(검수자 주의)**: 재사용은 짝 표 + `joinCapacity`(순수
  함수) + `foldGrade`까지. `attachCapacity`는 지표→한도 방향이라
  대상→일괄 조립은 새 코드다. **`foldScan` 금지** — 지표당 실차원
  avg가 pool_name·container_id 조인 축을 뭉갠다. `metricUnits`는
  배치 1회.
- **설정 스냅샷 파싱은 보류하되 사유·발동 조건을 정정**: 사유는
  "필요 미증명"이 아니라 **원천 실측 부재**다 — §2.0-1 실측:
  `collector_dpm_pg_config`에 max_connections 컬럼 자체가 없고
  `kcm_resources.yaml`은 containers null(28/28). 필요가 증명돼도
  파싱할 것이 없으므로, 한도 결손 오답이 실측되면 향할 곳은 파싱
  구현이 아니라 **수집 측 청구(§2.9)**다. 발동 조건 병기: "수집
  측이 해당 값을 스냅샷에 담기 시작함". 반사실 조건("한도를 알았
  으면 맞혔을까")은 관측 불가하므로 **커버리지 격자에 한도 섹션
  unknown율 계측을 지금 심는다** — golden 원인이 한도·포화 계열인
  케이스에서 한도 섹션이 빈손이던 run을 계수할 수 있어야 조건이
  켜진다(26B 첫 주행 "격자 빈칸 69%"가 계측 선행의 근거).
  **계측 통로 명시(§9 검토 보강)**: 현행 격자(pipeline/grid.go)는
  호출 이름·인자만 보고 응답을 보지 않으므로 격자로는 불가 — run
  산출물에 도구 응답 요약(한도 섹션 k/N·unknown 여부)을 보존하는
  독립 계수기로 심는다. 통로 없인 발동 조건부가 영구 보류가 된다.
- **"관측 지표 N종" 집계는 접고 센다**: `.bands`(§2.3 실측
  562종)·`.raw` 변종과 grade 팬아웃이 인벤토리를 부풀리므로,
  N은 scan_metrics와 같은 접기(§5.6 grade 접기·.bands 접기) 후
  기준으로 계수 — 안 접으면 "짝 아는 건 k종뿐" 인상이 왜곡된다.

**결정 3 — 한도 시점은 "창 하나 + 변화 1급"(§3.2-② as_of 요구의
재구성).** limit 값 = 조사 창 내 관측치. 창 안에서 바뀌었으면
**"변경 감지: 전→후 값 + 시각"을 1급으로 표시**(다중 변경·원복
100→200→100 포함 — 시작·끝 비교가 아니라 전 표본 스캔). 별도
"현재 시점" 재조회는 없다 — 재생에선 현재=캡처 끝일 뿐이고,
라이브는 창을 인시던트+여유로 잡으면 사후 변경이 창 내 변화로
잡힌다. §3.2-②·§4의 as_of 취지(사후 설정 변경 은폐 금지)는
유지하고 형태만 카탈로그에 맞게 단순화한 것. §6.3의 "한도 정적성
실측(32분 창 내 변화 0)"은 한 창 내부의 정적성일 뿐 이 요구를
면제하지 않는다(검토 지적).

3자 검토 보강 — 단정 하향과 구현 경계:

- **"창 내 불변"은 과단정 — "관측 N표본 동일"로**: 관측 1점은
  불변의 증거가 아니다(간헐 방출 limit 실재 — §5.6.1 intermittent
  실측과 같은 계열). 행에 표본 수·처음/마지막 관측 시각을 병기하고,
  표본이 희박하면(§6.4 episodic 문턱 상속) `unknown_sparse` —
  "동일"은 관측된 만큼만 주장한다. §6.3 검사 ③(오래된 한도로 계산
  금지)도 상속.
- **`current_not_checked`를 응답에 명시** — 현재값을 안 본 것을
  침묵하지 않는다(§3.2-② 원 3분법을 아는 독자가 "확인됐다"고
  오독하는 것 방지).
- **변화 감지는 `joinCapacity` 재사용 불가(새 코드)**: 그 함수는
  그룹당 last 값 하나만 뽑는다(capacity.go) — 창 내 시계열 전체
  스캔이 필요하며, 변화는 조인 키(라벨 조합) 단위로 본다. 결정 2의
  "구현 경계"에 이 항목 추가.

**결정 4 — 인자는 target_id 단수 + 일괄 스크리닝은 Triage 브리핑이
흡수.** 깊이형 도구(섹션 다수)라 일괄이면 응답이 배수로 부풀어
소형 모델 컨텍스트를 압박한다. 여러 대상 나란히 보기는
compare_peers 몫(겹침·병존 금지 — §7.1과 같은 원칙). 단수면
0건·미등록 처리도 부분 성공 없이 단순하다. 시간 인자는
read_timeseries 유연 파서 재사용(§8 결정 4 보강과 같은 근거).

3자 검토 보강 — 실측 충돌 해소(사용자 합의):

- **프론티어 실측과의 충돌**: Opus 두 주행 모두 get_target_meta를
  **일괄 스크리닝**으로 썼다(F01-R "이웃 전부 → 전원
  type=application, DB 없음" 확정 / F04-R "10 peers" 이웃 파악).
  단수 전환 + 자리 교체는 이 경로를 없앤다.
- **해소 = Triage 브리핑 노출**: Triage([1])가 조사 대상들의
  정체·유형·도메인을 이미 결정론으로 계산한다(TargetDomains).
  이를 조사자 첫 브리핑에 **결정론적으로 포함**해 "정체 뭐야?"
  일괄 질문 수요 자체를 없앤다. describe_target의 계약은 "추린
  소수의 상세 조회"로 명시 축소. (검토 대안이던 얕은 일괄 모드는
  도구 복잡화라 기각, find_targets 보류 해제는 발동 조건 미충족.)
- **내부 typed batch 경로는 유지**: `TargetMetaFunc(ids []string)`
  (pipeline·narrate 소비)는 LLM 표면과 무관한 내부 계약 — 단수
  전환의 영향 없음(cmd/rca 자체 SQL 실측 확인). LLM 도구 표면의
  단수와 내부 저장소 조회의 batch를 혼동하지 말 것.

**결정 5 — 0건·미등록은 기존 관례의 조립(단 미등록은 의도적 계약
변경).**

- **형식 오류(UUID 아님)**: 오류 + 형식 안내 + "topology 가짜 노드
  ID(`db:oracle`류, target_id 없음 — [1] Triage 구현 시 실측)일 수
  있음 — target_id는 seed 멤버·이전 도구 응답의 UUID만" 복구
  안내(오류엔 복구 정보 원칙, spike ④). 현행 get_target_meta 오류
  문구와 정합.
- **미등록(UUID인데 명부에 없음)**: 정상 응답 "미등록 대상"
  (`lookup_status: not_found` — "대상이 정상"으로 오독 방지) +
  복구 정보 = 등록 대상 유형 분포. **주의: 관례 재사용이 아니라
  변경이다** — 현행 targetMeta는 전건 미등록 시 error 반환
  (유형 분포는 오류문에 실림). 정상 응답 전환은 의도적 개선이며
  테스트 갱신 대상(검토 정정).
- **정상 ID인데 섹션이 빔**: 섹션마다 명시적 0 표현(침묵 금지) —
  소속 0은 "상위 소속 기록 없음(4원천 checked/not_applicable/error
  내역 동반)"으로 확인 범위를 밝히고, 하위 0은 "하위 대상 0", 한도
  0행은 결정 2의 고정 고지가 커버.
- **no_data 사유는 아는 척하지 않는다(검토 하향)**: 창 내 대상 지표
  관측 0의 사유(zero_observations vs collector_gap) 판별 재료는
  collectors 상태 = **get_data_coverage 몫**이고 describe_target의
  조회 원천(targets·kcm·asset_tree·VM)에는 없다. 따라서 사유는
  `unknown` + "판별은 get_data_coverage로" 안내(server_network.go
  기존 관례와 일치). **"지표 0 = 대상 죽음" 자동 판정 금지** — 실제
  정지와 수집 공백이 같은 모양이다. 결손 표현은 봉투 전역
  no_data_reason이 아니라 **섹션 단위**(정체·소속은 정상인데 한도만
  빈 부분 결손이 일반형).

**검토 기록 1 (2026-07-28, 결정 2에 대한 3자):** 독립 Claude
서브에이전트(레포 코드 실측 대조— capacity.go last 기준·foldScan
축 뭉갬·F01-R 창 의존 재현 경로 확인)와 Codex(gpt-5.6-sol, 스펙
§3.2/§6.3 대조) 교차 검토, 양쪽 모두 "수정 채택". 일치 지적 5건
(함수 재사용 불가·모름 명시 형태·as_of 누락·간접값 뭉뚱그림·보류
발동 조건) 전부 반영, 갈린 지점 1건(% 유지 여부)은 Claude안(%
제거 = 카탈로그)을 채택하고 Codex의 양방향 탐색·limit-only 행
반환을 흡수. Codex 원문:
`.omc/artifacts/ask/codex-describe-target-2-*-2026-07-28T23-45-35-*.md`.

**검토 기록 2 (2026-07-29, §9 전체 3자):** 결정 1·3·4·5 + 절 정합
초점. 양쪽 모두 "보강 후 통과"(재설계 불요) — 일치 지적: 원천 4번째
누락(전수 문구 착시), typed 관계·self-reference·충돌 표식, 하위
UUID 획득 경로 부재(id 문턱), "창 내 불변" 과단정(표본 밀도 병기),
joinCapacity로 변화 감지 불가, 단수 인자 vs 프론티어 일괄 스크리닝
실측 충돌(→ Triage 브리핑 흡수로 합의), no_data_reason 판별 재료
부재(→ unknown+안내 하향), 미등록 처리는 관례 변경, §3.2 원문 표식,
격자 계측 통로. 위 각 결정의 보강 문단으로 전부 반영. 자리 교체
안전성은 코드로 확인(아래). Codex 원문:
`.omc/artifacts/ask/codex-spec-tool-redesign-md-9-*-2026-07-29T00-11-11-*.md`.

자리 교체(§7.1): describe_target이 들어오면 get_target_meta가
빠진다 — 정체·소속 조회 몫을 포함하고 미등록 복구 정보(유형 분포)
관례를 승계하며, **일괄 스크리닝 몫은 도구가 아니라 Triage 브리핑이
승계**(결정 4). 검토 실측 확인: 파이프라인 typed 경로(cmd/rca
metaFunc 자체 SQL·`TargetMetaFunc` batch 계약)는 이 도구와 무관 —
교체로 안 깨진다. 격자 배정표에 get_target_meta 없음 — 격자 영향
없음. 배선 시 잔손질 4건: ①`tools/server_network.go`의
get_target_meta 지명 안내 문구 치환(§7.2 "구 이름 참조" 재발 방지)
②현행 응답의 `status`·`in_maintenance` 필드는 정체 섹션에 승계
③`tools/registry.go` 머리 주석 정정(현행도 코드 실태와 다름)
④§7.2 교체표에 본 행 추가.

## 10. 구현 설계 — list_changes (2026-07-29 합의, 3자 검토 반영 개정)

§3.3-⑤의 구현 설계. 결정 5개(조회 범위·무엇을 변경으로 볼지·원천 교차
접기·기본 창·응답 규율)를 사용자 문답으로 확정한 뒤, 3자 검토에서 **결정
2·3이 재작성**되고 결정 1의 논거가 교체됐다(검토 기록은 절 끝).

**원천 실측** (라이브 119 PG `lucida`, 2026-07-29 — 검토자가 독립 재현):
`change_history` 143행(2026-07-13~07-23 전 기간), `policy_deployments`
338행(전부 07-14 00:35:39~00:36:57, 78초), `collectors` 50행(대상 50개,
`created_at`만), `targets` 1,442건.

### 10.1 사건 단위 실측 — 결정 1의 근거를 다시 센 결과

**초판의 "행 42/143 = 71% 거짓 음성"은 오류다** — 행 수를 사건 회수율로
착각했다(검토 일치 지적). 같은 사건이 여러 원천에 중복 기록되므로 **접기
후 고유 사건**을 분모로 다시 셌다:

| 구분 | 사건 수 | 내역 |
|---|---|---|
| **현행(대상 지목)이 이미 찾는 것** | **75** | 정책 배포 13(`pd.target_ref` UUID) · 수집기 등록 20(`collectors.target_id`, kind×초 접기) · 구성 대상 41(name 일치) · K8s 1 |
| 못 찾는 것 | 39 | ↓ |
| ├ **재료가 있는데 조회 경로가 없어 놓침** | **23** | Live-SQL — `name`이 대상 UUID 문자열(23/23) + `detail.target_id`(23/23)인데 현행은 `name`을 targets.name/display_name/address와만 대조(`changes.go:130`)해 **0건 반환** |
| ├ 대상 개념이 없는 전역 사건 | 8 | 자산 트리 편집 6(접기 후) + 사용자 2 |
| └ 삭제된 대상 | 8 | `tb-w1-commerce`·`tb-w2-food`·`tb-w3-banking`·`rca-testbed`의 등록↔삭제 쌍 — 지목할 대상이 이미 없음 |

**이 표가 결정 1의 논거를 바꾼다.** 23건은 전역 반환이 아니라 **귀속 경로
추가**(UUID 대조)로 풀린다 — 대상 지목 방식으로도 도달 가능하다. 전역
반환이 *유일* 해법인 것은 전역 사건 8건뿐이다. 초판이 이 23건을 결정 1의
근거로 쓴 것은 과장이었다.

**폐기한 논거 2건**(검토 일치 지적):

- "이름 귀속 29%로 71% 거짓 음성" — 위 재계산으로 폐기.
- "프론티어가 get_changes 0건을 받고 배포 가설을 닫았다 = 도구가 부른
  오판" — **그 창에 실제 변경이 있었다는 ground truth가 없다.** 실측
  정책 배포는 07-14인데 두 궤적(F01-R·F04-R)은 07-21이고, 궤적 문서는
  오히려 "변경 없음"을 검증된 사실로 기록한다. 새 24시간 기본창으로도
  07-14 배포는 안 보인다. **"오판" → "0건의 강한 해석 위험"으로 하향**하고,
  반사실 재생(F01/F04 창에 새 알고리즘 적용)으로 결론이 달라지는지
  입증하기 전에는 이 논거를 쓰지 않는다.

### 10.2 실측 정정 — §2.3 Q10·§3.3-⑤ 기술의 오류 4건

**상위 절도 함께 고친다**(검토 지적: §10의 각주성 정정만으로는 정본 충돌이
남는다 — §2.3:194 · §2.9-3:332 · §3.3-⑤:523).

1. **`change_history`의 대상 귀속 재료는 name 문자열뿐이 아니다.**
   `detail->>'target_id'` 24행(Live-SQL 23 + 쿠버네티스 1) + **`name` 칸이
   대상 UUID 문자열인 23행**(Live-SQL). 정확 귀속 경로는 3개가 아니라
   **5개**: `pd.target_ref` · `collectors.target_id` ·
   `ch.detail.target_id` · `ch.name`이 UUID일 때 `targets.id` 대조 ·
   `ch.name` 연성(name/display_name/address). 다수인 `구성 대상`(49)·
   `수집기`(27)·`정책 템플릿`(13)이 name뿐인 것은 맞다.
2. **`change_history`에 조회성 감사가 섞여 있다** — 단 **`action`은 그
   판별자가 아니다**(초판 오류, 결정 2에서 재작성).
3. **`policy_deployments`는 이력이 아니라 현재 상태 표다.**
   `UNIQUE(template_id, role, target_kind, target_ref)`이고, 생산 코드가
   **재배포 시 해당 template의 기존 링크를 삭제 후 삽입**한다(검토 실측:
   `lucida-next .../store/policy_templates_store.go:1553`). 따라서 과거
   창 조회 결과는 "당시 부착 상태"가 아니라 **"현재 남아 있으며 그
   `deployed_at`이 그 창에 든 링크"**일 뿐이다. 재배포·해제·대상 이동은
   전부 거짓 음성을 만든다.
4. **현행 get_changes는 이 잡음을 흘리지 않는다**(초판의 "25행을 흘린다"는
   거짓): Live-SQL 23행의 name은 UUID 문자열, 사용자 2행은 `test`라
   현행 name 경로로 **0행** 반환된다. 잡음은 결정 1이 **새로 만드는**
   문제이지 현행 결함이 아니다.

### 10.3 결정 1 — 대상은 필터가 아니라 정렬 힌트다 (유지, 논거 교체)

`target` 필수 인자로 걸러내던 현행 표면을 뒤집는다: 창 안의 변경을 전부
반환하고, `target`이 주어지면 귀속된 것을 위로 올리되 나머지도 "같은 창의
다른 변경"으로 함께 보인다. **남은 논거는 규모·매칭률과 무관한 두 개다:**

- **옆 대상 변경 도달 불가(구조적).** 정책 배포 1건이 26대상을 건드린다
  (실측). 지목 대상이 그 26에 없으면 화면에 아예 안 나오는데, 이웃 변경이
  원인인 것은 RCA의 일반형이다. 데이터가 크든 작든, 이름이 맞든 안 맞든
  성립한다.
- **전역 사건 8건은 원리적으로 지목 불가**(10.1) — 자산 트리 편집·계정
  관리는 대상 개념이 없다.

**규모 논거는 쓰지 않는다**(검토 일치 지적 — 과적합). 143행 중 95행이
테스트베드 구축 활동이고, `policy_deployments`는 상태 표라 대상 1,442건
규모의 정책 운용에서는 *이력*이 아니라 *인벤토리 크기*를 따라 커진다.
전역 반환의 안전성은 밀도 추정이 아니라 **결정 5의 잘림 계약**이 보장한다.

`scope`/`mode` 파라미터로 가르지 않는다 — sample_logs의 map/grep이 깔때기
*단계*를 가르는 것과 달리 여기서는 같은 질문의 두 표현이고, 어느 쪽이
맞는지의 근거를 조사자는 호출 전에 알 수 없다.

**동반 필수 작업(결정 1과 별개):** 10.1의 23건을 살리는 **귀속 경로 보강** —
`ch.name`이 UUID 형식이면 `targets.id`와 대조, `detail.target_id`도 읽는다.
이건 전역 반환 여부와 무관하게 필요하다(`SESSION_KILL`이 여기 산다).

### 10.4 결정 2 — 무엇을 감추는가 (재작성: 제외 → allowlist + 강등)

**초판 폐기.** 초판은 `action=''`을 "정의상 아무것도 바꾸지 않았다"로 단정해
기본 제외했으나, 이는 **추론이 뒤집힌 것**이고 실제 파괴 작업을 숨긴다.

**결정적 반례(생산자 소스 실측, 검토가 발견 — 데이터 관측으로는 잡을 수
없었다):** DB **세션 강제 종료**가 `Category: "데이터베이스 Live-SQL"`,
`Operation: "SESSION_KILL"`, **Action 미기록**(→ `''`)으로 적힌다
(`lucida-next .../collector-sms/handler/agent_dpm_commands.go:209`,
`buildKillAuditDetail`이 `{target_id, sql_id, sessions, killed, skipped,
errors}` 동봉). 세션 강제 종료는 앱 커넥션 절단의 직접 원인 — **초판 규칙은
이걸 두 겹으로 숨긴다**(action='' 제외 + Live-SQL 카테고리 제외). 라이브
119의 Live-SQL 23행은 전부 조회성이라 **실측만으로는 영영 못 잡을
함정이었다.** 스키마상으로도 `action`은 기본값이 빈 문자열인 배지일 뿐
NOT NULL·CHECK 제약이 없다.

**확정 규칙 — 미지의 것은 노출 방향으로 보낸다:**

- **감출 근거는 `category + operation`의 검증된 읽기 전용 allowlist뿐이다.**
  실측 조회 operation: `CURRENT_SESSION_AND_LOCK`(7)·`PARAMETER`(6)·
  `BACKEND_MEMORY_INFO`(2)·`CURRENT_SESSION_SUMMARY`(2)·
  `SHARED_MEMORY_INFO`(2)·`ORACLE_{TABLE,SCHEMA,USER,PARAMETER}`(각 1).
  **`SESSION_KILL`은 allowlist에 없으므로 항상 보인다.**
- **allowlist에 없는 `action=''`은 `action_missing` 표식과 함께 기본
  노출**한다(§8의 "매핑에 없는 신규 detector는 감지로 폴백하되 원문 노출"과
  같은 처리). 신규 카테고리가 action을 안 채워도 변경이 조용히 사라지지
  않는다.
- **`사용자` 카테고리는 제외하지 않고 `scope: control_plane`로 낮게
  정렬**한다(검토 지적). 이 카테고리는 로그인만이 아니라 계정 잠금·역할·
  그룹·비밀번호 정책을 포함해 인증·권한 장애의 직접 원인일 수 있다
  (`lucida-next .../query/audit/descriptors.go:207`).
- **감춘 것의 메타는 건수만으로 부족하다** — allowlist 제외분은 건수 +
  첫/끝 시각 + operation 종류를 싣고, `include_audit_activity: true`로
  전량 열람 가능하게 한다(파라미터명은 `include_all`에서 변경 — 접기까지
  푸는지 오해를 없애기 위해 범위를 이름에 박는다).

**Live-SQL 조회 기록도 무가치하지 않다**(검토 지적): `CURRENT_SESSION_AND_LOCK`
7건은 "그 시각 누군가 그 DB의 세션·락을 들여다봤다"는 **동시 대응 활동
흔적**이고 대상 UUID도 붙는다. allowlist 제외는 "무관"이 아니라 "기본
화면에서 내림"이다.

### 10.5 결정 3 — 원천 교차 접기와 provenance (재작성: 3축 분리)

**관찰은 유효하다.** 원천 3곳은 독립이 아니라 같은 사건을 다른 단위로
적는다 — 수집기 등록은 `collectors` 07:13:35.350(UUID 있음)과
`change_history` 07:13:35.353(이름만)에 **3ms 차이**로 이중 기록되고,
정책은 `change_history` 13행(실행 횟수) ↔ `policy_deployments` 338행
(닿은 대상)이다. 현행 도구는 전자를 2건으로 반환하고(중복), 후자 창을
조회하면 338줄을 쏟는다.

**초판의 짝짓기 규칙은 폐기한다.** 검토가 실측으로 깼고 재현 확인했다:

- **이중 조건의 두 번째 항이 판별력 0이었다.** `ch.name`(수집기 카테고리)과
  `collectors.kind`는 값 집합이 문자 그대로 동일하고, 정책의
  `ch.detail.include`는 13행 전부 26 · pd 묶음 크기도 전부 26 —
  **상수 대 상수**다. 조건 유무로 후보쌍 305개 동일. "실측 일치"는
  검증이 아니라 항진명제 확인이었다.
- **수집기는 1:1이 아니라 다대다다.** ch 27행당 후보 collector 수 분포:
  **1개 = 7행 / 3개 = 3행 / 17개 = 17행**(초 단위 버스트 등록 —
  `polestar_apm` 19건이 2초 안에 몰림). 초판의 "40행이 UUID를 얻는다"는
  거짓 — 실제는 **20**(수집기 7 + 정책 13).
- 정책은 시각 단독으로 1:1이 성립한다(묶음당 ch 1개, ch당 묶음 1개, 13건
  전부).

**확정 규칙:**

1. **짝짓기 결과를 감추지 않고 3분류로 반환**: 후보가 정확히 1개일 때만
   `unique_inferred`로 접고, 0개는 `unmatched`, **2개 이상은 `ambiguous`**로
   후보 목록과 함께 별도 반환한다. 억지 1:1을 만들지 않는다.
2. **수집기는 1:1 짝짓기를 포기하고 `collectors` 쪽도 접는다** —
   `(date_trunc('second', created_at), kind)` 묶음 = 1 사건("07:36:03
   polestar_apm 수집기 19개 등록 — 대상 19개"). 오짝 대신 정직한 **집합
   귀속**이 되고 정책 접기와 동형이다. 실측 20 사건. **`collectors` 50행 중
   23행은 ch 짝이 아예 없다**(otel_recv 13·sms 7·db_poll 2·kcm 1) —
   `unmatched`로 정상 반환한다(누락 아님).
3. **정책 묶음 키에 `template_id + role + target_kind`를 포함**한다(초 단위
   시각만으로는 동시 실행이 뭉친다). 감사 detail에는 template ID가 없고
   개수만 있으므로(`lucida-next .../handler/policy_templates.go:215`)
   ch↔pd 연결은 언제나 추정이다 — `correlation`에 그대로 표기.
4. **2초 임계는 잠정값**이다 — Δt 분포·동시작업 충돌 실측으로 확정하기
   전까지 값과 근거를 응답 메타가 아니라 코드 주석·본 절에 남긴다.
5. **자산 트리는 "내용 동일"이라 하지 않는다**(초판 오류). 실측 detail은
   6종(`members` 0→1→2→3 단조 증가, 01:15:36~01:18:02)으로 **사람이
   멤버를 하나씩 추가해 간 편집 과정**이며, 생산 코드도 트리나 해시가
   아니라 **개수만** 기록한다(`.../handler/asset_tree.go:96`) — 멤버 구성이
   바뀌어도 개수가 같으면 같은 detail이다. 따라서 ①문구는 **"기록된 요약
   동일"**로 낮추고 ②접기 키는 `category+action+operation+name+detail`
   전체 ③표기는 변화를 보이는 형태(**"자산 트리 저장 28회 — members 0→3,
   01:15:36~01:18:02"**) ④`count`·첫/끝 시각·원본 `refs` 보존.
   `episode_id`라는 사건 식별자가 있는 §8 접기와 **동형이 아니다.**

**provenance는 한 값이 아니라 3축이다**(검토 일치 지적 — 초판의 4값은 사건
상관·대상 귀속·범위를 뒤섞었다. §8은 `source`/`match_basis`/
`match_confidence`를, §9는 `source`/`consistency`를 분리한 전례):

| 축 | 값 |
|---|---|
| `scope` | `target` \| `global`(자산 트리류) \| `control_plane`(사용자 카테고리) |
| `correlation` (원천 간 사건 짝짓기) | `exact`(같은 원천 내) \| `unique_inferred` \| `ambiguous` \| `unmatched` + `basis`·`delta_ms` |
| `match_basis` (대상별 귀속) | `exact_id`(pd.target_ref·collectors.target_id·detail.target_id·name이 UUID) \| `name`(name/display_name/address 연성 — rename·삭제 시 끊김, 실측 rename 1건: `rca-testbed → prod-k8s-01`) |

두 축이 독립이라는 점이 요점이다 — 정책 사건은 ch와의 연결이 추정
(`unique_inferred`)이어도 그 안의 `pd.target_ref`는 `exact_id`다. 각 사건에
`sources[]`·원본 `refs[]`·원천별 행 수를 보존한다.

### 10.6 결정 4 — 기본 창은 firstEvent 기준 24시간 소급

변경은 다른 관점과 시간 성질이 다르다 — **원인 선행사건**이라 인시던트
창 안에 없는 것이 일반형이다(배포는 새벽, 발현은 오전; 누수·고갈은 축적형).

**기준점 정정**(검토 일치 지적): 초판은 `to` 기본을 "인시던트 창 끝"이라
썼으나 **Toolset이 받는 시간 재료는 `firstEvent` 하나뿐**이고
(`tools/registry.go`, §7.2) `cmd/tool`에도 last-event 입력이 없다. 논거에
맞는 기준점도 `to`가 아니라 발단이다(창이 6시간이면 `to−24h`는 발단 기준
18시간 소급에 불과). 따라서 **기본 창 = `[firstEvent − 24h, to]`**, `to`
미지정 기본은 replay면 seed `LastEventAt`, live면 `now`로 하고 응답에
`window_basis`로 출처를 밝힌다.

밀도 수치는 정정한다(초판의 "1시간 0~2건·24시간 평균 14건"은 143행을
11일 달력 폭으로 나눈 희석 평균이라 같은 절 안의 "시간당 최대 41행"과
자기모순이었다): 실측 **시간당 최대 41 · 롤링 24h 최대 108(ch)·492(3원천)**.
접기 후에도 최악 창은 120줄 규모라 **결정 5의 잘림 계약이 필수**다.

응답에 **"[firstEvent−24h, to]에서 찾음 — 그 이전은 안 봤음, 더 보려면 창을
늘리세요"**를 명시한다(read_timeseries "onset이 창 밖"·list_events "창 안에서
해소 기록 없음"과 같은 정직성 원칙). 7일 기본은 미채택 — 오래된 변경이
최근 것과 섞여 근접성 신호가 죽는다.

### 10.7 결정 5 — 응답 규율 (신설: §8·§9 관례 상속)

초판이 통째로 빠뜨린 것(검토 일치 지적 — §8·§9는 전부 명시했다). **구현
착수 전 고정한다.**

- **인자 스키마**: `target?`(UUID — 형식 오류는 에러, §9 결정 5의 topology
  가짜 노드 ID 복구 안내 상속) · `from?` · `to?` · `include_audit_activity?`
  (bool, 기본 false) · `limit?`. 시간 인자는 **read_timeseries
  유연 파서 재사용**(`now-15m`류 — §8 결정 4 보강·§9 결정 4 관례). 기본값을
  도입해 시간 인자를 선택적으로 만드는 절이므로 파서 규율이 특히 필요하다.
  **정정(2026-07-30) — `cursor?`/`next_cursor` 삭제:** 초판이 선언했으나
  **구현(`tools/listchanges.go`)에 cursor가 존재하지 않는다**(전역 limit만
  적용). 재개 토큰의 정규화 인자·정렬 키·스냅샷 정책을 정하지 않은 채 필드명만
  적은 것이라, 계약이 아니라 종이 위의 선언이었다(§12 재검토가 이 전례를
  지적). **선언을 구현에 맞춰 내린다** — 페이지네이션이 실제로 필요해지면
  (§3.3-③ 유보 항목과 같은 발동 조건부로) 그때 계약째 설계한다.
- **정렬**: `target hint 관련 구획 → event_at DESC → 안정 tie-break(사건 ID)`.
  `target` 미지정이면 첫 구획을 건너뛴다. **동시각 tie는 반드시 결정론.**
- **잘림**: 원천별 quota + **서버측 사건 접기 후** `total_raw` ·
  `total_folded` · `returned` · `excluded` · `truncated`를
  **원천별로** 반환(§8 `listevents.go:31,411`의 quota·truncated 정본 상속).
  대량 대상 사건은 `targets_total` · 소수 UUID 표본 · `targets_truncated` ·
  `hint_target_included`만 제공하고 **무제한 `array_agg` 금지**.
  `target` 미지정 시 `hint_target_included`를 낼 수 없으므로 폴백은 **유형별
  개수 요약**("26대상 = application 20, database 3, node 3")과 §9 결정 4가
  신설한 `TriageResult.TargetNames`와의 교집합 표시.
- **원천별 조회 시도 여부**: `checked` / `not_applicable` / `error`
  (§9 결정 1 관례). **한 원천이라도 실패하면 "3원천 모두 0건"을 주장하지
  않는다.**
- **0건 표현**: "기본 노출 0건"과 진짜 0건을 구분해, 제외 건수 · 원천별
  checked/error · 과거 미확인 경계(`[firstEvent−24h` 이전은 안 봄) ·
  `policy_deployments`의 현재상태 한계를 함께 싣는다.
- **`source_semantics: current_state_not_history`를 policy 원천에 고정
  표기**하고, 이 원천의 0건으로 과거 배포 부재를 판정하지 못하게 한다.

### 10.8 한계 (응답에 싣는다, 숨기지 않는다)

- **과거 배포 상태는 복원 불가**(10.2-3): 재배포가 기존 링크를 삭제·삽입해
  과거가 소실된다. 조회 결과는 *현재 남은 링크의 최신 `deployed_at`*이지
  당시 부착 상태가 아니다. 근본 해결은 **append-only 배포 이벤트 원장**.
- **name 귀속 끊김**(§2.9-3 청구 3): rename·삭제된 대상은 `name` 경로가
  깨진다. 실측 8건(삭제 쌍)이 이 경우.
- **ch↔pd 연결은 영구 추정**: 감사 detail에 `deployment_batch_id`나
  template ID가 없다. **수집측 청구에 추가**(§2.9-3 확장).
- **`collectors`는 등록만**: `updated_at`은 매 폴마다 갱신되는 노이즈라
  변경 신호가 아니다(현행 파일 머리 주석의 실측 승계).
- **자산 트리 detail은 개수만** — "내용 동일" 판정 불가. 근본 해결은 생산
  측의 revision·내용 해시 기록. **수집측 청구에 추가**.

### 10.9 자리 교체 (§7.1)

list_changes가 들어오면 **get_changes가 빠진다** — 변경 조회 몫 전부를
승계하고 표면을 넓힌다(대상 필수 → 창 전역). 파이프라인 [2] `ScanChanges`가
쓰는 내부 typed 자리(`NewChangesFunc`)는 **유지**한다 — LLM 표면과 무관한
결정론 호출이고 대상 지목이 계약이다(§9 `TargetMetaFunc`와 동형. 코드 확인:
`pipeline/run.go:26` · `cmd/rca/main.go:118`이 Toolset 이름이 아니라 typed
함수를 직접 바인딩). **단 현행은 `queryChanges`를 typed와 도구가 공유하므로
(`tools/changes.go:8,29,62`) 새 전역 조회는 반드시 별도 함수로 분리**한다 —
"변경"의 정의가 코드에 두 벌 생긴다는 사실을 명시해 둔다.

배선 잔손질(검토 grep 실측으로 보강):

| 위치 | 작업 |
|---|---|
| `tools/changes_smoke_test.go:56` | `NewChangesTool(db)` — **제거 시 컴파일 실패**, 갱신 필수 |
| `tools/changes.go:1-9` | 머리 주석 "파이프라인용과 조사자용이 같은 조회를 공유" — 교체 후 거짓 |
| `pipeline/changes.go:3,22,38` | 주석·오류 문구의 구 도구명(38행 "조사자의 get_changes 도구는…") |
| `tools/registry.go:1,72` | 머리 주석 카운트("신5+구12" → "신6+구11") + 배선 |
| `docs/ref-tool-data-access.md:64` | 도구명·원천 설명 갱신 |
| `llm/mockworld_test.go:111,187` | 목 갱신 |
| `docs/spec-agent-tools.md:123,178` | Deprecated 문서지만 표면 표에 구 이름 잔존 |
| `pipeline/generate_test.go:37` | 문자열 fixture — 무해, 선택 |
| `docs/frontier-trajectory-*.md` | **건드리지 않는다**(역사 기록) |

격자 배정은 불요 — `pipeline/grid.go:28,36-44`가 metrics·events·logs 3열
뿐이고 "변경 관점은 [2]가 담당" 주석이 명시돼 있다(`get_changes` 미배정).

**이월 (구현 범위 밖, 별건 판단):** 결정 1의 도달 불가 논거는 파이프라인 [2]
`ScanChanges`에도 그대로 적용된다 — 대상 지목 계약이라 전역 사건·이웃
변경에 닿지 못하고, 그 결과가 [4] 가설 생성의 재료가 된다. **따라서 본
절의 성과는 "RCA의 변경 미수집을 고쳤다"가 아니라 "LLM 수동 조회 경로만
부분 완화했다"로 정확히 말해야 한다**(검토 지적). 승격 시 `correlation`·
`scope` 개념을 typed `ChangeEvent`에 어떻게 실을지가 딸려 온다.
**발동 조건**: 평가에서 [2]의 0건이 오답에 기여한 것이 실측되면 승격
(§4.3 후보 기록 관례).

### 10.10 검토 기록 (2026-07-29, §10 초판 3자)

독립 Claude 서브에이전트(라이브 PG 재현 검증 — 헤드라인 수치 17건 일치,
**파생 수치 6건 불일치**)와 Codex(gpt-5.6-sol — **생산자 소스
`lucida-next`까지 추적**) 교차 검토. 총평은 갈렸다: Claude "보강 후 통과,
단 결정 3은 재작성" / Codex **"재설계 필요"**. 결과적으로 **결정 2·3 재작성 +
결정 1 논거 교체 + 결정 5 신설**로 Codex 판정에 가깝게 개정했다.

일치 지적 6건(전부 반영): ①결정 3 짝짓기 규칙 무효(이중 조건 판별력 0·
다대다) ②`action=''` 제외가 변경을 은폐 ③"자산 트리 내용 동일"은 거짓
④잘림·정렬·스키마·0건·에러 규율 통째 누락 ⑤`policy_deployments` 서술
자기모순 ⑥`to` 기본값의 재료가 코드에 없음.

Codex 단독 지적(반영): provenance 3축 분리 · `사용자` 카테고리 제외 철회 ·
`SESSION_KILL` 반례 · 재배포의 링크 삭제·삽입 · "71% 거짓 음성" 계산 무효 ·
프론티어 ground truth 부재. Claude 단독 지적(반영): 사건 단위 재계산 재료 ·
수집기 후보 분포 실측(1/3/17) · `collectors` 23행 무짝 · 현행 도구는 잡음을
0행 반환 · `name`이 대상 UUID인 경로 발견 · 밀도 최악값.

**본 개정이 사용자 합의 4결정 중 2건(결정 2·3)의 구체 규칙을 바꿨다** —
방향(전역 반환·잡음 처리·교차 접기·24h 소급)은 유지, 구현 규칙만 교체.
Codex 원문: `.omc/artifacts/ask/codex-home-ydkim-project-2025-rca-agent-next-rca-list-changes-docs-2026-07-29T00-59-06-522Z.md`.

## 11. 구현 설계 — compare_peers (2026-07-29 합의, blind 3자 패널)

**상태: 합의 완료.** §8~§10과 달리 문답으로 쌓지 않고 **blind 3자 설계
패널**로 생성했다(§11.0). 결정 2는 권고안(자기 기준선 대비 이탈)으로
확정하되, **Codex 반대 의견을 11.2에 살려 둔다** — 평가 주행이 권고안의
실패를 실증하면 되돌아올 자리다(§11.11 후보 2).

### 11.0 방식 — 검토가 아니라 설계를 blind로 (신규)

기존 3자(§10.10 등)는 **완성안을 때리는 검토**였다. 이번엔 **공통 문제지를
던져 셋이 독립으로 설계**했다 — 자체·독립 Claude(opus 서브에이전트)·Codex
(0.145.0). 문제지에서 **갈림 지점·결정 목록을 의도적으로 뺐다**: 셋이
독립적으로 같은 결정을 짚으면 신호, 하나만 짚으면 검증 대상이라는 설계.

문제지에 실은 것: Q12·부착 vs 도구 확정(§3.4)·현행 `cohort.go`·원천 실측
(§2.4)·확정 규율 5개(봉투 v2·D10 경고·provenance·접기·규율 3)·**과설계 금지와
실제 기각 전례**(커버리지 강제 철회·`find_targets` 보류). 답할 것에 **"일부러
안 만든 것"을 필수 항목**으로 넣었다 — 못 쓰면 다 넣은 것이다.

산출물 4종: `.omc/artifacts/panel/compare-peers-*-2026-07-29.md`.

**결과: 10항목 독립 수렴 + 자체안 3결정 중 2건 기각.** 자체안이 남긴 ±50%와
replica 등급이 둘 다 떨어졌다 — 이 절의 골격은 사실상 외부 2자가 세웠다.

### 11.1 결정 1 — 이 도구의 본체는 시계열이 아니라 또래 명단이다

`read_timeseries`는 **이미** 복수 target을 받아 각 시계열 onset을 산출하고
선후 타임라인까지 부착한다(`tools/read.go:32,40,476`). "여러 대상의 같은
지표를 나란히 놓고 누가 먼저인지 본다"는 능력은 이미 표면에 있다.

따라서 `compare_peers`의 존재 이유는 **또래가 누구인지 코드가 아는 것** 하나로
좁혀진다 — 조사자는 또래 UUID를 손에 쥐고 있지 않다. 시계열·onset·선후를
새로 짜지 않고 조회 경로(`foldGrade`·버킷·counter 변환)를 **재사용**한다.
규율 3(c) 위반이 아니다: 조합의 어려운 절반인 "비교쌍 선택"이 코드에만 있는
지식이기 때문이다.

**귀결: 이 도구의 품질 = 명단의 품질.** 이하 결정 대부분이 명단과 그 신뢰도
표식에 쓰인다.

### 11.2 결정 2 — 이탈 척도: 각자 **자기 기준선 대비** 이탈을 세고 그 수를 비교 (권고, 반대 의견 있음)

현행 `±50%` 배율 판정은 **폐기**(3자 만장일치). 대체안이 갈렸다.

- **권고(독립 Claude)** — 대상·또래 각각에 **동일 척도로 자기 시간 기준선
  대비 이탈**을 판정하고(`scan_metrics`/`detectOnset`의 robust z≥3, 연속
  3중 2 — §5.2/§6.4 상수 **그대로 상속**), 산출물은 `peers_deviating /
  peers_compared`. **compare_peers 고유 임계는 0개.**
  근거: ①또래는 규모가 다르다(트래픽 10배 또래와의 절대 비교는 무의미) —
  자기 기준선 대비는 **규모 불변**. ②프론티어 2건이 그대로 재현된다(또래는
  기준선 0·현재 0이라 비이탈, 대상만 이탈). ③새 상수를 만들지 않는다.
- **반대(Codex)** — **0/비0 존재 관계만** 판정하고 비0 크기 차이는 계산만
  하되 판정으로 승격하지 않는다. 논거: robust z는 **시간 기준선** 비교에서
  검증된 것이지 **또래 횡단면** 판정에서 검증된 바 없다.

**권고 채택 사유**: Codex 반론은 횡단면 z를 겨냥하는데 권고안은 횡단면 z를
쓰지 않는다 — 각 대상이 **자기 시간 기준선**과 비교되므로 Codex가 인정한
검증 범위 안이다. 반면 존재 관계만 보면 **또래도 나도 비0인데 나만 100배**
(응답시간 50ms vs 5000ms)가 `not_isolated`로 떨어져 진범을 놓친다 — Codex
자신도 이를 확장 후보로 남겼다.

**단, Codex 반론의 유효 잔여분**: 이탈자 수 비교는 "또래도 평소와 다르다"를
셀 뿐 "같은 방향으로 다르다"를 보증하지 않는다. 응답에 방향(상향/하향)을
표기하되 판정에는 쓰지 않는다.

절대 크기(self 값, 또래 min/median/max, self 순위)는 **표에 싣되 판정에
쓰지 않는다** — D10은 "산술은 코드"지 "raw를 숨겨라"가 아니다.

### 11.3 결정 3 — verdict는 **방향 없는 4값**, 실증된 경계는 "이탈 또래 0" 하나뿐

원천에 "높을수록 나쁜가"가 없다(§2.4) → "또래보다 나쁘다"는 만들 수 없다.
Q12는 방향 질문이 아니라 분포 질문이다.

| verdict | 조건 | 뜻 |
|---|---|---|
| `alone` | self 이탈 + 이탈 또래 **0** | 국소 원인 후보 |
| `shared` | self 이탈 + 이탈 또래 **≥1** | 공통 상류 후보 |
| `self_not_deviating` | self가 자기 기준선 대비 미이탈 | 이 지표/창에선 질문 불성립 |
| `undecidable` | 비교 가능한 또래 0 | 또래 축으로는 답이 안 나온다 |

**"과반 이탈이면 shared" 같은 비율 임계를 만들지 않는다** — 실증된 경계는
"또래 이탈 0"뿐(궤적 2건 모두 0). `1/7`과 `7/7`은 숫자로 구분되지만 딱지는
같다. **표본 2건 위에 등급 사다리를 세우지 않는다**(§4.3 규율).

`shared`는 원인 지목이 아니다 — "공통 상류를 보라"까지만, 상류 지목은
`expand_topology`/`describe_target` 몫. **suspect/victim 어휘 금지**(3자 일치).

### 11.4 결정 4 — 또래 집합 2단 + confidence. 폴백 사다리를 늘리지 않는다

1. 대상이 속한 `asset_tree_members(scope, kind='service', group_id)` 그룹 ∩
   같은 `targets.type` → `basis=service_group`, `confidence=high`
2. **서비스 그룹 소속이 전혀 없을 때만** 같은 `targets.type` 전체 →
   `basis=type_fallback`, `confidence=low` + 고정 경고(도메인 혼재)

세부 규율(Codex 단독 지적 3건 포함):

- **"그룹 없음"과 "그룹에 동형 또래 없음"은 다르다** — 그룹은 있는데 동일
  type 또래가 없으면 **fallback하지 않고** `not_collected`로 끝낸다.
  현행은 `len(peers)<=1`이면 곧장 fallback한다(`tools/cohort.go:69`) — 폐기.
- **`targets.status`로 또래를 거르지 않는다** — 과거 창을 현재 상태로
  필터링하면 **생존자 편향**(그때 죽어가던 또래가 명단에서 사라진다).
- **다중 service 그룹 소속이면 임의 합집합 금지** → `unknown` + 그룹 키 목록.
- 4원천 소속(§9)은 **또래를 발명하는 데 쓰지 않는다** — "같은 호스트니까
  또래"는 추론이다. `affiliation_consistency` 표식만 하고 포함/제외에 미사용.
- self는 또래 집합에서 제외하고 표에서 `self:true`로 구분(현행 유지).

**약한 근거에서도 판정은 한다** — `confidence=low` 딱지로 표시할 뿐 침묵하지
않는다. 서비스그룹이 3개(12/7/6)뿐이라 **실전 대부분이 fallback**이고, 여기서
판정을 끄면 도구가 대부분의 경우 무용해진다. (자체안의 "약한 근거면 status
미부착"은 2:1로 기각.)

### 11.5 결정 5 — 관측 없는 또래를 정상 또래로 세지 않는다 (최대 오답원)

`alone` 판정은 "또래가 조용하다"에 기댄다. 조용함에는 **진짜 0 / 이 또래엔
미적용 / 수집 공백**이 섞여 있고, 뭉치면 **수집 결손이 "혼자 이상"이라는
고신뢰 오답**을 만든다. F01-R의 사살 근거가 "또래 pending 0"이었음을 상기하면
— 그게 측정된 0이 아니라 미수집이었다면 근거가 아니라 착각이었다.

- 또래별 `observed` / `observed_bucket_count` / `expected` / `presence_ratio`
  / `first_observed_at` / `last_observed_at`.
- **미관측 또래는 판정 모집단에서 제외**하고 `eligible / observed / missing`
  **분모를 1급 필드로** 낸다. "또래 12개 중 판정에 쓴 건 3개"가 안 보이면
  `alone`은 위험한 수치다.
- 결론 문구는 **"관측된 또래 N개가 모두 0"** — "전체 또래가 0"이라고 쓰지
  않는다. `comparison_scope: full_cohort | observed_peers_only`.
- 기준선 창 표본이 없는 또래(신규 배포 등)는 `baseline_missing`으로 제외,
  현재 값은 표에 남긴다.
- **`zero_observations`/`collector_gap`을 이 도구가 추측하지 않는다**
  (Codex 지적 — metric→collector 검증 매핑이 없다). 수치 표본 없음은
  `no_data_reason=unknown` + `get_data_coverage` 복구 경로. 또래 개념 자체가
  성립 안 하면 `not_collected`.

### 11.6 결정 6 — 판정 모집단은 자르지 않고 표시 행만 접는다

현행 `cohortCap=12`는 최대 그룹 크기와 같아 경계에 걸리고, DB 반환 순서대로
잘라 **결정적인 또래가 소리 없이 사라진다** — 절단이 `alone` 오답을 만든다.

- VM 팬아웃은 대상 수에 거의 비례하지 않으므로 **또래 전원으로 판정**한다.
  fallback이 과대하면 조회만 내부 chunking. 판정 모집단 상한이 필요하면
  두되(초안 40) **근거가 실무 감뿐임을 명시**하고 실주행 분포로 고친다.
- 출력은 접는다(규율 (d)): self 행 + **이탈 또래 전 행**은 펴고, 비이탈
  `observed` 또래는 **한 줄로**(수·min/median/max·대표 refs), 제외 또래는
  사유별 한 줄. 또래 40개여도 봉투는 대여섯 행.
- **raw 행 생략은 `truncated`가 아니다** — 조회 자체가 잘렸을 때만 `true`.

### 11.7 결정 7 — 조회 경로·입출력

**조회**: 현행 단일 instant `avg by(target_id)(avg_over_time(...))`를 폐기한다
— `scan.go` 실측 경고대로 **grade 라벨은 알람 등급별 의도된 복제라 `max`로
접어야** 하고, `avg`는 복제 가정이 깨질 때 **조용히 왜곡**한다. 게다가 창
전체 평균 하나로는 지속 이탈을 판정할 수 없다.
→ `{__name__=<metric>, target_id=~"^(id|...)$"}` **range query 2회**(현재 창 /
기준선 창) → `foldGrade` → target_id별 버킷 시계열. counter는 검증된
`(value_type, delta_op)` allowlist에 한해 변환(**실 DB enum 확인이 구현 선결
과제** — 확인 전 counter를 gauge로 가정 금지). 정규식 주입 전 `uuidRe` 검증
(현행의 좋은 습관, 유지). `gradeMismatch`는 `read.go`처럼 계수.

**입력**: `target`·`metric`·`from`·`to` **4개 필수, 그 이상 없음.**
지표 단수·fuzzy 금지(모르면 `scan_metrics`로 보내는 깔때기 문구). 시간은
`parseFlexTime` 공유. 기준선은 `read_timeseries` 계약 상속(기본
`[firstEvent-60m, firstEvent)`, override는 `baseline_from`/`baseline_to` 동반).

**응답**(봉투 v2): findings를 `verdict` / `peer_set` / `self` / `peer`(이탈분) /
`peers_folded` / `peers_excluded` / `compare_meta` 로 구획.
`status`는 self 이탈 여부에 대응(`anomalous`/`normal`), 비교 불가는 `no_data`.
**`normal`은 건강을 뜻하지 않는다** — `assessment_basis`에 "각자 자기 기준선
대비 이탈 여부의 또래 대조. 값 크기 비교·좋고 나쁨 판정 없음"을 고정 문구로.

### 11.8 일부러 안 만든 것

- **`peers` 파라미터(조사자 직접 지정)** — 2:1 기각. 임의 명단을 고신뢰 코드
  판정처럼 포장하게 된다. (독립 Claude 소수 의견: "비교쌍은 조사자가 안다"가
  능동 호출의 근거이므로 통로를 열자 — 기록해 둔다.)
- **replica 또래 등급** — §11.11 발동 조건부 후보로 강등(실측 근거).
- **임계 파라미터 노출**(`z_threshold`·`min_peers`·`ratio`·`direction`·
  `bad_when`) — 인자 0개, 척도는 공통값 상속.
- **통계 검정**(Mann-Whitney·MAD 순위·outlier score·유의확률·클러스터링) —
  표본 최대 12. 신뢰의 외양만 늘린다.
- **onset·선후·한도%·`group_by` 분해·또래 raw 시계열** — `read_timeseries`
  몫(§3.4 Q11·Q13). 겹치지 않고 **다음 수를 문장으로 넘긴다**(UUID를 실어
  조사자가 바로 호출하도록).
- **다중 지표 교차** — 한 호출 한 가설. 응답이 배수로 분다.
- **비교할 지표 자동 추천** — 판정 방향 정보가 카탈로그에 없다.
- **또래 자동 부착·의무 호출 규칙** — §4.3-1 발동 조건부 후보 그대로, 지금
  켜지 않는다.
- **호스트·클러스터·nodeSelector 기반 추가 폴백**, **도메인 이름 매핑
  하드코딩**, **과거 소속 복원**, **결과 캐싱**, **collector 진단 복제**.
- **`shared`의 공통 상류 지목**, **suspect/victim 출력**.

### 11.9 한계 (응답에 싣는다)

| 못 하는 것 | 왜 | 봉투 처리 |
|---|---|---|
| "또래보다 나쁘다" | 판정 방향이 원천에 없음(§2.4) | 방향 없는 verdict + `health_interpretation=not_available` |
| 미관측 또래를 정상으로 세기 | 미수집·미적용·진짜 0이 VM에서 같은 모양 | 분모 3종 + `observed_peers_only` + 추측 금지(11.5) |
| 진짜 기능적 또래 찾기 | 명단 원천이 자산 트리뿐 — 그룹 3개(12/7/6), 나머지는 혼재 fallback | `confidence=low` + 고정 경고 |
| `shared`에서 또래들이 같은 원인의 피해자인지 | 상관만 보고 계보는 안 봄 | "공통 상류를 보라"까지만, 인과 지목 금지 |
| 소속의 과거 상태 | 현재 스냅샷뿐(§9 상속) | `membership_snapshot=current` |
| sparse gauge의 대표성 | 방출 주기가 대상별로 다름 | `presence_ratio`·표본 수, episodic이면 이탈 판정 skip |
| 기준선 창이 이미 병들었을 때 | 탐지 지연 | `baseline_thin`·override + `baseline_median` 노출 |

가장 위험한 오답 시나리오는 하나로 요약된다: **"또래가 조용해서 alone"**.
결정 5가 전부 그 하나를 막는 장치다. 그 밖에서는 규칙을 늘리지 않았다.

### 11.10 자리 교체 (§7.1)

compare_peers가 들어오면 **get_cohort가 빠진다**. 배선 실측 결과(구현 완료):

| 위치 | 작업 |
|---|---|
| `tools/cohort.go` · `tools/cohort_smoke_test.go` | 삭제(`git rm`) |
| `tools/registry.go:1,77` | 머리 주석 카운트(신6+구11 → **신7+구10**) + 배선 교체 |
| `pipeline/grid.go:39` | **격자 배정표 키 교체** — 놓치면 이 도구 호출이 metrics 칸을 채우지 못한다(빈칸 오계상) |
| `llm/mockworld_test.go:136,189` | 목 도구명·응답 문구 |
| `docs/ref-tool-data-access.md:66` | 원천 설명 갱신(2단 열거·두 창 range·status 미필터) |
| `docs/spec-agent-tools.md:125` | Deprecated 문서지만 표면 표 잔존 → 취소선 표기 |
| `docs/backlog-tool-legibility.md:120` · `docs/frontier-trajectory-*.md` | **건드리지 않는다**(역사 기록 — F01-R step 10이 `get_cohort`로 사살한 기록) |

파이프라인 typed 자리는 없다 — `get_cohort`는 LLM 표면 전용이었다(§9의
`TargetMetaFunc`·§10의 `NewChangesFunc` 같은 결정론 호출 짝이 없음).

**구현 산출**: `tools/comparepeers.go`(도구+또래 열거+판정+봉투),
`tools/comparepeers_test.go`(순수 단위 7 — §11.5 회귀 가드 포함),
`tools/comparepeers_smoke_test.go`(실 PG+VM). 판정 규칙은 순수 단위가
고정하고 스모크는 계약 형태만 본다 — 실 데이터의 verdict는 데이터에
달렸으므로 고정하지 않는다.

### 11.11 패널 기록 + 발동 조건부 후보

**수렴 10항목**(셋 독립 일치, 사실상 확정): ±50% 폐기 · 창 전체 avg 폐기 ·
`avg by(target_id)` 폐기 · cap12 선절단 폐기 · 미관측 또래를 정상으로 세지
않음 · onset/선후/한도%는 read_timeseries 몫 · 통계 검정 금지 · 지표 단수+
fuzzy 금지 · basis 구조화+confidence · suspect/victim 금지.

**자체안 기각 2건**: ①±50% 존치(→ 자기 기준선 대비 이탈로 대체) ②replica
등급 신설(→ 실측 기각, 아래).

**Codex 단독 지적 3건**(전부 반영): 그룹 있는데 동형 또래 없음 ≠ fallback 조건 ·
`targets.status` 필터의 생존자 편향 · `zero_observations`/`collector_gap`
추측 금지.

**발동 조건부 후보** (§4.3 관례):

1. **replica 또래 등급** — 같은 워크로드 형제 pod를 최상위 등급으로.
   `kcm_resource_targets.parent_target_id`로 **구현 가능함은 확인**했으나,
   **테스트베드 매니페스트 185개가 전부 `replicas: 1`이라 형제 pod가 0개**
   (2026-07-29 실측). 항상 빈 집합이므로 만들지 않는다. **발동 조건**:
   다중 replica 환경(운영 확장 등)이 대상이 될 때 재검토.
   — 패널 3자 모두 이 사실을 몰랐다(둘은 "추론은 위험" 논거로 반대, 자체는
   스키마만 보고 찬성). **스키마 가능성과 데이터 실재는 다르다**는 교훈.
2. **비0 크기 차이의 판정 승격** — 이탈자 수만으로 못 잡는 "또래도 나도 비0인데
   크기만 다름"을 판정으로 올릴지. **발동 조건**: "도구는 호출됐으나 비0
   크기 차이를 놓쳐 오답"이 평가에서 실측될 때.
3. **`shared`의 하위 구분** — `1/7`과 `7/7`이 같은 딱지인 것이 오답을 만들면
   그때 쪼갠다.
4. **`peers` 파라미터** — 조사자가 또래를 아는데 코드가 막아 오답이 나면.

산출물: `.omc/artifacts/panel/compare-peers-{problem-sheet,design-main,
design-claude,design-codex}-2026-07-29.md`.

## 12. 구현 설계 — expand_topology (2026-07-29 합의, 3자 검토 2회 반영 2026-07-30)

§3.5의 구현 설계. 사용자 문답으로 결정 5개를 확정한 뒤 **3자 검토를 두 차례**
받았다. 1차에서 결정 1·4가 재작성되고 결정 2·5가 신설됐으며, 2차(개정판
재검토)에서 **신설분의 실측 오류 2건과 "이름만 있고 계약이 없는 필드" 3건이
드러나 다시 고쳤다**(검토 기록은 12.11).

**2차 검토가 드러낸 이 절의 실패 모드**: 구현 가능성을 확인하지 않은 채
필드 이름을 선언하는 것. `logical_calls`·`cursor`·"dedup 키를 명시한다"가
전부 그랬다. **§10.7도 같은 병에 걸려 있었다**(선언한 `next_cursor`가
`tools/listchanges.go`에 부재 — 발견 즉시 §10.7에서 삭제했다). 이번 개정의
원칙은 **선언을 늘리지 않고 줄이는 것**이다.

### 12.1 원천 실측과 그 지위

라이브 119(CH `lucida` + PG). **모든 수치는 한 캡처의 관측이지 계약이
아니다** — "지위" 열이 정본이고 수치는 `observed_in_capture` 등급이다.
2차 검토가 다른 시점 캡처로 재현 검증했고, 구조적 주장(링크 0건·소켓 앱
프로세스 0·`status_code` 2값·관측 비대칭·간선쌍 24)은 두 캡처에서 일치했다.

| 원천 | 실측(1h) | 지위 |
|---|---|---|
| CH `otel_traces_local` 앱↔앱 | span 9.1만~10.5만 · 서비스 21 · **간선쌍 24** · 최대 차수 7 | 주력 |
| ↳ `resource_attributes['lucida.target_id']` | 99.0~99.3%, 결손 전량 `ingest-selfsystem` | **배포 규약이지 계약 아님**(정정 1) |
| CH 앱→DB(`db.system` CLIENT) | 2.2만~2.8만 span | 끝점 resolve는 환경 관측(정정 2) |
| PG `network_fdb_hosts`/`network_neighbors` | 119~121행/29행, 스위치 5대 | **현재 상태 표**(12.7) |
| CH `host_connections` | 37.4만~44.8만행(TTL 롤링) · 대상 5 · **앱 프로세스 0** | 제외(12.4) |

**정정 1 — 트레이스에 대상 UUID가 실리나 계약이 아니다.** 설치 스크립트는
`OTEL_RESOURCE_ATTRIBUTES`에 강제 upsert하지만(`lucida-next .../collectorPlugins/
agentScript.ts:86,116`) **공통 Go 초기화는 선택적 env 병합**이고
(`backend/pkg/observability/otel.go:169,196`) **otel-collector는 trace
파이프라인에 주입하지 않는다**(`deploy/config/otel-collector/config.yaml:158`).
수동·외부 OTLP·구버전 스크립트·설정 누락 서비스는 조용히 빠진다. 생산자는
서비스별 `max(lucida.target_id)`로 혼재를 숨기는데(`topology_store.go:385`)
**우리는 숨기지 않는다**(12.2).

**정정 2 — 앱→DB resolve 경로는 typed meta를 쓰되 계약이 아니다.**
`db_resource` 대상이 `resource_kind`/`resource_key`/`resource_type`/
`parent_target_id`/`host_target_id`를 완비한다(실측 6행 전부, 2차 재현 일치).
**이름 문자열 파싱은 쓰지 않는다**(§8 결정 3 규율). 다만 생산자 정본
(`targets저장표준.md` §7.1 도메인 표)이 이 도메인을 **"미승격(설계 — VM 메트릭
스냅샷 파생), DBDpmResourceSnapshot 탐색 전용"**으로 규정하므로 등급은 **환경
관측**이다. Oracle(PDB 대신 tablespace만)·Redis(엔진 switch 분기 없음)가 빠지는
것은 **생산자의 의도적 분기**다(`database_dpm_resources_store.go:44,49`).
→ **§2.9-1 청구 유지**(초판의 "축소" 철회).

**정정 3 — 소켓 원천은 뭉개짐 이전에 비어 있다.** 프로세스 13종 전부 인프라,
앱 프로세스 0건(두 캡처 일치). 행 수는 TTL 롤링이라 "전 기간" 프레이밍은
부정확했다.

**정정 4 — 창 확대 이득이 재현되지 않는다.** 간선쌍 600s=24 · 1h=24 ·
24h=24~25. §3.5의 "freshness 창 탈락분을 창 확대로 되살린다"는 **쓰지
않는다**. 남는 이득은 ①조사 중 새로 쥔 대상 주변(박제는 격상 시점·anchor
1-hop 고정) ②홉 고정 아님.

**정정 5 — 생산자에 서비스→호스트 원천이 있다.** `topology_store.go:588`
`topoServiceHostEdges`. 초판이 이 다리를 빠뜨려 앱 그래프와 물리 그래프가
단절됐다(1차 Codex 구조적 결함) → 결정 4로 흡수.

**정정 6(2차 검토) — 앱 span에는 호스트 대상 UUID가 없다.** 개정 1판이
결정 4의 원천으로 지목한 `lucida.host_target_id`는 실측 **32~38 span,
distinct UUID 1개, 전량 `collector-dpm`(앱 21종 중 0종)**이고, `host_name`은
호스트명이 아니라 **파드명**(`testbed-product-5b5964f879-mkhd8`),
`k8s_node_name`은 **전 span 빈 값**이다. 즉 1판의 `hosted_on` 예시는 지정
원천으로 **산출 불가능**했다 → 결정 4에서 원천 교체(12.5).

### 12.2 결정 1 — 끝점 정체성: provenance 축 분리

초판의 `exact`/`resolved`/`unresolved` 단일 등급은 서로 다른 축을 뒤섞은
것이라 폐기했다(§10이 3축 분리로 개정된 오류의 재발).

| 축 | 값(닫힌 집합) |
|---|---|
| `resolution_status` | `resolved` \| `ambiguous` \| `unresolved` |
| `match_basis` | `resource_attr_uuid` \| `db_resource_meta` \| `service_name` \| `pod_name` \| `ip` \| `mac` \| `peer_authority` |
| `confidence` | `producer_contract` \| `deployment_convention` \| `environment_observation` \| `heuristic` |
| `scope` | `managed_target` \| `peer_observed` \| `control_plane` |
| `candidate_count` / `candidates[]` | `ambiguous`일 때 필수 |

`confidence`는 **근거의 계약성**을 값으로 갖는다(2차 지적: 값 도메인 부재).
`resource_attr_uuid`는 `deployment_convention`(정정 1), `db_resource_meta`는
`environment_observation`(정정 2)이 상한이다. `scope`를 4번째 축으로 **표에
편입**한다(1판이 표 밖에서 도입한 것을 정리).

**`resolved` 판정 규율**: `resource_attr_uuid`는 ①유효 UUID ②PG `targets`에
실존 ③해당 endpoint/창에서 **distinct UUID 정확히 1개**일 때만. 복수면
`ambiguous`(생산자의 `max()`처럼 숨기지 않는다), 미등록이면 `unresolved`.
미주입 서비스는 `service_name` 폴백 + 표식 — **가짜 UUID 금지**(§9 결정 5
전례). `ambiguous` 규율은 전 축 공통이며, 생산자도 복수 후보 자동 병합을
거부한다(`topology_merge.go:256`). **단 이 환경에서는 발동 경로가 없다**
(서비스 21종 전부 distinct UUID 1개) — 규율은 옳으나 미검증임을 밝힌다.

**미해소 끝점도 간선을 살린다** — 버리면 의존이 없는 것처럼 보인다(§10에서
없앤 거짓 음성). **coverage를 반환한다**: target_id 보유율 · 미주입 서비스 ·
복수 UUID 서비스 · 미등록 UUID 수.

### 12.3 결정 2 — 한쪽 관측만 있는 간선도 보존한다

**1차 검토가 잡은 구조적 결함**: 조립이 parent↔child INNER JOIN뿐이라
**상대가 span을 내보내지 않으면 간선이 생기지 않는다.** 실측 —
`testbed-external-pg-mock`(계측 없는 외부 결제 의존)을 두 결제 서비스가
236~256회/h 호출하는데 호출자 span에 목적지가 남아 있는데도 지도에 안
나타난다. **타임아웃에서 더 나쁘다** — 피호출자 기록이 없으면 조인이
제거하므로 가장 의심스러운 상황에서 지도가 가장 조용해진다.

**확정 규칙 — 판별 우선순위를 명시한다(2차 필수 수정).** 1판은 "자식 없는
CLIENT/PRODUCER에서 끝점을 만든다"고만 써서 **DB CLIENT span 전량을 삼켰다**:
자식 없는 CLIENT 중 `db.system` 보유가 **28,282건**, 비-DB는 **270건**(2차
실측)이다. DB span은 피호출자 미계측이라 정의상 전부 자식이 없다. 1판이 든
근거 "239건"은 애초에 비-DB 부분집합인데 그 배제 조건이 규칙에 없었다.

1. `db.system` 보유 → **`apm_db`**(12.1 정정 2의 typed meta resolve). 우선.
2. 그 외 자식 없는 **CLIENT** + 목적지 속성 보유 → **`apm_client_peer`**.
3. 자식 없는 **PRODUCER**(실측 537~561건, 전량 Kafka publish) → 목적지
   속성(`server.address`·`url.full`·`peer.service`)이 **전부 빈 값**이고
   가진 것은 `messaging.system` + `messaging.destination.name`(토픽)뿐이다.
   → **끝점을 만들지 않는다.** 토픽별 건수만 `unpaired_producer[]`로 보고한다
   (토픽은 대상이 아니라 채널이라 노드로 만들면 §12.2의 축이 무너진다).
   §4.3 후보 2(Kafka 계보)와 연동해 승격 여지를 남긴다. **1판은 이 537건을
   조용히 탈락시켰다.**

**끝점의 지위**: `scope: peer_observed`(1판의 `external_observed`에서 하향 —
"외부"라 단정할 근거가 없다. 내부 서비스의 계측 누락·샘플링·ingest 유실·창
경계도 같은 모양을 만들고, 실제 파이프라인에 sampler가 있다),
`resolution_status: unresolved`, `match_basis: peer_authority`,
`callee_observation: missing`, `missing_reason: unknown`.
**"미계측인지 타임아웃인지 본 도구는 구별하지 못한다"**를 명시한다.

**노드 dedup 키(2차 필수 수정 — 1판은 "명시한다"는 지시문뿐이었다)**:
peer 노드 키는 **정규화된 `(scheme, authority=host|ip, port)`**이며
**`url.full`의 path·query는 키에서 제외**한다. 경로를 키에 넣으면
`?page=43&size=20` 같은 동적 URL마다 노드가 생겨 상한이 즉시 포화한다(실측에
그 실례 존재). 경로는 **간선 속성의 표본 목록(상위 N)**으로 보존한다.
따라서 **여러 호출자가 같은 목적지를 부르면 노드는 하나, 간선은 호출자별**이다.

**목적지 정체 해소(구현 스모크가 잡은 결함)**: 첫 실주행에서
`commerce-gateway → peer:http://testbed-product:8081`이 나왔는데, 같은
의존이 이미 `sync_call → target:766ec1cd…(commerce-product)`로도 있었다 —
**같은 의존이 두 정체로 중복**된 것이다. authority의 host가 **k8s Service
대상**으로 풀리기 때문이다(실측: `testbed-product` →
`…:service:rca-testbed-commerce/testbed-product`). 정체를 알 수 있는데
그림자 노드를 만드는 것은 §12.2 "가짜 노드 금지"의 취지에 어긋난다.
→ **authority host를 k8s Service 단축명으로 먼저 해소**하고
(`match_basis: k8s_service_name`, 등급은 `environment_observation`),
복수 네임스페이스에 같은 이름이면 `ambiguous`, 못 풀면 그때 peer 노드다.
간선 종류는 `apm_client_peer`를 유지하되(관측이 한쪽뿐인 것은 사실이므로)
노트에 "목적지 정체는 해소됨"을 밝힌다. 실측 효과: peer 그림자 노드 3개 → 1개
(남은 하나는 redis — 정당한 미해소).

**창 기준**: `apm_client_peer`는 child가 없어 12.6의 "child 기준"을 적용할 수
없다 → `window_basis: caller_timestamp`로 **별도 표기**한다(한 응답에 두 창
의미가 섞이는 것을 숨기지 않는다).

**§12.9 문구 정정**: 1판 한계의 "계측 안 된 의존은 어떤 원천으로도 못 본다"는
**거짓**이다 — 호출자가 계측돼 있으면 CLIENT span이 상대를 지목한다.

### 12.4 결정 3 — 소켓 간선 제외 + 기존 소켓 도구 처리

§3.5는 소켓을 접어 넣기로 합의했다. **결론은 뒤집되 논거는 교체한다** —
초판 논거("숨은 의존을 못 찾게 된다")는 12.3으로 반증됐다.

**확정 논거**: 소켓이 줄 수 있는 호스트↔호스트:포트는 ①앱 소유권이 없어
의존 간선이 못 되고(앱 프로세스 0건) ②그 몫을 `apm_client_peer`가 더 정확히
수행한다. **승격 조건**은 "§2.9-2 해결"이 아니라 **실행 시 app-owner
coverage > 0**(데이터 관측)이다 — 스키마 변경 없이 수집이 개선돼도 살아난다.
**coverage는 반환한다**: `socket_source: checked_unusable_for_app_ownership`
+ app-owned row 수 + 전체 row 수. "안 봤다"가 아니라 "봤는데 앱 소유권을
만들 수 없다"를 밝힌다.

**기존 `get_runtime_connections`(초판 누락, 양쪽 검토 지적).** `registry.go:78`
배선, 원천이 바로 `host_connections`, 설명문이 "topology에 없는 런타임 의존
발견용", 응답이 "known_target=null인 원격은 계측 밖 의존 후보"다
(`tools/meta.go:33,98`). **초판이 경계한 거짓 안심을 이 도구가 이미 주고
있다.** → 자리 교체로 함께 뺀다(12.10). 단 **`docs/spec-agent-design.md:618`이
이 도구를 격자의 "조사 대상 축" 정의에 쓰고 있으므로**(단순 문서 언급이 아니라
조사 정책) 그 축 정의를 `apm_client_peer`로 승계 서술해야 한다.

### 12.5 결정 4 — 기본 2홉 + 호스트는 `hop_cost=0` 간선

**홉 기본값**: 기본 2, 최대 3. 남는 근거는 **"1홉은 박제와 동일 시야"**
하나다 — "원인은 대개 한 다리 건너"·"3홉이면 전부 들어옴"은 한 캡처
일반화라 뺀다(이 캡처는 3도메인이 격리돼 도메인 교차 간선이 1개뿐이다).
각 노드에 홉 거리를 싣는다.

**호스트 다리(2차 재작성).** 1판은 "간선이 아니라 노드 부착"으로 두었으나
**두 결함이 드러났다**: ①지정 원천이 앱 span에 없었다(정정 6) ②"부착이므로
노드가 아니다"(12.5)와 "그 호스트 노드가 frontier에 등장한다"(12.7)가
**서로를 전제로 반대 결론**을 냈다.

**확정**: `hosted_on`을 **`hop_cost=0`인 typed 간선**으로 만든다.

- 각 BFS 층에서 **zero-cost closure를 먼저 계산**한다 — 호스트는 정식
  노드이자 간선이지만 **홉 거리를 증가시키지 않는다.** 따라서 앱 이웃을
  밀어내지 않으면서 **단일 응답 안에서 앱↔물리 그래프가 이어진다**(1판의
  "조사자 재호출" 우회를 폐기 — 2차 Codex가 "다리가 아니라 다음 호출
  포인터"라고 정확히 지적).
- **노드·간선 cap에는 포함**하되 hop만 증가시키지 않는다.
- **복수 호스트를 허용한다** — 생산자 원천이 `(service, host)`별 행이라
  한 서비스가 여러 호스트에 배치될 수 있다. `hosted_on[]` 배열로 보존한다
  (1판의 단수 예시는 이 카디널리티를 정의하지 못했다).
- **원천(구현 중 재실측으로 3차 정정)**: 개정 2판이 적은
  `kcm_resource_targets(resource_kind='pod')` 경로도 **노드에 닿지 못한다** —
  pod 대상의 `parent_target_id`·`host_target_id`가 노드가 아니라 **클러스터
  UUID**이고, span의 `k8s_namespace`·`k8s_pod_name`·`k8s_node_name`은 전부
  빈 값이다(실측). 실제로 닿는 경로는:

  ```
  span.host_name(=파드명)
    → PG kcm_resources(kind='pod', name=파드명).data->>'nodeName'   ["tb-w1"]
    → targets(resource_kind='node', resource_key=그 이름)            [노드 UUID]
  ```

  실측 커버리지 **55/55**. `data->>'nodeId'`는 k8s UID라 우리 대상 UUID가
  아니므로 `nodeName`을 거쳐야 한다. `match_basis: pod_name`,
  `confidence: environment_observation`. **커버리지를 응답에 싣고 "층 이동
  다리는 항상 있다"고 단정하지 않는다.**
- 해소 실패 시 12.2의 축 표기를 그대로 따른다.

### 12.6 결정 5 — 간선 속성: span-kind 행렬·관측 주체·집계 축

**관측 비대칭이 이 결정의 뿌리다**(두 캡처 재현 일치):

| 간선 | 호출 | caller(CLIENT) 에러 | callee(SERVER) 에러 |
|---|---|---|---|
| commerce-gateway → commerce-product | 2,525~3,116 | **1,305~1,573 (52%)** | **0** |
| commerce-product → commerce-inventory | 987~1,132 | **822~958 (83%)** | **0** |

피호출자 기준으로만 그리면 83% 실패 중인 시스템이 건강해 보인다. 따라서
`caller_observed`·`callee_observed`를 **병치**하고 어긋남을
`observation_mismatch`로 표기한다(판정 없이 사실 병치 — §10·§11 관례).

**근거 문장 정정(2차 지적)**: 1판이 "에러 CLIENT가 전부 자식 보유 =
피호출자가 5xx를 돌려준 경우"라 썼는데 **같은 절의 표가 `callee_err 0`이라
자기모순**이다. 실제로는 피호출자가 **정상 종료**했고 호출자 측
타임아웃·서킷브레이커·클라이언트 검증 실패다. 결론(간선 소멸 위험 실재)은
유지되나 근거 문장을 교체한다.

**규율 4건:**

1. **span-kind 조합표 고정.** 부모-자식 조인을 무제한 일반화하지 않는다.
   - `CLIENT→SERVER` → `sync_call`
   - `PRODUCER→CONSUMER` → **`async_candidate`** — parent-child만으로 메시지
     계보를 보장하지 않는다. **실측: `links_span_id`가 전 kind 0건**(두 캡처
     확정)이라 링크 경로는 쓰이지 않는다. §4.3 후보 2는 **해소가 아니라
     부분 관측**으로 남는다.
   - 그 외 → `unknown_parent_child`(반환하되 등급 낮춤)
2. **INTERNAL 에러는 간선이 아니라 노드 속성.** `commerce-inventory`의
   INTERNAL 에러 855~956건이 어느 간선 속성에도 안 실리면 "피호출자 자신이
   실패 중"이라는 결정적 사실이 사라진다. → 노드에 `self_observed`(자기
   span의 kind별 에러수)를 싣는다. **인과 귀속은 하지 않는다**(node-local
   증거). 2차 실측이 근거를 강화했다(cart INTERNAL 176 · shipping 122).
3. **지연은 `latency_basis` 필수 + 분포 분리.**
   `caller_client_duration` / `callee_server_duration` / `producer_duration` /
   `consumer_duration`. 각각 **p50·p95·max**를 낸다(2차 지적: 어떤 통계인지
   미지정이었다). 하나로 합쳐 "간선 지연"이라 부르지 않는다. **비동기는
   `latency_semantics: consume_duration_not_edge_delay`** — consumer duration은
   소비 처리시간이지 발행↔소비 지연이 아니다.
4. **집계 축을 실제 계산 가능한 것으로 한정한다(2차 필수 수정).**
   1판의 `raw_spans`/`paired_spans`/**`logical_calls`** 중 `logical_calls`는
   **삭제한다** — retry는 서로 다른 span_id를 낳고 OTel에 retry 표식 필드가
   없으며 생산자 스키마에도 논리 요청 ID 계약이 없다(`ingest/transform/
   otlp.go`). 링크도 0건이라 묶을 근거가 없다. 이름만 만들면 `paired_spans`와
   같아지거나 임의 규칙이 된다. → **`raw_rows` · `unique_spans`
   (`(trace_id, span_id)` dedup — 생산자 CH는 일반 `MergeTree`라 저장 중복을
   제거하지 않는다) · `paired_span_pairs`(`(trace_id, caller_span_id,
   callee_span_id)`)** 로 한정하고 **`retries_collapsed: false`를 명시**한다.

**조인 시간창**: 간선 소속은 **child(피호출자) 기준**. 다만 parent가 창 밖일
때 간선이 사라지는 것을 막기 위해 **parent lookback = child 창 + 5분**을
고정한다(2차 지적: 소급 범위 미정이었다 — 좁은 onset 구간 조회에서 이 규칙이
막으려던 실패가 그대로 재발한다). `apm_client_peer`의 창 기준은 12.3.

**status_code 리터럴**: 이 캡처의 실제 값은 `ERROR`/`UNSET` 2값이다. **단
"2값뿐"은 불변식이 아니다**(2차 지적 — 생산자 계약과 충돌) — `OK`를 포함한
다른 값이 올 수 있으므로 **`ERROR`만 에러로 세고 나머지는 비에러로 묶되
미지의 값은 원문 노출**한다. `STATUS_CODE_ERROR`로 필터하면 에러 0건이
조용히 나온다(설계 중 실제로 겪은 오측정) — 구현·테스트에서 고정 확인.

### 12.7 결정 6 — 네트워크 원천: 유형 정본·현재상태·frontier 판정

1. **유형 판별 정본**: `targets.type`은 정본이 **아니다**(§8 결정 3 —
   pod·container·node가 전부 `kubernetes_resource`). 정본은
   `meta.resource_kind` + `resource_key`이고 `type`은 상위 도메인 힌트다.
   실측상 네트워크 로컬끝 5대는 `type='network'`라 우연히 풀리지만 **server
   계열 판별은 미검증**이다.
2. **판정은 seed가 아니라 frontier 노드마다.** 결정 4가 `hosted_on`을
   `hop_cost=0` **간선**으로 확정했으므로 **호스트는 정식 frontier 노드다** —
   1판의 모순(부착이면 frontier가 아님)이 해소됐다. 앱에서 출발해도
   zero-cost closure로 서버 노드가 같은 응답에 들어오고, 그 노드에서
   네트워크 조회가 발동한다.
3. **폴백 조건 한정(2차 지적)**: "판별 실패 시 조회 폴백"은
   **`resolution_status=resolved`이면서 유형 판별만 실패한 노드**에만
   적용한다. `apm_client_peer` 같은 미해소 노드는 조회 키(`local_target_id`)
   자체가 없어 실행 불가이고, 앱 노드까지 `checked`+0건을 받으면 12.8의
   "유형상 미조회" vs "진짜 0건" 구분이 무너진다. → 미해소 노드는
   `not_applicable` 고정.
4. **`source_semantics: current_state_not_history` 고정 표기**(§10.7 규율).
   두 표는 upsert 현재상태다(`UNIQUE(local_target_id, local_if_name, mac)` /
   `UNIQUE(local_target_id, local_if_name, remote_chassis_id, source)`,
   `collected_at` 범위 5분). **`from`/`to`가 적용되지 않는다는 사실을
   응답에 노출**하고, 이 원천의 0건으로 "과거에 연결이 없었다"를 판정하지
   못하게 한다.
5. **원격끝 해소 규칙 명시(2차 지적)**: 두 표에 `remote_target_id` 컬럼이
   **없다** — fdb는 `mac`/`ip`, neighbors는 `remote_mgmt_addr`/
   `remote_sys_name`뿐이고 "해소 4+4"는 조인 파생이다. §2.5 Q14가 이미
   `server.address ↔ targets.address` 매칭 실패를 기록했으므로 **검증 없이
   `match_basis: ip|mac`을 쓰지 않는다** — 조인 규칙(`ip = targets.address`
   정확 일치, mac은 미해소 고정)과 실패율을 응답에 싣는다.
6. `network_neighbors`에는 **`confidence` 컬럼이 이미 존재**(전 행 1)한다 —
   12.2의 provenance 축 `confidence`와 **이름이 충돌**하므로 응답에서는
   `source_confidence`로 이름을 분리한다. `remote_kind`는 29행 전부 빈 값.

`not_applicable`/`checked`/`error`를 언제나 밝힌다.

### 12.8 결정 7 — 응답 규율

**선언을 줄인다**(2차 원칙). 계약을 못 고정하는 필드는 만들지 않는다.

- **인자 스키마**: `target`(UUID 필수) · `hops?`(기본 2, 최대 3) ·
  `from?`/`to?` · `edge_kinds?`(enum: `sync_call` `async_candidate`
  `unknown_parent_child` `apm_db` `apm_client_peer` `hosted_on`
  `network_fdb` `network_lldp`) · `limit?`. 시간 인자는 read_timeseries
  유연 파서 재사용.
  - **`cursor` 없음** — §10.7의 전례대로 재개 토큰은 정규화 인자·정렬 키·
    스냅샷 정책이 함께 정해져야 계약이 된다. 필요가 실증되면 그때 설계한다.
  - **`edge_kinds`는 탐색이 아니라 출력 필터다**(2차 지적: 미정이었다).
    탐색은 항상 전 종류로 하고 출력에서 거른다 — 탐색을 막으면 걸러진
    종류를 거쳐야 닿는 노드가 조용히 사라진다.
  - **탐색 방향은 양방향**(incoming+outgoing). "누가 나를 부르나"가 피해
    전파 추적의 절반이다.
  - **`limit`은 간선 수 상한**이며 노드는 간선에서 파생된다.
  - **기본 창**: `[firstEvent, lastEvent]`. 한쪽만 있으면 있는 쪽 기준으로
    ±30분, 둘 다 없으면 `now-1h`. `window_basis`로 어느 경우인지 밝힌다.
    `incidentID` 바인딩은 **제거**한다(박제를 안 읽음).
- **잘림**: `max_nodes=200` · `max_edges=400` ·
  `max_fanout_per_node_per_kind_per_hop=20`. **이 숫자는 실측 도출이 아니라
  현 규모(서비스 21·간선쌍 24·최대 차수 7)의 약 10배 여유폭이다** — 이
  환경에서는 발동하지 않으므로 `expansion_truncated_at_hop`·`boundary_nodes`·
  `complete_through_hop`은 **검증 경로가 없음을 밝힌다**(2차 지적).
  실제로 걸리는 곳은 둘: **네트워크 FDB**(`local_target_id`당 최대 32행 >
  20)와 **`apm_client_peer`**(경로 카디널리티 — 12.3의 노드 키로 차단).
- **선정 순서(원천별로 분리 — 2차 필수 수정)**: apm 계열은
  `에러율 상위 → 호출수 상위`(호출량이 적고 에러율이 높은 간선의 탈락
  방지). **네트워크 계열은 에러율도 호출수도 없으므로**
  `local_if_name → mac`/`remote_chassis_id` **사전순 결정론 tie-break**.
- **홉 확장 × 잘림**: **잘린 노드는 확장 시드가 되지 않는다.**
  `expansion_truncated_at_hop` · `boundary_nodes` · `complete_through_hop`으로
  노출한다.
- **집계 표기**: 원천·홉·간선종류·`scope`별로 `discovered` / `resolved` /
  `returned` / `omitted` / `truncated`. dedup 키는 12.3(peer 노드)·12.6-4
  (span·pair)·`target:<uuid>`(해소 노드)·
  `(source_node_id, target_node_id, edge_kind, observation)`(간선)로 **본문에
  고정**한다(1판의 "명시한다"는 지시문을 실제 키로 대체).
- **시간 예산 30s는 도구 전체 deadline**이며, 초과 시 **부분 결과 + 어느
  원천이 미완인지**를 반환한다(에러 아님).
- **원천별 조회 시도 여부**: `checked` / `not_applicable` / `error`.
  **한 원천이 실패하면 "연결 없음"을 주장하지 않는다.**
- **0건 표현**: "이 창에 관측된 호출 없음" / "원천 조회 실패" / "유형상
  미조회"를 구분한다. **트레이스 0건 = 의존 없음이 아니다**(계측이 없을 수
  있다).
- **에러 처리**: `target` 형식 오류·미등록·삭제 대상을 구분해 복구 안내
  (§9 결정 5 관례).
- 합성 overlay(`event_cluster`류)는 **조립하지 않는다**(원천 관측이 아님).

### 12.9 한계

- **`lucida.target_id`는 배포 시 env 주입 의존**: 미주입 서비스는
  `service_name` 폴백. coverage로 노출.
- **DB 끝점 resolve는 생산자 계약이 아니다**: `db_resource` 행은 표준상
  "미승격·탐색 전용"이라 사라질 수 있고 Oracle PDB·Redis는 **의도적으로**
  빠진다. §2.9-1 청구 유지.
- **소켓 부재**(§2.9-2): 앱 소유권 없음. 단 **"계측 안 된 의존을 어떤
  원천으로도 못 본다"는 거짓** — 호출자가 계측돼 있으면 `apm_client_peer`가
  지목한다. 호출자마저 미계측이면 못 본다.
- **PRODUCER 고아(537~561건/h)는 끝점이 없다** — 토픽만 있어 건수 보고에
  그친다(12.3-3). 비동기 계보는 `async_candidate` 등급, §4.3 후보 2 유지.
- **`hosted_on` 커버리지 미확정**: 파드명→노드 경로의 실측 커버리지를
  구현 시 측정해 응답에 싣는다. 앱 span에 호스트 UUID는 없다(정정 6).
- **네트워크는 현재 상태**(12.7-4) + 원격끝 조인 파생 + `remote_kind` 전부
  빈 값.
- **retry는 접히지 않는다**(`retries_collapsed: false`) — 호출수는 논리
  요청 수가 아니다.
- **트레이스 계측 의존**: 계측 없는 서비스는 지도에 없다. 0건을 "의존
  없음"으로 읽으면 안 된다.

### 12.10 자리 교체 (§7.1)

expand_topology가 들어오면 **`get_topology`와 `get_runtime_connections`가
함께 빠진다**(12.4). 표면 산술: 현행 신 7 + 구 10 = 17 → **신 8 + 구 8 =
16종**.

| 위치 | 작업 |
|---|---|
| `tools/k8s_topology_smoke_test.go:66` | `NewTopologyTool(pg, incID)` — **제거 시 컴파일 실패**, 재작성 필수 |
| `tools/meta_smoke_test.go:33` | `NewRuntimeConnectionsTool(ch, pg)` — **제거 시 컴파일 실패**(2차 지적, 1판 누락). coverage 전용 테스트와 분리 |
| `tools/topology.go` | 파일 제거 |
| `tools/meta.go` | `get_runtime_connections` 제거 — 실제 범위는 머리 주석 2~5행 + 20~107행(`knownAddresses` 포함, 유일 사용처 `meta.go:74`) |
| `tools/registry.go:78,88` | 배선 2건 + 머리 주석 카운트 |
| `tools/registry.go:60` · `cmd/rca/main.go:113` · `cmd/react/main.go:126` | `Toolset` 시그니처에서 `incidentID` 제거 시 동반 수정 |
| `cmd/tool/main.go:31,37,68` | **`-incident` 플래그 자체** 재검토(다른 용도 없으면 제거) |
| **`docs/spec-agent-design.md:618`** | **격자 "조사 대상 축" 정의**가 `get_runtime_connections`를 지명 — 단순 문서가 아니라 **조사 정책**이므로 `apm_client_peer` 승계로 서술 교체(2차 지적) |
| `llm/mockworld_test.go:151,203` | 목 갱신 |
| `docs/ref-tool-data-access.md` | 두 도구 행 갱신 |
| `docs/spec-agent-tools.md` | Deprecated 문서 표면 표 |
| `docs/backlog-tool-legibility.md:49` | 노드 ID 가공 문제 — 해소 여부 정리 |
| `docs/frontier-trajectory-*.md` | **건드리지 않는다**(역사 기록) |

**seed 브리핑의 `incidents.topology` 소비자는 남긴다** — 박제는 §3.5대로
"증상 시점의 그림" 참고 자료다. 일괄 제거 금지.

격자 영향 없음 — `pipeline/grid.go:104`가 미배정 도구를 건너뛰고 관점 3열에
topology가 없다(2차 재확인).

### 12.11 검토 기록

**1차 (2026-07-30, §12 초판).** 독립 Claude(라이브 재현 — 헤드라인 전건
재현) + Codex(생산자 소스 추적). 총평 갈림: "보강 후 통과" / **"재설계
필요"**. 일치 지적 10건 전부 반영 → 결정 1·4 재작성, 결정 2·5 신설,
결정 3 논거 교체, 결정 6·7 구체화. 갈린 지점 1건(DB resolve가 계약인가)은
**양쪽 다 옳음을 직접 확인**해 종합안 채택(typed 경로를 쓰되 환경 관측으로
격하 + §2.9-1 청구 유지).

**2차 (2026-07-30, 개정 1판 재검토).** 총평 갈림: Claude "보강 후 착수" /
Codex **"재설계 필요"**(1차 지적 13건 중 해소 5·부분 5·미해소 3).
**"선언만 하고 본문은 그대로"인 항목은 없었으나**, 개정이 신설한 결정 2·4가
실측과 어긋났다:

- **결정 2 판별식이 DB span 28,282건을 삼켰다**(비-DB는 270건) — 측정 쿼리에
  있던 `db.system` 배제가 본문에 옮겨지지 않았다. → 우선순위 명시(12.3).
- **결정 4의 지정 원천이 앱 span에 없었다**(`lucida.host_target_id` 32~38건,
  전량 `collector-dpm`; `host_name`은 파드명) → 원천 교체(12.5).
- **결정 4 ↔ 결정 6 내부 모순**(부착 호스트가 frontier인가) →
  `hop_cost=0` 간선으로 통일(12.5·12.7-2).
- **이름만 있고 계약이 없는 필드 3건**: `logical_calls`(계산 불가 — 삭제) ·
  `cursor`(삭제) · dedup 키("명시한다"는 지시문 → 실제 키로 고정).
  **§10.7도 같은 병**(선언한 `next_cursor`가 `listchanges.go`에 부재)이라
  §10.7에서도 삭제했다.
- 그 외 반영: PRODUCER 고아 537건 처리(1판은 조용히 탈락) ·
  `external_observed` → `peer_observed` 하향 · parent lookback 5분 ·
  latency 통계 지정 · `status_code` 2값 불변식 철회 · 네트워크 선정 순서 ·
  30s 예산 의미 · `edge_kinds`는 출력 필터 · 탐색 양방향 · 원격끝 조인 규칙 ·
  `confidence` 이름 충돌 · 자리 교체 2건 추가(`meta_smoke_test.go:33` ·
  `spec-agent-design.md:618`) · 근거 문장 자기모순 정정(12.6).

**미검증으로 남기는 것**: `ambiguous` 분기(이 환경은 전부 distinct 1개) ·
잘림 3필드(상한이 현 규모의 10배라 미발동) · `hosted_on` 커버리지.
전부 12.8·12.9에 명시했다.

Codex 원문: `.omc/artifacts/ask/codex-…rca-expand-topology-d-2026-07-30T00-10-30-727Z.md`(1차),
`.omc/artifacts/ask/codex-…docs-spec-tool-re-2026-07-30T00-41-54-778Z.md`(2차).


## 13. 구현 설계 — db_blocking (2026-07-30 합의, blind 3자 패널)

§3.6 DB 메뉴 2도구 중 첫째의 구현 설계. §11과 같은 **blind 3자 패널**(브리프
하나를 자체·독립 Claude·Codex가 독립 설계)로 생성했다. 이 절이 남기는 두 개의
처음:

- **자체안이 핵심 결정에서 기각된 첫 사례.** 자체안의 결정 1(루트 단위 사건
  접기)을 다른 두 패널이 **독립적으로 같은 논거**로 반박했고(13.2), 자체안을
  접었다.
- **실측이 세 안 전부를 정정한 첫 사례.** 세 안이 모두 status 판정에 지속
  시간을 넣었는데, 사건 단위 재실측이 "지속은 배경과 사건을 가르지 못한다"를
  보여 판정식이 규모 단독으로 바뀌었다(13.5).

### 13.1 원천 실측과 그 지위

라이브 119(CH `lucida.dpm_session_local` + PG) + 생산자 소스(lucida-next).
**engine 목록·키 계약은 생산자 소스가 정본이고 수치는 `observed_in_capture`
등급이다.** §2.6·§3.6이 "engine 3종"으로 기술한 것은 불완전했다 — 실제 계약은
8종이고 sentinel도 균일하지 않다.

| 원천 | 실측 | 지위 |
|---|---|---|
| 폴 간격 | 1분 (3h 창에 distinct timestamp 176~177) | 관측 — 코드는 데이터에서 구한다 |
| engine 실재 | postgresql·oracle·mysql 각 대상 1개 | 환경 관측 |
| 블로킹 희박도 | PG 588/30,407(1.9%) · Oracle 487/1,679,512(0.03%) · **MySQL 0/73,995** | 관측 |
| 사건 수(연속 폴 접기) | PG 28건 · Oracle 374건 | 관측 — 13.5의 근거 |
| `db_sql_text` | engine별 ~200키, 전수 아님 | 관측 |

**정정 1 — engine은 열린 집합이다.** 적재 게이트(`ingest/writer/
clickhouse_dpm.go:196`)가 아는 engine은 7종(postgresql·oracle·tibero·mysql·
mariadb·mssql·clickhouse)이지만 **모르는 engine도 `degraded=true`로 적재된다**
(raw body 보존). 실제로 `cubrid`가 게이트 목록에 없는데 같은 테이블에
`engine='cubrid'`로 들어온다(`store/database_dpm_cubrid_ch_store.go:50`).

**정정 2 — sentinel이 균일하지 않다.** CUBRID는 `blockingTid = -1`이 "없음"
이다(`database_dpm_cubrid_ch_store.go:28`, 주석이 "PG/Oracle의 0과 다름"을
명시). `!= 0` 판별식은 **CUBRID 전량을 오탐한다**.

| engine | 막힘 키 | sentinel | 세션 id | 블로킹 원천 |
|---|---|---|---|---|
| postgresql | `blockingPid` | 0 | `pid` | `pg_blocking_pids()` |
| oracle | `blockingSession` | 0 | `sid` | V$SESSION |
| tibero | `blockingSession` | 0 | `sid` | `V$WAITER_SESSION`(WAIT_SID→HOLD_SID) |
| mysql | `blockingTid` | 0 | `tid` | `sys.innodb_lock_waits` |
| mariadb | `blockingTid` | 0 | `tid` | lock_pairs |
| mssql | `blockingSid` | 0 | `sid` | blockingSet |
| cubrid | `blockingTid` | **-1** | `tid` | 컬렉터 classify |
| **clickhouse** | **없음** | — | — | **없음**(13.6) |

**정정 3 — 공통 축과 막힘 키는 다른 것을 뜻한다.** `log_attributes
['is_blocking']`은 **"내가 남을 막는다"**이고 막힘 키는 **"내가 막혔다"**다.
PG 전 기간 교차:

| is_blocking | 막힘 키 비0 | 행수 | 뜻 |
|---|---|---|---|
| 0 | 0 | 29,759 | 무관 |
| 1 | 1 | 476 | 막히면서 막는 중간 노드 |
| 1 | 0 | **73** | **루트**(아무에게도 안 막히며 남을 막음) |
| 0 | 1 | **112** | 막혔는데 상대가 표시를 못 받음 |

→ **루트를 그래프 계산 없이 원천이 직접 표시한다.** 실측 대조: PG 07-16
08:36:30 스냅샷에서 루트로 판정된 `25206`이 정확히 체인의 머리였고, Oracle
07-22 두 폴에서도 루트가 폴마다 유일했다.

**정정 4 — 짝은 구조적으로 불완전하다(이 절의 최대 제약).** PG 수집 SQL 정본
(`backend/pkg/dbpoll/postgres_logs.go:57~58`):

```sql
COALESCE(ib.is_blocking, 0)       AS is_blocking,   -- 배열 어디에 있든 1
COALESCE((a.blocking_pids)[1], 0) AS blocking_pid,  -- ★배열 첫 원소만
```

`pg_blocking_pids()`는 배열을 반환하는데 **첫 원소만 저장된다.** 그래서
"`is_blocking=1`인데 자기를 지목한 세션이 아무도 없는" 세션이 생긴다 —
누군가의 2번째 이후 blocker이고 그 짝이 지워진 것이다. 실측 대조: 위 스냅샷의
`25039·25049·25051·25058·25059`가 정확히 이 부류다. **어떤 계산으로도 복원되지
않는다.**

부수 계약: 모집단은 `state NOT IN ('idle')`(순수 idle 제외) · 컬렉터 자기 세션
미제외 · `relNames`(pg_locks 조인한 잠긴 relation 이름)는 블로킹 관련 행에만
채워지고 **Oracle/Tibero엔 없다**.

**정정 5 — 사건의 모양이 engine마다 다르다.**
- PG는 **안정적 다단 체인**: 07-16 `25206 → 25043 → {…} → 25071 → {25074,
  25081}` 깊이 3이 4폴 이상 유지. 07-23은 깊이 4. 07-24는 막힌 집합이 폴마다
  9→19→29로 성장.
- Oracle은 **루트가 폴을 넘어 이동**: 07-22 07:28:38 루트 `227`(220을 막음) →
  07:29:39 루트 `220`(227을 막음). **각 폴 안에는 사이클이 없다** — 락이
  넘어간 것이다. 초판의 "두 폴을 합치면 사이클"은 오독이었고 철회한다.

**정정 6 — 경합 대상 테이블이 원천에 있다(PG 한정).** `relNames` 실측 상위가
`inventory, inventory_pkey, inventory_product_id_key`(339건)이고, 이는
테스트베드 `scenario-01-inventory-lock.sh`의 `SELECT … FOR UPDATE` 대상과
일치한다. "원천에 락 식별자가 없다"는 초판 기술은 PG에서 틀렸고 철회한다.
단 relNames는 **그 세션이 잡거나 기다리는 relation 전부**이며 경합 중인 그
락의 특정이 아니다.

### 13.2 결정 1 — 사건 키는 (대상, engine, 끊기지 않은 블로킹 폴 구간). 루트도 짝도 키가 아니다

**근거:** 자체안은 루트 단위 접기를 제안했으나 다른 두 패널이 독립적으로 같은
반박을 냈다 — §13.1 정정 5의 **Oracle 루트 이동이 하나의 경합을 폴 수만큼
분열시킨다.** 227↔220 두 폴은 세션 집합이 거의 같은 같은 경합인데 루트를 키로
삼으면 "두 개의 짧은 사건"으로 보고되어 조사자가 오독한다. 순서쌍 단위는
07-24의 막힌 집합 성장(9→29) 때문에 사건 수가 대기 행렬 크기만큼 부풀고,
정정 4의 지워진 짝은 순서쌍이 없어 아예 보고되지 않는다. 연결 성분 단위는
정정 4가 성분을 거짓 분할한다. 남는 안정적 불변량은 "이 대상에서 블로킹이
관측된 폴이 연속인가"뿐이다.

**계약:**
- 폴 = 동일 `timestamp`를 공유하는 행 묶음. **블로킹 폴** = 그 폴에
  `is_blocking='1'` 행이 있거나 막힘 키가 sentinel이 아닌 행이 있는 폴.
- 사건 = 같은 `(target_id, engine)`의 연속한 블로킹 폴 최대 구간. 실제로
  수집된 **비블로킹 폴이 끼면 끊는다.** 그 구간에 이 대상의 행이 아예 없으면
  폴 자체가 없는 것이므로 끊지 않고 `gap_polls`로 센다.
- 폴 간격은 창 내 distinct timestamp의 중앙 간격으로 **데이터에서 구한다**
  (1분 하드코딩 금지 — 폴 주기는 계약이 아니다).
- 루트가 폴 사이에 바뀌어도 **한 사건**이며, 변천은 `root_timeline`과
  `root_migrated`로 보존한다. Oracle 07-22 두 폴 = 사건 1개, `polls=2`,
  `root_migrated=true`.
- 막힌 집합이 자라면 합집합 하나로 뭉개지 않는다 — 폴별 수를
  `blocked_timeline`, 최대를 `blocked_peak`, 마지막 폴을 `blocked_last`로
  나눠 싣는다. 합집합만 주면 "지금 29개가 막혀 있다"와 "총 29개가 스쳐갔다"를
  구분할 수 없다.
- `first_seen`/`last_seen`은 **폴 시각이며 락 시작·해소 시각이 아니다.**

**기각한 대안:** 루트 단위(자체안 — 위 근거로 기각) · 순서쌍 단위 · 무방향
연결 성분(락 식별자가 원천에 없어 "같은 락"이 관측이 아니라 추론).

### 13.3 결정 2 — 트리를 그리지 않는다. 관측된 짝 + 두 방향 결손 명단

**근거:** 정정 4로 짝의 집합이 원천에서 이미 손실됐다. 트리를 그리면 조사자는
그것이 전부라고 믿고, `25039` 등 5개는 잎으로 잘못 그려져 "남을 막는 중"이라는
사실이 지워진다. 그리고 결손은 **양방향**이다 — 정정 3의 73행(막는데 상대
없음)과 112행(막혔는데 상대가 표시 못 받음)은 서로 다른 부류이므로 한 필드로
합칠 수 없다(독립 Claude 단독 지적, 자체안·Codex 모두 놓쳤다).

**계약:** 세션의 역할은 두 축의 조합으로만 정한다(지워진 짝에 영향받지 않는
축이다).
- `root` = is_blocking=1 & 막힘 키 = sentinel
- `both` = is_blocking=1 & 막힘 키 ≠ sentinel
- `blocked_only` = is_blocking=0 & 막힘 키 ≠ sentinel

그리고 세 목록을 낸다(전부 항상 존재, 빈 배열 허용 — 필드 부재로 결손을
표현하지 않는다):
- `edges`: 막힘 키가 sentinel이 아닌 행마다 `{blocked, blocker}`. **아는
  만큼**이며 PG에서는 "첫 blocker로 저장된 관측 간선"이다.
- `unpaired_blockers`: `is_blocking='1'`인데 `edges`의 어떤 `blocker`도 아닌
  세션. = 정정 4가 만든 짝 지워진 blocker. 실측 5개.
- `orphan_blocked`: 막힘 키가 비sentinel인데 지목된 blocker의 행이 그 폴에
  없거나 그 행의 `is_blocking='0'`인 세션. = 정정 3의 112행 부류.

체인 깊이는 `chain_depth_min_observed`로만 낸다 — **이름에 `min`을 박아
하한임을 계약으로 못 박는다**(독립 Claude 제안). 트리·그래프 필드는 만들지
않는다.

**기각한 대안:** 짝 보강해 트리 완성(원천에 정보가 없다 — 인용 불가한 관측이
된다) · `is_blocking=1`을 모두 루트로 승격(중간 노드 476행과 모순) · 결손을
`limits` 문구로만 알림(어느 세션인지 짚을 수 없는데 세션 id는 산출 가능하다).

### 13.4 결정 3 — 짝 계열 필드는 대표 폴 한 폴의 관측이다

**근거:** 독립 Claude 단독 지적이고 자체안의 실제 결함을 잡았다. 자체안은
창 전역의 distinct 짝을 모았는데, 그러면 **서로 다른 시점의 짝이 섞여 어느
순간에도 존재하지 않은 트리가 합성된다.** Oracle처럼 루트가 이동하면
`(220←227)`과 `(227←220)`이 같은 목록에 들어가 모순된 그림이 된다.

**계약:** `representative_poll_ts` = 사건 중 막힌 세션 수가 최대인 폴, 동수면
가장 늦은 폴. `edges`·`unpaired_blockers`·`orphan_blocked`·
`chain_depth_min_observed`·`contended_objects`·`wait_summary`·`sql_refs`는
**전부 이 한 폴 소속**이며, 그 사실을 `limits`에 폴 시각과 함께 싣는다.
사건 전역 값은 `roles`·`blocked_timeline`·`root_timeline` 등 폴별 집계로만
낸다.

**기각한 대안:** 창 전역 distinct 짝(자체안 — 위 근거로 기각) · 짝별로
first/last 관측을 붙여 접기(Codex안 — 접힌 목록이 여전히 시점을 섞는다).

### 13.5 결정 4 — status는 규모 단독. 지속은 배경과 사건을 가르지 못한다

**근거:** 세 안이 모두 지속을 판정에 넣었다(Codex는 "블로킹 1건이면 이상",
자체안·독립 Claude는 "2폴 이상 지속 **또는** N세션 이상"). 사건 단위 재실측이
셋 다 정정했다 — 연속 블로킹 폴 구간을 사건으로 접어 (지속 폴 수 × 최대 막힌
세션 수) 분포를 세면:

| engine | 사건 수 | 지속 2~3폴인데 최대 막힘 1세션 | 최대 막힘 ≥3 |
|---|---|---|---|
| oracle | 374 | **54** | 3 |
| postgresql | 28 | 2 | 11 |

**Oracle의 배경 잡음은 오래 지속되지만 작다**(1세션이 2~3폴). 반면 실제
사건은 규모로 드러난다 — PG 10~30세션, Oracle 07-22 8세션. 즉 `또는 2폴 이상`
절은 Oracle 배경 54건을 전부 이상으로 올리고, "1건이면 이상"은 374건 중
352건을 오탐한다. §5.4에서 이미 신뢰를 잃은 도구를 반복하는 셈이다.

**계약:**
- `status='anomalous'` ⟺ 사건 중 하나라도 `blocked_peak >= 3`.
- 블로킹은 있으나 전부 `blocked_peak < 3` → `status='normal'`, **findings는
  그대로 싣고** summary에 소규모 관측임을 적는다(판정과 보고를 분리한다).
- 지속 폴 수는 판정에서 빼고 관측 정보로만 싣는다.
- 임계 3은 **파라미터로 노출하지 않는다**(13.7).
- `assessment_basis`에 창·폴 수·판정식·이 창의 블로킹 행 비율을 넣는다.
  비율은 창 안에서 실제로 세며 §13.1의 전기간 수치를 상수로 인용하지 않는다.
- **임계 3은 PG·Oracle 관측으로만 교정된 임시값이다**(13.12-8).

**기각한 대안:** 블로킹 1건=이상(Codex안) · 지속 OR 규모(자체안·Claude안) ·
체인 깊이 기준(정정 4로 하한만 아는 값에 임계를 걸면 판정이 원천 결손에 따라
흔들린다) · `relNames`가 핵심 테이블인지로 판정(무엇이 핵심인지 자료에 없다).

### 13.6 결정 5 — engine 커버리지 3등급. ClickHouse는 "없음"이 아니라 "판단 불가"

**근거:** 결정 2(§6 전제)의 "공통 축은 전 engine 공통"이 **ClickHouse에서
거짓이다**(독립 Claude가 전제의 틈으로 지적했고 소스 확인으로 확정).
`dbpoll/clickhouse_session.go`는 `system.processes`(실행 중 쿼리)를
`engine='clickhouse'`로 같은 테이블에 담는데, body에 막힘 키가 없고 bounded
attrs가 **3키(user/query_kind/collection_scope)뿐 — `is_blocking`이 없다.**
ClickHouse에는 세션 간 락 경합이라는 개념 자체가 없다. 여기서 "블로킹 없음"을
반환하면 조사자가 "확인했고 깨끗하다"로 오독한다.

**계약:** 내부 engine 표의 각 항목에 `pair_linking` 등급을 둔다.
- `verified` — postgresql · oracle (블로킹 실측으로 짝이 확인됨)
- `unverified` — tibero · mysql · mariadb · mssql · cubrid (생산자 계약은
  확인했으나 블로킹 실측 대조가 없다. cubrid는 sentinel `-1` 필수)
- `unsupported` — clickhouse 및 표에 없는 모든 engine

등급은 `coverage.pair_linking`에 그대로 싣는다. `unverified`에서 사건이 나오면
limits에 "이 engine의 짝 연결은 생산자 계약에서 읽은 것이며 블로킹 실측으로
대조된 적이 없다"를 추가한다. **`unverified`라도 결과를 숨기거나 낮추지
않으며** status 판정식은 등급과 무관하게 동일하다. 등급은 값이지 필드가 아니라
실측 확보 시 승격만 하고 스키마는 바뀌지 않는다.

**공통 축 자체도 런타임에 검사한다** — `log_attributes`에 `is_blocking` 키가
없고 막힘 키도 산출 불가면 13.9의 세 번째 빈손 분기다. 표에 있는 engine이라도
키 부재는 데이터가 답하게 한다.

**기각한 대안:** 표에 없는 engine 전부 거절(공통 축이 있는 경우의 관측을
버린다) · 미검증 분기를 코드에서 빼기(그 engine에서 도구가 침묵한다) ·
검증 등급을 응답에서 감추기(빈손의 이유가 도구 결함인지 실제 부재인지
조사자가 구분할 수 없다).

### 13.7 결정 6 — findings 필드와 계약

findings 1행 = 사건 1개. 아래는 전부 `dpm_session_local`의
`timestamp`/`target_id`/`engine`/`body`/`log_attributes`만으로 계산된다.

| 필드 | 계약 |
|---|---|
| `engine` | 행의 `engine` 그대로(정규화·소문자화 없음) |
| `first_seen`/`last_seen` | 사건 첫·마지막 블로킹 폴의 timestamp. **폴 시각이며 락 시작 시각이 아니다** |
| `polls` | 사건에 속한 블로킹 폴 수. **"지속 시간"이 아니라 목격 횟수** |
| `gap_polls` | 사건 구간 안에서 이 대상의 행이 아예 없던 폴 수(구간 판정 미사용·신뢰도 참고) |
| `root_timeline` | 폴별 `{ts, root_session_ids[]}`. 루트 = 13.1 정정 3의 조합 |
| `root_migrated` | 인접 폴 간 `root_session_ids` 집합이 한 번이라도 달라지면 true |
| `blocked_timeline` | 폴별 `{ts, blocked_n}`. `blocked_n` = 막힘 키가 비sentinel인 행 수 |
| `blocked_peak`/`blocked_last` | `blocked_timeline`의 최대 / 마지막 값. `blocked_peak`가 판정 입력(13.5) |
| `roles` | `{root[], both[], blocked_only[]}` — 사건 전역 합집합(13.3) |
| `representative_poll_ts` | 13.4의 대표 폴 |
| `edges` | 대표 폴의 `{blocked, blocker}` 배열(13.3) |
| `unpaired_blockers`/`orphan_blocked` | 대표 폴의 두 결손 명단(13.3) |
| `chain_depth_min_observed` | `edges`만으로 만든 최장 경로의 노드 수. 하한(13.3) |
| `wait_summary` | 대표 폴 블로킹 관련 행의 대기 표시를 `{키: 행수}`로. 원천 키는 engine별(PG `waitEvent` · Oracle/Tibero `event`+`waitClass`). **MySQL `command`는 대기 이벤트가 아니므로 쓰지 않는다** |
| `contended_objects` | 13.10 |
| `sql_refs` | 대표 폴 블로킹 관련 행의 `{session, sql_ref}` — PG/MySQL `sqlHash` · Oracle `sqlId`. **식별자일 뿐 본문이 아니다** |
| `blocked_predicate` | 실제로 쓴 판별식 문자열(예: `blockingTid > 0`) — 조사자가 검산할 수 있게. 초판의 `sentinel_used`를 구현이 교체했다(13.15-1) |
| `refs` | `ch:dpm_session_local:<target>:<ts>:session:<id>` — 그대로 재조회 가능한 좌표 |

**세션 id는 10진 문자열로 직렬화한다**(Codex 제안) — engine 간 JSON 숫자 타입
차이를 응답 밖으로 내보내지 않는다. 배열 정렬은 최초 관측 시각, 동시각이면
세션 id 오름차순으로 고정한다.

**넣지 않는 필드와 이유:** 체인 깊이(하한만 안다 — 이름으로 격하해 넣는다) ·
블로킹 지속 시간(초) — §5.5의 `tranTime`/`duration`/`lockTime`/`waitTime`/
`stateTime`은 **단위가 engine별로 확인되지 않았다**. 지속은 폴 수로만 센다 ·
락 종류(원천에 없다).

### 13.8 결정 7 — 파라미터

| 인자 | 필수 | 기본 | 근거 |
|---|---|---|---|
| `db` | 예 | — | DB 대상 UUID. 이름 문자열 파싱 금지(§8 결정 3 규율) |
| `from`/`to` | 아니오 | 인시던트 창 | 창 전역 스캔이 이 도구의 핵심이다. §5.4의 실패가 좁은 창이었으므로 조사자에게 창을 좁히도록 강요하지 않는다 |
| `max_events` | 아니오 | 20 | 초과 시 `blocked_peak` → `polls` 내림차순으로 자르고 `Truncated` + 총 사건 수를 밝힌다 |
| `max_timeline_polls` | 아니오 | 60 | 초과 시 timeline은 앞 30 + 뒤 30만. `polls`·`blocked_peak`는 **생략 없이 전량에서** 계산한다 |

**임계 3(13.5)은 파라미터로 노출하지 않는다** — 조사자가 임계를 낮춰 원하는
답을 만들 수 있고, 같은 인시던트에서 호출마다 판정이 달라져 평가가 재현되지
않는다. 판정식은 도구가 책임진다.

`engine`도 받지 않는다 — 원천 행에서 읽는다. 받으면 조사자의 잘못된 추정이
막힘 키 선택을 오염시킨다. 한 대상에 여러 engine 값이 있으면 engine별 사건으로
분리한다.

### 13.9 결정 8 — 빈손은 세 갈래. "블로킹 없음"은 no_data가 아니다

**근거:** §5.4에서 기존 도구가 신뢰를 잃은 방식이 빈손의 이유를 구분해 주지
않은 것이다. 그리고 "조회했고 없었다"는 **인용 가능한 normal 관측**이다 —
no_data로 내리면 조사자가 배제 근거로 쓸 수 없다(registry.go 봉투 ref 규율과
같은 논리).

| 상황 | status | reason |
|---|---|---|
| 행 있고 블로킹 축(있는 것 전부) 전부 꺼짐 | **normal** | — (findings 빈 배열, summary에 "조회했고 없었다") |
| 창 내 이 대상의 행 0 | `no_data` | `zero_observations`. 인접 창을 한 번 더 조회해 "이 창만 빔"과 "대상 자체 미수집"을 사유 문구로 구분 |
| `is_blocking` 키 부재 + 막힘 키 산출 불가 | `no_data` | `not_collected` — "이 대상엔 원래 미적용"(ClickHouse가 이 자리, 13.6) |

짝 키만 없고 공통 축이 있는 engine에서 블로킹이 있으면 status는
normal/anomalous이고 `coverage.pair_linking='unsupported'` + `edges=[]`로 짝
불가만 밝힌다. **`[]`는 "지원되며 관측 결과 없음", 필드 부재·`null`은 "산출
계약 없음"으로 엄격히 구분한다**(Codex 제안).

### 13.10 결정 9 — relNames는 쓴다. 없는 engine에서는 필드를 생략하고 밝힌다

**근거:** 정정 6 — RCA에서 "무엇을 두고 다퉜나"에 직접 답하는 유일한 열이고,
실측이 시나리오의 락 대상과 일치했다.

**계약:** `contended_objects` = 대표 폴 블로킹 관련 행의 `relNames`를 콤마
분리·trim·중복 제거·사전 정렬. **원문 그대로** — 테이블/인덱스 구분이나
`_pkey` 같은 접미사 제거를 하지 않는다(접미사 규칙이 자료에 없고, 가공하면
조사자가 인용한 이름이 원천에 없게 된다. 실측 `inventory_pkey`는 인덱스다).
필드가 없는 engine(Oracle·Tibero 등)에서는 **필드를 생략하고**
`coverage.contended_objects='unavailable_for_engine'`으로 밝힌다 — 빈 배열로
내면 "경합 대상이 없다"로 오독된다. summary에서 "경합 테이블"이 아니라
"블로킹 관련 세션에서 관측된 relation"으로 표현한다.

### 13.11 일부러 안 만든 것

1. **세션 총수·상태 분포·대기 분포·커넥션 포화** — §3.6 규율 3(c)대로
   read_timeseries + Q11 한도 부착의 몫. 옴니버스 금지.
2. **SQL 본문·플랜 조회** — `sql_refs`로 식별자만 낸다. `db_sql_text`는
   engine별 ~200키의 부분집합이라 붙이면 "있으면 보이고 없으면 안 보이는"
   비결정적 열이 생기고, 조사자가 부재를 신호로 오독한다. 연쇄는
   db_slow_queries가 갖는다.
3. **완전한 트리·정확한 깊이·전체 fan-out** — 정정 4로 불가(13.3).
4. **다중 blocker 복원** — 원천에 없다. 추정하지 않는다.
5. **락 지속 시간(초)** — 시간 필드 단위가 engine별로 미확인(13.7).
6. **MSSQL `blockerSid`** — 의미가 반대다(이 세션이 막는 세션의 **개수**).
   짝 연결에는 `blockingSid`만 쓴다. 이름이 한 글자 차이라 혼동 위험이 크다.
7. **CUBRID `sessionType` 기반 분류** — 컬렉터의 판단이고 실측 대조가 없다.
   짝 판정은 `blockingTid`(sentinel `-1`)와 `is_blocking`으로만.
8. **사이클 탐지** — 폴 내 사이클 부재가 실측 2건에서 관측됐을 뿐 계약이
   아니고, 정정 4의 결손 상태에서 사이클 판정은 거짓 양성을 낸다.
9. **데드락 판정** — 별 원천(`dpm_deadlock_ch_store`)이 있고 다른 질문이다.
10. **조치(세션 kill 등)** — 진단 도구는 관측만.

### 13.12 한계 (응답에 싣는다)

1. **짝은 첫 blocker만 보존된다**(정정 4). `unpaired_blockers`로 존재는
   알리지만 그들의 상대는 알 수 없다. 다른 engine의 배열 여부는 미확인이다.
2. **폴 해상도 관측이다.** 폴보다 짧은 블로킹은 보이지 않고, `polls=1`은
   "1폴 간격 지속"이 아니라 "한 번 목격"이다.
3. **PG 모집단은 `state NOT IN ('idle')`** — 순수 idle 세션은 스냅샷에 없다.
   다른 engine의 모집단 조건은 확인하지 않았다. 컬렉터 자기 세션은 포함된다.
4. **루트가 폴 사이에 이동하면 한 사건으로 접힌다**(13.2) — `root_migrated`로
   알리지만, 락 소유가 넘어간 것과 별개 경합이 이어진 것을 구분하지 못한다.
5. **사건은 대상 단위 구간이다** — 같은 DB에서 서로 무관한 두 경합이 동시에
   있으면 한 사건으로 뭉친다(13.2에서 성분 분할을 기각한 대가).
6. **짝 계열 필드는 대표 폴 한 폴의 관측이다**(13.4).
7. **`contended_objects`는 PG 계열만**이고 경합 락의 특정이 아니다(13.10).
8. **status 임계 3은 PG·Oracle 관측으로만 교정된 임시값이다**(13.5). 다른
   engine에서 적절한지 모른다.
9. **`roles`는 사건 전역 합집합이라 한 세션이 두 구획에 동시에 나올 수
   있다**(구현 실측 — Oracle 07-22 사건에서 `220`이 `root`와
   `blocked_only`에 함께 등장). 루트가 이동했기 때문이며 어느 폴에서
   무엇이었는지는 `root_timeline`이, 이동 사실은 `root_migrated`가
   가리킨다. 그래도 `roles.root`를 "동시 루트 2개"로 읽으면 오독이다.
10. **MySQL 계열 분기는 미검증이며 그 이유는 시나리오 부재다.** 수집기는 정상
   작동한다 — MySQL 세션이 실행 중 쿼리 26,624건·ACTIVE 트랜잭션 27,227건으로
   수집되고 있고, 수집 SQL이 `sys.innodb_lock_waits`를 한 쿼리에서 조인하므로
   **행이 적재된다는 것 자체가 그 뷰를 읽는 데 성공했다는 증거**다(권한
   문제라면 쿼리 전체가 실패해 행이 없다). 락 대기 시간 최대치가 0.59ms로
   경합이 실제로 없었다. 원인은 food-delivery 시나리오 7종에 row-lock이
   없다는 것이고, **이는 의도된 배정**이다(service-spec이 "plopvape stock
   (integer lock 필요)과 **다른** boolean toggle race"를 명시). → **닫는 길:
   food-delivery에 MySQL row-lock 시나리오 1건 추가**(`/testbed-generate-
   scenarios`). 그러면 `unverified` 6종이 5종으로 줄어든다. 이 절의 구현과는
   별건이며 순서상 도구가 먼저다(2026-07-30 사용자 판단).

### 13.13 자리 교체 (§7.1)

db_blocking이 들어오면 **`get_db_sessions`가 빠진다.** 표면 산술: 현행 신 8 +
구 8 = 16 → **신 9 + 구 7 = 16종**.

| 위치 | 작업 |
|---|---|
| `tools/db.go:1~2,60~194` | `NewDBSessionsTool`·`queryDBSessions`·`sessionKeysByEngine` 제거. **`dpmSnapshot`·`bodyOf`·`num`·`topCounts`·`dbParse`는 get_slow_queries가 공유**하므로 남긴다 |
| `tools/registry.go:89` | 배선 교체 + 머리 주석 카운트(56~60행) |
| `tools/db_smoke_test.go:32` | `NewDBSessionsTool(ch)` — **제거 시 컴파일 실패**, 재작성 필수 |
| `pipeline/obligations.go:32` | `"database": {"get_db_sessions", "get_slow_queries"}` — **의무 목록이라 단순 문서가 아니다.** 이름 교체 |
| `pipeline/obligations_test.go:129` · `pipeline/generate_test.go:43` | 목 갱신 |
| `llm/mockworld_test.go:86,95` | 목 갱신(86행은 summary 문구 안 도구명) |
| `llm/investigator_smoke_test.go:37,45,166` | 목 갱신 |
| `docs/ref-tool-data-access.md` · `docs/spec-agent-tools.md` | 표면 표 갱신 |
| `docs/frontier-trajectory-f01r.md:41,125,133,134` | **건드리지 않는다**(역사 기록) |

**주의:** `dpmSnapshot`은 "창 내 마지막 스냅샷 10초"를 보는 함수다(§5.4의
결함 그 자체). db_blocking은 이것을 쓰지 않고 창 전역 스캔을 새로 만든다 —
get_slow_queries가 계속 쓰므로 함수는 남지만 **재사용하면 결정 1이 무너진다.**

### 13.14 패널 기록

**방식(§11.0 상속).** 브리프 하나(원천 계약·실측·확정 결정 2개·열린 질문
10개)를 자체·독립 Claude·Codex가 blind로 독립 설계. 브리프와 3안 원문:
`.omc/artifacts/panel/dbblocking-{brief,design-main,design-claude,
design-codex}-2026-07-30.md`.

**수렴(3/3).** 사건 키는 루트가 아니라 구간(자체안만 루트 → 기각) · 트리 금지 ·
relNames 채택 · SQL 본문 미포함(식별자만) · 세션 볼륨 미포함 · 지속 시간(초)
필드 금지 · MSSQL `blockerSid` 배제 · 조치 없음.

**자체안이 기각된 지점.** 결정 1(루트 단위)과 결정 3(창 전역 distinct 짝).
둘 다 **다른 두 패널이 독립적으로 같은 반박**을 냈다. 후자는 자체안이 어느
순간에도 존재하지 않은 트리를 합성한다는 실제 결함이었다.

**독립 Claude 단독 기여 3건.** ① `orphan_blocked`(112행 부류 — 자체안·Codex
모두 놓쳤다) ② **§6 전제의 틈 지적** — 미지 engine에서 공통 축이 채워지는지
자료에 없다 → 소스 확인으로 ClickHouse에서 실제로 거짓임을 확정(13.6)
③ `chain_depth_min_observed`의 이름으로 하한 못 박기.

**Codex 단독 기여 3건.** ① 세션 id를 10진 문자열로 직렬화(engine 타입 차이)
② `[]` vs `null` 엄격 구분 ③ `blocked_count_by_poll` 시계열 보존(독립
Claude도 `blocked_timeline`으로 같은 결론 — 실은 2/3 수렴, 자체안만 peak/last
스칼라).

**실측이 세 안을 정정한 지점.** status 판정식. 셋 다 지속을 넣었고(Codex는
"1건=이상"이라 사실상 지속·규모 모두 무시), 사건 단위 재실측이 Oracle 배경
54건이 2~3폴 지속임을 보여 지속 절을 제거했다(13.5). **패널 셋이 다 틀릴 수
있고 실측만이 가른다**는 것이 이 절의 교훈이다.

**미검증으로 남기는 것.** `unverified` engine 6종의 짝 연결 · 임계 3의 타
engine 적정성 · `gap_polls`(실측 환경에 수집 공백이 없어 미발동) ·
`max_timeline_polls` 잘림(최장 사건 8폴이라 미발동). 전부 13.12에 명시했다.

### 13.15 구현 정정 (2026-07-30, `tools/dbblocking.go`)

구현이 설계를 세 군데 고쳤다.

**1. `sentinel_used` → `blocked_predicate`.** 설계는 sentinel 차이(CUBRID
`-1` vs 나머지 `0`)를 필드로 노출하려 했으나, **세션 id가 어느 engine에서도
양수라 판별식 `> 0`이 두 sentinel을 동시에 흡수한다.** 그러면 "이 engine의
sentinel은 -1"이라고 싣는 것은 실제로 쓴 판별식과 어긋난다 — 실제 판별식
문자열을 싣는 필드로 교체했다. sentinel 차이는 여전히 §13.1 정정 2의 실측이나,
코드가 그것에 의존하지 않는다는 것이 더 강한 성질이다.

**2. 폴 주기는 중앙값이 아니라 최빈값이다(단위 가드가 잡은 결함).** 설계의
"중앙 간격"을 그대로 구현했더니 gap 테스트가 실패했다 — 폴 [36, 37, 40]의
간격은 [1m, 3m]이고 중앙값이 3m이 되어 **3분 공백을 정상 주기로 삼고
`gap_polls`가 0이 된다.** 표본이 적을 때 공백 자체가 중앙값을 밀어 올린다.
주기는 "가장 자주 나오는 간격"이므로 최빈값(초 단위로 뭉쳐 셈, 동수면 짧은
쪽)으로 교체했다.

**3. `wait_summary` 원천에 MSSQL `waitType`을 포함했다.** 설계 표는 PG
`waitEvent`·Oracle/Tibero `event`+`waitClass`만 열거했으나, MSSQL `waitType`은
MySQL `command`와 달리 **진짜 대기 유형 필드**라 같은 원칙의 확장으로 넣었다
(생산자 body 계약 확인: `dbpoll/mssql_logs.go:108`).

**라이브 검증(119).** PG 07-24 사건: 사건 1건 · 최대 30세션 · 7폴 ·
깊이 하한 3 · 경합 `inventory, inventory_pkey, inventory_product_id_key` ·
**`unpaired_blockers` 28개 검출**(정정 4의 첫 원소 손실이 실물에서 드러났다).
Oracle 07-22 사건: 2폴 · 8세션 · `root_migrated=true`(227→220) · 대표 폴 짝의
blocker가 전부 `220`으로 **시점 혼합 없음**(§13.4가 실물에서 작동) ·
`wait_summary`에 `enq: TX - row lock contention (Application)` 8건 +
루트의 `SQL*Net message from client (Idle)` 1건. 최근 창 3 engine 전부
`normal`(블로킹 없음이 no_data로 새지 않음 — §13.9 확인).

**단위 가드 9개**(사건 접기 4 · 결손 명단 · 판정식 · sentinel · 등급 · 깊이
사이클) + 라이브 스모크 2개(최근 창 3 engine · 블로킹 실재 창 겨냥).
전체 `go test ./...` 통과.

**추가 미검증.** ClickHouse의 `no_blocking_signal` 분기(119에 clickhouse DPM
대상이 없어 도달 불가) · `zero_observations` 분기(행 0 대상 미확보).


## 14. 구현 설계 — db_slow_queries (2026-07-30 합의, blind 3자 패널)

§3.6 DB 메뉴 2도구 중 둘째. §11·§13과 같은 blind 3자 패널(브리프 하나를 자체·
독립 Claude·Codex가 독립 설계)로 생성했다. 브리프와 3안 원문:
`.omc/artifacts/panel/dbslowqueries-{brief,design-main,design-claude,
design-codex}-2026-07-30.md`.

이 절이 남기는 처음: **판정식 두 안을 배경 데이터에 나란히 돌려 하나를 실측으로
탈락시킨 첫 사례**(14.3). §13.5는 실측이 세 안을 함께 정정했지만, 여기서는 두
경쟁 판정식이 같은 실물 사건에서 서로 다르게 행동하는 것을 관측했다.

### 14.1 원천 실측과 그 지위

라이브 119(CH `lucida.dpm_topsql_local` + PG `db_sql_text`·`db_sql_plan`·
`collector_dpm_engine_version`) + 생산자 소스(`backend/pkg/dbpoll/*_topsql.go`,
`services/ingest/writer/clickhouse_dpm.go`). **키·단위·축 계약은 생산자 소스가
정본이고 수치는 `observed_in_capture` 등급이다.**

| 원천 | 실측 | 지위 |
|---|---|---|
| 폴 간격 | ~61초(드리프트 있음) | 관측 — 코드는 데이터에서 구한다 |
| 볼륨 | pg 410,268 · mysql 350,266 · oracle 258,151행(전 기간, 대상 1개씩) | 관측 |
| 폴당 행수 | pg 15~19 · mysql 14~16 · oracle 9~14 | 관측 — §14.1-2의 결과 |
| 24h distinct SQL | pg 58 · mysql 54 · oracle 35 | 관측 |
| `db_sql_text` | pg 204 · mysql 208 · oracle 157행 | 관측 |
| `db_sql_plan` | oracle 118행 · mysql 70,979행 · **pg 0행** | 관측 |
| 배경 변동(30분 창 중앙값 비) | 창쌍 1,448개에서 max 2.61배 · **3배 초과 0건** | 관측 — 14.3의 근거 |

**정정 1 — 단위가 engine마다 다르다(현행 도구의 실측된 결함).** PG/MySQL/MariaDB
`avgExecTime`은 **ms**, Oracle/Tibero/MSSQL `avgElapsedTime`은 **μs**
(`oracle_topsql.go:25` "★elapsed 단위 = μs(PG ms 와 다름)", `tibero_topsql.go:20`은
UNITPROBE 실측으로 μs 확정, `mssql_topsql.go:20`). 24h 분포가 이를 확인한다 —
oracle p50 6,953(=7ms) · p99 44,049(=44ms). 현행 `get_slow_queries`는 두 값을
`avg_elapsed_ms` 한 필드에 그대로 담는다(`tools/db.go:133,185`) — **Oracle이
1000배 부풀어 보인다.**

**정정 2 — 원천은 이미 절단된 top-N이고, 절단축은 기록되지 않는다.** 수집기는
폴마다 **축별 top-5를 뽑아 합집합**을 발행한다(`postgres_topsql.go:384`
`selectTopSQLKeys` · `oracle_topsql.go:443` · `mysql_topsql.go:160`).

| engine | 축 | N |
|---|---|---|
| postgresql(delta) | avgExecTime · totalExecTime · sharedHit · sharedRead · walBytes | 5 |
| postgresql(legacy) | 위 4축(walBytes 제외) | 5 |
| oracle/tibero(delta) | avgElapsedTime · totalElapsedTime · executions · avgCpuTime · avgLogicalReads | 5 |
| oracle/tibero(legacy) | **DB측 단일축** `ORDER BY avg_elapsed_time DESC ROWNUM<=5` | 5 |
| mysql/mariadb | avgExecTime · calls · avgLockTime · rowExamined | 5 |

→ 어떤 행은 **느려서가 아니라 WAL을 많이 써서** 들어온 것일 수 있고, 어느 축이
그 행을 뽑았는지는 저장되지 않는다. top-5 밖 SQL은 데이터에 존재하지 않는다.

**정정 3 — 식별자와 본문 경로가 engine마다 다르다.** 적재기
(`clickhouse_dpm.go:131~163`)가 PG top-SQL만 특수 취급한다.

| engine | CH `sql_id` | CH `sql_hash` | `db_sql_text.sql_key` | body 내 본문 |
|---|---|---|---|---|
| postgresql | **빈 문자열** | queryid(음수 가능) | queryid 10진 문자열 | **항상 있다**(0/20,185 빈값) |
| oracle/tibero | SQL_ID(13자) | 0 | SQL_ID | **항상 없다**(2,890/2,890 빈값) |
| mysql/mariadb | digest(64hex) | 0 | digest | **항상 있다**(0/17,671) |

Oracle 24h 35개 SQL_ID는 전부(35/35) `db_sql_text`에 있었다. 본문은 정규화본이다 —
PG는 `pg_stat_statements` $1 자리표시자 + `redactCredentials`, MySQL은 `digest_text`
(`?`). **바인드 값은 어느 engine에도 없다.**

**정정 4 — `mode`가 값의 뜻을 가른다.** bounded attrs `mode` ∈ `{delta, legacy}`
(키 부재 = legacy로 해석, 생산자 규약 `postgres_topsql.go:36`). `delta`는 폴 간
누적 카운터 차분이라 값이 **1분 구간 Δ**이고, `legacy`는 PGSS 인스턴스/커서 생애
누적이라 **생애 평균**이다. 라이브 119는 3 engine 전부 `delta`이지만
**기본값은 `legacy`**(env `DPM_TOPSQL_MODE=delta`로 켠 상태)이므로 다른 환경은
legacy일 수 있다. delta는 cold-start·first-seen·reset·`Δcalls<=0`에서 행 또는
사이클 전체를 skip한다 — **폴이 비는 것이 정상 동작이다.**

**정정 5 — MySQL 플랜 해시는 사전 키가 아니다.** `db_sql_plan`의 mysql 행
70,979개에 distinct `plan_hash_value`가 **70,979개**(sql_id는 28개뿐). 캡처마다
해시가 달라 "같은 플랜"을 해시로 식별할 수 없다. Oracle은 안정적(네이티브
plan_hash_value). PG는 플랜 수집기가 없어 0행이다.

**정정 6 — 컬렉터 자기 쿼리가 top을 점유한다.** 원천에 "모니터링 쿼리" 표식은
없다. 본문 패턴으로 센 24h 결과: **mysql 5,689/17,686(32%)** · pg 1,822/20,202(9%) ·
oracle 0/2,902. MySQL 상위 5위권 `cdd92d38…`(p50 27.9ms · max 601ms)이 컬렉터
자신의 `sys.x$processlist` + `innodb_lock_waits` 조회다.

**정정 7 — 대상의 engine은 CH 없이도 알 수 있다.** PG
`collector_dpm_engine_version(target_id, engine, version, updated_at)`에 대상별
engine과 버전이 있다(실측 3행 — oracle 23.0.0.0.0 · mysql 8.0.46 · postgresql
16.14). 이것이 결정 8의 "0행인데 engine을 어떻게 아나"를 푼다.

**정정 8 — 등장하는 SQL 집합은 작고 안정적이며, 최상위는 만성이다.** MySQL
최상위 `b79f4ca8…`은 `SELECT * FROM dispatches WHERE STATUS=? AND
DATE_ADD(assigned_at, INTERVAL eta_minutes MINUTE) < NOW()`로 rowExamined ≈ 89만,
p50 324ms·max 1,455ms이며 **하루 1,182폴 전부에 등장**한다. "가장 느린 쿼리"는
인시던트와 무관할 수 있다.

**정정 9 — 실물 지연 사건이 데이터에 있다(14.3의 시험 대상).** Oracle
`ggrfcy439k2xd` = `select … from accounts a1_0 where a1_0.id=:1 for update`.
07-26 13:00~07-27 03:00 중앙값 0.11~0.13ms로 평온하다가 07-27 04:00~10:00에
중앙값 26~76ms·최대 46,540ms(46초)로 뛴다. **플랜 해시는 전 구간 동일**
(1602363137) — 플랜 변경이 아니라 행 락 경합이다(db_blocking이 보는 사건과 같은
가족). 그리고 이 구간에 **이 SQL의 시간당 폴 수가 ~55에서 12~22로 떨어진다** —
`Δexecutions<=0`이면 행을 버리는 생산자 규칙(`oracle_topsql.go:402~`) 때문에
**쿼리가 멈춰 있을 때 관측이 사라진다.** 결손 자체가 증거다.

### 14.2 결정 1 — 접기 키는 (대상, engine, sql_key, mode)이고 2단으로 접는다

**근거:** 조사자가 인용할 단위는 `child_number`나 `toplevel`이 아니라 SQL
식별자다. 그러나 행 키는 engine마다 더 세분(§3.2)이므로 한 폴에 같은 식별자가 여러
행일 수 있다 — 그것을 백분위에 그대로 던지면 한 폴이 여러 표본이 되어 가중이
왜곡된다. **독립 Claude와 Codex가 독립적으로 같은 해법(폴 내 실행횟수 가중 선접기)을
냈고, 자체안의 "충돌은 플래그로만 노출"보다 낫다 — 자체안을 접었다.**
`mode`를 키에 넣는 것은 Codex 단독 기여다: 생애 평균과 구간 평균을 한 시계열로
합치면 뜻이 깨진다.

**계약:**
- `sql_key` = PG는 `toString(sql_hash)`(CH `sql_id`가 빈 문자열이므로 쓰지 않는다),
  그 외는 CH `sql_id`. 둘 다 비면 그 행은 버리지 않고 `dropped_no_key`로 센다.
- **1단(폴 내)**: 같은 `(timestamp, sql_key, mode)`의 행들을
  `execs_poll = Σ 실행횟수` · `latency_ms_poll = Σ(latency_ms_행 × execs_행) / execs_poll`.
  실행횟수 키는 PG/MySQL/MariaDB `calls` · Oracle/Tibero `executions` ·
  MSSQL `executionCount`. `execs_poll = 0`이면 산술평균으로 후퇴하고 그 폴을
  `unweighted_polls`로 센다.
- `latency_ms_행` = ms 정규화(정정 1): PG/MySQL/MariaDB 그대로, Oracle/Tibero/MSSQL
  `÷1000`. `latency_unit_source`로 원천 단위를 밝힌다(결정 B).
- **2단(폴 간)**: `latency_ms_p50` · `latency_ms_max` · `latency_ms_max_at`(그 폴의
  timestamp) · `latency_ms_last`(창 내 마지막 폴) · `polls_seen`(distinct timestamp) ·
  `first_seen`/`last_seen`(**폴 시각이며 쿼리 실행 시각이 아니다**).
- `execs_total = Σ execs_poll`은 **`mode=delta`에서만** 싣는다. legacy는 누적
  카운터라 폴별로 더하면 대규모 중복 계산이다 — legacy 행은 `execs_total`을
  생략하고 그 사유를 밝힌다.
- 창 안에서 한 SQL의 mode가 바뀌면 **두 findings로 분리**한다(같은 행에 섞지 않는다).
  판정은 각 mode 안에서만 한다.
- 폴 주기는 데이터에서 구한다 — 간격의 **최빈값**(§13.15-2가 중앙값의 결함을 이미
  실측했다: 공백이 표본을 밀어 올린다).

**기각한 대안:** 폴별 한 행 그대로(결정 A 위반) · `(sql_key, db, user)` 세분 접기
(engine 비대칭이 스키마로 새고 행이 불어난다 — 관측된 db/user 집합은 부수 필드로)
· legacy를 도구가 스스로 차분해 delta로 만들기(reset·eviction 감지를 재구현해야
하고 틀리면 조용히 폭발값을 만든다) · 자체안의 "충돌 플래그만"(위 근거로 기각).

### 14.3 결정 2 — 판정은 **직전 24시간 중앙값 대비 3배**, 두 팔(중앙·최대). Codex의 p99 판정식은 실측으로 탈락했다

**근거:** 세 안이 모두 "순위만으로는 부족하다"에 수렴했다 — 정정 8 때문에 순위는
워크로드 상수다. 갈린 것은 판정식이다.

- 자체안·독립 Claude: **배수**(자기 기준선 중앙값 대비 3배).
- Codex: **`current.p50 > baseline.p99`** + 기준선 100폴 이상 게이트. 배수 임계는
  "자료에 근거가 없다"며 명시적으로 기각.

**두 판정식을 배경과 실물 사건에 나란히 돌렸다(라이브 119, 4일).**

| 시험 | 배수 3배 | Codex p99 |
|---|---|---|
| 배경 오발 (30분 창쌍) | 1,448쌍에서 **0건** | pg 995쌍에서 **6건**(0.6%) · mysql/oracle 0건 |
| 기준선 게이트 통과율 | 폴 3개면 통과 | oracle **65/134(49%)** — 절반이 판정 불가 |
| 정정 9의 실물 사건(6창) | **6창 전부 발화** | **첫 창만 발화**, 이후 침묵 |

Codex 판정식이 사건 도중 침묵하는 이유는 **기준선이 자기 사건에 오염되기**
때문이다 — 직전 24시간 p99가 이미 사건의 최고값을 담아서 이후 창의 p50이 그것을
넘지 못한다. 배수 판정은 분모가 **중앙값**이라 몇 창의 급등에 흔들리지 않는다
(사건 중에도 기준선 중앙값 0.12ms 유지). 사건이 여러 창에 걸치는 것이 정상이고
(정정 9는 6시간) **조사자는 창을 사건 중간에 잡는다** — 도중 침묵은 치명적이다.

같은 시험이 **자체안의 기준선 정의도 고쳤다.** 자체안은 기준선을 "직전 동길이 창"으로
뒀는데, 그것도 사건이 길면 같은 오염을 겪는다(사건 2번째 창의 분모가 1번째 창).
기준선은 **직전 24시간**으로 늘린다(Codex의 길이 + 자체안·Claude의 중앙값 통계).

**두 번째 팔(최대)은 교차 실측이 정당화했다.** 두 팔을 4일간 나란히 세어 보니
최대 팔 발화 7건이 **전부 중앙값 팔이 놓친 것**(교집합 0)이었고, 그 중 둘이 실물
사건이다:

| 사례 | 창 중앙값 / 기준선 | 창 최대 / 기준선 최대 | 판단 |
|---|---|---|---|
| mysql `e5ffcbfa`+`4ca1b0a1` 07-28 23:30 | 226.6/253.5 · 168.2/206.1(평평) | 2,961/718 · 2,252/601 | **같은 창에 두 SQL 동시 급등 = 실물 정지** |
| oracle `3p52tpw4mm` 07-27 09:00 | 0.21/0.23(평평) | 1,010.7/34.7(29배) | **정정 9 사건 창의 두 번째 SQL** |
| pg `-141323029` 07-27 22:00 · `-163783633` 07-29 20:30 | 평평 | 0.3/0.1 | 서브밀리초 잡음(하한 부재의 대가) |

배경 비용은 4일 3 engine에 7건(창쌍 6,943개 중 0.1%)이다.

**단, 최대 팔의 기준선은 부서지기 쉽다(실측).** 기준선 최대는 24시간에 단 한 번의
급등으로 정해진다 — 정정 9 사건의 09:00 창은 `ratio_max = 17,342/46,540 = 0.37`로
**자기 사건의 앞 시간이 만든 기준선 때문에 최대 팔이 침묵**한다. 그래서 두 팔은
OR이고 AND가 아니다(독립 Claude는 최대를 AND 확인 조건으로 뒀는데, 이 실측이
그것을 기각한다 — AND면 이 창에서 판정 자체가 사라진다).

**계약:**
- 기준선 창 = `[from - 24h, from)`. **파라미터로 받지 않는다**(조사자가 "이상이
  나오는 기준선"을 찾게 만들지 않는다 — §5.3 선례). 기준선 폴이 3개 미만이면
  이 행은 `verdict="insufficient_baseline"`이고 status 판정 모집단에서 제외한다.
- `ratio_p50 = latency_ms_p50 / baseline_latency_ms_p50`,
  `ratio_max = latency_ms_max / baseline_latency_ms_max`. 분모 0이면 그 비를
  계산하지 않고 필드를 생략한다.
- `verdict`:
  - `slower` — `ratio_p50 >= 3.0` **또는** `ratio_max >= 3.0`.
    발화한 팔을 `slower_basis` ∈ `{median, max, both}`로 싣는다.
  - `faster` — `ratio_p50 <= 1/3`(회복 관측 — §3.7 Q18 동반 회복의 재료).
  - `chronic` — `baseline_presence >= 0.9`이고 `1/3 < ratio_p50 < 3.0`.
    독립 Claude 단독 기여. 정정 8의 `b79f4ca8…`이 정확히 이 모양이고, **만성은
    `anomalous`의 근거가 아니다**(만성으로 status를 올리면 모든 인시던트에서 발화).
  - `stable` — 나머지.
  - `insufficient_baseline` · `not_assessable_legacy`(legacy는 생애 평균이라 배수가
    회귀를 뜻하지 않는다).
- 봉투 `status`: `slower` 행이 하나 이상 있고 **그 중 하나라도
  `self_monitoring=false`이면** `anomalous`(결정 7). 아니면 `normal`.
- `assessment_basis` = "창 내 폴 통계를 직전 24시간 같은 SQL의 통계와 비교(중앙값
  3배 또는 최대 3배). 이 문턱은 라이브 24시간 배경 창쌍 1,448개에서 중앙값 비 최대
  2.61배·3배 초과 0건, 최대 비 3배 초과 0.06~0.17%라는 실측에서 왔다. 절대 소요가
  큰 것(만성)과 변화가 큰 것(사건)은 분리해 표시한다."
- **절대 하한은 두지 않는다.** PG는 정상 max가 23.6ms라 어떤 ms 하한도 PG를 통째로
  침묵시킨다. 대가는 실측된 대로 있다 — 배경 시험에서 `0.06ms → 0.16ms`가 2.8배로
  잡혔다. 절대값을 행에 실어 조사자가 크기를 보게 하는 것이 답이고, 하한 신설은
  **발동 조건부 후보**(자격 케이스에서 서브밀리초 발화가 조사자를 실제로 오도한
  것이 관측되면 그때 근거를 갖고 넣는다).

**기각한 대안:** Codex의 p99 판정식(위 실측) · 순위만(정정 8) · engine별 절대
임계표(대상 1개씩의 실측을 일반화할 수 없다 — 정상 p99가 pg 2.57ms·mysql 586ms로
세 자리 차이) · robust z(창당 폴이 10~30개뿐이라 MAD=0인 안정 SQL이 흔하다) ·
창 내부만으로 판정(창이 사건 전체를 덮으면 평평해진다) · 폴 단위 등장/소멸 분류
(폴 단위 존재는 들쭉날쭉하고 창 단위는 안정적이라는 실측 — `presence`로만 노출).

### 14.4 결정 3 — 절단은 세 층으로 명시한다. 역추정하지 않는다

**근거:** 정정 2에 서로 다른 세 무지가 있다 — ① 절단축 미기록 ② top-5 밖 부재
③ 결손과 미실행 구분 불가. ①②는 감출 방법이 없고, ③은 정정 9가 오히려
**결손이 증거일 수 있다**는 것까지 보여줬다.

**계약:**
- ① `coverage.selection_axes` = engine·mode에 따른 축 목록(정정 2의 표를 코드
  상수로). `coverage.selection_axis_per_row = "unavailable"` — 행별로는 알 수 없다.
  **축 역추정 금지**(절단된 집합 안의 순위는 원래 축 순위와 다르다).
- ② 순위 필드 이름은 `rank`가 아니라 **`rank_among_collected`**. `summary`·
  `assessment_basis`에 "이 DB에서 가장 느린 쿼리" 형태를 쓰지 않는다 — 허용 형태는
  "수집된 top-SQL 집합에서". 이 문구 규율은 테스트로 고정한다.
- ③ 행별 `polls_seen` / `polls_in_window`(창 내 그 대상의 distinct timestamp 실측) /
  `presence = polls_seen / polls_in_window`. 창 길이÷60초로 기대치를 계산하지 않는다.
  `presence < 0.5`면 `partial_presence=true`. 그리고 **정정 9를 근거로**
  `limits`에 "지연 사건 중에는 `Δ실행수<=0`으로 행이 버려져 presence가 떨어질 수
  있다 — presence 하락은 미실행의 증거가 아니다"를 싣는다.
- 결손 폴을 0이나 마지막 값으로 채우지 않는다(백분위를 조용히 왜곡한다).

### 14.5 결정 4 — findings 필드와 계약

| 필드 | 원천과 계산 |
|---|---|
| `sql_key` | 결정 1(PG `toString(sql_hash)` / 그 외 `sql_id`) |
| `id_kind` | engine 파생 상수 `pg_queryid`/`oracle_sql_id`/`mysql_digest` |
| `engine` | CH `engine` 컬럼 |
| `mode` | 행의 `log_attributes['mode']`, 키 없으면 `legacy` |
| `scope` | engine별 bounded attrs 집합: PG `{dbs, users}` · MySQL `{dbs}` · Oracle `{schemas, modules}`. 창 내 distinct 배열. 키 없으면 필드 생략 |
| `latency_ms_p50` `_max` `_max_at` `_last` | 결정 1의 2단 통계 |
| `latency_unit_source` | `"ms"` 또는 `"us"`(정정 1) |
| `execs_total` `execs_per_poll_p50` | 결정 1. `mode=legacy`면 `execs_total` 생략 |
| `total_ms` | PG `totalExecTime`(ms)·Oracle/Tibero `totalElapsedTime`(μs→ms) 폴별 합. 그 키가 없는 engine(MySQL/MariaDB)은 `latency_ms_poll × execs_poll` 합으로 파생하고 `total_ms_source` ∈ `{source_key, derived}`로 밝힌다. `mode=legacy`면 생략 |
| `polls_seen` `polls_in_window` `presence` `partial_presence` | 결정 3 |
| `first_seen` `last_seen` | 폴 시각(실행 시각 아님) |
| `baseline_latency_ms_p50` `baseline_latency_ms_max` `baseline_polls` `baseline_presence` | 결정 2의 기준선 창 통계 |
| `ratio_p50` `ratio_max` `verdict` `slower_basis` | 결정 2 |
| `rank_among_collected` | 결정 3 — 정렬축(결정 9) 기준 순위 |
| `sql_text` `sql_text_source` `sql_text_truncated` `sql_text_full_length` `sql_text_kind` | 결정 5 |
| `self_monitoring` `self_monitoring_pattern` | 결정 7 |
| `plan_records` | 결정 6 |
| `io` | engine별 계약(폴별 값의 중앙값): PG `{shared_hit, shared_read, temp_written, wal_bytes}` · MySQL `{rows_examined, rows_sent, lock_ms, disk_temp_tables}` · Oracle `{logical_reads, physical_reads, cpu_ms}`. body에 키가 없으면 **그 키를 생략**(0으로 채우지 않는다) |
| `refs` | `ch:dpm_topsql_local:<target>:sql:<sql_key>` + 본문/플랜을 실었으면 `pg:db_sql_text:…` · `pg:db_sql_plan:…` |

정정 9의 실물 사건을 창 `[2026-07-27T09:00Z, 10:00Z)`로 통과시킨 결과.
**아래는 손으로 쓴 예시가 아니라 구현된 도구의 실제 출력이다**(초판은 CH
`median()`으로 잰 32.52를 실었는데, 구현은 nearest-rank라 38.58이다 —
14.15-2가 그 차이의 기록이다):

```json
{
  "sql_key": "ggrfcy439k2xd",
  "id_kind": "oracle_sql_id",
  "engine": "oracle",
  "mode": "delta",
  "scope": {"schemas": ["BANKING"], "modules": ["JarLauncher"]},
  "latency_ms_p50": 38.58,
  "latency_ms_max": 17342.694,
  "latency_ms_max_at": "2026-07-27T09:47:09Z",
  "latency_ms_last": 321.834,
  "latency_unit_source": "us",
  "execs_per_poll_p50": 38,
  "execs_total": 1540,
  "total_ms": 301834.06,
  "total_ms_source": "source_key",
  "polls_seen": 22,
  "polls_in_window": 59,
  "presence": 0.373,
  "partial_presence": true,
  "first_seen": "2026-07-27T09:02:19Z",
  "last_seen": "2026-07-27T09:49:11Z",
  "baseline_latency_ms_p50": 0.12,
  "baseline_latency_ms_max": 46540.395,
  "baseline_polls": 1024,
  "baseline_presence": 0.725,
  "ratio_p50": 320.774,
  "ratio_max": 0.373,
  "verdict": "slower",
  "slower_basis": "median",
  "rank_among_collected": 1,
  "sql_text": "select a1_0.id,a1_0.balance,a1_0.holder,a1_0.status from accounts a1_0 where a1_0.id=:1 for update",
  "sql_text_source": "dictionary",
  "sql_text_kind": "dict_captured",
  "sql_text_full_length": 98,
  "self_monitoring": false,
  "plan_records": {"count": 1, "distinct_plan_hash": 1,
                   "hash_stability": "unknown_single_capture",
                   "latest_captured_at": "2026-07-13T08:56:24Z"},
  "io": {"logical_reads": 5.567, "physical_reads": 0, "cpu_ms": 0.199},
  "refs": ["ch:dpm_topsql_local:00149c2f-…:sql:ggrfcy439k2xd",
           "pg:db_sql_text:00149c2f-…:ggrfcy439k2xd",
           "pg:db_sql_plan:00149c2f-…:ggrfcy439k2xd"]
}
```

이 한 행이 조사자에게 주는 것: 중앙값이 271배 느려졌고(사건), 최댓값이 17초이며,
`for update`라 락 경합이 의심되고(→ `db_blocking` 호출), **presence 0.373이 "멈춰서
관측이 사라진 것"**(정정 9)이고, 플랜 캡처는 07-13 한 건뿐이라 **플랜 원인을 배제할
근거가 없다**(`unknown_single_capture` — 이 예시를 실측으로 채우다가 발견한
것이 14.7의 등급 결함이다).

`io.physical_reads = 0`과 `logical_reads = 5.6`이 함께 말하는 것도 재료다 — 이
쿼리는 I/O를 하지 않고 17초를 기다렸다(락 대기의 서명).

### 14.6 결정 5 — 본문은 싣는다. 목록은 800자 절단, 전문은 지정 드릴다운

**근거:** `sql_key`만으로는 조사자가 아무것도 못 한다(digest/queryid는 사람이 읽을
수 없다). 길이 실측이 절단을 요구한다 — 평균 176~605자인데 최대 23,851자다.
Codex는 "안전한 절단 길이의 근거가 자료에 없다"며 무절단을 주장했으나, 23KB 본문
하나가 봉투를 삼키는 것이 더 큰 해악이고 절단 사실을 필드로 밝히면 감추는 것이
아니다(2/3 수렴).

**계약:**
- 획득 순서: ① `body.sqlText`가 비어 있지 않으면 그것(`sql_text_source="ch_body"`)
  ② 비면 `db_sql_text`를 `(resource_id, sql_key)`로 조회(`"dictionary"`)
  ③ 둘 다 없으면 `sql_text` 생략 + `sql_text_source="none"` + `limits`에 결손 건수.
  **engine으로 가정하지 않고 값으로 판단한다**(정정 3은 관측이고 계약이 아니다).
- 목록 절단 800자 + `sql_text_truncated` + `sql_text_full_length`. 드릴다운
  `full_text_for: [sql_key]`(최대 3개)는 무절단(단 20,000자 안전 상한).
- `sql_text_kind`: PG `pgss_normalized_masked`(`$1` + credential 마스킹) ·
  MySQL `digest_normalized`(`?`) · Oracle `dict_captured`(**정규화 여부는 자료에
  없다 — 주장하지 않는다**). `limits`에 "어느 파라미터 값에서 느렸는가는 이 도구로
  답할 수 없다".
- `db_sql_text`는 write-once — 최초 관측본이며 갱신되지 않는다(`limits` 한 줄).

### 14.7 결정 6 — 플랜은 존재·안정성 요약 + 드릴다운. 플랜 변경은 판정하지 않는다

**근거:** 플랜은 "왜 느린가"의 직계 증거이고 현행 도구가 `db_sql_plan`을 아예 쓰지
않는 것은 결함이다. 그러나 실측 플랜 하나가 82,881자이고, MySQL은 창 안에 같은
SQL의 플랜이 수천 건일 수 있다(70,979/28 ≈ 2,535). 그래서 목록에는 요약만 두고
본체는 드릴다운으로 준다(`sample_logs`의 map + 드릴다운 선례와 같은 형태).

**자체안의 `plan_change_observed`는 기각했다.** 독립 Claude·Codex가 둘 다 플랜 변경
판정을 거부했고(2/3), 결정적으로 **정정 9의 실물 사건에서 플랜 해시가 전 구간
동일**했다 — 자체안이 기대한 신호가 실제 사건에서 작동하지 않았다. 7일간 sql_id
86개 중 플랜 다수는 2개뿐이라는 실측도 근거 부족을 확인한다. CH body의
`planHashValue`를 창 내 관측으로 싣는 것은 **발동 조건부 후보**로만 남긴다.

**계약:**
- 목록 행 `plan_records`(`resource_id`+`sql_id`로 집계): `count` ·
  `distinct_plan_hash` · `latest_captured_at` · `hash_stability`
  (`count < 5`면 **`unknown_single_capture`** — 등급을 매기지 않는다. 그 이상에서
  `distinct/count >= 0.9` → `unstable` · `<= 0.2` → `stable` · 사이 `mixed`).
  MySQL은 실측 1.0이라 항상 `unstable`, Oracle은 조회로 얻는다.
  **`count < 5` 가드는 실측이 요구했다** — 14.5의 JSON 예시를 실측으로 채우다가
  Oracle `ggrfcy439k2xd`의 플랜 캡처가 1건뿐인 것을 발견했고, 독립 Claude의 원식은
  `1/1 = 1.0`이라 **이 SQL을 `unstable`로 오분류**한다. 캡처 1건은 안정성에 대해
  아무 말도 하지 않는다.
- engine이 `postgresql`이면 `plan_records`를 **생략**하고 `limits`에 "PG는 플랜
  수집기가 없다(`db_sql_plan` 0행) — 플랜 부재는 플랜이 없다는 뜻이 아니다".
- 드릴다운 `plan_for: [sql_key]`(최대 2개): Oracle/Tibero는 `plan_hash_value`별
  최신 1건(최대 3해시), MySQL/MariaDB는 `latency_ms_max_at`에 **시간상 가장 가까운**
  캡처 1건 + `plan_selection="nearest_to_peak_poll"`(해시가 캡처마다 다르므로
  "이 해시가 그 쿼리의 플랜"이라고 말하지 않는다). 노드는
  `operation·options·depth·parentId·cost·estRows·actRows`만 남기고 술어는 200자 절단.
- 플랜 전용 신설 도구는 만들지 않는다 — 진입 키(`sql_key`)를 얻는 유일한 길이 이
  도구이므로 같은 호출의 파라미터가 왕복을 줄인다.

### 14.8 결정 7 — 컬렉터 자기 쿼리는 표식하고 남긴다. 순위에는 남고 `status`는 올리지 못한다

**근거:** 정정 6이 MySQL 32%다. 배제하면 "모니터링이 DB를 때린다"는 진짜 사건을
잃고, 방치하면 상위가 오염된다. 자체안·Codex는 "판정에도 참여"였는데,
**배경 시험에서 컬렉터 자기 쿼리 `cdd92d38…`이 07-28 06:30에 3.34배로 발화**했다 —
그대로 두면 이 도구가 인시던트마다 모니터링 쿼리를 이상으로 올린다. 독립 Claude의
"판정 근거에서만 제외"가 실측으로 뒷받침된다.

**계약:**
- 패턴 6개 고정 + 응답에 그대로 노출(`coverage.self_monitoring_patterns`):
  `innodb_lock_waits` · `x$processlist` · `events_statements_summary` ·
  `pg_stat_statements` · `pg_locks` · `pg_blocking_pids`. 소문자 부분일치.
- `self_monitoring=true` 행은 ① findings에 남고 순위에도 참여 ② `verdict`는 그대로
  계산해 싣는다(모니터링 부하 자체가 진단거리일 수 있다) ③ **봉투 `status`를
  `anomalous`로 올리지 못한다** ④ `summary`에서 애플리케이션 쿼리로 서술되지 않는다.
- `self_monitoring_pattern`에 맞은 패턴 문자열을 싣는다 — **근거 없는 불리언 금지**
  (조사자가 오탐을 되짚을 수 있어야 한다).
- 본문이 없는 행은 판별 불가 → `self_monitoring` 필드를 **생략**한다(false로
  단정하지 않는다).
- `exclude_self_monitoring`(기본 `false`) 파라미터로 명시적 배제만 허용하고, 배제한
  건수를 `coverage`에 밝힌다.

### 14.9 결정 8 — status 3값, 빈손은 네 갈래. engine은 PG 레지스트리에서 얻는다

**근거:** Codex는 "0행이면 engine을 알 수 없으니 `engine`을 필수 파라미터로"라고
했다. 정정 7이 그것을 푼다 — `collector_dpm_engine_version`에 대상별 engine이
있으므로 **조사자에게 묻지 않고 코드가 알아낸다**(패널 셋 중 아무도 몰랐던 원천).

**계약:**

| 상황 | status | reason |
|---|---|---|
| 창 내 행 ≥1, `slower`(비-자기관측) ≥1 | `anomalous` | — |
| 창 내 행 ≥1, 그 외 | `normal` | — |
| 창 내 행 0, 같은 대상 창 밖 행 있음(`LIMIT 1` 확인) | `no_data` | `collector_gap` |
| 창 내·밖 모두 0, 레지스트리 engine이 top-SQL 수집기 있는 종류 | `no_data` | `zero_observations` |
| 레지스트리 engine이 수집기 없는 종류(cubrid·mongodb·clickhouse) | `no_data` | `engine_unsupported` |
| 레지스트리에도 없음 | `no_data` | `target_engine_unknown` |

- `normal`의 `summary`는 "느린 쿼리 없음"이라고 쓰지 않는다 — "수집된 top-SQL
  집합에서 기준선 대비 회귀가 없음"이다(만성 느림은 `chronic` 행으로 보인다).
- 모르는 engine(정정: 적재 게이트는 열린 집합)은 `engine_unsupported`에 넣지 않고
  공통 경로로 처리하고 `coverage`에 "미검증 engine"을 표시한다.
- MSSQL은 식별자가 CH의 어디에 어떤 형식으로 들어가는지 **미확인**이다(Codex가
  이 틈을 지적했다). `id_kind`를 단정하지 않고 `limits`에 싣는다.

### 14.10 결정 9 — 파라미터 6개. 기준선·단위·모드는 받지 않는다

| 파라미터 | 기본값 | 근거 |
|---|---|---|
| `db`(target_id UUID) | 필수 | — |
| `from` / `to`(UTC RFC3339) | 필수 | — |
| `top_n` | **10** | 창당 distinct SQL이 pg 23~42·oracle 14~23. 5는 절반 이상을 자른다. 절단 시 `truncated` |
| `sort` | **`total_ms`** | 3안 중 2안(Claude·Codex)이 총 시간 기여를 기본 순위축으로 냈다 — 부하가 어디 몰렸나가 다음 도구 선택의 근거다. 판정은 정렬과 분리된다(`verdict`). 허용값 `total_ms`\|`ratio_p50`\|`latency_p50`\|`latency_max`\|`execs`. **잘못된 값은 오류**(조용한 기본값 대체 금지) |
| `full_text_for` / `plan_for` | 없음 | 결정 5·6 드릴다운(각 3·2개 상한) |
| `exclude_self_monitoring` | `false` | 결정 7 |

- **기준선 창·단위·mode는 파라미터가 아니다**(결정 2·B·1).
- 창 내 폴이 3개 미만이면 거절하지 않고 **판정 없이 관측만** 준다 + 그 사실을
  `assessment_basis`에 쓴다(거절은 도구 미호출로 이어진다).
- `min_calls` 같은 필터는 두지 않는다 — `calls=1` 행은 노이즈일 수도 있고 "한 번
  돌고 46초 걸린 쿼리"일 수도 있다(정정 9가 후자다). 감추지 않고 `execs_*`를 실어
  조사자가 판단한다.

### 14.11 일부러 안 만든 것

- 튜닝 권고(인덱스·힌트·rewrite) — 도구는 관측만.
- 플랜 변경 판정(14.7 — 실물 사건이 근거를 기각).
- 절단축 역추정 · top-5 밖 SQL 추정 · 결손 폴 보간.
- 절대 ms 임계(14.3 — 발동 조건부 후보).
- engine 간 latency 순위 비교(단위는 정규화하지만 워크로드가 다르다).
- 락·블로킹 짝(=`db_blocking`) · 커넥션 포화(=`read_timeseries` + 한도 부착).
- 바인드 값 복원 — 원천에 없다.
- legacy를 도구가 차분해 delta로 만들기(14.2).
- 플랜 전용 신설 도구(14.7).

### 14.12 한계 (응답에 싣는다)

1. 모집단은 폴별 다축 top-5 합집합이다 — 창의 모든 SQL이 아니다.
2. 어느 축이 각 행을 뽑았는지 원천에 없다.
3. `presence < 1`은 미실행일 수도, 축에서 밀린 것일 수도, **`Δ실행수<=0`으로 버려진
   것일 수도** 있다(정정 9 — 지연 사건 중에 presence가 떨어진다).
4. `mode=legacy` 행은 생애 평균이라 창 내 변화·총량을 산출할 수 없다
   (`verdict="not_assessable_legacy"`).
5. `first_seen`/`last_seen`은 폴 시각이며 쿼리 실행 시각이 아니다.
6. 본문은 정규화·마스킹된 최초 관측본이다 — 어느 파라미터 값에서 느렸는지는
   답할 수 없다. Oracle 사전 텍스트의 정규화 여부는 미확인.
7. PG는 플랜 원천이 0행이고 MySQL 플랜 해시는 캡처마다 달라 "같은 플랜"을 식별할
   수 없다.
8. 자기관측 판별은 본문 패턴 부분일치다 — 앱이 시스템 뷰를 조회하면 오탐한다
   (`self_monitoring_pattern`으로 확인 가능).
9. 3배 문턱과 두 팔의 배경 오발률은 라이브 119 3 engine·4일에서 얻었다 — 다른
   워크로드에서 적정성은 미검증.
10. 절대 하한이 없어 서브밀리초 쿼리의 배수 발화가 가능하다(실측: 최대 팔 발화
    7건 중 2건이 0.1→0.3ms짜리다). 크기는 `latency_ms_*`로 확인해야 한다.
10a. **최대 팔의 기준선은 24시간 중 단 한 번의 급등으로 정해진다** — 사건이 길면
    자기 사건의 앞 시간이 기준선 최대를 채워 이후 창에서 최대 팔이 침묵한다
    (실측: 정정 9의 09:00 창 `ratio_max = 0.373`). 중앙값 팔이 그 경우를 받는다.
11. 기준선은 같은 절단 원천으로 계산된다 — 기준선 자체가 top-N 절단이다.
12. 회귀는 인시던트 원인의 증명이 아니다.
13. MSSQL 식별자 배치와 Tibero/MariaDB 본문 경로는 미확인(계약만 있고 실측 없음).

### 14.13 자리 교체 (§7.1)

db_slow_queries가 들어오면 **`get_slow_queries`가 빠진다.** 표면 산술: 신 9 +
구 7 = 16 → **신 10 + 구 6 = 16종**.

| 위치 | 작업 |
|---|---|
| `tools/db.go` | **파일 통째 제거**(구현 실측 — 14.15-1). 설계는 `bodyOf`·`num`·`dbParse`를 계승한다고 봤으나 구현이 body 추출을 CH `JSONExtract`로 옮겨 Go 헬퍼가 전부 무주인이 됐다. `dpmSnapshot`(마지막 스냅샷 10초 = §5.4의 결함)도 함께 사라진다 |
| `tools/registry.go` | 배선 교체 + 머리 주석 카운트 |
| `tools/db_smoke_test.go` | `NewSlowQueriesTool` 참조 — 재작성 필수(컴파일 실패) |
| `pipeline/obligations.go` | `"database"` 의무 목록의 이름 교체 — **문서가 아니라 의무 계산기** |
| `pipeline/obligations_test.go` · `pipeline/generate_test.go` | 목 갱신 |
| `llm/mockworld_test.go` · `llm/investigator_smoke_test.go` | 목 갱신 |
| `docs/ref-tool-data-access.md` · `docs/spec-agent-tools.md` | 표면 표 갱신 |
| `docs/frontier-trajectory-*.md` | **건드리지 않는다**(역사 기록) |

새로 필요한 배선: PG `collector_dpm_engine_version` 조회(결정 8) ·
`db_sql_plan` 조회(결정 6) — 둘 다 기존 PG 핸들 재사용.

### 14.14 패널 기록

**방식(§11.0 상속).** 브리프 하나(원천 계약·실측 8건·확정 결정 2개·열린 질문
10개)를 자체·독립 Claude·Codex가 blind로 독립 설계.

**수렴(3/3).** 순위만으로는 부족하다 · 절대 임계 금지(engine별 3자리 차이) ·
컬렉터 자기 쿼리를 배제하지 않고 표식 · 절단축 역추정 금지 · 결손 보간 금지 ·
플랜 전문을 목록에 넣지 않는다 · 본문은 싣는다 · 인과 판정 금지.

**수렴(2/3).** 폴 내 실행횟수 가중 선접기(Claude·Codex — 자체안은 플래그만) ·
기본 정렬은 총 시간 기여(Claude·Codex — 자체안은 변화율) · 본문 절단
(자체안·Claude — Codex는 무절단).

**자체안이 기각된 지점 2건.** ① 폴 내 충돌을 접지 않고 플래그로만 노출(위) ②
`plan_change_observed`(14.7 — 다른 두 안이 거부했고 실물 사건에서 플랜 해시가
불변이었다).

**독립 Claude 단독 기여 3건.** ① `chronic` 판정값 — 만성을 이상으로 올리지 않고
이름을 붙여 노출 ② 자기관측 행이 `status`를 올리지 못하게 하는 규율(배경 시험에서
컬렉터 쿼리가 3.34배로 발화하는 것을 실측해 뒷받침됨) ③ 플랜 요약 + 드릴다운의
2단 형태.

**Codex 단독 기여 3건.** ① `mode`를 접기 키에 넣기(생애 평균/구간 평균 혼합 차단)
② MSSQL 식별자 배치 미확인을 인정하고 단정하지 않기 ③ "빈 창의 원인을 단정할 수
없다"(수집기 heartbeat가 원천에 없다) — 결정 8의 `collector_gap` 판별을 창 밖 행
존재로만 한정하게 만들었다.

**실측이 독립 Claude의 두 형식도 고쳤다.** ① 최대를 `regressed`의 **AND 확인
조건**으로 둔 것 — 정정 9의 09:00 창에서 `ratio_max = 0.373`(기준선 최대가 자기
사건에 오염)이라 AND면 판정이 사라진다 → OR 두 팔로 교체(14.3). ②
`hash_stability = distinct/count` — 실측 캡처가 1건인 SQL을 `unstable`로 오분류한다
→ `count < 5` 가드 신설(14.7). 둘 다 **14.5의 JSON 예시를 실측값으로 채우는
과정에서** 드러났다. 예시를 손으로 채우면 못 보는 것들이다.

**실측이 판정식을 갈랐다(이 절의 처음).** 자체안·Claude의 배수 판정과 Codex의
`p50 > baseline.p99` 판정을 같은 배경(4일)과 같은 실물 사건(정정 9)에 나란히
돌렸다. 배경 오발은 배수 0건 vs p99 6건, 기준선 게이트 통과율은 oracle 49%,
그리고 **6창 사건에서 p99 판정은 첫 창만 발화하고 침묵**했다(기준선이 자기 사건에
오염). 같은 시험이 자체안의 기준선 정의(직전 동길이 창)도 같은 이유로 고쳤다 —
직전 24시간으로 늘렸다.

**미검증으로 남기는 것.** legacy 모드 전 경로(라이브가 전부 delta) · `chronic`
문턱 0.9/2.0 · MSSQL·Tibero·MariaDB 전 계약 · `plan_for` MySQL 최근접 선택 ·
`engine_unsupported`·`target_engine_unknown` 분기(해당 대상 미확보) · 절대 하한
부재의 실해(서브밀리초 발화가 조사자를 오도하는지). 전부 14.12에 명시했다.

### 14.15 구현 정정 (2026-07-30, `tools/dbslowqueries.go`·`dbslowqueries_pg.go`)

구현이 설계를 네 군데 고쳤다.

**1. `tools/db.go`가 통째로 사라졌다(설계보다 큰 교체).** 14.13은 `bodyOf`·
`num`·`dbParse`를 신 도구가 계승한다고 적었으나, 구현은 body 추출을 Go가 아니라
**CH `JSONExtract*`로 옮겼다** — engine이 대상당 하나로 확정되므로(정정 7의
레지스트리) engine별 식을 SQL에 박는 것이 `multiIf` 나열이나 Go 파싱보다 정직하고
서버측 집계(기준선 24시간)까지 같은 식을 재사용한다. 그 결과 Go 헬퍼 3개가 전부
무주인이 되어 파일이 남을 이유가 없어졌다. `dpmSnapshot`(마지막 스냅샷 10초 —
§5.4의 결함 그 자체)도 함께 사라졌다.

**2. p50 규약을 nearest-rank로 통일했다(예시 수치가 바뀐 이유).** 창 통계는 Go가,
기준선은 CH가 계산한다. CH `quantileExact(0.5)`는 위치 기반이고 기존 Go 헬퍼
`median()`은 짝수에서 두 중앙값을 **보간**한다 — 섞으면 비(ratio)가 두 정의의
차이를 흡수한다. 그래서 `sqP50`을 따로 두고 짝수에서 위쪽 원소를 쓴다. 14.5의
예시가 32.52 → 38.58로 바뀐 것이 정확히 이 차이다(폴 22개).

**3. `pgTextArray`를 폐기했다.** 초안은 `text[]` 리터럴 생성기를 넣었는데,
pgx stdlib이 `[]string`을 그대로 받는 것을 라이브에서 확인했다(기존 `describe.go`
관례와 동일). 불필요한 부품을 지웠다.

**4. 목록 밖 키의 본문 드릴다운을 막지 않는다(드릴다운 실호출이 잡은 비대칭).**
설계는 "목록에 있는 키만 드릴다운"이었는데, 실호출에서 `plan_for`는 목록 밖 키를
받고(PG를 키로 직접 조회) `full_text_for`만 거절하는 것이 드러났다. 사전
(`db_sql_text`)도 키만으로 조회되므로 근거 없는 비대칭이다 — 사전을 시도하고,
**"창 내 관측이 아니라 사전에서만 가져왔다"를 note로 밝힌다**. 실측: mysql·pg
모두 목록 밖 키의 본문이 사전에서 나왔다(83자·238자).

**라이브 검증(119).** 설계문의 실물 사건 창 `[07-27 09:00, 10:00)`을 그대로
겨냥해 `status=anomalous` · `ggrfcy439k2xd` **320.8배**(median 팔) ·
`ratio_max=0.373`(기준선 최대가 자기 사건에 오염 — 두 팔이 OR여야 하는 이유가
실물에서 재현) · presence 0.373 · 본문은 사전 경로 · 플랜은
`unknown_single_capture`. 회귀 창 자동 발굴 스모크는 3 engine 전부 발화했다:
mysql 07-28 08:00(7.2배) · oracle 07-27 09:30(**2,671.8배**) · postgresql
07-27 22:30(4.4배 — 단 `0.049→0.217ms`로 **한계 10의 서브밀리초 발화가 실물에서
나왔다**).

최근 창(now-1h) 3 engine은 전부 `normal`이고 상위 행이 `chronic`으로 나왔다 —
만성이 이상으로 새지 않는다(§14.3 확인). `top_n`을 40으로 열면 자기관측 행 10개가
드러나고 그 중 `a5ed9d95…`가 **`verdict=slower`인데 봉투는 `normal`**이었다 —
§14.8의 "자기관측은 status를 올리지 못한다"가 실물에서 작동했다.

**단위 가드 18개**(2단 접기 3 · 판정 두 팔·chronic·faster·게이트 5 · legacy 강등 2 ·
파생 total · presence · 본문 절단·출처 3 · 자기관측 표식 · 정렬 강등 · 플랜 등급 ·
p50 규약 · 축 계약 · 창 해석) + 라이브 스모크 2개(최근 창 3 engine 필드 계약·단위
정규화 · 회귀 창 자동 발굴). 전체 `go test ./...` 통과.

**추가 미검증.** `full_text_for`의 "목록에서 잘린 body 본문" 분기(사전에 없는
engine에서만 도달) · `source_rows_capped_at`(창 2만 행 초과 미발생) ·
`dropped_other_engine_rows`(대상당 engine 1종). 드릴다운 두 경로는 실호출로
확인했다 — Oracle 플랜이 `INDEX UNIQUE SCAN` + `FOR UPDATE`를 돌려주어 정정 9
사건이 플랜 원인이 아님을 조사자가 직접 배제할 수 있었다.


## 15. 구현 설계 — breakdown_endpoints (2026-07-30 합의, blind 3자 패널)

§3.6 APM 메뉴의 유일한 도구이자 **12개 신 도구의 마지막**이다. §11·§13·§14와 같은
blind 3자 패널(브리프 하나를 자체·독립 Claude·Codex가 독립 설계)로 생성했다.
브리프와 3안 원문: `.omc/artifacts/panel/breakdown-endpoints-{brief,design-main,
design-claude,design-codex}-2026-07-30.md`.

이 절이 남기는 처음 둘:

1. **세 판정식을 같은 배경·같은 사건에 나란히 돌려 우열을 실측으로 가른 첫 사례.**
   §14.3은 두 안을 한 사건에 돌렸고, 여기서는 셋을 3일 전체 배경(8,870 버킷) +
   사건 3구간에 동시 대입해 **하나를 기각하고 둘을 합쳤다**(15.4).
2. **평가 라벨 자체가 실측으로 정정된 첫 사례.** 자체 라벨의 "배경"에서 발화한 8건
   중 6건이 라벨 밖 실사건이었다(p95 15,624ms 등) — 오탐률을 재는 자가 틀렸다(15.5).

### 15.1 원천 실측과 그 지위

라이브 119 CH `lucida.otel_traces_local` 단일 원천. **스키마·속성 보유율은 계약
등급이고 수치는 `observed_in_capture` 등급이다.**

| 원천 | 실측 | 지위 |
|---|---|---|
| 보존 | 전체 9,525,890행, `2026-07-27 00:00` ~ `07-30 04:54` | **TTL 3일** — 스키마 `TTL … + toIntervalDay(3)`, 일 파티션 드롭 |
| 볼륨 | 하루 ~300만 span | 관측 |
| kind 분포(24h) | INTERNAL 1,137,041 · CLIENT 738,403 · SERVER 295,325 · PRODUCER 33,132 · CONSUMER 24,992 | 관측 |
| 축 카디널리티 | 서비스×kind당 span_name 최대 19개 | 관측 — 폭발하지 않는다 |
| 진입점 행 수 | 30분 창 서비스당 최대 9행(commerce-gateway), 대부분 3~6 | 관측 — 절단이 거의 없다 |
| 저장 중복 | 최근 창 100,979행 = `uniqExact((trace_id,span_id))` 100,979 | 관측 — **0이지만 MergeTree라 보장 아님** |
| 대상 UUID | `resource_attributes['lucida.target_id']` 209,252/210,080 | 관측 — 서비스와 **1:1**(빈 값은 전부 `ingest-selfsystem`) |
| `k8s_pod_name`·`k8s_node_name` | **전 span 빈 값**(395,545/395,545) | 관측(독립 Claude) — 파드 식별은 `host_name` 하나뿐 |
| `links_span_id` | 전 kind 0건 | §12 실측 상속 — span link 미사용 |

**정정 1 — SERVER 한정이 4개 서비스를 통째로 지운다(§3.6 주장의 실측 확인).**
메시지 구동 서비스의 SERVER span **전수**가 `GET /actuator/health`다:
food-delivery-notify(1,034) · commerce-notification(1,051) · core-banking-ledger(1,018) ·
commerce-shipping(1,058). 실제 처리는 CONSUMER(각 13,928 · 4,476 · 2,065 · 2,204)와
INTERNAL(commerce-shipping 77,812)에 있다. **현행 `get_slow_endpoints`는 이 넷을
헬스체크 한 줄로 보고한다.**

**정정 2 — 루트의 12%가 INTERNAL 스케줄러다.** 24h 루트 분포: SERVER 148,571 ·
**INTERNAL 38,836** · CLIENT 11 · CONSUMER/PRODUCER 0. INTERNAL 루트의 정체는
서비스마다 `OutcomeTrackingRunnable.run`(scope `io.opentelemetry.spring-scheduling-3.1`,
서비스당 ~3,200/일) + `ingest.traces.batch` + `collectordb.poll`. **SERVER 축에
존재하지 않는 독립 처리 흐름**이다.

**정정 3 — CLIENT span_name은 축이 아니다.** CLIENT ERROR 상위가
`commerce-gateway / GET / 32,439`, `commerce-product / GET / 19,639` — 이름이 HTTP
메서드뿐이고 목적지가 없다. 목적지는 속성에 있고 종류마다 키가 갈린다(3h 187,493건):
`server.address` 100% · `db.system`·`db.statement` 147,749 · `db.name`·`db.user` 144,228 ·
`db.operation` 135,719 · `url.full`·`http.request.method` 39,744 · `error.type` 13,595.
즉 CLIENT는 **DB 호출 ~79% + HTTP 호출 ~21%**다.

**정정 4 — ★ 에러 신호 세 원천이 서로 다른 것을 센다.** 24h:

| 신호 | 값 | 정체 |
|---|---|---|
| SERVER `status_code='ERROR'` | **14** | 5xx만. OTel 규약상 서버측 4xx는 ERROR가 아니다 |
| SERVER `http.response.status_code` | 200 207,694 · **404 84,386** · 401 3,016 · 409 484 · 400 193 · 500 13 · 502 1 | 속성 보유율 **100%**(75,710/75,710) |
| CLIENT `status_code='ERROR'` | 52,440 | **HTTP 클라이언트 계측은 4xx도 ERROR로 표시**(서버측과 반대). commerce-product http 86.5% 만성. **db CLIENT는 0%** |
| INTERNAL `status_code='ERROR'` | 25,985 | 정체는 `jakarta.persistence.NoResultException`. 행 단위 **85.9%**(commerce-inventory `SELECT …Inventory` 3,817/4,443) — **정상 업무 흐름** |
| `events_name='exception'` | INTERNAL 26,004 · CLIENT 14 · SERVER 12 | INTERNAL ERROR와 짝. 카운터로 쓰면 안 되고 라벨로만 |

→ **현행 `get_slow_endpoints`의 `countIf(status_code='ERROR')`는 404 84,386건을
0으로 센다.** 그리고 `status_code='ERROR'`를 SERVER 밖에서 에러로 세면 만성 정상
흐름을 이상으로 부른다.

**정정 5 — 4xx는 만성이고 트래픽에 비례한다.** commerce-gateway 시간당 404 비율이
24시간 내내 **40~44%로 일정**(233/563 ~ 2,886/7,170). 인접 30분 창 사이 4xx 비율 차의
분포(SERVER 6,020창): 중앙값 0 · q95 4.19pp · q99 10.23pp · >10pp 63건(1.0%).
→ **"에러 존재 = 이상"이 아니라 "비율 변화 ≥ 10pp"**여야 한다.

**정정 6 — 5xx는 에피소드성이다(브리프 §3.2를 3일로 넓혀 정정).** 24h 표는
`500=13, 502=1`이지만 3일 전체로는 수천 건이고, core-banking-api SERVER 5xx 비율이
07-27 04~10시 **74~83%**, 07-28 04시 82.8%로 치솟았다가 그 밖에는 **정확히 0%**다.
30분 창 6,025개 중 `failed_rate ≥ 5%`인 창이 278개(4.6%), `0 < rate < 5%`가 75개(1.2%).

**정정 7 — 계측 중첩은 형제가 아니라 부모-자식 사슬이다.** commerce-product 부모-자식
(kind, scope) 쌍: `INTERNAL spring-data → INTERNAL hibernate` 14,421 ·
`INTERNAL hibernate → CLIENT jdbc` 13,979 · `SERVER tomcat → INTERNAL spring-data` 7,069 ·
`SERVER tomcat → CLIENT http` 2,191. → §3.6이 "2중 계상"이라 부른 것의 정확한 모양은
사슬이고, **직계 자식만 빼면 중복 차감이 구조적으로 불가능하다**(15.6).

**정정 8 — 야간 트래픽이 거의 0으로 떨어진다.** 07-29 시간당 전체 span: 00시 125,639 →
02시 **1,086** → 09시 **949** → 11시 165,840. → "창에 데이터가 있는가"와 "판정할 만큼
있는가"는 다른 문제다. `no_data`와 별개로 **행 단위 표본 부족 표식**이 필요하다.

### 15.2 결정 1 — 행의 단위는 "진입점", 축은 kind마다 다르되 명시한다

**행 = (section, kind, label) 하나를 창 전역에 걸쳐 접은 것.** 3안이 독립적으로
같은 결론에 도달했다(옴니버스 금지 하에서 CLIENT를 어떻게 다루느냐만 갈렸다).

| section | kind | axis | label |
|---|---|---|---|
| `entry` | SERVER | `http.route` | `span_name`(템플릿 라우트) |
| `entry` | CONSUMER | `messaging.destination` | `span_name`(`<토픽> process`) |
| `entry` | INTERNAL **이면서 `parent_span_id=''`** | `scheduled_task` | `<scope 축약> :: <span_name>` |
| `step` | INTERNAL(비루트) | `framework_segment` | `<scope 축약> :: <span_name>` |
| `egress` | CLIENT(`db.system` 있음) | `db_call` | `db <db.system> <db.operation> <db.sql.table>` |
| `egress` | CLIENT(`url.full` 있음) | `http_call` | `http <method> <server.address>:<server.port>` |
| `egress` | CLIENT(그 외) | `peer` | `peer <server.address>` |
| `egress` | PRODUCER | `messaging.destination` | `span_name`(`<토픽> publish`) |

scope 축약 = `replaceOne(scope_name, 'io.opentelemetry.', '')`.

**세 구획으로 나누는 이유는 시간의 소유자가 다르기 때문이다.** `entry`는 "이 서비스가
한 일 전체", `egress`는 "남을 기다린 시간", `step`은 그 사이 프레임워크 구간. 하나의
리스트에 정렬해 섞으면 정정 7의 사슬에서 **부모와 자식이 나란히 경쟁**한다.

**`entry`의 정의는 "트레이스 루트"가 아니라 "이 서비스에서 일이 시작되는 지점"이다.**
CONSUMER는 100% 부모를 가지지만(3h 6,806/6,806, 부모 조인도 100% 성공) 프로세스
관점에서는 진입점이다. 이 사실을 감추지 않기 위해 행에 `is_trace_root`를 싣는다.

**기각한 자체안**: 자체안은 CLIENT·비루트 INTERNAL을 **행에서 완전히 배제**하고
`kinds="all"` 인자로만 노출하려 했다. 다른 두 안이 독립적으로 "구획을 나누되 배제하지
않는다"에 도달했고, 그쪽이 옳다 — 배제하면 조사자가 "안이 뭐냐"를 물을 때 두 번째
호출이 강제되고, 미호출 실패 모드(§3.5의 소켓 접기와 같은 논리)를 재생산한다.
**축이 이질적인 것은 숨길 문제가 아니라 `axis` 필드로 드러낼 문제다.**

`sections` 인자로 부분 요청을 허용하되 **기본은 셋 전부**다.

### 15.3 결정 2 — 에러는 "실패"와 "거절"을 분리한다

정정 4·5·6이 요구하는 계약. 행마다:

```
failed_n / failed_rate        확정 실패
rejected_n / rejected_rate    요청 거절(4xx 계열). 해당 없으면 null
error_labels                  최대 2개. error.type 우선, 없으면 exception.type
error_semantics               http_server | http_client | db | framework | none
```

| kind / 분류 | `failed_n` | `rejected_n` | `error_semantics` |
|---|---|---|---|
| SERVER | http status ≥ 500 **OR** (http status **문자열이 비어 있고** `status_code='ERROR'`) | http 400~499 | `http_server` |
| CLIENT(http) | 같은 식 — fallback이 잡는 것은 응답을 못 받은 경우(연결 거부·타임아웃) | http 400~499 | `http_client` |
| CLIENT(db) | `status_code='ERROR'` | null | `db` |
| INTERNAL / CONSUMER / PRODUCER | `status_code='ERROR'` | null | `framework` |

**단위 가드 3건(구현이 반드시 지킬 것):**
1. `toUInt16OrZero`가 빈 속성을 0으로 만들어 `>=500` 비교에서 조용히 빠진다 →
   fallback 조건은 **"http status 문자열이 비어 있고"**로 명시한다. 숫자 0으로
   판단하지 않는다.
2. `exception` 이벤트는 **카운터로 쓰지 않는다** — 26,004건이 거의 전부
   NoResultException이다. `error_labels`의 설명 문자열로만.
3. `error_semantics='framework'`인 행은 **실패 규모 팔로 판정하지 않는다**(15.4) —
   근거가 정정 4의 85.9%다.

4xx와 5xx를 **같이 싣되 절대 합치지 않는다.** 합치면 commerce-gateway는 24시간 내내
"에러율 40%"다. 분리하면 `failed_rate=0%, rejected_rate=41%`가 되고 조사자가 둘을
따로 읽는다.

### 15.4 ★ 결정 3 — 판정식: 세 안을 배경·사건에 나란히 돌려 하나를 기각하고 둘을 합쳤다

세 안의 지연 판정식이 갈렸다.

| 안 | 식 |
|---|---|
| 자체 | `p50 ≥ 3 × 직전창 p50`, 절대 하한 없음 |
| 독립 Claude | `p95 ≥ 3 × 직전창 p95 AND p95 ≥ 300ms` (L1) **또는** `p95 ≥ 3000ms` (L2, 규모) |
| Codex | 48h 내 **호출량 근접** 12버킷의 중앙값 `B`·MAD `M`에 대해 `p95 > max(1.5B, B + max(4M, 50ms))` |

**시험 설계**: 보존 3일 전 진입점을 30분 버킷으로 접어(8,870+2,009 버킷) 세 식을
동시 대입. 사건 라벨 3구간(I1 07-27 03:30~11:00 core-banking · I2 07-28 02:00~06:00 ·
I3 07-28 23:00~24:00 food-delivery), 나머지는 배경.

| 안 | 배경 발화 | I1 | I2 | I3 |
|---|---|---|---|---|
| 자체(p50×3) | 11 | 9 | 5 | 6 |
| Claude(L1∨L2) | **8** | **75** | **30** | 4 |
| Codex(호출량 매칭 MAD) | **59** | 74 | 32 | 6 |

**Codex안 기각.** 배경 발화가 7배이고, 그 정체가 `commerce-order POST
/api/orders/checkout` 13회 · `commerce-gateway POST /api/orders/**` 12회처럼
**같은 대상을 반복 발화**하는 것이다. 호출량 매칭이 §4.2의 부하-지연 상관을 잡겠다는
의도는 옳았으나, MAD가 작은 안정 대상에서 문턱이 지나치게 낮아진다.

**자체안 단독 기각.** I1에서 9건(Claude 75건)으로 재현이 현저히 낮다. p50은 꼬리
사건에 둔감하다.

**합친다 — 그리고 두 팔의 사각지대가 서로 다르다는 것이 실측으로 드러났다.**

- **Claude L1의 `p95 ≥ 300ms` 하한이 저지연 서비스의 실사건을 죽인다.** I3에서 자체안이
  잡고 Claude안이 놓친 4건이 전부 food-delivery-restaurant SERVER다 — p50이 1.5→
  11.6~23.7ms로 2.5~2.8배 뛰었는데 p95가 71~95ms라 300ms 하한에 걸려 탈락했다.
  5개 엔드포인트가 같은 창에 동시에 튄 군집의 일부다.
- **자체안의 p50 팔은 스케줄러에서만 소음이다.** 배경 발화 11건이 **전부**
  `OutcomeTrackingRunnable.run`(INTERNAL 루트)이었다. kind별로 p50 인접창 변동을 재면
  이유가 보인다:

  | kind | 배경 쌍 | q95 | q99 | **3배 초과** |
  |---|---|---|---|---|
  | SERVER | 4,185 | 1.12 | 1.28 | **0건 (0%)** |
  | CONSUMER | 650 | 1.16 | 1.52 | **0건 (0%)** |
  | INTERNAL(루트) | 1,321 | 1.30 | 2.75 | 11건 (0.83%) |

  스케줄러는 주기 배치라 폴마다 작업량(큐 적재량)이 달라 중앙값이 배로 뛰는 것이 정상
  동작이다. 요청 단위인 SERVER·CONSUMER는 중앙값이 안정적이다.

**합의 판정식** — 행이 `anomalous`가 되는 조건(하나라도 참):

```
전제(모든 팔): 창 표본 uniq_n ≥ 30

L0  지연·배율   kind ∈ {SERVER, CONSUMER}                 ← INTERNAL 루트 제외
                AND 기준선 표본 ≥ 30
                AND p50 ≥ 3 × p50_base
L1  지연·변화   기준선 표본 ≥ 30
                AND p95 ≥ 3 × p95_base  AND  p95 ≥ 300ms
L2  지연·규모   p95 ≥ 3000ms                              ← 기준선 불요(오염 보험)
E1  실패·규모   error_semantics ≠ "framework"
                AND failed_n ≥ 5  AND  failed_rate ≥ 0.05
E2  실패·변화   failed_n ≥ 5 AND failed_rate ≥ 2 × base AND Δ ≥ 0.10
R1  거절·변화   rejected_n ≥ 20 AND Δ ≥ 0.10
                AND (rejected_rate ≥ 2 × base  OR  1-rejected_rate ≤ (1-base)/2)
```

**R1의 둘째 팔은 구현이 붙였다(15.12-2).** 상승비만 두면 기준선 거절율이
50%를 넘는 순간 "2배"가 원리적으로 불가능해져(102%) **팔이 죽는다** —
gateway의 만성 51% 404는 92%로 뛰어도 발화하지 못한다. 잔여 성공분이 절반으로
줄었는지를 OR로 같이 본다. 라이브 3일 대조에서 이 팔을 더해도 배경 발화는
4건 그대로다(사건 구간 재현도 동일).

**합친 결과(L0을 SERVER·CONSUMER에 한정했을 때):**

| 구간 | 발화 | 비고 |
|---|---|---|
| 배경 8,870 | **8** | 15.5에서 정체를 밝힌다 |
| I1 1,211 | **76** | Claude 단독 75 → +1 |
| I2 645 | **33** | 30 → +3 |
| I3 153 | **8** | 4 → **8 (2배)** |

L0을 kind로 제한한 것의 대가는 0이다 — 배경 p50 발화 11건이 전부 INTERNAL이었으므로
제한 후 **배경 기여가 정확히 0**이고, I3 재현은 2배가 된다.

**에러 팔 대입(각 안이 독립적으로 확인).** 정정 5의 만성 404는
`rate ≥ 2×base`를 통과하지 못한다 — 3일 5,147 인접 쌍에서 R1 발화 **1건**.
정정 6의 5xx는 E1의 5% 문턱이 만성 저율(75창)을 걸러 내고 에피소드 278창만 남긴다.
Codex가 같은 데이터로 독립 확인한 값: gateway `GET /api/products/**`의 최신 창
4xx율 51.813%가 배경 중앙값 51.456% 대비 문턱 64.32% 미만 → 불발.

**기준선은 직전 동일 길이 창**이다(자체·Claude 공통). Codex의 48h 호출량 매칭은
위에서 기각했다. 표본 부족 시 한 번만 창 길이를 4배로 넓혀 재시도하고, 그래도 30건
미만이면 **변화 팔(L0·L1·E2·R1)을 끄고 규모 팔(L2·E1)만** 적용하며
`baseline.state="unavailable"`을 싣는다.

### 15.5 ★ 15.4의 "배경 8건"은 대부분 배경이 아니었다 — 라벨이 틀렸다

합의 판정식의 배경 발화 8건 전수:

| 창 | 대상 | 이전 p95 → p95 | 팔 |
|---|---|---|---|
| 07-27 02:00 | core-banking-account `POST /api/accounts/transfer` | 152.8 → 610.2 | L1 |
| 07-27 02:00 | core-banking-api `POST /api/transfers` | 189.0 → 1,151.1 | L1 |
| 07-27 11:30 | core-banking-account `POST /api/accounts/transfer` | 111.1 → **15,624.3** | L1+L2 |
| 07-27 11:30 | core-banking-api `GET /api/transfers` | 1,608.6 → **10,608.5** | L1+L2 |
| 07-27 11:30 | core-banking-api `POST /api/transfers` | 114.6 → 2,095.1 | L1 |
| 07-28 00:30 | commerce-gateway `POST /api/orders/**` | 117.9 → 1,222.1 | L1 |
| 07-27 21:30 | food-delivery-dispatch `OutcomeTrackingRunnable.run` | 39.0 → 312.5 | L1 |
| 07-28 15:30 | food-delivery-dispatch `OutcomeTrackingRunnable.run` | 60.6 → 311.8 | L1 |

**앞 6건은 사건이다.** 자체 라벨이 I1을 03:30~11:00으로 잡았는데 실제 사건은 02:00에
시작해 11:30까지 이어졌다(p95 15.6초를 배경이라 부를 수 없다). 나머지 2건만이 진짜
소음성 발화이고, 그것도 스케줄러 p95다.

→ **판정식의 오탐률은 라벨의 정확도를 넘을 수 없다.** 이 절은 그 사실을 실측으로
드러낸 자리로 남긴다. 합의 판정식의 실 오탐은 8,870 버킷 중 **2건(0.02%)**이다.

교차 확인 하나 더: I1 구간은 `db_slow_queries` 설계 §14.3이 근거로 삼은 Oracle 행 락
사건(07-27 04:00~10:00, SQL `ggrfcy439k2xd`, `… for update`)과 같은 시각이다.
**같은 사건이 DB 축과 트레이스 축에서 독립적으로 관측된다.**

### 15.6 결정 4 — self-time은 넣는다 (Codex의 반대를 실측으로 기각)

Codex는 **v1에서 제외**를 주장했다 — "부모별 직접 자식 시간 구간의 **합집합**을 구하지
않고 단순 duration 합을 빼면 병렬 실행·중첩 때문에 부정확하다". 논거는 타당했으나
이 환경에서 실현되지 않는다.

**측정 1 — 자식 합이 부모를 넘는가.** 진입점 89,911건(3h): SERVER 76,621 · CONSUMER
6,992 · INTERNAL 루트 6,298. **초과 0건**(전 kind), 최대 비 1.00.

**측정 2 — 자식들끼리 겹치는가(Codex의 실제 논점).** 자식이 2개 이상인 부모
32,775건에 대해 `sum(자식 duration)` vs `max(end) - min(start)`(자식들의 시간 포괄폭):

| 부모 kind | n | **sum > 포괄폭** | 평균 비 | q99 | 최대 |
|---|---|---|---|---|---|
| SERVER | 27,432 | **0건** | 0.981 | 1.000 | 1.00 |
| INTERNAL | 4,059 | **0건** | 0.978 | 1.000 | 1.00 |
| CONSUMER | 1,284 | **0건** | 0.967 | 0.998 | 1.00 |

**자식들이 겹치지 않는다(순차 실행).** 합집합과 단순 합이 같으므로 Codex가 지적한
편향이 0이다. → self-time을 단순 합 차감으로 계산해도 된다. 단 이것은 **관측이지
보장이 아니므로** 계약에 안전망을 둔다.

**계산식(구현 고정):**

```sql
-- 1단계: 자식을 부모별로 먼저 접는다 (부모 중복 계상 함정)
kids AS (SELECT trace_id, parent_span_id, sum(duration_ns) child_ns, count() child_n
         FROM otel_traces_local
         WHERE service_name = {svc} AND timestamp >= {from} AND timestamp < {to} + INTERVAL 60 SECOND
           AND parent_span_id != ''
         GROUP BY trace_id, parent_span_id)
-- 2단계: 진입점에 LEFT JOIN. 직계 자식만 (ORM↔JDBC 중복 차감 회피)
self_ns = greatest(entry.duration_ns - coalesce(kids.child_ns, 0), 0)
```

- **부모 중복 계상**: `kids`를 먼저 접어 부모당 정확히 1행으로 만든다. 접지 않고 조인한
  뒤 `sum(parent.duration)`을 하면 부모가 자식 수만큼 부풀어 오른다(브리프 §5에서 실제로
  밟았다 — pairs 1,728건에 부모 합 1,391.7초 / 자식 합 146.7초).
- **ORM↔JDBC 중복 차감**: 조인이 `p.span_id = c.parent_span_id`, 즉 **직계 자식만**이다.
  정정 7의 사슬 `SERVER → spring-data → hibernate → jdbc`에서 SERVER가 빼는 것은
  spring-data 하나뿐이고 그 안의 hibernate·jdbc는 이미 그 duration에 한 번만 포함돼
  있다. **자손을 쓰지 않으면 중복 차감이 구조적으로 생기지 않는다** — kind 구분이
  필요 없다.
- **자식 조회 창을 `to + 60초`까지 넓힌다**: 창 끝 부모의 자식이 잘리는 것을 막는다.
  관측된 최대 진입점 duration은 27.7초. **이 60초는 가정이며 상한이 증명된 값이
  아니다** — `limits`에 적는다.
- `greatest(…, 0)` clamp가 발동하면 숨기지 않고 `self_clamped_n`으로 노출한다.

**행에 싣는 것**(`entry` 구획 한정 — `step`·`egress` 행은 그 자체가 이미 "부모의 시간이
어디로 갔나"의 답이라 그 위에 self를 얹으면 층이 겹친다):
`self_ms_p50` · `self_ms_p95` · `child_share`(자식 합/부모 합) · `child_span_n` ·
`self_clamped_n`.

**`child_span_n`이 필수인 이유**: food-delivery-notify `food.dispatch process`는
`child_share = 0.000`인데 **자식 span이 아예 없다**(계측 깊이 부족). 이 값을 함께 싣지
않으면 "전부 자기 처리"로 오독된다.

### 15.7 결정 5 — 구획 교차 비율을 만들지 않는다

기존 `get_trace_breakdown`의 `time_share_of_server = CLIENT 합 / SERVER 합`은 두 가지로
깨진다: ①CLIENT가 병렬이면 1을 넘고 ②스케줄러 트레이스(정정 2, 24h 38,836건)의 CLIENT는
SERVER 밖에 있어 분모에 대응물이 없다. 실측으로도 commerce-order 30분 창에서
`egress`+`step` 합이 `entry` 합을 넘는다(INTERNAL 루트 스케줄러와 그 자식이 SERVER
트레이스 밖이므로).

→ `time_share`는 **구획 내부 비율**만 존재한다(`row.total_ns / section.total_ns`).
`section_totals_ns` 셋은 싣되 **더하지 말라**를 `limits`에 넣는다. "자기 처리 비중"의
자리는 15.6의 `child_share`가 대신한다 — 그것은 부모-자식 관계로 정당하게 계산된다.

### 15.8 결정 6 — 정렬·절단·refs

**정렬**: 구획별로 독립 정렬. 키 = `(verdict_rank ASC, total_duration_ns DESC)`,
`verdict_rank`: anomalous 0 · normal 1 · insufficient_sample 2.

`total_ns` 정렬 근거: p95/max 정렬은 드물게 한 번 느린 것을 올린다
(`commerce-gateway DELETE /api/carts/**`는 p50 6.6ms·n 3,646인데 max 3,009ms).
기존 `errors DESC, p95 DESC`는 정정 4의 만성 노이즈 때문에 CLIENT/INTERNAL 행이
영구히 상위를 점거한다.

**절단**: 구획당 `top_n`(기본 20). **anomalous 행은 top_n 밖이어도 반드시 emit한다** —
판정을 내려 놓고 근거 행을 잘라내면 조사자가 인용할 수 없다. `truncated`는 실제로
잘린 행이 있을 때만 세우고 `truncated_sections: {section: 잘린 수}`를 동봉한다.

**refs** — 두 종류만, 둘 다 실측으로 뽑히는 식이 있다:
- `ch://lucida.otel_traces_local?service=…&kind=…&label=…&from=…&to=…` — 행을 재현하는
  질의 좌표
- `trace://<trace_id>#<span_id>` — `argMax(trace_id, duration_ns)` /
  `argMax(span_id, duration_ns)`로 뽑은 실재 span. 행마다 최대 2개(가장 느린 것,
  실패 예시 1개)

**`next_cursor`류 페이지네이션 필드는 선언하지 않는다** — 절단이 거의 없고(30분 창
서비스당 진입점 최대 9행) 구현 계획이 없으면 §10.7·§12에서 두 번 고친 그 병이다.

### 15.9 결정 7 — coverage / limits / no_data

**`coverage`**: `resolved_by`(service_name | lucida.target_id) · `service_name` ·
`target_id` · `hosts`(host_name distinct — 파드 식별은 이것뿐, 정정 1의 k8s_* 전 빈 값) ·
`window` · `baseline_window`+`state` · `spans_total` · `spans_unique`
(`uniqExact((trace_id,span_id))`) · `duplicate_ratio` · `spans_by_kind` ·
`rows_emitted`/`rows_total`.

**dedup 규율**: 별도 패스를 두지 않고 같은 GROUP BY 안에서 `uniqExact`를 세어
`duplicate_ratio`로 드러낸다(현재 관측 1.000이지만 MergeTree라 보장 아님).

**`limits`(조건부 — 해당하는 것만):**

| 조건 | 문장 |
|---|---|
| 항상 | 트레이스 보존은 3일(TTL)이다. 그보다 이전과의 비교는 불가능하다 |
| 항상 | `k8s_node_name`·`k8s_pod_name`은 전 span 빈 값이다. 인스턴스 구분은 `host_name`뿐 |
| SERVER 행이 `/actuator/*`뿐 | 이 서비스의 HTTP 진입점은 헬스체크뿐이다. 실제 처리는 CONSUMER/INTERNAL 구획을 보라 |
| `egress` CLIENT 행 존재 | CLIENT span_name은 HTTP 메서드뿐이라 목적지를 속성으로 복원했다 |
| `step` + `db` `egress` 공존 | ORM 구간과 JDBC 호출은 같은 DB 접근을 다른 층에서 본 것이다. 두 시간을 더하지 말 것 |
| `framework` 행 존재 | INTERNAL/CONSUMER의 ERROR는 대부분 정상 흐름이다(NoResultException). 변화로만 판정했다 |
| `rejected_n>0` | 4xx는 이 환경에서 만성이다. 존재가 아니라 변화로만 판정했다 |
| 라벨에 `**` | 게이트웨이 라우트는 와일드카드라 개별 경로가 접혀 있다 |
| PRODUCER 행 존재 | PRODUCER duration은 로컬 publish 소요다. 브로커·컨슈머 지연은 **이 원천에 없다** |
| 항상 | `section_totals_ns` 셋을 더하지 말 것 |
| self-time 계산 시 | 자식 스캔 창을 60초 넓혔다. 관측 최대 27.7초에 근거한 가정이며 상한이 증명된 값은 아니다 |
| `baseline.state ≠ ok` | 기준선 표본이 부족해 변화 팔을 끄고 규모 팔만 적용했다 |
| clamp 발동 | 자식 시간 합이 부모를 넘은 span N건의 self-time을 0으로 깎았다 |

**`no_data` 3분기** — 셋 다 다음 행동을 준다:
1. 대상 미해소 → `known_services`(창 내 서비스 전수, 실측 21개로 작다)
2. 해소됐으나 창에 span 0건 → `nearest_data`(창 앞뒤 가장 가까운 span 시각). 정정 8의
   야간 무트래픽이 이 분기의 주 원인이다
3. span은 있으나 모든 행이 표본 미달 → **`no_data`가 아니라 `normal`**, 행마다
   `insufficient_sample`. 데이터는 있고 없는 것은 판정 근거다 — `no_data`라 부르면
   거짓말이 된다

**Codex에서 살려 둔 문구**: `normal`은 "이 도구의 보수적 변화식으로 이상 상승을 확인하지
못함"이지 "서비스가 건강함"이 아니다. 모든 행의 기준선이 없으면 summary에 비교 불가를
명시한다.

### 15.10 결정 8 — 입력·비용

```
{ "target": "<service_name 또는 lucida.target_id>",   // 필수, service_name 먼저 시도
  "from": "<RFC3339 UTC>", "to": "<RFC3339 UTC>",     // 필수
  "sections": ["entry","step","egress"],               // 선택, 기본 셋 전부
  "top_n": 20 }                                        // 선택, 구획당. 상한 50
```

`target_id`가 서비스와 1:1이므로(15.1) 어느 쪽 이름을 쥐고 있어도 통한다. **파드·인스턴스
인자는 두지 않는다** — `k8s_pod_name`이 전 빈 값이고 레플리카 비교는 `compare_peers` 몫.

**CH 쿼리 3회**(정상 경로): ①창 집계(모든 행·지표·예시 span) ②기준선 집계 — **①과
완전히 동일한 SQL, 창만 다름**(정의 불일치로 인한 가짜 비율 방지) ③`entry` self-time.
`no_data` 분기에서만 작은 진단 쿼리가 추가된다.

실측 소요(commerce-order 30분 창): 창 집계 45ms · 기준선 45ms · self-time 27ms =
**~117ms**. `ORDER BY (service_name, timestamp, …)`이라 `service_name`+시간이 프라이머리
키 접두사로 쓰인다. 12시간 창도 집계 메모리는 그룹 수(≈30)에 비례해 상수다.

### 15.11 자리 교체와 이월

`get_trace_breakdown`·`get_slow_endpoints` **둘을 이 도구 하나가 승계한다**(§12
expand_topology에 이은 두 번째 2→1 승계). 기존 결함이 닫히는 자리:

| 기존 결함 | 닫는 결정 |
|---|---|
| `get_slow_endpoints` ① SERVER 한정 → 4개 서비스가 헬스체크뿐 | 15.2 `entry`에 CONSUMER·INTERNAL 루트 |
| ② `status_code='ERROR'`라 404 84,386건이 0 | 15.3 `rejected_n` 분리 + R1 |
| ③ 기준선 없어 만성/사건 구분 불가 | 15.4 직전 창 기준선, 배경 실 오탐 2/8,870 |
| `get_trace_breakdown` ① `net.peer.name` 부재(현행 속성은 `server.address`) | 15.2 속성 기반 라벨 |
| ② SERVER 대비 비율이 1 초과/음수 | 15.7 구획 교차 비율 폐지 |
| ③ INTERNAL 제외로 ORM 안 보임 | 15.2 `step` 구획 |

**닫는 길(구현 시 확인):**
1. `L2 = p95 ≥ 3000ms`의 3,000ms는 이 환경 3일치에서 나온 값이다(배경 최대 창 p95
   1,534ms의 약 2배). **다른 환경에 재측정 없이 옮길 근거가 없다** — 상수를 설정으로
   노출하고 `assessment_basis`에 **쓴 상수와 출처를 매번 적는다**.
2. 기준선 오염(사건이 `from` 이전 시작)은 L2가 보험이지만 완전히 막지 못한다. 기준선
   창의 p95가 그 행의 3일 중앙값보다 2배 이상이면 `limits`에
   `baseline_contamination_risk`를 세운다.
3. 15.6의 "자식이 겹치지 않는다"는 이 환경 관측이다. 비동기·병렬 실행이 도입되면
   `self_clamped_n`이 발동하기 시작한다 — 그때 합집합 계산(`end_timestamp` 보유)으로
   승격하는 것이 Codex안의 남은 가치다.
4. 15.5가 드러낸 것: **평가 라벨이 판정식보다 부정확할 수 있다.** 앞으로 판정식을
   배경에 돌릴 때 "배경 발화"를 오탐으로 바로 세지 말고 전수를 눈으로 확인한다.

### 15.12 구현이 설계를 고친 것 (2026-07-30 구현 완료)

구현·라이브 주행이 설계를 **세 곳** 고쳤다.

1. **기준선 행에 실패/거절 파생을 돌리지 않으면 R1이 만성 4xx에 발화한다.**
   설계는 "행에 파생을 적용한다"고만 썼고 구현이 창 행에만 적용했다 —
   `base.rejRate`가 0으로 남아 `rejected_rate ≥ 2 × base` 조건이 항상 참이 되고,
   gateway 만성 51% 404가 **4행에서 rejection_shift로 발화했다**(§15.4가 통과하지
   않아야 한다고 못 박은 바로 그것). 봉합은 호출 순서 교정이 아니라 **파생을 집계
   함수 안으로 옮기는 것**이다 — 호출자가 빠뜨릴 수 있는 자리에 두지 않는다.
2. **R1의 상승비 팔은 기준선이 50%를 넘으면 죽는다**(위 §15.4 개정). 단위 가드가
   잡았다 — 설계 검증이 "배경에서 발화하지 않는다"만 봤기 때문에, 발화할 수 없는
   팔과 잘 걸러내는 팔을 구분하지 못했다. **오탐 0은 그 자체로 팔이 살아 있다는
   증거가 아니다.**
3. **스모크의 전제가 실사건에 깨졌다.** "만성 4xx 서비스는 평시에 조용하다"로 쓴
   회귀 스모크가 첫 주행에서 실패했는데, 원인은 결함이 아니라 **그 순간 진행 중이던
   실사건**이었다(commerce-gateway 409가 10.2%→47.1%, 4개 서비스 동시). 회귀
   가드는 "대상이 조용하다"가 아니라 **"거절율이 안정적인 행은 발화하지 않는다"**는
   불변식으로 써야 결함만 잡는다.

**실물 주행 확인**(라이브 119):

| 창 | 결과 |
|---|---|
| 07-27 04:00~04:30 core-banking-account | `anomalous` — 기준선 창이 보존 밖이라 변화 팔이 전부 꺼졌는데 **L2·E1 규모 팔이 잡았다**(p95 15,619ms · 5xx 57/72). §15.4가 L2를 둔 이유의 실증 |
| 07-28 23:30~24:00 food-delivery-restaurant | `anomalous` — **L0가 4행**(p50 1.5~1.9 → 11.6~23.7ms). p95는 71~95ms라 L1의 300ms 하한에 걸린다. 두 팔을 합친 이유의 실증 |
| 07-30 03:00~03:30 commerce-gateway | `normal` — 만성 51% 404가 조용하다(수정 후) |
| 07-30 03:00~03:30 food-delivery-notify | `normal` — CONSUMER 3행이 보이고, "HTTP 진입점은 헬스체크뿐" 한계가 실렸다 |
| 없는 이름 | `no_data` + 창 내 유효 서비스 21개 |

**단위 가드 16개**(구획 배정·축 이름·실패/거절 분리·fallback 빈 문자열·L0 kind
한정·L1 하한 유지·L2 기준선 불요·framework 제외·만성 4xx 침묵과 급변 검출·표본
하한·self-time SQL 형태 3건·교차 비율 부재·판정 행 절단 면제·arms 널 금지·기준선
보존 밖·헬스체크 한계 노출·sections 기본값) + 라이브 스모크 2개(구획·refs·축·한계
계약 / 안정 거절율 불변식). 전체 `go test ./...` 통과.

**자리 교체 완료**: `tools/apm.go`(get_trace_breakdown·get_slow_endpoints)를
삭제했다. 살아남은 것은 창 내 서비스 목록 헬퍼 하나뿐이고 `beServiceList`로
옮겼다. 과도기 표면은 **신 11 + 구 4**.

**추가 미검증**: `self_clamped_n`(자식 겹침 실측 0건이라 발동 미관측) ·
`sections="step"` 단독 요청 · `top_n` 상한 50 · 기준선 `widened` 경로(야간
저트래픽 창에서만 도달) · CLIENT `peer` 축(속성이 없는 span을 창에서 만나지 못했다).

## 근거·관련 문서

- 재설계 결정: [adr-rca-agent-redesign.md](adr-rca-agent-redesign.md) (D5·D6·D7·D8·D9·D10)
- 검수 문제지: [backlog-tool-legibility.md](backlog-tool-legibility.md) (T1~T8,
  topology 지도의 간극 실증)
- Codex 교차검토 원본: `.omc/artifacts/ask/codex-aiops-sre-*-2026-07-23*.md`
- 기존 도구 계약(대체 예정 대상): [spec-agent-tools.md](spec-agent-tools.md)
