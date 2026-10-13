---
title: 도구·legibility 청구서 — 프론티어 2케이스 근거
status: Backlog (not a spec change)
owner: project
last_reviewed: 2026-07-23
tags:
  - rca
  - tools
  - backlog
summary: blind 프론티어 주행 2건(F04-R 카프카 consumer 정지, F01-R DB hot row 락)에서 반복 확인된 도구/legibility 요구를 능력(capability) 수준으로 정리한다. 현 18도구를 패치하든 재설계 도구면(D9)을 새로 짓든 살아남도록, 특정 함수가 아니라 "조사자가 할 수 있어야 하는 것"으로 적는다.
---

# 도구·legibility 청구서 — 프론티어 2케이스 근거

## 규율

- **(2026-07-23 역할 강등)** 이 청구서는 도구 재설계의 *의제*가 아니라 *검수 문제지*다.
  재설계는 하향식으로 간다 — 조사자에게 필요한 능력(D9·D10)에서 출발해 각 도구를
  lucida-next 실제 수집 스키마와 정합성을 따지며 설계하고, 다 그린 뒤 이 문서의 T1~T8과
  프론티어 궤적 2건으로 "그 표면이었으면 어디서 막혔을까"를 검수한다. 테스트베드 케이스
  2건에 대한 과적합을 피하기 위한 결정이다.
- 이건 **backlog(청구서)**다. `spec-agent-tools.md`의 스펙 변경이 아니라 그 작업의 입력이다.
- 근거는 두 blind 프론티어 주행뿐이다: **F04-R**(카프카 consumer 정지→배송 지연),
  **F01-R**(DB hot row 락→inventory 풀 고갈→체크아웃 실패). 궤적 원본은
  [frontier-trajectory-f04r.md](frontier-trajectory-f04r.md) /
  [frontier-trajectory-f01r.md](frontier-trajectory-f01r.md) (2026-07-24 docs/로 보존).
- **[2건]** = 두 케이스 모두에서 확인(일반화 근거 있음). **[1건]** = 한 케이스만(약함).
- 능력 수준으로 적는다 — 재설계(`adr-rca-agent-redesign.md` D9 "형사 하나 + 상황별 메뉴")가
  도구 표면을 바꿔도 요구 자체는 유지되도록.

## 관통하는 두 병목

두 주행이 가리키는 병목은 도구 *개수*가 아니라 두 가지다:

- **(A) 조사 공간 legibility** — "무엇을 조사할 수 있는가(그 대상의 이름·주소)"가 안 보인다.
  그래서 넓히기도, 확정 도구를 겨냥하기도 막힌다. (재설계 D5와 직결)
- **(B) 비교 사실을 코드가 안 준다** — 또래 대비/선후/‘정상’ 판정을 모델이 날숫자로 눈대중해야
  한다. 강모델은 해냈지만 소형 모델(프로덕션)은 못 할 수 있다. (D5 정련 → D10)

## topology 지도의 간극 — 병목 (A)의 뿌리 (2026-07-23 논의 + 코드·데이터 실증)

"원인이 topology 밖에 있다"는 말은 **인과적으로 연결 안 된 곳에 원인이 있다는 뜻이
아니다** — 원인이 증상에 영향을 줬다면 반드시 실제 경로(호출·공유 자원·메시지 큐)로
이어져 있다. 문제는 **실제 의존은 존재하는데, 수집된 topology 그래프에 그 간선이 안
그려져 있거나 반쪽으로 그려진 경우**다. 지도와 실제 땅의 차이다.

실증 원천(2026-07-23, 소스코드 + case-f08-h 복원 DB 읽기전용 조사): topology 정본은
`incidents.topology` PG jsonb 박제(lucida-next가 생성, `tools/topology.go:61`)이고,
`get_topology`는 그 edge의 source/target 문자열을 **가공 없이** 내보낸다
(`tools/topology.go:124-128`). 복원 DB 15개 인시던트의 edge 종류는 5종이 전부다 —
access_host 779 / network_link 114 / apm_call 63 / apm_db 40 / event_cluster 5.
간극은 세 가지 꼴로 실재한다:

