# Polestar RCA 시나리오 다양성 분석 — 무엇이 빠져 있나

작성 2026-10-19 · 대상: `split-v4-20261018.yaml` 의 `train` 59 + `validation` 10 + `training_quarantine` 2 = **71 캡처 (고유 시나리오 66, 가족 43)**.
`sealed_eval` 12건과 `excluded` 1건은 열지도, 세지도 않았다. 범위 안 메타 텍스트 일부가 봉인 가족 ID를 대조군으로 언급하지만, 그 내용은 보지 않았고 분석에도 쓰지 않았다. `/data/eval-cases` 는 읽기만 했다.

## 0. 요약 — 가장 큰 공백

1. **음성 케이스와 방해 요소가 하나도 없다.** 결함이 없는 케이스, 양성(무해한) 변경만 있는 케이스, 진짜 원인과 무관한 변경·부하가 겹친 케이스가 모두 0건이다. 반면 롤아웃(이미지 릴리스 13 + 설정 롤아웃 15 = 28건, 39%)은 거의 모두 같은 결정 증거로 풀린다. KCM ScalingReplicaSet 이벤트와 새 ReplicaSet spec이다. 그래서 "가장 최근 롤아웃 = 원인"이라는 지름길이 보상받는 분포다. 골든 계약에도 "근원 없음"을 표현하는 상태가 없다 (`expected_status` 는 confirmed/provisional 두 값뿐).
2. **다중 근원이 사실상 1개다.** f15-g 하나를 2번 캡처한 것이고, 두 근원 모두 DB 락이며 동시에 시작한다. 이종 조합(예: 릴리스 + 인프라)이나 시차를 둔 다중 근원은 0건이다. 공개 OpenRCA2(o2) 442건에는 2-근원 18건과, 근원이 아닌 공동 주입 결함(방해 요소)이 있는 케이스 152건이 있다.
3. **공개 학습셋에서 가장 흔한 결함 유형에 대응하는 Polestar 케이스가 0~1건이다.**
   - 서비스 간 네트워크 지연·손실·손상·대역폭 제한: 0
   - 일시적 파드/컨테이너 kill: 0 (가용성 상실은 전부 변경이 원인이고 지속형이다)
   - 앱 컨테이너 CPU 호그: 0 (CPU limit 스로틀 1건뿐)
   - 소켓/FD 고갈: 0
   - 디스크 스트레스: 1 (노드 디스크 IO)
4. **같은 템플릿이 과대표집되어 있다.**
   - DB 객체 누락(테이블 drop·rename) 8건. 그중 "개발자 PC의 마이그레이션이 운영 DB 테이블을 drop" 서사가 5건이고, food MySQL에 6건이 몰려 있다.
   - 계약/포맷 불일치 6건 중 5건이 food다.
   - 커넥션 누수 릴리스 3건이 같은 형태다.
   - "transfer 엔드포인트 0 → Connection refused" 4건(f17-r, f17-h, f50-r, f52-r)은 피해 서비스와 증상이 같고 원인 객체만 다르다.
   - 사용자 증상이 "commerce checkout 실패" 또는 "food 주문 생성 실패"인 케이스가 약 85% (60/71)다.
5. **도메인 × 기전 조합에 큰 구멍이 있다.**
   - food: 락 경합, 메모리, 가용성 상실, 비동기 파이프라인, 데이터 오류가 모두 0이다.
   - commerce: 이미지 릴리스가 1건뿐이고, 계약 불일치·네트워크·DNS가 0이다.
   - banking: 트래픽/용량, 외부 의존, CPU, 레이트리밋이 0이다.
   - 도메인을 넘는 전파는 banking→commerce 한 방향뿐이고, food는 완전히 고립돼 있다.
6. **플랫폼/인프라 근원이 얇다.**
   - 현황: 노드 3, k8s 객체 2(ResourceQuota, Job), 호스트 방화벽 2.
   - 0건인 것: 노드 NotReady, CoreDNS/CNI 장애, 볼륨·테이블스페이스 고갈, HPA/PDB, Kafka 브로커 장애, 인증서 만료.
   - 모든 디플로이먼트가 replicas=1이라서 "N개 중 일부 레플리카만 불량"인 형태가 없다.
7. **결정 증거가 앱 로그에 쏠려 있다.**
   - 1차 결정 증거가 앱 로그의 에러 문자열인 케이스가 28건(39%)이다. 10월 배치는 대부분 "보존 기간 동안 한 번도 없던 에러 문구"가 결정타다.
   - 반면 DB 세션은 7건, Top SQL 8건, 호스트 syslog 3건, 비즈니스 불변식 4건뿐이다.
   - 로그가 침묵해서 메트릭이나 트레이스로만 풀리는 형태가 부족하다. 지연만 있는 열화는 3건, 간헐/부분 장애는 5건이다.

## 1. 범위와 방법

- **입력**
  - 케이스별 `/data/eval-cases/<case>/meta.json` 의 `scenario_metadata`: title, description, cause, injection_summary, user_impact, distinguishing_evidence
  - 골든 계약 `configs/eval/train-family-v4-expanded-20261018.yaml`: roots[].target_aliases, pseudo_kind, expected_status, allowed_note
- **분류 방법**: 71건의 메타 원문을 전부 읽고 6개 축에 직접 라벨을 붙였다. 키워드 룰은 쓰지 않았다. 라벨 원본은 스크래치패드의 `div/labels.py`, 집계는 `div/tables*.py` 다.
- **깊이의 정의**: 근원 컴포넌트에서 사용자 오류를 반환하는 엔트리 서비스까지의 호출 홉 수다. 게이트웨이와 nginx는 세지 않는다.
  - DB → 그 DB를 쓰는 서비스 = 1
  - 0 = 근원 서비스 자신의 엔드포인트가 실패한 경우
  - 비동기로 침묵하는 경우는 생산·소비 서비스 기준
- **경계 사례**
  - **f50-r**: 골든 근원은 transfer 워크로드와 namespace이지만, 결함 객체가 ResourceQuota라서 "k8s 플랫폼 객체"로 셌다.
  - **f44-p, f44-r**: 골든 근원은 노드다. 기전이 호스트 방화벽이라서 "네트워크(호스트 방화벽)"로 따로 셌다.
  - **f11-r**: 클러스터 안에서 도는 Redis다(origin rule). "클러스터내 의존"으로 분류했다.
  - **f20-r**: 근원은 order 워크로드가 보낸 비효율 쿼리다. 앱으로 분류했고, 기전은 "쿼리비용→DB 포화"다.
  - **f40-r** (quarantine): 운영자의 잘못된 설정 행(row)을 "DB 데이터 변경"으로 분류했다.
- **변형 문자(h, p, r, s, g, t1 …)의 뜻 (추정)**: 고정된 코드가 아니다. 가족 안의 형제 슬롯이다.
  - **8월 가족(f01–f25)**: 한 주제 아래 기전이 다른 형제들이다. 예:
    - F05: P = 노드 메모리 eviction, R = 컨테이너 limit OOM, H = liveness 프로브 kill
    - F19: P = 외부 지연 < timeout, S = 지연 > timeout
    - F06: H = 테이블 락, P = food 외부 429, R = 외부 30초 hang
  - **G**: 다중 근원(f15-g)으로 보인다.
  - **10월 가족(f30+)**: 대부분 "공개 포스트모템 재구성"이다. R이 대표 시나리오이고, H/P는 같은 기전을 다른 서비스·도메인에 옮긴 것(f33 R/P, f49 H/P/R, f53 R/P, f44 R/P)이거나, 같은 사용자 증상에 원인만 다른 것(f51 R/H)이다.
  - split 문서도 "동형 기전을 가족 안에서 갈라 근거리 전이를 측정하는 설계"라고 명시한다. 즉 형제끼리 서로 닮는 것은 의도된 설계다. 다만 다양성 관점에서는 사실상 중복이다.
