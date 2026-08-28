# RCA Student 모델 개발 실험 보고서

> 기준 시점: 2026-08-28 UTC  
> 목적: 시스템 구성 설명보다 **무엇을 학습했고, 실제 추론이 어떻게 달라졌으며, 어떤 RL 실험이 왜 실패했는지**를 기록한다.

## 1. 지금까지의 결론

우리가 만드는 것은 장애 설명문 생성기가 아니라, 관측 도구를 직접 선택하고 근본원인을 증거로 입증하는 RCA Student다.

고정된 학습 순서는 다음과 같다.

```text
검증된 전체 조사 궤적 생성
→ whole-episode SFT
→ SFT 정책을 시작점으로 RL
→ train monitor / development holdout
→ 설정 동결
→ 새 봉인 평가와 Claude·Codex 비교
```

현재까지 확인된 결과는 다음과 같다.

- SFT는 바닐라보다 **같은 조회를 반복하는 행동을 줄이고**, 여러 증거 표면을 연결해 실제 원인 후보에 도달하는 능력을 높였다.
- 그러나 SFT도 결정적 proof를 완성하지 못하는 경우가 많다. 현재 공정 train monitor에서 strict 정답은 `2/18`, 평균 root F1은 `0.222`다.
- RL v1~v3은 SFT보다 퇴행했다. reward가 잘못된 행동까지 강화하거나, 성공 rollout이 부족한 상태에서 전체 실패 궤적을 억제한 것이 주요 원인이다.
- v4의 성공 궤적 replay는 가능성을 보였지만 평가 provenance가 현재 기준을 충족하지 않아 승격 근거로 쓸 수 없다.
- v5에서 ThinkFL에 가까운 progressive DAPO를 구성했지만, 20개 사건 중 11개는 그룹 내 최종 reward가 모두 같고 4개는 모두 0점이었다. 상대정책 학습 신호가 부족했다.
- v6는 이 문제를 우회하기 위한 보수적 offline RPO 실험이다. ThinkFL식 온라인 GRPO 자체는 아니다.

최종 목표는 내부 모델끼리의 개선이 아니다.

```text
내부 승격 조건: RL ≥ SFT ≥ vanilla
최종 목표: 동일 하네스·도구·평가에서 Student ≥ Claude/Codex
```

---

## 2. 실제 추론 변화: 바닐라와 SFT 비교

아래 비교는 같은 하네스, 같은 12개 과거 holdout case, case당 3회, temperature 0, guidance structured output 조건에서 얻은 역사적 진단 결과다.

중요한 제한이 있다. 당시 manifest에는 현재 요구하는 모델 artifact fingerprint와 restore checksum 일부가 없고, 이 결과를 본 뒤 하네스와 RL을 수정했다. 따라서 **최종 봉인 성능 주장이 아니라, 모델 행동이 어떻게 달라졌는지 보여주는 개발 기록**으로만 사용한다.

### 2.1 전체 경향

| 모델 | 실행 | 평균 reward | 평균 root F1 | evidence complete | strict |
|---|---:|---:|---:|---:|---:|
| 바닐라 Muse-Glimmer 30B | 36 | 0.116 | 0.099 | 21/36 | 0/36 |
| whole-episode SFT | 36 | 0.189 | 0.217 | 30/36 | 0/36 |

SFT 이후:

- 평균 reward: `0.116 → 0.189` (`+62%`)
- 평균 root F1: `0.099 → 0.217` (`+119%`)
- evidence complete: `21/36 → 30/36`
- strict 정답: 여전히 `0/36`

즉 SFT는 조사와 원인 후보 식별을 분명히 개선했지만, 결정적 proof까지 완성하는 수준에는 도달하지 못했다.

### 2.2 실제 사례 A — F15-H, 외부 PG rate limit

사건의 핵심 증거는 다음과 같았다.

- `food-delivery-payment`의 외부 `/pay` 호출에서 HTTP 429 발생
- 로그에 `429 Too Many Requests`, `RATE_LIMITED`
- payment 인바운드·아웃바운드 실패가 각각 33건
- 주문·상품 서비스의 5xx로 전파

#### 바닐라 모델

실제 행동:

```text
env_entity(tb-w3)
→ 같은 env_entity 반복
→ 같은 env_entity 반복
→ env_top
→ 다시 같은 env_entity 여러 번
→ env_aggregate
→ env_top
→ insufficient
```