1. **간선이 반쪽** — edge는 있는데 끝점이 해석 불가. **apm_db 간선 40건 전원**이
   source=앱 UUID, target=`db:<engine>` 문자열(`db:postgresql`·`db:mysql`·`db:oracle`·
   `db:redis`)이다 — apm_call(서비스→서비스)은 양끝 모두 UUID인 것과 대조된다. 한편
   그 문자열이 가리키는 실 대상은 인벤토리에 UUID로 실재한다(예: `PostgreSQL-commerce`
   = `cf97076f-…`, type=database — 복원 DB의 database 대상 5건 전부 UUID 보유). 코드는
   이 문자열을 구제하지 못한다: UUID 채움은 노드에만, 이미 UUID일 때만 적용되고
   (`cmd/rca/main.go:269-273`), `get_target_meta`는 UUID 정규식으로 비-UUID를 거부한다
   (`tools/targetmeta.go:44-47`). 그래서 F01에서 확정 도구(`get_db_sessions`)를 겨눌
   대상을 끝내 못 얻었다(→ T2).
2. **관계 유형 자체가 지도에 없음** — 인과는 흐르는데 호출 그래프에 표현이 안 되는
   관계. Kafka 계보(producer→topic→consumer)는 topology edge 종류에 없고, 메트릭
   카탈로그(`metric_definitions` 2,002건)에도 kafka/consumer-lag 계열이 0건이다
   (**정정 2026-07-23**: 이 0건은 F04-R 당시 환경 기준 — f08-h 캡처엔 kafka 계열
   157개 이름·records_lag 실데이터가 존재한다. 카탈로그 부재가 아니라 **환경·시점별
   수집 편차**가 정확한 진단이다. spec-tool-redesign.md §2.0-4). 단
   **원자료에는 계보가 살아 있다**: `otel_traces_local`에 PRODUCER 2,316 / CONSUMER
   1,579 span과 `messaging.destination.name`(topic)·`messaging.kafka.consumer.group`
   속성이 실재해, commerce-order →(`commerce.orders`)→ commerce-shipping 같은 실제
   계보를 span에서 조립할 수 있다. 그러나 현 도구 코드는 CONSUMER span을 어디서도
   읽지 않는다(`tools/apm.go` — span_kind는 SERVER·CLIENT/PRODUCER만). 즉 이 꼴은
   "수집이 없다"가 아니라 **"원자료에 있는데 아무도 조립하지 않는다"**다(→ T1 계보,
   T2 consumer 구간).
3. **호출이 아닌 의존** — 같은 호스트의 리소스 경쟁 이웃, 공유 스토리지, 경유 장비.
   동거를 표현하는 edge 종류도, target 메타의 host FK도 없다(`meta.host_target_id`는
   app·DB 대상에서 자기 자신을 가리키는 self-reference). 동거는 IP 문자열 상관으로만
   복원 가능하다(예: PostgreSQL-commerce와 server 대상이 같은 address
   `192.168.200.136`인데 어떤 간선도 없음). 유일한 런타임 우회가
   `get_runtime_connections` — topology가 아니라 CH `host_connections`(복원 DB 기준
   199만 행)의 실 소켓 관측을 `targets.address`와 대조하는 방식(`tools/meta.go`)이며,
   이 도구의 존재 자체가 간극의 기존 인정이다.

T1 부재도 코드로 확정: `targets` 조회는 `WHERE id = ANY($1::uuid[])`뿐
(`tools/targetmeta.go:55-58`) — UUID를 이미 알아야만 조회 가능하고, 유형/도메인/이름
열거·검색 경로는 없다(코드 주석에 "전체 목록 불가" 명시). 열거만 있었으면 F01은
`db:postgresql` → "database 대상 5건 나열" → `PostgreSQL-commerce` UUID로 확정 도구를
걸 수 있었다.

경계 주의: 여기서 말하는 조사 범위는 어디까지나 **관제 인벤토리 안**이다. 관제하지
않는 대상은 데이터가 없어 조사 불가이며, 그건 재설계 D7의 멈춤 사유 `못 하는 도메인`
영역이다. 간극은 "관제 안에 있는데 인시던트 topology 부분그래프가 손을 못 뻗는 대상"
에서 생기고, T1(전역 열거·검색)이 그 우회로다.

## Tier 1 — 두 케이스 모두에서 *확정을 막은* 것

### T1. 타깃 디렉터리 / 열거·검색  — (A) **[2건]**
조사자가 "이 유형/도메인의 대상 전부"를 나열하거나 검색할 방법이 없다. 이미 손에 쥔
UUID로만 pivot이 가능하다.
- F01: DB 락이 교과서적으로 드러났는데 정작 그 **PG의 target UUID를 어디서도** 못 얻어
  확정 도구를 못 걸었다. "모든 database 대상 나열"이 필요했다.
- F04: producer→topic→consumer-group 계보가 없어 넓히기가 결정론이 아닌 추론이었다.
- 요구: 유형/도메인/이름으로 대상을 **열거·검색**하는 경로. 넓히기(D5)의 뿌리.