- **약어**: 유형 = 근원 컴포넌트 유형. 형태의 `ext+boundary` = 외부 의존 근원 + 경계 서비스. `dep+boundary` = 클러스터내 의존 + 경계 서비스. `multi-root(2)` = 2-근원.

## 2. 전체 케이스 표 (71건)

| # | 케이스 | split | 도메인(근원→증상) | 근원 유형 | 트리거 | 기전(주) / 세부 | 결정 증거 | 형태 | 깊이 | 특이 | 골든 근원 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | f01-h | train | commerce | 외부 의존 | 외부 장애 | 레이트리밋(429) / error-mistranslation(429->502) | 트레이스 | ext+boundary | 2 | - | external-pg + payment |
| 2 | f01-p#1 | train | banking→commerce | DB 인스턴스 | DB 락 보유(장기 트랜잭션) | 락 경합 / timeout->pool | DB 세션/블로킹, 트레이스 | single | 3 | - | Oracle BANKING |
| 3 | f01-p#2 | train | banking→commerce | DB 인스턴스 | DB 락 보유(장기 트랜잭션) | 락 경합 / timeout->pool | DB 세션/블로킹, 트레이스 | single | 3 | - | Oracle BANKING |
| 4 | f03-h | train | commerce | 앱 서비스 | 트래픽/워크로드 | 스레드풀 고갈 / serialized monitor | 메트릭(앱/JVM/풀/컨테이너/노드), 트레이스 | single | 0 | - | order |
| 5 | f04-r | train | commerce | 앱 서비스 | 운영자 k8s/호스트 조작 | 비동기 파이프라인 정지 / consumer stopped (lag) | 메트릭(앱/JVM/풀/컨테이너/노드), 비즈니스 불변식/시계열, k8s 이벤트 | single | 0 | zero-5xx | shipping |
| 6 | f05-p | train | multi | 호스트/노드 | 인프라 자원 장애 | 메모리(OOM/GC/eviction) / node eviction | k8s 이벤트, 메트릭(앱/JVM/풀/컨테이너/노드) | single | 1 | - | node tb-w1 |
| 7 | f05-r | train | commerce | 앱 서비스 | 설정 변경 롤아웃 | 메모리(OOM/GC/eviction) / OOMKill restart | k8s 이벤트, 변경 이력(롤아웃 spec) | single | 1 | - | payment |
| 8 | f06-h | train | commerce | DB 인스턴스 | DB 락 보유(장기 트랜잭션) | 락 경합 / pods Ready | DB 세션/블로킹 | single | 2 | - | commerce PG |
| 9 | f07-h#1 | train | commerce | 앱 서비스 | 트래픽/워크로드 | 커넥션풀 고갈 / capacity knee (no change) | 메트릭(앱/JVM/풀/컨테이너/노드), 변경 이력(롤아웃 spec) | single | 0 | - | order |
| 10 | f07-h#2 | train | commerce | 앱 서비스 | 트래픽/워크로드 | 커넥션풀 고갈 / capacity knee (no change) | 메트릭(앱/JVM/풀/컨테이너/노드), 변경 이력(롤아웃 spec) | single | 0 | - | order |
| 11 | f09-h | train | commerce | 앱 서비스 | 설정 변경 롤아웃 | 메모리(OOM/GC/eviction) / GC pause latency | 메트릭(앱/JVM/풀/컨테이너/노드) | single | 0 | latency-only | order |
| 12 | f09-p | train | commerce | 앱 서비스 | 설정 변경 롤아웃 | CPU 스로틀 / throttling | 메트릭(앱/JVM/풀/컨테이너/노드), 변경 이력(롤아웃 spec) | single | 2 | - | inventory |
| 13 | f10-h | train | food | 호스트/노드 | 인프라 자원 장애 | 디스크 IO 포화 / DB IO wait | 메트릭(앱/JVM/풀/컨테이너/노드) | single | 2 | latency-only | node tb-w3 |
| 14 | f11-r#1 | train | commerce | 클러스터내 의존(캐시) | 의존 미들웨어 중단 | 커넥션풀 고갈 / cache fallback -> DB pool | 앱 로그, 메트릭(앱/JVM/풀/컨테이너/노드) | dep+boundary | 2 | - | redis (+cart) |
| 15 | f11-r#2 | train | commerce | 클러스터내 의존(캐시) | 의존 미들웨어 중단 | 커넥션풀 고갈 / cache fallback -> DB pool | 앱 로그, 메트릭(앱/JVM/풀/컨테이너/노드) | dep+boundary | 2 | - | redis (+cart) |
| 16 | f14-p | train | banking | DB 인스턴스 | DB 스키마/DDL | 쓰기/로그인 거부 / swallowed -> silent data loss | 앱 로그, 비즈니스 불변식/시계열 | single | 1 | zero-error | Oracle BANKING |
| 17 | f15-g#1 | train | cross→commerce | DB 인스턴스 | DB 락 보유(장기 트랜잭션) | 락 경합 / two independent locks | DB 세션/블로킹, 트레이스 | multi-root(2) | 3 | - | commerce PG + Oracle |
| 18 | f15-g#2 | train | cross→commerce | DB 인스턴스 | DB 락 보유(장기 트랜잭션) | 락 경합 / two independent locks | DB 세션/블로킹, 트레이스 | multi-root(2) | 3 | - | commerce PG + Oracle |
| 19 | f16-h#1 | train | commerce | 앱 서비스 | 설정 변경 롤아웃 | 가용성 상실(엔드포인트 0) / auth fail-close 401 | k8s 이벤트, 메트릭(앱/JVM/풀/컨테이너/노드) | single | 1 | 4xx | user |
| 20 | f16-h#2 | train | commerce | 앱 서비스 | 설정 변경 롤아웃 | 가용성 상실(엔드포인트 0) / auth fail-close 401 | k8s 이벤트, 메트릭(앱/JVM/풀/컨테이너/노드) | single | 1 | 4xx | user |
| 21 | f17-r | train | banking→commerce | 앱 서비스 | 설정 변경 롤아웃 | 가용성 상실(엔드포인트 0) / readiness -> endpoints 0 | k8s 이벤트, 트레이스 | single | 2 | - | transfer |
| 22 | f18-p | train | banking | 앱 서비스 | 설정 변경 롤아웃 | 비동기 파이프라인 정지 / producer(outbox) stopped | 비즈니스 불변식/시계열, 메트릭(앱/JVM/풀/컨테이너/노드) | single | 0 | zero-5xx | transfer |
| 23 | f19-p | train | food | 외부 의존 | 외부 장애 | 커넥션풀 고갈 / downstream latency holds upstream pool | 트레이스, 메트릭(앱/JVM/풀/컨테이너/노드) | ext+boundary | 2 | - | external-pg + payment |
| 24 | f19-s | train | food | 외부 의존 | 외부 장애 | 타임아웃(지연>timeout) / retry + CB | 트레이스 | ext+boundary | 2 | - | external-pg + payment |
| 25 | f20-r | train | commerce | 앱 서비스 | 트래픽/워크로드 | 쿼리비용→DB 포화 / shared-DB noisy neighbor | DB Top SQL, 메트릭(앱/JVM/풀/컨테이너/노드) | single | 0 | latency-only | order/product |
| 26 | f23-r | train | commerce | 앱 서비스 | 설정 변경 롤아웃 | 비동기 파이프라인 정지 / batch stopped -> depletion 409 | 비즈니스 불변식/시계열, 변경 이력(롤아웃 spec) | single | 2 | 4xx | inventory |
| 27 | f25-h | train | commerce | DB 인스턴스 | 설정 변경 롤아웃 | 메모리(OOM/GC/eviction) / DB OOMKill blip | k8s 이벤트 | single | 1 | - | commerce PG |
| 28 | f17-h | train | banking→banking+commerce | 앱 서비스 | 릴리스(이미지) | 가용성 상실(엔드포인트 0) / ErrImageNeverPull | k8s 이벤트, 변경 이력(롤아웃 spec), 앱 로그 | single | 2 | - | transfer |
| 29 | f30-r | train | food | 앱 서비스 | 설정 변경 롤아웃 | 계약/포맷 불일치 / JSON naming 400 | 앱 로그, 변경 이력(롤아웃 spec) | single | 1 | 4xx | payment |
| 30 | f32-h | train | food | 앱 서비스 | 설정 변경 롤아웃 | 계약/포맷 불일치 / number-as-string | 앱 로그, 변경 이력(롤아웃 spec) | single | 1 | - | dispatch |
| 31 | f32-r | train | food | 앱 서비스 | 설정 변경 롤아웃 | 한도/쿼터 오설정 / capacity limit misconfig | 앱 로그, 변경 이력(롤아웃 spec) | single | 1 | - | dispatch |
| 32 | f33-p | train | commerce | DB 인스턴스 | DB 스키마/DDL | 쿼리비용→DB 포화 / index invalid -> full scan | DB Top SQL, DB 인벤토리 메트릭, 앱 로그 | single | 2 | - | commerce PG |
| 33 | f33-r | train | food | DB 인스턴스 | DB 스키마/DDL | 쿼리비용→DB 포화 / index drop -> full scan | DB Top SQL, DB 인벤토리 메트릭 | single | 2 | - | food MySQL |
| 34 | f35-r | train | banking→banking+commerce | DB 인스턴스 | DB 설정/계정/프로파일 | 쓰기/로그인 거부 / account lock -> pool drain | 앱 로그, DB 세션/블로킹, k8s 이벤트 | single | 3 | - | Oracle BANKING |
| 35 | f36-r | train | food | DB 인스턴스 | DB 스키마/DDL | 객체 누락(테이블/컬럼) / column rename | 앱 로그 | single | 2 | - | food MySQL |
| 36 | f37-r | train | banking | 앱 서비스 | 설정 변경 롤아웃 | 오라우팅/DNS / dnsPolicy -> DNS fail | 앱 로그, 변경 이력(롤아웃 spec), 트레이스 | single | 0 | - | api |
| 37 | f38-r | train | food | DB 인스턴스 | DB 설정/계정/프로파일 | 쓰기/로그인 거부 / global read_only | 앱 로그, DB Top SQL | single | 1 | - | food MySQL |
| 38 | f39-r | train | banking | 앱 서비스 | 설정 변경 롤아웃 | 오라우팅/DNS / wrong endpoint URL | 앱 로그, 변경 이력(롤아웃 spec) | single | 1 | - | account |
| 39 | f41-r | train | banking | 앱 서비스 | 릴리스(이미지) | 메모리(OOM/GC/eviction) / leak -> OOM restart loop | 메트릭(앱/JVM/풀/컨테이너/노드), k8s 이벤트, 앱 로그 | single | 1 | intermittent | account |
| 40 | f42-r | train | banking→banking+commerce | 앱 서비스 | 릴리스(이미지) | 쿼리비용→DB 포화 / new non-sargable query | DB Top SQL, 변경 이력(롤아웃 spec) | single | 2 | - | transfer |
| 41 | f43-r | train | commerce | 앱 서비스 | 릴리스(이미지) | 커넥션풀 고갈 / connection leak (restart loop) | 메트릭(앱/JVM/풀/컨테이너/노드), 앱 로그, k8s 이벤트 | single | 1 | - | cart |
| 42 | f44-p | train | banking→commerce | 네트워크(호스트 방화벽) | 운영자 k8s/호스트 조작 | 네트워크 차단(드롭) / host firewall (cross-node) | 호스트 syslog, 앱 로그 | single | 2 | - | node tb-w2 |
| 43 | f44-r | train | food | 네트워크(호스트 방화벽) | 운영자 k8s/호스트 조작 | 네트워크 차단(드롭) / host firewall (same node) | 호스트 syslog, 앱 로그 | single | 1 | - | node tb-w3 |
| 44 | f32-p | train | food | DB 인스턴스 | DB 스키마/DDL | 객체 누락(테이블/컬럼) / drop table (wrong env) | 앱 로그, DB 인벤토리 메트릭 | single | 2 | - | food MySQL |
| 45 | f40-h | train | banking→commerce | 앱 서비스 | 릴리스(이미지) | 계약/포맷 불일치 / validation tightening 400 | 앱 로그, 트레이스, 변경 이력(롤아웃 spec) | single | 2 | - | transfer |
| 46 | f48-p | train | food | DB 인스턴스 | DB 스키마/DDL | 객체 누락(테이블/컬럼) / botched backfill drops table | 앱 로그, DB 인벤토리 메트릭 | single | 1 | - | food MySQL |
| 47 | f49-h | train | food | 앱 서비스 | 릴리스(이미지) | 계약/포맷 불일치 / client-side parse (callee healthy) | 앱 로그, 트레이스, 변경 이력(롤아웃 spec) | single | 0 | - | order |
| 48 | f50-r | train | banking→banking+commerce | k8s 플랫폼 객체 | 운영자 k8s/호스트 조작 | 가용성 상실(엔드포인트 0) / ResourceQuota admission refusal | k8s 이벤트, 변경 이력(롤아웃 spec) | single | 2 | - | transfer/namespace (quota) |
| 49 | f51-r | train | banking | 앱 서비스 | 운영자 k8s/호스트 조작 | 가용성 상실(엔드포인트 0) / scale to 0 | k8s 이벤트, 앱 로그 | single | 1 | - | account |
| 50 | f53-r | train | food | DB 인스턴스 | DB 스키마/DDL | 객체 누락(테이블/컬럼) / drop table (wrong env) | 앱 로그, DB 인벤토리 메트릭 | single | 1 | - | food MySQL |
| 51 | f54-r | train | banking | DB 인스턴스 | DB 설정/계정/프로파일 | 한도/쿼터 오설정 / profile per-call IO cap | 앱 로그, DB Top SQL | single | 2 | partial-endpoint | Oracle BANKING |
| 52 | f49-p | train | food | 앱 서비스 | 릴리스(이미지) | 계약/포맷 불일치 / response id type change | 앱 로그, 트레이스, 변경 이력(롤아웃 spec) | single | 1 | - | payment |
| 53 | f55-r | train | food | 앱 서비스 | 릴리스(이미지) | 레이트리밋(429) / per-IP limiter (shared NAT) | 앱 로그, 트레이스, 변경 이력(롤아웃 spec) | single | 1 | - | dispatch |
| 54 | f56-r | train | banking→commerce | DB 인스턴스 | DB 데이터 변경 | 데이터/상태 오류 / reference rows deleted | 앱 로그, 트레이스 | single | 3 | - | Oracle BANKING |
| 55 | f51-h | train | banking | 앱 서비스 | 릴리스(이미지) | 커넥션풀 고갈 / connection leak (no restart) | 메트릭(앱/JVM/풀/컨테이너/노드), 앱 로그, k8s 이벤트 | single | 1 | - | account |
| 56 | f57-r | train | banking→banking+commerce | DB 인스턴스 | DB 스키마/DDL | 데이터/상태 오류 / identity rewind -> PK collision | 앱 로그 | single | 3 | - | Oracle BANKING |
| 57 | f23-p | train | commerce | DB 인스턴스 | DB 스키마/DDL | 객체 누락(테이블/컬럼) / drop table (wrong env) | 앱 로그, DB 인벤토리 메트릭 | single | 2 | - | commerce PG |
| 58 | f58-r | train | food | 앱 서비스 | 릴리스(이미지) | 설정 키 누락 / prod config key missing | 앱 로그, 트레이스, 변경 이력(롤아웃 spec) | single | 0 | - | order |
| 59 | f59-r | train | food | k8s 플랫폼 객체 | 배치/잡 | 쿼리비용→DB 포화 / deep-offset backfill job | DB Top SQL, k8s 이벤트, 트레이스 | single | 2 | - | Job notify-delivery-backfill |
| 60 | f01-r | valid | commerce | DB 인스턴스 | DB 락 보유(장기 트랜잭션) | 락 경합 / partial -> widening pool | DB 세션/블로킹 | single | 3 | partial | commerce PG |
| 61 | f05-h | valid | commerce | 앱 서비스 | 설정 변경 롤아웃 | 재시작 루프(프로브) / liveness probe kill loop | k8s 이벤트, 변경 이력(롤아웃 spec) | single | 1 | intermittent | payment |
| 62 | f06-p | valid | food | 외부 의존 | 외부 장애 | 레이트리밋(429) / 429 passthrough | 트레이스 | ext+boundary | 2 | 4xx | external-pg + payment |
| 63 | f06-r | valid | commerce | 외부 의존 | 외부 장애 | 타임아웃(지연>timeout) / hang -> pool | 트레이스 | ext+boundary | 2 | - | external-pg + payment |
| 64 | f48-r | valid | food | DB 인스턴스 | DB 스키마/DDL | 객체 누락(테이블/컬럼) / botched backfill (one endpoint) | 앱 로그, DB 인벤토리 메트릭 | single | 1 | partial-endpoint | food MySQL |
| 65 | f49-r | valid | food | 앱 서비스 | 릴리스(이미지) | 계약/포맷 불일치 / response restructure | 앱 로그, 트레이스, 변경 이력(롤아웃 spec) | single | 1 | - | restaurant |
| 66 | f53-p | valid | food | DB 인스턴스 | DB 스키마/DDL | 객체 누락(테이블/컬럼) / drop table (wrong env) | 앱 로그, DB 인벤토리 메트릭 | single | 2 | - | food MySQL |
| 67 | f33-h | valid | food | 앱 서비스 | 릴리스(이미지) | 쿼리비용→DB 포화 / added COUNT per request | DB Top SQL, 변경 이력(롤아웃 spec) | single | 1 | - | dispatch |
| 68 | f35-h | valid | banking→banking+commerce | DB 인스턴스 | DB 스키마/DDL | 객체 누락(테이블/컬럼) / drop table (wrong env) | 앱 로그, DB 인벤토리 메트릭 | single | 3 | - | Oracle BANKING |
| 69 | f43-p | valid | food | 앱 서비스 | 릴리스(이미지) | 커넥션풀 고갈 / connection leak (restart loop) | 메트릭(앱/JVM/풀/컨테이너/노드), 앱 로그, k8s 이벤트 | single | 1 | - | dispatch |
| 70 | f40-r | quara | commerce | 앱 서비스 | DB 데이터 변경 | 데이터/상태 오류 / bad promotion row -> zero amount | 앱 로그, 변경 이력(롤아웃 spec) | single | 1 | - | pricing |
| 71 | f52-r | quara | banking→banking+commerce | 호스트/노드 | 운영자 k8s/호스트 조작 | 가용성 상실(엔드포인트 0) / node image store purge | 호스트 syslog, k8s 이벤트 | single | 3 | - | node tb-w2 |

