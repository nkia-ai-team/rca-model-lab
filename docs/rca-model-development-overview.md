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
| 기본 학습 순서는? | 전체 조사 궤적 SFT → SFT 모델 위에서 온라인 RL |
| 최종 목표는? | 같은 조건에서 Student가 Claude·Codex와 비슷하거나 더 높은 RCA 성능 달성 |

### 현재 상태

- 계약 검사를 통과한 `20개 시나리오 / 23개 전체 궤적`으로 SFT v3 학습 완료
- `69/69 step`, `3 epoch`, 최종 train loss `0.72`
- 최종 LoRA와 데이터·설정·manifest 체크섬 검증 완료
- SFT 봉인 평가 진행 중
- 다음 단계: SFT LoRA를 초기 정책으로 사용하는 Prime-RL 온라인 GRPO

아직 최종 성능이 확정된 것은 아니다. SFT v3와 RL 모델은 동일한 봉인 평가를 통과해야 한다.

## 3. 산출물 관리 위치

| 관리 대상 | 위치 | 관리 내용 |
|---|---|---|
| 모델·데이터 | [Hugging Face · nkia-ai-lab](https://huggingface.co/nkia-ai-lab) | 기본 모델 정보, LoRA adapter, 학습·평가 데이터셋, dataset card |
| 코드 | [GitHub · rca-model-lab](https://github.com/nkia-ai-team/rca-model-lab) | 하네스, scorer, 데이터 생성, SFT/RL, 평가 코드와 설정 |
| 학습 파라미터·실험 결과 | [Weights & Biases · nkia-ai](https://wandb.ai/nkia-ai/projects) | learning rate, batch, loss, reward, GPU 지표, 실험별 비교 |

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
root F1 = 0.0
원인 후보 = 0개
status = insufficient
```

상위 증거에 payment 429가 있었지만 호스트 메트릭에서 다른 증거 표면으로 이동하지 못했다.

### Whole-episode SFT 모델

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
reward = 0.572
root F1 = 0.8
status = provisional
strict correct = false
```

개선된 점:

- 동일 대상 반복에서 벗어났다.
- 외부 PG 경계와 실제 증거 ID를 찾았다.
- 증거가 부족하므로 `confirmed` 대신 `provisional`을 제출했다.

남은 문제:

- inventory DB lock이라는 독립된 두 번째 원인을 놓쳤다.
- 결정적 proof type을 충족하지 못했다.
- 따라서 완전한 RCA 정답은 아니었다.

이 사례의 결론은 단순하다. SFT는 조사 경로를 개선했다. 그러나 모든 독립 원인을 찾고 증명하는 능력은 아직 부족하다.

## 5. 학습 데이터

### 데이터 단위

각 턴을 별도 샘플로 쪼개지 않는다. 질문부터 최종 답까지의 전체 episode를 하나의 학습 샘플로 사용한다.

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
| 전체 episode | 23 |
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

SFT에는 검증을 통과한 전체 episode를 사용한다. 실패·비평·교정 기록은 향후 critic 및 preference 데이터로 보존한다.

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
| `answer` | 구조화된 최종 RCA 제출 | 원인별 mechanism, refs, proof, status 제출 |

`env_grep`은 보조 수단이다. 주 조회 방식은 target/source/kind/time 기반 구조 조회다.

## 7. 하네스 내부 구성

### Capability registry

이번 사건에서 실제 실행할 수 있는 action, target, source, metric을 보관한다. 목록에 없는 이름은 실행 전에 거절한다.

### Evidence store와 evidence map

로그·메트릭·트레이스·이벤트 원본을 모두 보존한다. 각 evidence에는 `ev221` 같은 ID가 붙는다. 프롬프트와 한 번의 조회 결과만 크기를 제한하며 원본 저장소는 자르지 않는다.

### Typed ledger

모델이 실행한 모든 action과 observation을 순서대로 저장하는 episode 내부 기록이다. 별도 장기 DB가 아니라, 실행 중 구조화된 상태로 누적되고 완료 후 episode artifact에 저장된다.

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

실패·중복 action도 삭제하지 않는다. scorer와 RL이 전체 조사 과정을 평가하는 데 필요하다.

### Typed scorer

최종 문장만 보지 않는다. ledger와 답을 함께 읽고 다음 항목을 채점한다.

- 정답 근본원인 집합 일치도
- 원인별 결정적 proof 규칙
- `support_refs`와 `counter_refs`의 유효성
- 증상에서 근본원인으로 이어지는 mechanism
- `confirmed`, `provisional`, `insufficient` 상태의 타당성
- 중복·invalid action·근거 없는 과확신

특정 도구를 실행했다는 사실 자체에는 보상하지 않는다. 그렇지 않으면 모델이 도구 호출만 반복해 reward를 얻을 수 있다.

## 8. SFT와 RL에서 시도한 것

아래 결과는 이전 개발 평가다. 현재 SFT v3의 봉인 결과와 혼동하면 안 된다.

| 단계 | 방법 | 관측 결과 | 판단 |
|---|---|---|---|
| Historical SFT | 검증된 전체 episode 모방 | vanilla보다 reward·root F1 개선 | 이후 RL의 기준 정책 |
| RL v1 | 종합 reward DAPO | SFT 대비 퇴행 | 약한 행동 지표가 오답도 강화 |
| RL v2 | diagnosis-only DAPO | 동일 reward·all-zero 그룹 다수 | 상대 advantage 신호 부족 |
| RL v3 | teacher positive + Student negative | 더 큰 퇴행 | 실패 episode의 정상 조사 token까지 억제 |
| RFT v4 | 성공·교사 episode만 positive replay | monitor 개선 가능성 | provenance 부족으로 미승격 |
| v5 | terminal reward + turn credit | 조사 단계 credit 실험 | gold target 접근 자체 보상 위험 |
| RL v6 | offline whole-trajectory RPO | 평균은 거의 유지, majority strict 퇴행 | 폐기 |

### v6가 실패한 이유

v6는 저장된 좋은 episode와 나쁜 episode를 비교했다.

```text
chosen episode의 확률 상승
rejected episode의 확률 하락
```

그러나 실제 하네스에서 새 rollout을 만들지 않았다. 평균 reward는 `0.2285 → 0.2303`으로 거의 유지됐지만 majority strict case가 `1/6 → 0/6`으로 떨어졌다. 평균값이 사건 단위 안정성 퇴행을 가렸으므로 승격하지 않았다.

## 9. 다음 RL: Prime-RL 온라인 GRPO

다음 RL은 현재 모델이 실제 하네스에서 새 조사 결과를 생성하고, 그 점수로 바로 정책을 갱신한다.

```text
SFT v3 LoRA
→ 같은 RCA 사건을 8회 조사
→ typed scorer가 각 전체 episode 채점
→ 같은 사건 안에서 상대 advantage 계산
→ language model LoRA 갱신
→ 갱신된 LoRA로 새 rollout 생성
→ 반복
```

점수 예:

| Rollout | 결과 | 예시 점수 |
|---|---|---:|
| A | 모든 root와 proof, evidence 충족 | 1.00 |
| B | root는 맞지만 proof 부족 | 0.65 |
| C | 증상만 식별 | 0.25 |
| D | 원인 오류와 근거 없는 과확신 | 0.00 |

0/1 점수만 사용하면 한 그룹이 `[0, 0, 0, 0]`일 때 advantage가 모두 0이 된다. 따라서 root·proof·evidence 기반 중간 점수를 사용하되, 도구 실행 자체는 보상하지 않는다.

참고: [ThinkFL 논문](https://arxiv.org/abs/2504.18776), [ThinkFL 공식 저장소](https://github.com/LLM4AIOps/ThinkFL), [Prime-RL](https://github.com/PrimeIntellect-ai/prime-rl)

## 10. 학습·추론 실행 구조

| 위치 | 책임 |
|---|---|
| Train H200 세션 | SFT와 Prime-RL LoRA trainer만 실행 |
| vLLM H200 세션 | SFT/RL 정책 추론과 rollout 생성만 실행 |
| 로컬 | Prime orchestrator, RCA 하네스, typed scorer, LoRA relay |

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

## 11. 평가와 승격 기준

모든 모델은 같은 case, 하네스, 도구, scorer, 반복 수, 생성 제한으로 비교한다.

평가 항목:

- majority strict cases
- strict runs
- mean root F1
- mean reward
- evidence complete
- format error
- unsupported confirmed
- 평균 조사 turn과 중복 action

승격 조건:

```text
RL ≥ SFT v3 ≥ vanilla
```

평균 reward 하나만 높아진 모델은 승격하지 않는다. 사건 단위 strict, proof, evidence completeness가 함께 비퇴행해야 한다.

최종 Claude·Codex 비교에는 개발 중 반복 확인하지 않은 새 failure family를 사용한다. 과거 12개 holdout은 결과를 본 뒤 설계를 변경했으므로 최종 test가 아니라 development holdout이다.

## 12. 바로 다음 작업

1. SFT v3 봉인 평가 완료
2. vanilla·historical SFT·SFT v3 결과를 같은 기준으로 비교
3. Prime-RL 3-step 온라인 GRPO smoke 실행
4. LoRA 갱신과 vLLM hot-load 검증
5. smoke가 SFT v3 대비 비퇴행이면 본 RL 실행
6. RL 봉인 재평가
7. 비퇴행 후보만 승격
8. 새 미노출 failure family에서 Claude·Codex와 최종 비교

## 13. 현재 결론

현재까지 확실한 개선은 whole-episode SFT에서 확인됐다. 모델은 같은 조회 반복을 줄이고 실제 원인 후보와 증거를 더 잘 찾기 시작했다. 그러나 독립된 원인을 모두 찾고 결정적 proof를 완성하는 능력은 아직 부족하다.

따라서 현재 방향은 다음과 같다.

```text
계약을 통과한 전체 episode로 SFT
→ SFT 모델을 실제 RCA 하네스에 투입
→ 사건당 여러 on-policy rollout 생성
→ typed scorer 기반 온라인 GRPO
→ 봉인 평가에서 비퇴행 확인
→ Claude·Codex와 동일 조건 비교
```