### T2. 확정 도구를 올바른 대상에 겨냥 가능하게  — (A) **[2건]**
도메인 확정 도구는 있는데, 정작 그 도구를 가리킬 **대상을 그래프가 노출하지 않는다.**
같은 형태가 두 도메인에서 반복됐다.
- F01: 앱 topology의 DB 이웃이 UUID 없는 문자열 `db:postgresql`. → `get_db_sessions`·
  `get_slow_queries`(락/blocking 확인 전용)가 **무용지물**. 요구: 앱 edge가 그 앱이 쓰는
  DB의 **실 UUID**를 실어야 한다.
- F04: 트레이스 도구가 SERVER span만 노출 → Kafka 구동 서비스의 실제 처리 구간
  (CONSUMER/INTERNAL span)이 안 보인다. 요구: consumer/메시지 처리 구간 분해.

## Tier 2 — 조사를 *오도하거나 저하*시킨 것

### T3. 또래 비교(cohort)를 1급으로  — (B) **[2건, 최우선 신호]**
두 케이스 모두 **결정타가 또래 비교**였다: 넓은 가설을 죽이고 진범을 국소화한 한 방이
둘 다 "옆은 멀쩡, 얘만 이상"이었다(F04 peer lag 0 vs shipping 1087 / F01 peer pending 0 vs
inventory 20). 지금은 조사자가 `get_cohort`를 *떠올려야* 쓴다.
- 요구: 또래 비교를 쉽게/눈에 띄게, 가능하면 **자동으로** 붙인다 — 예: `discover_signals`
  결과에 "이 이상이 또래 대비도 이상인가"를 함께. D5 정련("코드가 비교를 떠먹인다")의 핵심 구현.

### T4. discover_signals 이상 랭킹 신뢰화  — (B) **[2건]**
두 케이스 다 이상 순위 상단에 엉뚱한 지표가 올라 **첫 가설을 오도**했다.
- F01: `change_score`가 미정규화 — 0→값은 무한대급으로 튀어, from-zero 지표를 과대평가.
- F04: episodic gauge(rebalance/join)가 "신규 등장"으로 떠 rebalance 오진 유도.
- 요구: 변화 점수 정규화 + "이 gauge는 이벤트 때만 방출/기동 잔상" 표시.

## Tier 3 — primitive·데이터층 (D10 primitive가 굴러가게)

### T5. 정렬된 버킷 시계열  — (B) **[1건: F01]**
`get_metric_series`가 창 *요약*(avg/max)만 줘서, 두 대상 곡선을 나란히 놓는 선후(lead-lag)·
onset 비교를 못 했다(coarse 이벤트 타임스탬프로 우회). D10의 "선행/후행" primitive를 코드가
굴리려면 데이터층이 **정렬된 버킷 시계열**을 내야 한다.

### T6. baseline: measured-zero vs no-sample 구분  — **[2건]**
"무관측"이 "0으로 측정됨"인지 "표본 없음"인지 구분되지 않아, ‘정상’ 판정을 신뢰할지
판단이 흐려졌다. 요구: baseline별 measured-zero / no-sample 플래그.

### T7. 아무 레벨 표본 로그 N줄(필터)  — **[2건, 대조 사례]**
- F04: 결정적 증거가 **INFO** 줄("Consumer/partitions/commit", 컨테이너 pause/resume)에
  있었는데 `get_logs`가 WARN+만 노출 → **못 읽어 정확 트리거 미확정**(그 케이스 유일한 미확정 원인).
- F01: 결정적 로그가 마침 WARN급(HikariCP timeout)이라 통과 → **같은 도구가 한 번은 막고
  한 번은 통과**. 요구: "필터로 N줄 표본 raw 로그(레벨 불문)" 모드.

### T8. 오류에 유효 id 열거(계약 원칙 복원)  — (A) **[2건]**
도구 계약 §2 "오류엔 복구 정보"가 이 도구들엔 안 지켜졌다: 잘못된 id로 `get_db_sessions`·
`get_runtime_connections`를 불러도 유효 id 목록 없이 generic `no_data`만 와, id를 탐색으로
발견할 수 없었다. T1(디렉터리)과 묶어 해소.

## 거리 주의 (증류)

강모델(Opus)은 T3(또래 비교)·T5(선후) 같은 비교를 *스스로 떠올려* 수행했다. 프로덕션
소형 모델(gemma4-26b)이 이를 재현한다는 보장은 없다. 따라서 T3·T5·T6은 조사자에게
맡기지 말고 **코드가 선제 계산·노출**해야 한다(떠먹이기). 이것이 "강모델을 성공시킨 것을
코드가 대신 공급"이라는 증류 방향의 구체 목록이다.