## 3. 축별 분포

### 3.1 근원 유형 × 근원 도메인

| | commerce | food | banking | cross(2-root) | multi(node) | 계 |
|---|---|---|---|---|---|---|
| 앱 서비스 | 14 | 10 | 10 | · | · | 34 |
| DB 인스턴스 | 5 | 8 | 8 | 2 | · | 23 |
| 외부 의존 | 2 | 3 | · | · | · | 5 |
| 호스트/노드 | · | 1 | 1 | · | 1 | 3 |
| 클러스터내 의존(캐시) | 2 | · | · | · | · | 2 |
| 네트워크(호스트 방화벽) | · | 1 | 1 | · | · | 2 |
| k8s 플랫폼 객체 | · | 1 | 1 | · | · | 2 |
| **계** | 23 | 24 | 21 | 2 | 1 | 71 |

- **DB 엔진별 근원**: commerce PostgreSQL 5(+f15-g 2), food MySQL 8, banking Oracle 8(+f15-g 2).
- **앱 근원이 몰린 서비스** (가장 많이 등장하는 순): food dispatch 5, banking transfer 5(+f50-r), banking account 4, commerce order 5(같은 시나리오 중복 포함).

### 3.2 트리거 × 근원 도메인

| | commerce | food | banking | cross(2-root) | multi(node) | 계 |
|---|---|---|---|---|---|---|
| 설정 변경 롤아웃 | 8 | 3 | 4 | · | · | 15 |
| 릴리스(이미지) | 1 | 7 | 5 | · | · | 13 |
| DB 스키마/DDL | 2 | 7 | 3 | · | · | 12 |
| DB 락 보유(장기 트랜잭션) | 2 | · | 2 | 2 | · | 6 |
| 운영자 k8s/호스트 조작 | 1 | 1 | 4 | · | · | 6 |
| 외부 장애 | 2 | 3 | · | · | · | 5 |
| 트래픽/워크로드 | 4 | · | · | · | · | 4 |
| DB 설정/계정/프로파일 | · | 1 | 2 | · | · | 3 |
| 인프라 자원 장애 | · | 1 | · | · | 1 | 2 |
| 의존 미들웨어 중단 | 2 | · | · | · | · | 2 |
| DB 데이터 변경 | 1 | · | 1 | · | · | 2 |
| 배치/잡 | · | 1 | · | · | · | 1 |
| **계** | 23 | 24 | 21 | 2 | 1 | 71 |

