# 논문과 현재 구현의 차이

2026-10-07 코드 열람 기준. 이 문서는 논문 효과를 재현했다는 주장이 아니다. 조사 중 학습 코드·설정은 변경하지 않았다.

| 항목 | 현재 확인한 계약 | 실험 해석 |
|---|---|---|
| 추론문 생성 | [rationale_writer.py:94](/home/swlee/projects/rca-model-lab-mcp/src/rca_lab/mcp/rationale_writer.py:94): 과거 conversation과 **이미 정해진 next_call**을 함께 입력 | 행동을 선택한 원래 사고과정의 기록이 아니라 행동 조건부 사후 재작성. 유용성은 별도 검증 필요 |
| 학습 데이터 판정 | [judge.py:59](/home/swlee/projects/rca-model-lab-mcp/src/rca_lab/mcp/judge.py:59): id, reasoning, actual_call만 전송. reasoning 끝 1,800자 사용 | grounded=true여도 관측과의 대조 검증을 완수했다고 볼 수 없음. 입력 계약에 observation 부재 |
| 평가용 기전 판정 | [rl_reward.py:107](/home/swlee/projects/rca-model-lab-mcp/src/rca_lab/mcp/rl_reward.py:107): reference_cause와 candidate_conclusion 비교 | 위 학습 데이터 판정기와 별개다. 현재 strict는 기준 원인과 결론의 기전 일치이며, 전체 조사 궤적의 증거 수집을 직접 감사한 점수는 아님 |
| 학습 방식 | [mcp_sft.py:82](/home/swlee/projects/rca-model-lab-mcp/src/rca_lab/train/mcp_sft.py:82): 기본 SFT. 현재 continuation recipe도 SFT | ThinkFL의 다단계 GRPO를 이미 실행 중이라고 표현하면 부정확 |
| 기존 RL 경로 | [mcp_sft.py:341](/home/swlee/projects/rca-model-lab-mcp/src/rca_lab/train/mcp_sft.py:341): assistant-token CE에 sample weight 적용, RL weight는 advantage | 이 경로에서 old-policy 비율·clipping·KL은 확인되지 않음. ThinkFL/GRPO 논문 알고리즘과 동일하다고 가정 금지 |
| 추론문 손실 | [mcp_sft.py:76](/home/swlee/projects/rca-model-lab-mcp/src/rca_lab/train/mcp_sft.py:76): reasoning_loss_weight 기본 1.0, 행동 span 1.0 | 구현을 크게 바꾸지 않고 추론문 손실 ablation 가능. weight=0은 reasoning-free 입력/출력 정책과 동일하지 않음 |
| 턴 표본 | [mcp_sft.py:121](/home/swlee/projects/rca-model-lab-mcp/src/rca_lab/train/mcp_sft.py:121): 마지막 턴 + case/run seed로 나머지 표본. public cap=6 | 중간 탐색·오류회복·반증 턴의 실제 포함률을 측정해야 함. cap 증가 시 토큰/업데이트 증가를 효과와 혼동 금지 |
| 순차 학습 | [mcp_continue_recovered.py:170](/home/swlee/projects/rca-model-lab-mcp/scripts/mcp_continue_recovered.py:170): TT4→OB4→O2→Polestar, 이전 adapter 초기화. max_steps가 epochs보다 우선 | 혼합/replay 효과는 별도 대조군 필요. 모든 stage가 정확히 1 epoch라고 단정 금지; 최소 50 step 적용 |
| 현재 평가 | [Sol 결과](/home/swlee/projects/rca-model-lab-mcp/outputs/model-progress-20261007/sol-report.json): base strict21/36, ckpt94 strict18/36 | 6개 사례×6회. root-hit, 기전, 수집 증거, 비용을 분리하고 사례별 집계. seed/환경 일치 없는 run-index pairing은 인과 비교 아님 |

## 우선 가설

1. **증거 없는 grounding 판정:** 전체 데이터를 폐기한다는 뜻이 아니다. train-only 층화 표본을 관측 포함 판정·사람 감사로 대조해 영향부터 측정한다.
2. **사후 추론문 손실의 영향:** 같은 행동·샘플·초기화·업데이트 예산에서 reasoning_loss_weight만 비교한다. action loss와 rollout 성공률 모두 확인한다.
3. **순차 도메인 망각:** 데이터·토큰 예산을 맞춘 순차 vs 혼합/replay 대조군. validation 정답을 학습에 재사용하지 않는다.
4. **정답과 증거의 불일치:** 현재 mechanism judge와 별개로 관측된 evidence ID의 존재·시점·대상·주장 지지를 검증하는 설계를 검토한다.

이 가설들은 코드와 문헌을 연결한 조사자의 제안이다. 현재 성능 하락의 확정 원인은 아니다.