관측 기록상 같은 대상 조회를 여러 번 반복했다. 최종 답은 다음과 같았다.

> sms.network_interface.rx_packets 이상은 보이지만 연관 서비스와 오류 경계를 특정할 근거가 부족하다. 원인 후보를 검증할 수 없다.

결과:

```text
root F1 = 0.0
원인 후보 = 0개
status = insufficient
```

문제는 “모른다”고 답한 것 자체가 아니다. 상위 증거에 payment 429가 있었는데도 호스트 metric 조회에서 벗어나지 못했다.

#### SFT 모델

실제 행동:

```text
env_top
→ commerce-product 확인
→ food-delivery-payment 확인
→ 핵심 evidence 원문 slice
→ "PG" grep
→ payment trace/log 구조 조회
→ 경쟁 서비스 비교
→ provisional 답변
```

최종 원인 주장은 다음과 같았다.

> food-delivery-payment가 외부 PG `/pay` 호출에서 `429 Too Many Requests: RATE_LIMITED`를 받았고, 이 실패가 payment API와 주문·상품 서비스 오류로 전파됐다.

지지 증거로 payment 인바운드·아웃바운드 429, PG 실패 로그를 인용했다.

결과:

```text
root F1 = 0.8
reward = 0.572
status = provisional
```

개선된 점:

- 최초 host metric을 원인으로 고정하지 않았다.
- trace → 서비스 → 외부 PG 로그로 경계를 이동했다.
- HTTP 429를 증상으로만 보지 않고 외부 rate-limit 메커니즘과 연결했다.
- 외부 원인을 typed pseudo-entity로 표현했다.

남은 문제:

- proof type이 `unknown`으로 남아 strict 정답은 아니었다.
- counter evidence와 메커니즘 문장이 충분히 정제되지 않았다.
- evidence complete 판정도 해당 실행에서는 통과하지 못했다.

### 2.3 실제 사례 B — F08-P, 반복 조회에서 원인 후보 식별로

바닐라 모델은 같은 `env_entity`를 4회, `env_top`을 6회 반복한 뒤 `insufficient`로 끝났다.

```text
바닐라: root F1 = 0.0, reward = 0.058
```

SFT 모델은 다음처럼 조사 경로를 바꿨다.

```text
env_top
→ commerce-order 상세
→ trace evidence 원문
→ 구조 query
→ payment grep
→ payment 대상 상세
→ 관련 evidence 비교
```

한 실행에서 정확한 root 집합에 도달했다.

```text
SFT: root F1 = 1.0, reward = 0.512
```

그러나 답변의 proof type이 `unknown`이었고 마지막 자연어 출력도 정리되지 않았다. 이 사례 역시 “완전 성공”이 아니라, **SFT가 탐색은 개선했지만 증명과 출력 완성도는 아직 부족하다**는 증거다.

---

## 3. SFT 데이터는 어떻게 만들었나

### 3.1 데이터 단위

한 턴씩 분리하지 않았다. 한 장애의 전체 multi-turn episode를 하나의 학습 단위로 보존했다.

```text
증상 확인
→ 후보 탐색
→ metric/log/trace 조회
→ 경쟁 가설 비교
→ 결정적 증거 확인
→ support_refs/counter_refs가 포함된 답변
```

현재 채택 데이터:

| 항목 | 수량 |
|---|---:|
| 학습 후보 시나리오 | 23 |
| 채택된 시나리오 | 20 |
| 채택된 전체 궤적 | 24 |
| 전체 assistant turn | 264 |
| 문맥에 남긴 실패 관측 | 27 |
| 보류 시나리오 | 3 |

### 3.2 교사 생성 방식

Claude와 Codex 교사가 먼저 독립적으로 하네스를 조사했다. 실패하면 정답을 바로 노출하지 않고 부족한 조사만 교정했다.

실제 교정 예:

```text
1차 결론:
HTTP 409가 급증했으므로 품절이 원인이다.

비평:
409만으로 정상 품절과 재입고 중단을 구분할 수 없다.

교정:
재고 전체 시계열, RESTOCK 유입, 배치 실패 이벤트를 비교하라.

재시도:
RESTOCK가 사라진 시점과 배치 실패가 선행했고,
재고 고갈이 지속된 뒤 409가 증가했음을 확인한다.
```