- **8월 배치(31건)와 10월 배치(40건)의 성격이 정반대다.**
  - 8월: 설정 롤아웃 10, DB 락 6, 외부 5, 트래픽 4, 인프라 2, 의존 2. 자원·인프라·락 중심이다.
  - 10월: 릴리스 13, DDL 11, 설정 5, 운영자 조작 5, DB 설정 3, DB 데이터 2, 잡 1. 변경 중심이다.
  - 최근 증설이 모두 "변경 원인" 쪽으로만 이뤄졌다.

### 3.3 기전 × 근원 도메인

| | commerce | food | banking | cross(2-root) | multi(node) | 계 |
|---|---|---|---|---|---|---|
| 커넥션풀 고갈 | 5 | 2 | 1 | · | · | 8 |
| 객체 누락(테이블/컬럼) | 1 | 6 | 1 | · | · | 8 |
| 가용성 상실(엔드포인트 0) | 2 | · | 5 | · | · | 7 |
| 락 경합 | 2 | · | 2 | 2 | · | 6 |
| 쿼리비용→DB 포화 | 2 | 3 | 1 | · | · | 6 |
| 계약/포맷 불일치 | · | 5 | 1 | · | · | 6 |
| 메모리(OOM/GC/eviction) | 3 | · | 1 | · | 1 | 5 |
| 레이트리밋(429) | 1 | 2 | · | · | · | 3 |
| 비동기 파이프라인 정지 | 2 | · | 1 | · | · | 3 |
| 쓰기/로그인 거부 | · | 1 | 2 | · | · | 3 |
| 데이터/상태 오류 | 1 | · | 2 | · | · | 3 |
| 타임아웃(지연>timeout) | 1 | 1 | · | · | · | 2 |
| 한도/쿼터 오설정 | · | 1 | 1 | · | · | 2 |
| 오라우팅/DNS | · | · | 2 | · | · | 2 |
| 네트워크 차단(드롭) | · | 1 | 1 | · | · | 2 |
| 스레드풀 고갈 | 1 | · | · | · | · | 1 |
| CPU 스로틀 | 1 | · | · | · | · | 1 |
| 디스크 IO 포화 | · | 1 | · | · | · | 1 |
| 설정 키 누락 | · | 1 | · | · | · | 1 |
| 재시작 루프(프로브) | 1 | · | · | · | · | 1 |
| **계** | 23 | 24 | 21 | 2 | 1 | 71 |

