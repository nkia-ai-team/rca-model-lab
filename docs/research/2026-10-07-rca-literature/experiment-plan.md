# 문헌에서 실행 가능한 자체 모델 실험으로

이 문서는 연구 설계다. 아래 추가 학습은 아직 실행하지 않았다. 진행 중인 복구·평가·학습 서비스 설정은 조사 때문에 변경하지 않았다.

## 우선순위

| 순서 | 질문 | 바꾸는 것 | 고정/통제 | 산출물·실패 판정 |
|---|---|---|---|---|
| P0, 학습 전 | 현재 grounding label을 신뢰할 수 있는가? | 관측 없는 현재 판정 vs 관측 포함 판정·블라인드 사람 감사 | train-only 동일 추론문/행동; 모델명 숨김; schema·judge 버전 기록 | 거짓 승인/거짓 거절·판정 불가 비율. 판정 불가를 자동 통과로 바꾸지 않음 |
| P1, 가장 작은 학습 실험 | 사후 추론문 모방이 행동 학습을 방해하는가? | reasoning_loss_weight ∈ {0, 0.25, 1}; 예산 제한 시 0과1 먼저 | 초기 모델·LoRA·action target·train split·샘플 순서·optimizer step·맥락 동일 | action loss, root, strict, evidence, 토큰 비용. 0은 추론 없는 모델이 아니라 추론 span 손실만 제외한 대조군 |
| P2 | 도메인 순차 학습에서 앞선 능력을 잃는가? | 기존 순차 vs case-balanced 혼합/replay | 같은 데이터 버전, 총 supervision token/optimizer 예산, P1 선정 설정; 도메인별 노출량 기록 | 단계별 public·Polestar 별도 점수와 이전 도메인 퇴보량. 총점만 개선하면 채택하지 않음 |
| P3 | 학생이 실제 방문하는 실패 상태를 학습해야 하는가? | teacher-only trajectory vs student rollout의 전문가 교정 | train-only 환경·동일 신규 supervision token 예산·힌트 차단 | 오류 이후 복구율, 처음 보는 대상/장애 family 성과. validation 실패 원문을 target으로 사용 금지 |
| P4, 앞선 검증 후 | 검증 가능한 보상이 SFT를 넘는가? | 고정 SFT 초기화 vs outcome+evidence 보상의 제한된 on-policy RL | 같은 도구·관측·train/validation 분리, rollout 비용 기록; reward/version 고정 | root/strict/evidence 향상 및 무장애 오탐. 보상만 상승하고 held-out 성능이 하락하면 중단 |

P0/P1의 출발 근거는 [현재 코드 계약](implementation-crosswalk.md), P2/P3는 [학습 문헌](agent-training.md), P4는 [ThinkFL/RCA 연구](rca-agents.md)와 [TRACE의 개선·퇴보 양면](recent-diagnostic-rl.md)이다. 표의 설정값은 우리 실험 제안이며 논문의 최적값을 복사한 것이 아니다.

## 성과 계약

- **모델 가중치 효과:** 같은 harness·도구·prompt·컨텍스트·평가 시드·샘플링 정책을 쓴 fresh base와 비교한다. 도구 변경 효과와 혼합하지 않는다.
- **정확도:** root-hit, 원인+기전 strict, 증거에 의해 뒷받침된 판정은 별도다. 기준 답과 설명이 일치해도 실제 증거 수집 성공으로 간주하지 않는다.
- **효율:** 같은 정확도에서의 지연/토큰/도구 호출, 또는 같은 예산에서의 정확도를 비교한다. 빨라졌지만 덜 맞는 결과를 단독 성과로 포장하지 않는다.
- **통계:** 현재 6개 사례×6회는 탐색용. 사례 단위 평균과 분산, family/system 별 결과를 보고한다. 독립 사례가 적다는 한계를 CI로 해결했다고 주장하지 않는다.
- **일반화:** capture/run 중복 외에 장애 family·topology·service identity·시간·데이터 생성 seed의 공유를 점검한다. 개발 validation과 최종 보존 평가를 분리한다.
- **재현:** dataset hash, base/adapter hash, tool schema, prompt/context version, sampling 설정, judge/rubric, 환경 snapshot을 결과와 함께 저장한다.

## 병렬화

- CPU/분석: train 데이터 감사·평가 프로토콜 동결·도메인별 실패 집계는 데이터 복구와 병렬 가능.
- 모델 호출: 현재 복구와 judge quota를 공유하므로 추가 호출은 예산을 기록하고 충돌 없이 배치한다.
- GPU: 실험 설계·입력 manifest 생성은 병렬 가능. 실제 학습은 현재 GPU/lock·평가 guard 아래에서 순차 예약한다.
- 첫 실험부터 여러 설정을 동시에 바꾸지 않는다. 기본 비교에서 신호가 확인된 뒤 다음 축으로 확장한다.