저장되는 것은 한 줄짜리 정답이 아니다.

- 실패한 조사 branch
- 실패 이유
- 정답을 노출하지 않는 correction
- correction 이후 retry
- scorer가 채택한 최종 전체 궤적

SFT에는 채택된 최종 궤적만 넣었다. 실패 branch는 향후 recursive training과 preference/critic 데이터에 남겼다.

### 3.3 SFT가 실제로 배운 것

- 한 대상만 반복하지 않고 증거 표면을 이동하는 법
- `env_top → entity/query → 원문 slice → 전용 probe` 조사 패턴
- 존재하는 target·metric·action만 선택하는 법
- 증상과 근본원인을 분리하는 법
- 근거가 약하면 `confirmed` 대신 `provisional/insufficient`를 쓰는 법
- 최종 원인에 `mechanism`, `support_refs`, `counter_refs`를 붙이는 법

현재 공정 train monitor 결과:

```text
6 cases × 3 runs = 18/18 완료
strict = 2/18
majority strict cases = 1/6
mean reward = 0.229
mean root F1 = 0.222
evidence complete = 18/18
format errors = 0
unsupported confirmed = 0
```

좋은 기본 조사 형식은 생겼지만, 원인을 실제로 맞히는 능력은 아직 낮다. RL의 목적은 이 기반을 보존하면서 원인 식별과 proof 완성도를 높이는 것이다.

---

## 4. RL에서 실제로 시도한 것

### 4.1 우리가 원래 원한 RL

사용자가 처음 기대한 방식은 다음과 같다.

```text
SFT 모델에게 실제 하네스 제공
→ 같은 사건을 여러 번 독립 조사
→ scorer가 각 전체 궤적에 점수 부여
→ 그룹 평균보다 좋은 궤적 강화
→ 나쁜 궤적 약화
→ 업데이트된 모델이 다시 새 rollout 생성
→ 반복
```

이 방식이 ThinkFL의 Progressive Multi-Stage GRPO와 더 가깝다. ThinkFL은 Recursion-of-Thought actor가 도구를 사용해 여러 진단 경로를 만들고, multi-factor grader로 상대 reward를 계산한다.

참고:

- [ThinkFL paper](https://arxiv.org/abs/2504.18776)
- [ThinkFL official repository](https://github.com/LLM4AIOps/ThinkFL)

### 4.2 왜 정답 1점 / 오답 0점만으로 부족했나

같은 사건에서 4개 rollout을 만들었다고 하자.

```text
[0, 0, 0, 0]
```

모두 오답이면 그룹 안에서 무엇이 상대적으로 나았는지 알 수 없다. advantage가 모두 0이 되어 업데이트가 사라진다.

그래서 scorer는 다음을 함께 본다.

```text
root-cause F1 / exact root
+ 원인 유형별 proof
+ 올바른 confidence status
+ evidence refs와 mechanism
- unsupported confirmed
- format/schema violation
```

특정 도구를 실행했다는 이유만으로 보상하지 않는다. 예를 들어 `metric_fetch` 자체에 점수를 주면 모델은 원인과 상관없이 해당 도구를 반복해 reward를 해킹할 수 있다.

### 4.3 실험 타임라인

| 실험 | 방법 | 데이터/신호 | 관측 결과 | 다음 결정 |
|---|---|---|---|---|
| SFT | 검증된 전체 episode 모방 | 20 cases, 24 trajectories | 바닐라보다 reward/root F1 개선 | RL 시작 정책으로 고정 |
| RL v1 | whole-episode DAPO | 20 cases × 4 rollouts, 기존 종합 reward | SFT 대비 소폭 퇴행 | 효율·도구 성공 같은 약한 보상 제거 |
| RL v2 | diagnosis-only DAPO | root/proof/status 중심 reward, LR↓, KL↑ | 20개 중 11개 그룹 reward 동일, 4개 전부 0 | 성공 신호 보강 필요 |
| RL v3 | teacher-anchor DAPO | 교사 양성 + hard negative | 역사적 holdout에서 더 큰 퇴행 | 실패 episode 전체 억제 중단 |
| RFT v4 | 성공 episode + 교사 replay | 실패 rollout 제거, 강한 KL | train monitor는 개선 조짐, provenance 불충분 | 공정 승격 근거로 사용하지 않음 |
| v5 | progressive per-turn DAPO | terminal reward + route step reward | 구현/데이터 완성, 상대 성공 신호는 4/20 groups뿐 | on-policy 반복·reward 설계 재검토 |
| v6 | whole-trajectory RPO | 좋은/나쁜 전체 궤적 20쌍 | 학습 완료, 동일 monitor 평가 중 | SFT 비퇴행 여부로 유지/폐기 |

### 4.4 RL v1 — 종합 reward를 그대로 사용

80개 Student rollout을 case별로 정규화해 whole-episode DAPO를 수행했다.

문제:

- 원인 정확도 외에 턴 수와 도구 성공률 같은 약한 신호도 reward 차이를 만들었다.
- 모두 틀린 그룹에서도 “조금 빨리 끝난 오답”이 상대적으로 좋은 episode가 될 수 있었다.
- 전체 episode에 하나의 음수 advantage를 주면, 실패 경로 안에 있던 유용한 탐색 행동까지 함께 억제됐다.

역사적 12-case 결과:

```text
SFT     reward 0.189 / root F1 0.217
RL v1   reward 0.184 / root F1 0.204
```

### 4.5 RL v2 — 진단 중심 reward로 변경

reward를 root F1, exact root, proof, status 중심으로 축소했다. learning rate는 `5e-6 → 2e-6`, KL은 `0.02 → 0.05`로 보수화했다.

결과:

```text
80 rollouts
20 scenario groups
11 groups: optimization reward가 그룹 내 전부 동일
4 groups: reward가 전부 0
strict success: 7/80
```

역사적 holdout:

```text
RL v2   reward 0.177 / root F1 0.190
```

reward 방향은 더 정확해졌지만 학습 가능한 상대 차이가 크게 줄었다.

### 4.6 RL v3 — 교사 anchor 추가

모든 rollout이 틀린 사건에 검증된 교사 궤적을 양성 anchor로 넣고, Student의 hard negative와 비교했다. learning rate `1e-6`, KL `0.10`을 사용했다.

역사적 holdout:

```text
RL v3   reward 0.154 / root F1 0.138
```

가장 크게 퇴행했다.

가능성이 높은 원인:

- 교사와 Student 분포 차이가 큰 상태에서 anchor가 정책을 안정적으로 연결하지 못했다.
- 실패 episode의 모든 token을 음수로 밀면서, 그 안의 정상적인 `env_top`, `env_query`, evidence 읽기까지 억제했다.
- 성공보다 실패가 많은 작은 데이터에서 negative gradient가 조사 정책 전체를 손상시켰다.

### 4.7 RFT v4 — 실패를 밀지 않고 성공만 replay

실패 episode에 음수 gradient를 주지 않았다. 검증된 교사 궤적과 exact-root Student 성공만 재학습했다.

```text
33 positive episodes
learning rate = 1e-7
KL = 0.50
```

6-case train monitor에서:

```text
SFT     reward 0.229 / root F1 0.222
RFT v4  reward 0.281 / root F1 0.333
```

다만 당시 RFT manifest에는 현재 필수인 model SHA, restore SHA, 동일 actor SHA가 완전하지 않았다. 따라서 “개선 가능성”으로만 남기고 정식 승격하지 않았다.

### 4.8 v5 — ThinkFL에 가까운 progressive DAPO

최종 정답 reward만 쓰지 않고 각 조사 turn에도 제한적인 route credit을 계산했다.

예:

```text
새로운 증거를 얻음                     +
정답 root와 연결된 결정적 조회          +
실패한 도구 호출                        -
같은 action/target 반복                  -
최종 root/proof/status                   terminal reward
```

그러나 실제 데이터에서는:

```text
20 groups 중 terminal reward가 다른 그룹 = 9
성공과 실패가 함께 존재한 그룹 = 4
all-zero terminal groups = 4
```

또한 gold root를 직접 이용해 특정 target 조회에 step reward를 주는 방식은 모델이 인과 추론 대신 gold-target 접근 패턴을 외울 위험이 있다. v5는 구현과 데이터 진단에는 의미가 있었지만 최종 후보로 승격하지 않았다.

### 4.9 v6 — offline whole-trajectory RPO

v6는 온라인 RL이 아니다. 저장된 rollout으로 좋은/나쁜 전체 궤적 쌍을 만든 보수적 선호학습이다.

```text
chosen:
검증된 Student 성공, 없으면 accepted Claude/Codex teacher

rejected:
틀린 Student 중 reward가 가장 높은 hard negative
```

데이터:

```text
20 pairs
teacher chosen = 16
student chosen = 4
20 optimization steps
learning rate = 5e-7
beta = 0.10
imitation anchor = 0.005 × beta
```

학습 로그의 preference margin은 약 `-0.0029 ~ +0.0029`로 매우 작았고 loss도 대체로 `0.693` 부근이었다. 강한 변화보다 SFT 비퇴행을 우선한 실험이다.

추가로 발견한 기술 문제:

- `target_modules: all-linear`가 언어 모델뿐 아니라 vision tower에도 LoRA를 삽입했다.
- vision LoRA B tensor 304개는 모두 0이었지만 adapter 구조에는 남았다.
- multimodal vLLM의 LoRA profiling 중 shape assertion이 발생해 adapter 직접 serving이 실패했다.
- 평가를 위해서만 임시 merge를 만들었고, 정식 artifact는 adapter로 유지했다.
- 다음 실험부터는 `model.language_model.*`의 attention/MLP projection만 대상으로 제한해야 한다.

현재 v6는 SFT와 동일한 6-case × 3회 monitor 평가 중이다. 비퇴행 게이트를 하나라도 실패하면 승격하지 않는다.

---

## 5. 앞으로의 RL: 진짜 on-policy progressive GRPO

v6가 통과하더라도 최종 구조는 offline pair 학습에 머물지 않는다. 다음 목표는 ThinkFL 방향의 실제 환경 RL이다.

```text
1. 현재 SFT/RL actor를 하네스에 배포
2. train case마다 4~8개 stochastic rollout 생성
3. typed scorer로 전체 궤적 채점
4. 같은 case 그룹 안에서 advantage 계산
5. 언어 모델 전용 LoRA 업데이트
6. 업데이트된 최신 adapter로 새 rollout 생성
7. train monitor 비퇴행 확인
8. 반복
```

필수 계약:

- rollout은 항상 현재 학습 정책에서 생성한다.
- prompt/action/observation/final answer를 포함한 전체 episode를 보존한다.
- rollout-time policy identity, sampling seed, temperature, token logprob를 기록한다.
- root를 못 맞힌 episode에 긴 조사라는 이유만으로 양의 terminal reward를 주지 않는다.
- 특정 도구 사용 자체를 보상하지 않는다.
- 같은 조회 반복, invalid action, 근거 없는 confirmed는 명시적으로 감점한다.
- 모든 rollout이 0점인 그룹은 억지 gradient를 만들지 않는다. teacher correction 또는 새 exploration으로 다시 수집한다.

Progressive stage 제안:

| 단계 | 우선 학습 신호 | 승격 조건 |
|---|---|---|
| A. 실행 안정화 | schema, valid action, evidence refs | 형식 오류 0, invalid action 0 |
| B. 원인 식별 | root F1, exact root, 증상/원인 분리 | SFT root F1 비퇴행 |
| C. 증명 | proof rule, mechanism, support/counter refs | strict와 proof rate 개선 |
| D. 효율 | 중복 조회·불필요 턴 감소 | 정확도 비퇴행 상태에서만 비용 감소 |

---

## 6. 하네스가 학습에 제공하는 것

문서의 중심은 실험이지만, 결과를 해석하려면 다음 네 요소는 필요하다.

### Capability registry

현재 사건에서 실제로 존재하는 `target`, `metric`, `action`, `source`, `kind` 목록이다. 모델의 생각을 제한하지 않고 실행 가능한 행동만 검증한다.

### Evidence store와 evidence map

원본 metric/log/trace/event를 전부 보존하는 저장소와 ID 인덱스다. 프롬프트 응답만 cap하며 원본을 300개로 절단하지 않는다.

### Typed ledger

Student가 실제로 수행한 모든 성공·실패 도구 호출과 관측을 episode 안에 append하는 기록이다.

```json
{
  "turn": 4,
  "action": "env_query",
  "target": "food-delivery-payment",
  "ok": true,
  "evidence_refs": ["ev010", "ev011", "ev210", "ev221"]
}
```

현재는 episode 메모리와 `agent-*.jsonl`에 남는다. 중앙 DB와 중간 resume 저장소는 아직 없다.

### Typed scorer

최종 답과 ledger를 함께 읽는다. root 정확도뿐 아니라 실제로 조회한 evidence를 인용했는지, proof rule이 맞는지, unsupported confirmed인지 검사한다. SFT 데이터 채택, RL reward, 평가가 같은 계약을 사용한다.

---

## 7. 평가 원칙과 현재 한계

### 모델 승격 조건

후보는 같은 case, 같은 반복 수, 같은 actor, 같은 generation 설정, 같은 restore, 같은 scorer에서 다음을 모두 비퇴행해야 한다.

- majority strict cases
- strict runs
- mean reward
- mean root F1
- evidence complete
- format error = 0
- unsupported confirmed = 0

평균 reward 하나만 올라도 다른 핵심 지표가 내려가면 탈락이다.

### 기존 12개 “봉인셋”의 상태

12개 case는 원래 평가 전용으로 분리했지만, 바닐라·SFT·RL v1~v3 결과를 반복 확인하고 그 결과로 학습 방식을 수정했다. 따라서 연구적으로는 더 이상 완전히 봉인된 최종 test가 아니다.

앞으로의 올바른 사용:

```text
현재 20 train cases       → rollout·학습
현재 6-case monitor       → 빠른 비퇴행 확인
기존 12 cases             → development holdout
새로운 미노출 failure families → 최종 sealed parity test
```

Claude·Codex와의 최종 비교는 새 봉인 case를 누구도 튜닝에 사용하지 않은 상태에서 한 번 수행해야 한다.

---

## 8. 현재 위치와 다음 결정

| 작업 | 현재 상태 |
|---|---|
| Student 하네스 핵심 계약 | 구현 완료 |
| 교사 데이터 | 20 cases / 24 accepted trajectories |
| whole-episode SFT LoRA | 완료 |
| SFT 공정 monitor | 완료, strict 2/18, root F1 0.222 |
| RL v1~v3 | 퇴행, 폐기 |
| RFT v4 | 가능성만 확인, provenance 불충분 |
| progressive v5 | 데이터/알고리즘 진단 완료, 미승격 |
| offline RPO v6 | 학습 완료, 공정 monitor 진행 중 |
| online progressive GRPO | 다음 핵심 구현/학습 단계 |
| 최종 새 봉인 평가 | 미실행 |
| Claude·Codex parity | 미검증 |

바로 다음 결정:

1. v6 monitor를 끝낸다.
2. `RL v6 ≥ SFT`를 모든 승격 지표로 판정한다.
3. 실패하면 v6를 폐기하고 online progressive GRPO로 이동한다.
4. 통과해도 v6는 보수적 중간 후보일 뿐이며, on-policy RL과 비교한다.
5. 최종 후보 설정을 동결한 뒤 새 봉인 failure family에서 바닐라·SFT·RL·Claude·Codex를 동일 조건으로 비교한다.

현재 결론은 명확하다.

> **SFT는 바닐라의 반복·정체 행동을 줄이고 실제 원인 후보 식별을 개선했다. 하지만 strict RCA는 아직 약하다. 초기 RL은 이 기반을 손상시켰고, 현재 v6는 안전한 우회 실험이다. 다음 핵심은 최신 Student가 실제 하네스에서 반복 탐색하고 scorer reward로 갱신되는 on-policy progressive GRPO다.**

---

## 9. 근거 artifact

| 내용 | 파일 |
|---|---|
| 역사적 바닐라 평가 | `outputs/goal-eval/baseline-evidence-contract-v2.json` |
| 역사적 SFT 평가 | `outputs/goal-eval/sft-evidence-contract-v2.json` |
| SFT 공정 train monitor | `outputs/goal-eval/sft-fair-train-monitor.json` |
| RL v1/v2/v3 평가 | `outputs/goal-eval/rl.json`, `rl-v2.json`, `rl-v3.json` |
| RFT v4 monitor | `outputs/goal-eval/rft-v4-train-monitor.json` |
| DAPO v1~v5 데이터 | `outputs/rl/dapo-v1.jsonl` 등 |
| v6 preference data | `outputs/rl/rank-refinement-v6.jsonl` |
| v6 config | `configs/rl/muse-glimmer-30b-rank-refinement-v6.yaml` |
| 실제 바닐라/SFT episode | `outputs/eval/baseline-v6b-guidance-20260827`, `sft-v6b-guidance-20260827` |
