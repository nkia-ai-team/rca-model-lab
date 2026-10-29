# 실행 계획: 과거 실패를 반영한 관측 근거 감사

상태: P0 제한 표본 구현·실행 완료. 2026-10-07. 책임: 이 세션. 기존 복구/학습 chain과 별도 산출물.

전체 이력: [재발 방지 기록](history-lessons-20261007.md). 9월 8일의 선행 계획을 계승하며, 미완 항목을 현재 MCP 경로에서 실행한다.

## 최종 목표와 성과 기준

동일한 도구·관측·harness·평가 조건에서 자체 RCA 모델이 fresh base보다 원인 대상 및 기전을 더 정확히 진단하고, 관측하지 않은 인과를 확정하지 않도록 한다. 학습하지 않은 장애/서비스에서 확인하고 token·tool·latency 비용을 같이 보고한다. 문헌 수, 생성 데이터 수, train loss 감소는 이 목표의 대체 성과가 아니다. 소수 반복 validation은 후보 선별용이며 일반화 입증은 별도다.

모델별 계약: [Muse/Glimmer 특성 및 검증 조건](base-model-contract-20261007.md). 현재 base는 RCA adapter 이전 checkpoint이며 raw pretrained라고 가정하지 않는다.

## 근거와 재발 방지 계약

- 2026-08-21 decisions.md: 로컬 데이터 처리 / 원격 GPU 학습 분리, 실험은 config와 산출물로 관리.
- ../rca-model-lab/docs/20260908-sft-failure-root-cause.md: V24–V33 반복에도 rollout 개선 없음. tokenizer/runtime prefix 불일치, top-N metric inventory 누락, 변경된 관측 ID에 옛 action 재생 실패. 손실 감소를 rollout 개선으로 부르지 않는다.
- ../rca-model-lab/docs/20260909-abcd-sufficiency-pilot-readout.md: 짧은 입력 자체를 개선책으로 가정하지 않는다. 증거 인용과 인과적 충분성은 별개.
- src/rca_lab/mcp/context.py: 현재 student/writer/training의 rolling_view를 감사에서도 사용. 과거 전체 raw 관측을 학생에게 보였던 것으로 취급하지 않는다.
- docs/research/2026-10-07-rca-literature/experiment-plan.md: P0→P1→P2→P3→P4 연구 순서 유지.

- docs/mcp-student-pipeline.md (2026-10-02): fake FP8 activation quantization이 gradient를 차단한 결함을 확인해 앞선 최적점 절벽 가설을 정정했다. reasoning weight 0.3/0 실험도 이미 했지만 수정 전 결과라 이후 통제 재검증 대상으로 취급한다.
- 같은 문서 (2026-10-06): 관측 없는 judge와 frozen rationalized view 문제를 이미 기록. 이번 작업은 발견이 아니라 미해결 evidence audit의 구현이다. 기존 view_source/stale-view 계약을 우회하지 않는다.

## 이번 실행 범위 / 완료 기준

1. 두 달 기록의 실패→금지 반복→검증 게이트 표를 저장한다. 예전 harness 결함을 현재도 미수정이라고 단정하지 않는다.
2. 별도 observation evidence audit 모듈과 CLI를 구현한다. 기존 judge/recovery 데이터와 verdict는 변경하지 않는다.
3. 감사 입력 예산은 120,000 JSON 문자(토큰 아님)로 시작한다. 현재 rolling view를 추가 축약하지 않고 담는지 실제 측정한다.
4. 감사 표본은 명시된 split의 train 멤버만. holdout과 중복되면 오류. snapshot SHA와 case/run/turn, 정책 버전 기록.
5. turn 직전 rolling_view만 사용. 미래 observation, 정답 manifest, 다른 턴 rationale을 증거에 포함하지 않는다. 원문 위치/해시, 축약/생략량을 보존한다. 한도 초과는 evidence 부족으로 남기며 판정 통과로 승격하지 않는다.
6. 실제 복구된 train trajectory로 제한된 표본을 생성해 크기·축약률·누락률을 측정한다. 추가 압축은 결정론적 발췌만 사용하며 semantic summary/metric top-N을 도입하지 않는다.
7. gpt-6.1-sol로 소표본의 claim-support를 별도 감사한다. supported / contradicted / insufficient_evidence를 분리, evidence ref가 포함된 판정만 수용. 기존 mechanism 재채점 아님. 호출 실패는 재개 가능한 미완료 상태.
8. 시간 경계·split·입력 한도·출처·판정 schema를 테스트하고 결과를 기록한다. P0 결과로 큰 규모 적용 여부를 결정한다.

## 뒤따를 단계와 게이트