- 커넥션풀 고갈은 주 기전으로는 8건이다. 하지만 인과 사슬의 **중간 증상**으로는 약 19건에 등장한다 (f01-p, f01-r, f06-r, f33-p/r/h, f35-r, f42-r, f59-r 등). "Hikari 'Connection is not available'"만 보고 결론 내리는 지름길이 생기기 쉬운 분포다.
- 재시도와 서킷브레이커 증폭은 거의 모든 케이스의 사슬에 들어 있다. 증폭이 없는 경로는 f06-p(4xx 그대로 전달)와 f23-r(409) 정도다.

### 3.4 결정 증거 소스

| 증거 소스 | 1차(결정) | 등장(1차+보조) |
|---|---|---|
| 앱 로그 | 28 | 37 |
| 변경 이력(롤아웃 spec) | 0 | 22 |
| 트레이스 | 5 | 20 |
| 메트릭(앱/JVM/풀/컨테이너/노드) | 11 | 19 |
| k8s 이벤트 | 10 | 18 |
| DB 인벤토리 메트릭 | 0 | 9 |
| DB Top SQL | 6 | 8 |
| DB 세션/블로킹 | 6 | 7 |
| 비즈니스 불변식/시계열 | 2 | 4 |
| 호스트 syslog | 3 | 3 |

- "변경 이력"은 1차 증거로는 0건이지만 22건에서 보조 증거로 쓰인다. 롤아웃 케이스에서는 사실상 공동 1차 증거다.
- 앱 로그 1차 28건 중 20건 이상이 "보존 기간 동안 0회였던 새 에러 문구" 패턴이다. 예: ORA-xxxx, 1146, 'Unknown column', 'Required key', ClassCastException, 429 본문.
- **로그가 결정적이지 않은 케이스**: f03-h(스레드 상태), f07-h(유입량 대비 내부 호출 비율), f09-h(GC 주기), f10-h(디스크 busy), f20-r(공유 DB를 통한 이웃 지연), f23-r(재고 시계열 모양), f04-r·f18-p(lag 방향). 대부분 8월 배치다.

### 3.5 형태 · 깊이 · expected_status · 증상 모양

| 항목 | 분포 |
|---|---|
| 근원 수 | 단일 62 · 외부 의존 + 경계 5 · 클러스터내 의존 + 경계 2 · **2-근원 2 (f15-g 동일 시나리오 2캡처)** |
| 전파 깊이 | 0: 10 · 1: 26 · 2: 25 · 3: 10 · **≥4: 0** |
| expected_status | confirmed 63 · provisional 8 (외부/클러스터내 의존 7 + f09-h) |
| 증상 모양 | 전면 장애(한 경로 100%) 55 · 4xx로 가려짐 5 · 5xx 없음/에러 없음 3 · 지연만 3 · 간헐 2 · 부분(일부 엔드포인트·일부 상품) 3 |
| 사용자 증상 집중 | commerce checkout 또는 food 주문 생성이 1차 증상인 케이스 **60/71** · banking 자체 경로만 8 · 기타 3 |
| 도메인 경계 넘는 전파 | banking 근원 → commerce 증상 15건 (f01-p×2, f17-r, f17-h, f35-r, f42-r, f44-p, f40-h, f50-r, f56-r, f57-r, f35-h, f52-r + 2-근원 f15-g×2) · 노드 공유 1 (f05-p) · **commerce→banking 0 · food 관련 0** |
| 중복 캡처 | 5개 시나리오를 2회씩 캡처 (f01-p, f07-h, f11-r, f15-g, f16-h) → 고유 66 |

## 4. 조합 분석 (도메인 × 기전, 근원 유형 × 트리거)

### 4.1 근원 유형 × 트리거

| | 설정 변경 롤아웃 | 릴리스(이미지) | DB 스키마/DDL | DB 락 보유(장기 트랜잭션) | 운영자 k8s/호스트 조작 | 외부 장애 | 트래픽/워크로드 | DB 설정/계정/프로파일 | 인프라 자원 장애 | 의존 미들웨어 중단 | DB 데이터 변경 | 배치/잡 | 계 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 앱 서비스 | 14 | 13 | · | · | 2 | · | 4 | · | · | · | 1 | · | 34 |
| DB 인스턴스 | 1 | · | 12 | 6 | · | · | · | 3 | · | · | 1 | · | 23 |
| 클러스터내 의존(캐시) | · | · | · | · | · | · | · | · | · | 2 | · | · | 2 |
| 외부 의존 | · | · | · | · | · | 5 | · | · | · | · | · | · | 5 |
| k8s 플랫폼 객체 | · | · | · | · | 1 | · | · | · | · | · | · | 1 | 2 |
| 네트워크(호스트 방화벽) | · | · | · | · | 2 | · | · | · | · | · | · | · | 2 |
| 호스트/노드 | · | · | · | · | 1 | · | · | · | 2 | · | · | · | 3 |

