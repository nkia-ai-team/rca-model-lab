# RCA Student 모델 개발 개요

> 기준 시점: 2026-08-28 UTC  
> 이 문서는 프로젝트에 처음 참여한 사람이 현재 목표, 데이터, 학습 방식, 실패한 실험, 다음 작업을 빠르게 이해하도록 작성했다.

## 1. 한 문장 요약

RCA Student는 장애를 설명만 하는 모델이 아니다. 로그·메트릭·트레이스·이벤트를 직접 조회하고, 근본원인과 증거를 구조화해서 제출하는 모델이다.

## 2. 지금 알아야 할 내용

| 질문 | 답 |
|---|---|
| 무엇을 만드는가? | 운영 장애를 직접 조사하는 RCA 전용 모델 |
| 모델에게 무엇을 주는가? | 장애 시간, 조회 가능한 대상, 실행 가능한 도구 |
| 무엇을 주지 않는가? | 정답 원인, 정답 증거 ID, 올바른 조사 순서 |
| 모델은 무엇을 제출하는가? | 원인, 발생 과정, 지지·반대 증거 ID, 증명 방식, 확신 상태 |
| 기본 학습 순서는? | 다중 턴 조사 기록으로 SFT → SFT 모델을 초기 정책으로 온라인 RL |
| 최종 목표는? | 같은 조건에서 Student가 Claude·Codex와 비슷하거나 더 높은 RCA 성능 달성 |

### 현재 상태

- 계약 검사를 통과한 `20개 시나리오 / 23개 전체 궤적`으로 SFT v3 학습 완료
- `69/69 step`, `3 epoch`, 최종 train loss `0.72`
- 최종 LoRA와 데이터·설정·manifest 체크섬 검증 완료
- SFT 봉인 평가 진행 중
- 다음 단계: SFT LoRA를 초기 정책으로 사용하는 Prime-RL 온라인 GRPO

아직 최종 성능이 확정된 것은 아니다. SFT v3와 RL 모델은 동일한 봉인 평가를 통과해야 한다.

### 이 문서에서 사용하는 표준 용어

코드에 있는 내부 이름을 그대로 쓰지 않고, 일반적으로 쓰이는 용어를 우선 사용한다.

| 이 문서의 용어 | 뜻 | 코드에서 보이는 이름 |
|---|---|---|
| 시나리오 | 정답과 관측 데이터가 준비된 장애 사건 1건 | case, scenario |
| 다중 턴 궤적 | 장애 입력부터 여러 도구 호출과 최종 답까지의 전체 기록 | episode, trajectory |
| 모델 실행 | 한 시나리오를 처음부터 끝까지 한 번 조사한 결과 | rollout, run |
| 행동 공간 | 모델이 선택할 수 있는 도구와 유효한 인자 목록 | capability registry |
| 증거 인덱스 | 원본 관측 데이터에 ID와 메타데이터를 붙인 조회 목록 | evidence map |
| 실행 이력 | 도구 호출과 반환 결과를 시간순으로 저장한 기록 | typed ledger |
| 규칙 기반 평가 함수 | 정답, 증거, 검증 조건을 채점하고 RL 보상을 계산하는 함수 | typed scorer |
| 완전 정답 | 원인, 증거, 검증 조건, 답변 형식을 모두 통과한 결과 | strict correct |
| 사건 단위 통과 | 같은 시나리오를 3회 실행해 2회 이상 완전 정답인 경우 | majority strict |
| 잠정 결론 | 유력한 원인은 찾았지만 결정적 검증을 끝내지 못한 상태 | provisional |

`typed`, `registry`, `ledger`는 구현 방식에 가까운 표현이다. 이후 본문에서는 각각 `스키마 검증`, `행동 공간`, `실행 이력`으로 설명한다.

## 3. 산출물 관리 위치