- P1: 같은 초기화/데이터/action/step 예산에서 reasoning loss 0/0.25/1 비교. 먼저 runtime prefix parity와 데이터 admission 확인. GPU guard 아래 예약한다.
- P2: 순차 대비 domain-balanced mix/replay. 이전 domain 퇴보도 보고.
- P3: train-only student 실패 상태 교정. validation trace를 교사 데이터로 복사 금지.
- P4: SFT 비퇴행과 reward audit 후 on-policy RL. 기존 weighted CE를 GRPO로 명명 금지.

이 계획은 전체 GPU 실험 완료 주장이 아니다. 이번 완료 산출물은 역사 감사, P0 실행 코드/테스트, 실제 입력 크기와 Sol 소표본 결과다. 이후 단계는 게이트와 자원 충돌을 확인한 뒤 이어간다.

## 게이트 상태 (실행 전 확인)

| 게이트 | 현재 상태 | 다음 증거 |
|---|---|---|
| FP8 gradient 차단 방지 | checkpoint.py에서 disable_activation_fake_quant 후 assert_input_gradient_flows 호출 확인 | 실제 GPU 시작 시 런타임 probe 통과 로그 |
| stale view 방지 | export.view_source 존재, 이번 snapshot에서 일치 검증 | 감사 manifest의 view_source 및 SHA |
| train/holdout 격리 | 명시 split과 case identity 확인 구현 | admission 테스트와 실제 manifest |
| 관측 포함 판정 | P0 pilot + 확장17턴 실행 완료 | 새 승인0, 보류1, 제외후보16; correction 후 재감사 |
| 학습 성과 | 미달/미확정 | fresh base 동일 harness 대비 root/strict/evidence |

P0의 판정 불일치는 인간 정답 대비 거짓 승인률이 아니다. 사람 감사 전에는 같은 Sol의 관측 추가 전후 불일치와 근거 부족 비율만 보고한다.

## 실행 결과와 계획 갱신

[실제 P0 결과](evidence-audit-pilot-20261007.md): 6 train 사례/18입력 준비, Sol 6턴 감사. rationale6/6 supported지만 final action2/2 insufficient_evidence. 다음 게이트는 단순 rationale 필터가 아니라 final action 주장·확신도까지 함께 평가하는 admission 검증이다. 그 전 새 P1 GPU 실행을 자동 추가하지 않는다. 기존 recovery chain은 별도 진행 중이며 이 감사의 승인 완료 모델이라고 부르지 않는다.

## P0 확장 실행 (사용자 진행 지시 이후)

- 기존 pilot6사례를 제외한 새 train12사례에서 마지막 submit_rca와 legacy거절 최대1턴/사례를 선정한다. 무작위 표본이 아니므로 오염률 추정에 쓰지 않는다.
- 채점 전에 rubric을 고정한다: rationale support, action support, 둘의 consistency를 독립 판정한다. Codex/gpt-6.1-sol, itemSHA+rubric+model cache로 재개한다.
- approve: 두 support 모두 supported + consistent. hold: 근거부족/추가생략/미완료. reject: 명시적 모순/추론-행동 불일치. reject도 원본은 보존한다.
- 산출 admission은 감사 표본의 감독 승인 후보이며 episode전체/모델학습 승인으로 확대 해석하지 않는다. 자동 원본삭제·live학습필터 변경 없음.
- 다음 P1은 승인된 충분한 train표본 및 gradient/runtime parity 증거가 있어야 실행한다. 이번 코드가 그 gate를 통과한 것처럼 위장하여 GPU를 기동하지 않는다.

## 확장 실행 결과

[12사례/17턴 확장 결과](evidence-admission-expanded-20261007.md): 기존통과11턴 중 새제외후보10/보류1. 기준을 느슨하게 바꿔 학습을 기동하지 않는다. 다음은 action+rationale 공동교정→재감사이며 이후P1은 모델별 계약과 admission을 통과할 때만 실행한다.


## 관측 전달 하네스 선행 검증 (2026-10-07 추가)

사용자 승인으로 [원본 접근을 보장하는 관측 하네스](observation-harness-20261007.md)를 구현했다. legacy 재현 경로를 유지하고 structured-v1 옵션에서만 원본 보관·JSON Pointer 재조회를 사용한다. train 관측411참조의 저장 범위 exact 복원 검증과 기반모델/adapter 지정 사실 조회를 수행했다. 이는 RCA 정확도·모델 학습 성과가 아니다. 새 정책의 데이터는 SFT/RL 기존 catalog에 자동 유입되지 않는다. 다음 학습은 하네스 비교와 정책별 admission/prefix 검증을 통과한 뒤다.