- **릴리스와 설정 롤아웃은 모두 앱이 근원이다.** 예외는 DB StatefulSet 메모리 limit을 바꾼 f25-h 1건이다. 릴리스가 다른 계층(DB)을 포화시킨 형태는 f42-r, f33-h 2건뿐이다.
- **DB 락 6건이 모두 같은 합성형이다.** "정체불명 세션이 600초 동안 행·테이블을 잡고 커밋하지 않음"이다. 애플리케이션 배치가 만든 락, 데드락, 락 에스컬레이션은 0건이다.
- **외부 의존 5건이 모두 같은 PG mock이다.** 결함은 429(2건)와 지연(3건)뿐이다. 외부 5xx, 부분 오류율, 잘못된 본문, TLS, 외부 DNS는 0건이다.
- **배치/잡과 의존 미들웨어가 각각 1~2건이다.** 배치/잡은 f59-r 1건(배치 정지는 설정형 f23-r로 따로 있다). 의존 미들웨어는 Redis 정지 1개 시나리오(2캡처)다.

### 4.2 기전이 한 도메인(또는 한 DB 엔진)에만 있는 조합

| 기전 | 있는 곳 | 없는 곳 |
|---|---|---|
| 계약/포맷 불일치 | food 5, banking 1 | commerce 0 |
| 객체 누락 | food MySQL 6, commerce PG 1, Oracle 1 | — (food 쏠림) |
| 가용성 상실(엔드포인트 0) | banking 5, commerce 2 | food 0 |
| 락 경합 | commerce PG, Oracle | **MySQL(food) 0** |
| 메모리/OOM | commerce 3, banking 1, 노드 1 | food 0 |
| 비동기 파이프라인 정지 | commerce 2(소비 정지·배치 정지), banking 1(생산 정지) — 여기에 banking f14-p(쓰기 실패 삼킴, 주 기전은 쓰기 거부)를 더하면 침묵형 4건 | food 0 (food에도 outbox·notify 경로가 있음) |
| 오라우팅/DNS | banking 2 | commerce, food 0 |
| 트래픽/용량·스레드·CPU 스로틀·프로브 재시작 | commerce만 | food, banking 0 |
| 디스크 IO | food(노드)만 | — |
| DB 인스턴스 읽기 전용 | MySQL만 | — |
| DB 계정 잠금·프로파일 한도 | Oracle만 | — |
| DB 자체 OOM 재시작 | PostgreSQL만 | — |
| 데이터/상태 오류 | banking 2, commerce 1(quarantine) | food 0 |
| 레이트리밋 | 외부 2(commerce, food), 내부 1(food) | banking 0 |

### 4.3 전파 깊이와 경로

- **깊이 ≥ 4는 0건이다.** 깊이 3인 10건도 대부분 같은 줄기다. "Oracle → transfer → commerce payment → order" settlement 경로 하나다 (f01-p, f15-g, f35-r, f56-r, f57-r, f35-h, f52-r). commerce 쪽 깊이 3은 f01-r 하나(PG → inventory → product → order)다.
- **food의 최대 깊이는 2다** (MySQL → dispatch/restaurant → order).
- **비동기 경로(Kafka, outbox)를 거쳐 전파된 증상은 3건이다** (f04-r, f18-p, f14-p). 셋 다 "정지/손실" 형태다. 느린 소비자, 포이즌 메시지, 리밸런스, 중복 처리는 0건이다.
- **증상 서비스 = 근원 서비스인 깊이 0 케이스가 10건이다.** 그중 "호출 대상이 의심스러워 보이지만 실제로는 호출자 자신의 릴리스가 원인"인 역귀속 함정은 f49-h, f58-r 2건뿐이다.

## 5. 공백 정리

### 5.1 0~2건 카테고리
- **트리거**: 배치/잡 1 · DB 데이터 변경 2(그중 1건 quarantine) · 인프라 자원 장애 2 · 의존 미들웨어 중단 2(1 시나리오).
- **기전**: CPU 1 · 디스크 IO 1 · 스레드풀 1 · 프로브 재시작 루프 1 · 설정 키 누락 1 · 네트워크 차단 2 · 오라우팅/DNS 2 · 타임아웃 2 · 한도 오설정 2.
- **0건**: 서비스 간 네트워크 지연/손실/손상/대역폭, 일시적 파드·컨테이너 kill, 소켓·FD·에페머럴 포트 고갈, 디스크·볼륨 용량 고갈, DB 데드락, DB 서버측 연결 한도, 실행계획 회귀(DDL 없는 통계 변화), 복제·페일오버, Kafka 브로커 장애, 캐시 스탬피드·핫키, 인증서·시크릿 만료, 시계 오차, 노드 NotReady, CoreDNS/CNI, HPA/PDB, 일부 레플리카만 불량, 재시도 폭풍.
- **근원 유형**: k8s 플랫폼 객체 2 · 호스트 방화벽 2 · 노드 3 · 클러스터내 의존 2.
- **증거**: 호스트 syslog 3 · 비즈니스 불변식 4 · DB 세션 7 · Top SQL 8.

### 5.2 과대표집
- **DDL 12건(17%)**: 객체 누락 8, 인덱스 무효·삭제 2, READ ONLY 1, identity 되감기 1.
  - "개발자 PC 마이그레이션이 운영 테이블 drop" 템플릿이 5건이다 (f32-p, f53-r, f53-p, f23-p, f35-h).
  - "백필 취소가 원본 테이블 제거" 템플릿이 2건이다 (f48-p, f48-r).
  - 결정 증거도 같다. 에러 문구 '…doesn't exist / does not exist'와 DPM 테이블·인덱스 수 변화다.
- **롤아웃 28건(39%)**: 결정 증거 패턴이 같다(ScalingReplicaSet + 새 RS spec 또는 이미지 태그). 계약 불일치 6건과 커넥션 누수 3건은 같은 템플릿을 서비스만 바꿔 쓴 것이다.
- **"엔드포인트 0 → Connection refused"**: 7건 중 transfer가 피해 서비스인 것이 4건이다.
- **사용자 증상**: checkout 또는 food 주문 생성이 85%다. 조회 경로·배치·관리 API가 1차 피해인 케이스는 소수다 (f48-r 인기메뉴, f54-r 이력 조회, f20-r 통계, f23-r 재고, f04-r 배송 생성).

### 5.3 다중 근원 · 외부 원인
- **2-근원**: 1개 시나리오(f15-g)다. 동종(락+락), 동시 시작, 두 근원이 같은 요청 경로에 있다.
  - 빠진 형태: 이종 조합, 시차 발생, "원인 + 악화 요인"(예: 결함 + 동시 트래픽 급증), 서로 다른 도메인의 독립 장애가 우연히 겹친 경우.
- **외부·클러스터내 의존 근원 7건이 모두 provisional이다.** 외부 쪽은 결제 게이트웨이 한 종류다.

### 5.4 음성(무결함·양성 변경)과 노이즈
- **음성 0건.** 다음이 모두 없다.
  - 정상 롤아웃이나 설정 변경 직후인데 지표가 기준선 범위 안에 있는 케이스
  - 일시적 부하 상승만 있는 케이스
  - 알람은 울렸지만 원인이 없는 케이스
  - 결함은 다른 곳에 있는데 무관한 롤아웃이 같은 분에 일어난 케이스
- **가장 가까운 것들**:
  - f07-h: 변경이 전혀 없는 용량 초과. 다만 이것도 결함 케이스다.
  - f50-r, f52-r: "루틴 재배포는 트리거일 뿐 원인이 아님"을 가르친다.
  - f59-r: 다른 namespace의 오래된 Job 흔적이 같은 순간 삭제되는 우연한 방해 요소가 있다 (README 기록).