| 관리 대상 | 위치 | 관리 내용 |
|---|---|---|
| 모델·데이터 | [Hugging Face · nkia-ai-lab](https://huggingface.co/nkia-ai-lab) | 기본 모델 정보, LoRA adapter, 학습·평가 데이터셋, dataset card |
| 코드 | [GitHub · rca-model-lab](https://github.com/nkia-ai-team/rca-model-lab) | 하네스, scorer, 데이터 생성, SFT/RL, 평가 코드와 설정 |
| 학습 파라미터·실험 결과 | [Weights & Biases · nkia-ai](https://wandb.ai/nkia-ai/projects) | 학습률, 배치, loss, 보상, GPU 지표, 실험별 비교 |

모델을 재현하려면 세 위치의 식별자를 함께 남긴다.

```text
Git commit
+ Hugging Face model/dataset revision
+ W&B run ID
+ training config checksum
+ evaluation manifest
```

## 4. 실제 사례로 보는 학습 전후 차이

### 사건 F15-H

같은 시간에 commerce checkout과 food 주문이 함께 실패했다.

실제 근본원인은 두 개였다.

1. commerce inventory DB의 `EXCLUSIVE lock`
2. 외부 PG `/pay`의 HTTP 429 `RATE_LIMITED`

두 장애는 동시에 보였지만 하나의 공통 원인이 아니었다. 모델은 두 원인을 모두 찾고 분리해야 했다.

### 바닐라 모델

```text
env_entity(tb-w3)
→ 같은 대상 반복 조회
→ env_top
→ 다시 host 조회
→ insufficient
```

결과:

```text
근본원인 F1 = 0.0
원인 후보 = 0개
status = insufficient
```

상위 증거에 payment 429가 있었지만 호스트 메트릭에서 다른 증거 표면으로 이동하지 못했다.

### 다중 턴 궤적 SFT 모델

```text
env_top
→ payment 대상 확인
→ 관련 evidence 원문 조회
→ PG 오류 검색
→ trace와 log 비교
→ 외부 PG 원인 제출
```

실제 확인한 증거:

```text
ev010: payment 외부 호출 HTTP 429 33건
ev011: payment 인바운드 HTTP 429 33건
ev221: PG /pay failed ... 429 Too Many Requests ... RATE_LIMITED
```

결과:

```text
보상 = 0.572
근본원인 F1 = 0.8
status = provisional
완전 정답 = 아님
```

개선된 점:

- 동일 대상 반복에서 벗어났다.
- 외부 PG 경계와 실제 증거 ID를 찾았다.
- 증거가 부족하므로 `confirmed` 대신 `provisional`을 제출했다.

남은 문제:

- inventory DB lock이라는 독립된 두 번째 원인을 놓쳤다.
- 원인 종류별 결정적 검증 조건을 충족하지 못했다.
- 따라서 완전한 RCA 정답은 아니었다.

이 사례의 결론은 단순하다. SFT는 조사 경로를 개선했다. 그러나 모든 독립 원인을 찾고 증명하는 능력은 아직 부족하다.

## 5. 학습 데이터

### 데이터 단위

각 턴을 서로 독립된 학습 샘플로 취급하지 않는다. 질문부터 최종 답까지의 다중 턴 조사 기록을 하나의 학습 단위로 묶는다.

```text
장애 입력
→ 후보 탐색
→ 도구 호출
→ 관측
→ 가설 수정
→ 추가 검증
→ 최종 RCA 답변
```

앞선 관측이 다음 도구 선택의 이유이므로 전체 순서를 보존해야 한다.

### 현재 SFT v3 데이터

| 항목 | 수량 |
|---|---:|
| 시나리오 | 20 |
| 다중 턴 궤적 | 23 |
| 전체 turn | 254 |
| 문맥에 보존한 실패 관측 | 27 |

### 교사 데이터 생성

Claude와 Codex 교사가 하네스를 직접 조사한다. 첫 시도가 틀리면 정답을 바로 제공하지 않고 부족한 검증을 지적한 뒤 다시 조사하게 한다.

실제 예:

```text
1차 판단:
HTTP 409가 증가했으므로 정상 품절이다.

비평:
409만으로 정상 품절과 재입고 중단을 구분할 수 없다.

교정:
재고 전체 시계열, RESTOCK 유입, 배치 실패 이벤트를 비교한다.

재시도:
RESTOCK 중단과 배치 실패가 먼저 발생했고,
재고 고갈 이후 409가 증가했음을 확인한다.
```

SFT에는 검증을 통과한 전체 궤적을 사용한다. 실패·비평·교정 기록은 향후 비평 모델과 선호학습 데이터로 보존한다.

## 6. 모델이 사용하는 조사 도구

모델은 자유롭게 추론할 수 있다. 실제 실행되는 action과 최종 답만 Pydantic 기반 enum·schema로 제한한다.

| 도구 | 역할 | 실제 사용 예 |
|---|---|---|
| `env_top` | 장애 창에서 큰 변화와 후보 대상을 조회 | 오류가 급증한 서비스·호스트·DB 후보 확인 |
| `env_entity` | 한 대상의 관련 evidence 조회 | `food-delivery-payment`의 trace·log·event 확인 |
| `env_query` | target/source/kind/time 조건으로 구조 조회 | payment의 trace error만 시간 범위로 필터링 |
| `env_slice` | 선택한 evidence ID의 원문 조회 | `ev221` 로그 원문 확인 |
| `env_grep` | 원문 키워드 검색 보조 | `PG /pay failed` 반복 패턴 확인 |
| `metric_describe` | 사용 가능한 메트릭 정의 확인 | 자유 문자열 대신 registry의 metric 확인 |
| `metric_fetch_raw` | 요약하지 않은 원본 시계열 조회 | 재고·RESTOCK 변화 전체 확인 |
| `metric_compare` | 기준 시간과 장애 시간 비교 | 평상시 0건과 장애 시 33건 비교 |
| `probe_db_blocking` | DB blocking 관계 확인 | blocker, waiter, lock mode 확인 |
| `probe_traces` | 호출 경계와 오류 전파 확인 | 외부 429가 payment 응답으로 이어졌는지 확인 |
| `probe_changes` | 장애 직전 변경 확인 | 배포·설정 변경이 증상보다 먼저 발생했는지 확인 |
| `answer` | 구조화된 최종 RCA 제출 | 원인별 발생 과정, 증거 ID, 검증 방식, 확신 상태 제출 |

`env_grep`은 보조 수단이다. 주 조회 방식은 target/source/kind/time 기반 구조 조회다.

## 7. 하네스 내부 구성

### 행동 공간과 인자 검증

이번 사건에서 실행할 수 있는 도구, 대상, 데이터 종류, 메트릭을 목록으로 관리한다. 모델이 목록에 없는 메트릭이나 대상을 만들어 내면 Pydantic 스키마와 실제 목록을 대조해 실행 전에 거절한다. 코드에서는 이 목록을 `capability registry`라고 부른다.

### 원본 증거 저장소와 증거 인덱스

로그·메트릭·트레이스·이벤트 원본을 모두 보존한다. 각 관측에는 `ev221` 같은 ID와 대상·종류·시간 메타데이터가 붙는다. 코드의 `evidence map`은 원본을 요약한 문서가 아니라, 원본을 조건으로 찾고 ID로 다시 읽기 위한 인덱스다. 프롬프트와 한 번의 조회 결과만 크기를 제한하고 원본 저장소는 자르지 않는다.

### 실행 이력

모델이 실행한 모든 도구 호출과 반환 결과를 순서대로 저장한다. 코드에서는 `typed ledger`라고 부른다. 별도 장기 데이터베이스가 아니라 실행 중 메모리에 구조화된 상태로 누적하고, 실행 완료 후 다중 턴 궤적 파일에 함께 저장한다.

```json
{
  "turn": 4,
  "action": "env_slice",
  "target": "ev221,ev212,ev210",
  "ok": true,
  "evidence_refs": ["ev210", "ev212", "ev221"],
  "progress": true
}
```

실패·중복 호출도 삭제하지 않는다. 평가 함수와 RL이 전체 조사 과정을 판단하는 데 필요하다.

### 규칙 기반 평가 함수와 RL 보상

코드에서는 `typed scorer`라고 부른다. 최종 문장만 보지 않고 실행 이력과 답을 함께 읽어 다음 항목을 채점한다.

- 정답 근본원인 집합 일치도
- 원인 종류별 결정적 검증 규칙
- `support_refs`와 `counter_refs`의 유효성
- 증상에서 근본원인으로 이어지는 mechanism
- `confirmed`, `provisional`, `insufficient` 상태의 타당성
- 중복 호출·유효하지 않은 호출·근거 없는 과확신

특정 도구를 실행했다는 사실 자체에는 보상하지 않는다. 그렇지 않으면 모델이 도구 호출만 반복해 보상을 얻을 수 있다.

## 8. SFT 학습: 무엇을 정답으로 두고 어떤 loss를 사용했는가

### 학습 입력과 정답

한 학습 샘플에는 같은 시나리오의 모든 조사 턴이 들어간다. 각 턴의 입력은 실제 실행 때와 같은 시스템 지침과 현재까지의 상태이며, 정답은 교사가 그 시점에 생성한 구조화된 도구 호출 또는 최종 답이다.

```text
입력: 시스템 지침 + 장애 정보 + 이전 도구 결과
정답: 다음 도구 호출 JSON 또는 최종 RCA JSON
```

### SFT loss

사용한 loss는 다음 토큰 예측용 cross-entropy다. 다만 모든 토큰에 loss를 걸지 않는다.

Cross-entropy는 교사가 실제로 출력한 다음 token에 모델이 낮은 확률을 줄수록 커진다. 학습은 이 값을 낮춰 같은 문맥에서 교사의 도구 호출과 답변을 더 높은 확률로 생성하게 만든다.

- 교사의 `assistant` 출력 토큰: loss 계산
- 시스템 지침, 사용자 입력, 도구 관측: loss에서 제외
- 한 턴이 길다는 이유만으로 그 턴 전체가 과도하게 반영되지 않도록, 전체 궤적의 assistant 토큰 수로 평균
- 한 시나리오에 승인된 교사 궤적이 여러 개 있어도 시나리오별 총 가중치는 동일하게 조정

수식으로 쓰면 다음과 같다.

```text
SFT loss
= 시나리오 가중치
  × (전체 턴의 assistant-token cross-entropy 합)
  ÷ (해당 궤적의 assistant token 수)
```

도구가 반환한 로그나 메트릭을 그대로 재생성하도록 학습하지 않는다. 관측을 읽고 다음 행동을 선택하는 assistant 출력에만 loss를 건다.

### 현재 SFT v3 설정

| 항목 | 값 | 이유 |
|---|---:|---|
| 기본 모델 | Muse-Glimmer 30B | 현재 Student 기반 모델 |
| 시나리오 / 궤적 | 20 / 23 | 계약 검증을 통과한 데이터만 사용 |
| epoch | 3 | 전체 데이터를 세 번 학습 |
| learning rate | `1e-4` | LoRA SFT 업데이트 크기 |
| micro batch | 1개 궤적 | 조사 순서와 긴 문맥 보존 |
| gradient accumulation | 1 | 23개를 누락·중복 없이 epoch마다 한 번씩 사용 |
| 최대 문맥 | 32,768 token | 실제 하네스 문맥 보존 |
| LoRA | rank 8, alpha 16, dropout 0 | H200 메모리와 안정성 고려 |
| LoRA 적용 범위 | 언어 모델의 attention·MLP projection | 텍스트 RCA에 필요 없는 vision encoder 제외 |
| loss 구현 | assistant-only fused cross-entropy | 전체 vocabulary logits 메모리 사용 감소, 수학적으로 같은 CE |

최종 train loss `0.72`는 교사 assistant 토큰의 평균 cross-entropy다. RCA 정확도 점수가 아니다. 정확도는 별도 봉인 평가로 판단한다.

### SFT 버전별 변경

| 버전 | loss | 주요 변경 | 결과 |
|---|---|---|---|
| v1 | assistant-only cross-entropy | rank 16, 모든 linear layer, dropout 0.05 | 최초 행동 모방 가능성 확인 |
| v2 | 같은 cross-entropy + 시나리오 균형 가중치 | rank 8, dropout 0, 24개 궤적 | 메모리 개선. 이후 계약 위반 궤적 1개 발견 |
| v3 | 같은 cross-entropy + 시나리오 균형 가중치 | 위반 궤적 제거, 23개, 언어 모델 LoRA만 적용, fused CE | 학습 산출물 검증 완료. 봉인 평가 진행 중 |

## 9. RL 실험: 각 버전에서 무엇을 loss로 사용했는가

아래 결과는 이전 개발 평가다. 현재 SFT v3의 봉인 결과와 혼동하면 안 된다.

### RL v1~v5의 공통 policy loss

v1~v5는 DAPO/PPO 계열의 제한된 정책경사 loss를 사용했다. 정책경사는 보상이 높은 행동의 생성 확률을 높이고 낮은 행동의 확률을 낮추는 RL 학습 방식이다.

```text
확률비 r = 현재 정책의 token 확률 / 모델 실행을 생성한 이전 정책의 token 확률
policy loss = -min(r × advantage, clip(r) × advantage)
total loss = policy loss + β × KL(현재 정책, 고정 SFT 기준 정책)
```

`advantage`가 양수인 궤적의 assistant 토큰 확률은 높이고, 음수인 궤적은 낮춘다. `clip`은 한 번의 업데이트가 너무 커지는 것을 막고, KL 항은 정책이 SFT 모델에서 지나치게 멀어지는 것을 막는다. 시스템·관측 토큰은 RL loss에서도 제외했다.

| 단계 | reward와 advantage 구성 | 실제 loss 설정 | 관측 결과와 판단 |
|---|---|---|---|
| RL v1 | 전체 평가 reward를 같은 사건의 다른 실행과 비교해 표준화 | clipped policy loss, KL `β=0.02`, LR `5e-6` | 도구 성공률·턴 수 같은 약한 지표 차이도 advantage가 되어 오답 행동을 강화. SFT 대비 퇴행 |
| RL v2 | 원인 F1, 원인 완전 일치, 증거 검증, 상태 정확성 중심의 진단 reward만 사용 | 같은 loss, KL `β=0.05`, LR `2e-6` | 같은 사건의 실행이 모두 같은 점수이거나 모두 0인 그룹이 많아 advantage가 0. 학습 신호 부족 |
| RL v3 | 교사 궤적 `advantage=+1`, 가장 낮은 Student 궤적 `-1` | 같은 loss, KL `β=0.10`, LR `1e-6` | 실패 궤적 안의 올바른 탐색 토큰까지 모두 음수로 학습해 더 크게 퇴행 |
| RFT v4 | 교사 궤적 `+1`, 원인을 정확히 찾은 Student 궤적만 `+0.5`; 실패 궤적 제외 | 같은 policy loss, KL `β=0.50`, LR `1e-7` | 부정확한 음수 학습을 제거. 개발 평가 개선 가능성은 있었으나 모델·데이터·설정의 동일성 증명이 부족해 승격하지 않음 |
| RL v5 | 최종 진단 보상의 사건 내 상대점수에 신규 증거·중복·실패의 작은 턴별 보정을 추가 | 같은 loss, KL `β=0.10`, LR `1e-6` | 실행 단계별 보상 분배를 시도. 정답 대상 접근 자체를 보상하면 점수만 얻는 행동을 학습할 수 있어 현재는 사용하지 않음 |

### 보상 계산 예

v2 이후 최종 진단용 학습 reward는 다음 요소를 사용했다.

```text
0.55 × root-cause F1
+ 0.15 × root 완전 일치
+ 0.15 × 검증 충족률 × root-cause F1
+ 0.10 × 상태 정확성 × root-cause F1
+ 0.05 × 완전 정답
- 0.60 × 근거 없는 confirmed
- 0.10 × 형식 오류
```

값은 `0~1`로 제한한다. 같은 시나리오의 여러 실행 안에서 평균을 빼고 표준편차로 나눠 advantage를 만든다. 따라서 모두 같은 점수면 advantage가 전부 0이 된다.

### RL v6: 오프라인 선호학습

v6는 정책경사가 아니라 RPO 선호학습 loss를 사용했다. 각 시나리오에서 검증된 성공 궤적 또는 교사 궤적을 선호 응답(`chosen`), 점수가 가장 높은 실패 궤적을 비선호 응답(`rejected`)으로 구성했다.

```text
RPO loss
= DPO loss(chosen의 상대 확률 > rejected의 상대 확률)
+ 0.005 × 0.10 × chosen negative log-likelihood
```

설정은 `beta=0.10`, `imitation_eta=0.005`, LR `5e-7`, 1 epoch였다. 두 번째 항은 작은 데이터에서 성공 행동 자체가 지워지는 것을 줄이기 위한 imitation loss다.

### v6가 실패한 이유

v6는 저장된 좋은 궤적과 나쁜 궤적을 비교했다.

```text
chosen episode의 확률 상승
rejected episode의 확률 하락
```

그러나 학습 중 실제 하네스에서 새 모델 실행을 만들지 않았다. 평균 보상은 `0.2285 → 0.2303`으로 거의 유지됐지만 사건 단위 통과가 `1/6 → 0/6`으로 떨어졌다. 평균값이 사건 단위 안정성 퇴행을 가렸으므로 승격하지 않았다.

## 10. 다음 RL: Prime-RL 온라인 GRPO

다음 RL은 현재 모델이 실제 하네스에서 새 조사 결과를 생성하고, 그 점수로 바로 정책을 갱신한다.

```text
SFT v3 LoRA
→ 같은 RCA 사건을 8회 조사
→ 규칙 기반 평가 함수가 각 전체 궤적 채점
→ 같은 사건 안에서 상대 advantage 계산
→ 언어 모델 LoRA 갱신
→ 갱신된 LoRA로 새 모델 실행 생성
→ 반복
```

점수 예:

| Rollout | 결과 | 예시 점수 |
|---|---|---:|
| A | 모든 근본원인·검증·증거 충족 | 1.00 |
| B | 근본원인은 맞지만 검증 부족 | 0.65 |
| C | 증상만 식별 | 0.25 |
| D | 원인 오류와 근거 없는 과확신 | 0.00 |

0/1 점수만 사용하면 한 그룹이 `[0, 0, 0, 0]`일 때 advantage가 모두 0이 된다. 따라서 근본원인·검증·증거 기반 중간 점수를 사용하되, 도구 실행 자체는 보상하지 않는다.

### 현재 Prime-RL smoke의 loss

Prime-RL에서 `GRPO`는 같은 사건의 8개 실행을 묶어 상대점수(`advantage`)를 만드는 부분이다. 실제 token 업데이트는 Prime-RL의 IPO loss를 사용한다.

```text
정책 항 = -상대점수 × (학습 중 token 확률 / 생성 당시 token 확률)
신뢰영역 = 두 token 확률 차이가 eps=0.1보다 크면 해당 token 제외
안정화 항 = 0.001 × (학습 중 log 확률 - 생성 당시 log 확률)²
```

즉 `사건별 8개 실행 → GRPO 상대점수 계산 → IPO token loss로 LoRA 업데이트` 구조다. 연결 시험 설정은 사건당 8회, 배치 8, 학습률 `1e-6`, 최대 생성 2,048 token, 3회 업데이트다. 연결 시험은 loss 계산과 LoRA 갱신 검증용이며 최종 성능 학습이 아니다.

참고: [ThinkFL 논문](https://arxiv.org/abs/2504.18776), [ThinkFL 공식 저장소](https://github.com/LLM4AIOps/ThinkFL), [Prime-RL](https://github.com/PrimeIntellect-ai/prime-rl)

## 11. 학습·추론 실행 구조

| 위치 | 책임 |
|---|---|
| Train H200 세션 | SFT와 Prime-RL LoRA trainer만 실행 |
| vLLM H200 세션 | SFT/RL 정책 추론과 모델 실행 생성만 수행 |
| 로컬 | Prime 실행 제어기, RCA 하네스, 규칙 기반 평가 함수, LoRA 전송 |

기본 모델을 병합하지 않는다. 두 세션은 같은 기본 모델을 읽고 작은 LoRA만 교환한다.

vLLM 초기 처리량 설정:

```text
gpu_memory_utilization = 0.95
max_num_seqs = 8
max_num_batched_tokens = 16384
chunked prefill = on
prefix caching = on
```

`gpu_memory_utilization=0.95`는 메모리 누수가 아니라 모델 가중치와 KV 캐시의 예약 상한이다. 실제 부하에서 처리 토큰, KV 사용률, preemption, 대기열, OOM을 함께 보고 `0.90 / 0.93 / 0.95` 중 가장 높은 안전값을 선택한다.

두 KT 컨테이너는 공유 파일시스템이 없다. LoRA relay는 전송이 모두 끝난 뒤에만 새 adapter를 공개한다. 반쯤 복사된 adapter가 vLLM에 로드되는 것을 막는다.

## 12. 평가와 승격 기준

모든 모델은 같은 case, 하네스, 도구, scorer, 반복 수, 생성 제한으로 비교한다.

평가 항목:

- 사건 단위 통과 수(3회 중 2회 이상 완전 정답)
- 완전 정답 실행 수
- 평균 근본원인 F1
- 평균 보상
- 필수 증거 충족률
- 답변 형식 오류
- 근거 없이 `confirmed`를 제출한 수
- 평균 조사 턴과 중복 도구 호출

승격 조건:

```text
RL ≥ SFT v3 ≥ vanilla
```

평균 보상 하나만 높아진 모델은 승격하지 않는다. 사건 단위 통과, 결정적 검증, 필수 증거 충족률이 함께 유지되거나 개선돼야 한다.

최종 Claude·Codex 비교에는 개발 중 반복 확인하지 않은 새 failure family를 사용한다. 과거 12개 holdout은 결과를 본 뒤 설계를 변경했으므로 최종 test가 아니라 development holdout이다.

## 13. 바로 다음 작업

1. SFT v3 봉인 평가 완료
2. vanilla·historical SFT·SFT v3 결과를 같은 기준으로 비교
3. Prime-RL 3-step 온라인 GRPO 연결 시험 실행
4. LoRA 갱신과 vLLM hot-load 검증
5. smoke가 SFT v3 대비 비퇴행이면 본 RL 실행
6. RL 봉인 재평가
7. 비퇴행 후보만 승격
8. 새 미노출 failure family에서 Claude·Codex와 최종 비교

## 14. 현재 결론

현재까지 확실한 개선은 다중 턴 궤적 SFT에서 확인됐다. 모델은 같은 조회 반복을 줄이고 실제 원인 후보와 증거를 더 잘 찾기 시작했다. 그러나 독립된 원인을 모두 찾고 결정적 검증을 완성하는 능력은 아직 부족하다.

따라서 현재 방향은 다음과 같다.

```text
계약을 통과한 다중 턴 궤적으로 SFT
→ SFT 모델을 실제 RCA 하네스에 투입
→ 사건당 여러 현재 정책 실행 생성
→ 규칙 기반 평가 함수의 보상으로 온라인 GRPO
→ 봉인 평가에서 비퇴행 확인
→ Claude·Codex와 동일 조건 비교
```