- **설계된 방해 결함은 0건이다.** 공개 o2는 CPU/Memory stress, JVMRuntimeMutator 등을 비근원 공동 주입으로 넣어 둔다.
- **전제 조건**: 음성 케이스를 넣으려면 골든 계약에 "근원 없음" 상태와 채점 규칙이 필요하다. 현재 `expected_status` 값은 confirmed와 provisional뿐이다 (configs/eval 전체 확인).

## 6. 공개 학습 데이터셋과 비교

### 6.1 공개셋 결함 구성 (현재 `data/external/*/captures` 기준)

| 출처 | 케이스 | 시스템 | 근원 결함(필수) 분포 | 공동 주입(비근원) |
|---|---|---|---|---|
| OpenRCA2 o2ts | 271 | TrainTicket (근원 서비스 29종, 그중 mysql 8건은 네트워크 결함) | NetworkPartition 41, NetworkLoss 32, JVMLatency 29, NetworkCorrupt 24, ContainerKill 23, NetworkDelay 17, JVMMemoryStress 17, JVMException 12, HTTPResponseDelay 11, HTTPResponseReplaceCode 10, NetworkBandwidth 10, HTTPResponseAbort 9, HTTPRequestDelay 9, PodFailure 7, HTTPRequestAbort 6, HTTPRequestReplaceMethod 6, HTTPRequestReplacePath 5, HTTPResponseReplaceBody 3, JVMReturn 3, PodKill 2, DNSError 1 · 2-근원 6건 | 69건 (JVMRuntimeMutator 59, DNSError/NetworkDuplicate/Bandwidth 각 3, DNSRandom 1) |
| OpenRCA2 o2hs | 145 | Hotel Reservation (9종) | PodFailure 98, NetworkDelay 45, MemoryStress 6, NetworkPartition 5, CPUStress 3 · 2-근원 12건 | 70건 (MemoryStress 37, CPUStress 33) |
| OpenRCA2 o2ot | 26 | OTel demo (8종) | PodFailure 12, NetworkPartition 5, NetworkDelay 4, PodKill 3, NetworkLoss 1, NetworkCorrupt 1 | 13건 (CPUStress 6 등) |
| RCAEval tt4 | 90 | TrainTicket (5종) | cpu·mem·disk·socket·delay·loss 각 15 | 없음 |
| RCAEval ob4 | 88 | Online Boutique (5종) | cpu 14·mem 14·disk 15·socket 15·delay 15·loss 15 | 없음 |

(결함 이름의 정의: `scripts/openrca2_convert.py` 의 CHAOS_GLOSS(Chaos Mesh), `scripts/rcaeval_convert.py` 의 FAULT_TEXT(stress-ng, tc). OpenRCA2의 2-근원 조합은 PodFailure+PodFailure 4, MemoryStress+PodFailure 4, NetworkDelay+PodFailure 2, JVMLatency+Network* 5 등이다.)

### 6.2 공개 결함 유형 → Polestar 대응

| 공개 결함 유형 | 공개 규모 | Polestar 대응 | 판정 |
|---|---|---|---|
| NetworkDelay / RCAEval delay | 66 + 30 | 외부 hop 지연만 있음 (f19-p, f19-s, f06-r, PG mock) — 클러스터 내부 서비스 간은 0 | **내부 없음** |
| NetworkLoss / RCAEval loss | 33 + 30 | 없음 | **없음** |
| NetworkCorrupt / Duplicate / Bandwidth | 25 / 3 / 13 | 없음 | **없음** |
| NetworkPartition | 51 | 호스트 방화벽으로 새 연결 드롭 (f44-p, f44-r) | 부분 (2) |
| PodFailure / PodKill / ContainerKill | 117 / 5 / 23 | 엔드포인트 0은 모두 **변경이 원인이고 지속형**(probe·scale·quota·이미지). 일시적 kill과 컨트롤러 재생성은 0. f05-h 프로브 kill 루프가 가장 가깝다 | **사실상 없음** |
| CPUStress / RCAEval cpu | 3(+39 공동) + 29 | 컨테이너 CPU limit 스로틀 f09-p 1건 · DB CPU 포화(쿼리) 6건 | 부분 |
| MemoryStress / JVMMemoryStress / RCAEval mem | 6 / 17 / 28 | 누수 OOM f41-r · limit OOM f05-r · 힙 GC f09-h · 노드 eviction f05-p · DB OOM f25-h | 있음 (5) |
| RCAEval disk | 30 | 노드 디스크 IO f10-h | 부분 (1) |
| RCAEval socket | 30 | 없음 | **없음** |
| DNSError / DNSRandom | 1(+3) / (1) | dnsPolicy로 클러스터 DNS 상실 f37-r · 잘못된 호스트 f39-r | 있음 (2) |
| JVMLatency | 29 | 동기화된 렌더러가 만든 코드 지연 f03-h 1건 | 부분 |
| JVMException / JVMReturn | 12 / 3 | 릴리스 코드 결함 (f58-r 예외, f40-h/f49-* 잘못된 출력 형식) | 형태는 다르지만 있음 |
| HTTPResponseReplaceCode | 10 | 외부 429 (f01-h, f06-p) · 내부 429 (f55-r) | 있음 |
| HTTPResponseReplaceBody | 3 | 응답 계약 변경 (f49-p, f49-r, f32-h) | 있음 |
| HTTPRequest/ResponseAbort | 15 | 없음 (연결 거부는 엔드포인트 0형만) | **없음** |
| HTTPRequestReplacePath/Method | 11 | 요청 필드 계약 f30-r, 잘못된 엔드포인트 f39-r | 부분 |
| HTTPRequest/ResponseDelay (내부 엔드포인트) | 20 | 없음 (지연은 외부 mock만) | **없음** |

**공개에 있고 Polestar에 없는 것**
- 내부 네트워크 지연·손실·손상·대역폭 제한
- 일시적 pod/container kill
- 소켓 스트레스
- 컨테이너 내부 CPU·디스크 스트레스
- 내부 HTTP abort와 지연
- 비근원 공동 주입(방해 요소)
- 이종 2-근원

**Polestar에 있고 공개에 없는 것** (공개셋 근원은 사실상 전부 "서비스 파드에 가한 카오스"다)
- DB 계층 원인: 락, 테이블·컬럼 drop/rename, 인덱스 삭제·무효, 인스턴스 읽기 전용, 계정 잠금, 프로파일 한도, identity 되감기, 참조 데이터 삭제, DB 자체 OOM
- 설정·릴리스 변경: env, probe, dnsPolicy, 엔드포인트 URL, 용량 한도, 누수, 계약 변경, 레이트리미터, 비효율 쿼리
- k8s 플랫폼: ResourceQuota 승인 거부, scale 0, 이미지 부재·삭제, Job
- 비동기 파이프라인 정지와 침묵 데이터 손실 (4xx·0-5xx 장애, 비즈니스 불변식이 증거)
- 캐시 장애 폴백 증폭
- 외부 의존 + 경계 서비스 2-근원 표기
- 트래픽 용량 초과
- 도메인·namespace를 넘는 전파
- 결정 증거로서 변경 이력, DB 세션, Top SQL, 호스트 syslog

**형태 비교**
- 공개 o2는 근원 1개 + 비근원 공동 주입 0~1개가 기본이다. 2-근원은 약 4%다.
- RCAEval은 단일 서비스·단일 결함이고, 서비스 5종 × 결함 6종의 격자다.
- Polestar는 근원의 계층(DB, 플랫폼, 노드, 외부)과 트리거는 훨씬 다양하다. 반면 "물리적 결함"(네트워크·프로세스·자원 스트레스) 축은 공개셋보다 얇다.
- 따라서 학생 모델은 공개셋에서 네트워크와 kill 유형을, Polestar에서 변경과 DB 유형을 배운다. 두 분포가 겹치는 지점(예: Polestar 토폴로지 위의 네트워크 지연, 공개셋 위의 DB 원인)은 비어 있다.

## 7. 신규 Polestar 시나리오 제안 (우선순위순, 범주만 기술)

아래는 범주 수준의 설계 방향이다. 기존 케이스 서술을 재사용하지 않았고, 구체 수치·문구는 넣지 않았다.

| 순위 | 도메인 | 근원 유형 | 트리거 | 기전 | 결정 증거 | 메우는 공백 | 공개 대응 |
|---|---|---|---|---|---|---|---|
| P1-1 | 무관 (도메인마다 1건씩 권장) | **없음 (음성)** | 양성 롤아웃·설정 변경 또는 일시적 부하 상승 | 결함 없음. SLI는 기준선 안, 알람만 발생 | 변경 이력 + SLI·트레이스 정상 | 음성 0건, "최근 변경 = 원인" 지름길 | (없음) |
| P1-2 | food 또는 banking | 앱 근원 + **무관한 동시 변경** | 진짜 원인은 DB 또는 인프라, 같은 분에 다른 서비스의 무해한 롤아웃 | 기존 기전 아무거나 + 방해 요소 | DB 세션·노드 메트릭 vs 변경 이력 대조 | 설계된 방해 요소 0건 | 공동 주입(optional) |
| P1-3 | commerce + food (도메인 교차) | **이종 2-근원**, 시차 발생 | 앱 릴리스 결함 + 별개의 노드·자원 장애 | 서로 다른 기전 2개가 겹친 장애 | k8s 이벤트 + 호스트 메트릭, 두 사슬이 독립 | 2-근원 1개(동종) | o2 2-근원 |
| P1-4 | banking 또는 commerce | 앱 파드 네트워크 (노드·CNI 측) | 인프라 장애 (서비스 간 링크 지연·패킷 손실) | 꼬리 지연, 재시도 증폭. 자원은 정상 | 트레이스 client/server 시간 차, 파드 네트워크 오류·재전송 지표 | 내부 네트워크 0건 | NetworkDelay/Loss/Corrupt, RCAEval delay/loss |
| P1-5 | food 또는 banking | 노드 또는 앱 파드 | 인프라 장애 (노드 NotReady·kubelet 중단, 또는 프로세스 비정상 종료) | **일시적** 가용성 상실 후 자동 재생성·복구 | k8s 이벤트(NodeNotReady, 재스케줄, 종료 사유), 노드 syslog | 일시적 kill 0건 | PodFailure/PodKill/ContainerKill |
| P2-6 | food 또는 banking | 앱 (또는 같은 노드의 이웃 워크로드) | 릴리스(바쁜 루프) 또는 공존 배치 | 컨테이너·노드 CPU 포화 → 지연 | 컨테이너 CPU·스로틀 지표, 공존 파드 목록 | CPU 1건(스로틀) | CPUStress, RCAEval cpu |
| P2-7 | commerce | 앱 | 릴리스 (연결 재사용 안 함·FD 미반환) | 소켓·FD·에페머럴 포트 고갈 → 연결 실패 | 컨테이너 소켓 수 지표 + 앱 로그 OS 오류 | 소켓 0건 | RCAEval socket |
| P2-8 | banking 또는 commerce | DB 인스턴스 또는 노드 볼륨 | 배치·로그 증가로 **용량 고갈** (테이블스페이스·WAL·ephemeral) | 쓰기 실패 또는 파드 eviction | DB 인벤토리·볼륨 지표, k8s DiskPressure/Evicted, syslog | 디스크 용량 0건 | RCAEval disk(부분) |
| P2-9 | food | Kafka 소비자 앱 또는 브로커(클러스터내 의존) | 이벤트 스키마 변경 릴리스(포이즌 메시지) 또는 브로커 자원 장애 | 소비 재시도·크래시 루프, lag 증가, 침묵 | 소비자 lag + 소비자 로그 + 브로커 지표 | food 비동기 0건, 브로커 근원 0건 | (없음) |
| P2-10 | food (MySQL) | **배치 Job** + DB | 정기 배치가 큰 트랜잭션으로 행 잠금 (운영자 hold 아님) | 락 대기·데드락 → 풀 고갈 | DB 세션·블로킹 + Job 이벤트 | MySQL 락 0, 배치 1, 데드락 0 | (없음) |
| P3-11 | banking | 외부 의존 (banking에는 현재 외부 hop 없음) | 외부 부분 장애 (일부 비율 5xx, 인증서 만료, 외부 DNS) | 간헐 오류, 핸드셰이크 실패 | 트레이스 외부 span 상태 분포 + 앱 로그 예외 유형 | banking 외부 0, 외부 결함 2종뿐, 간헐 2건 | HTTPResponseReplaceCode/Abort |
| P3-12 | commerce (replicas>1 필요) | k8s 플랫폼 객체(HPA·PDB) 또는 **레플리카 일부** | 오토스케일 설정 변경 또는 한 레플리카만 불량 | 용량 부족, 부분 오류율(1/N) | 파드별 지표·로그 분해, HPA 이벤트 | 레플리카 1개 고정, k8s 객체 2건 | (부분) PodFailure 단일 파드 |

**설계 메모**
- **P1-1, P1-2는 골든 계약 확장이 먼저다.** "근원 없음" 상태, 방해 요소를 근원으로 지목했을 때의 감점 규칙, provisional/confirmed와의 관계를 먼저 정해야 한다. 기존 RL 보상(title + cause 채점)과도 정합을 맞춰야 한다.
- **P1-4, P1-5, P2-6, P2-7은 공개셋 유형을 Polestar 토폴로지와 Polestar 증거 소스로 옮기는 것이다.** 공개 → Polestar 전이를 직접 잴 수 있다. 증거가 로그가 아니라 메트릭과 트레이스가 되도록 하면 결정 증거 쏠림(§3.4)도 함께 완화된다.
- **도메인 배분 원칙**
  - food: 락, 메모리, 가용성, 비동기를 채운다.
  - commerce: 릴리스와 네트워크를 채운다.
  - banking: 트래픽, 외부, CPU를 채운다.
  - 크로스 도메인: commerce→banking 또는 food가 포함된 전파를 최소 1건 넣는다.
- **피해 갈 것**: 테이블 drop 템플릿과 계약 불일치 템플릿의 추가 증설. 이미 과대표집이다.
- 이 분석은 메타 텍스트만 봤다. 각 제안이 실제로 캡처될 수 있는지는 확인하지 않았다 (예: tc netem, 노드 중단, replicas>1 허용 여부). 운영 금지 제약(lucida-for-docker/k8s 레포 변경 금지 등)과 충돌하는지도 주입기 설계 단계에서 다시 확인해야 한다.

## 부록 — 산출물 위치
- 라벨 원본: `<scratchpad>/div/labels.py` (케이스별 6축 + 골든 근원 요약)
- 집계 스크립트: `<scratchpad>/div/tables.py`, `tables2.py` · 범위 내 메타 추출: `extract.py` → `inscope.json` (sealed·excluded 미포함을 assert로 확인)
