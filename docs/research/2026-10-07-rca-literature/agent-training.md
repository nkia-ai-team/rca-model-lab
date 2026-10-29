# 에이전트/도구사용 학습 문헌

기준일: 2026-10-07 KST. 목적은 현재 LoRA 기반 RCA agent를 더 학습할 때, 단순 SFT를 계속 밀지 아니면 데이터/보상/평가 계약을 먼저 바꿀지 판단하는 것이다. 아래 적용안은 조사자 해석이며, 논문 저자가 이 프로젝트 설정을 검증한 것은 아니다.

## 결론

현재 프로젝트는 ThinkFL식 GRPO 재현이 아니라, teacher 행동과 사후 rationale을 CE로 맞추는 SFT 계열이다. 아래 세 가지는 문헌을 종합한 실험 가설이며, 모든 논문이 각각 입증한 명제는 아니다.

1. tool agent 학습은 최종 답만이 아니라 trajectory 품질이 핵심이다.
2. 성공 trajectory만 모방하면 일반화가 약하거나 tool policy가 굳을 수 있다.
3. RL/GRPO는 검증 가능한 reward와 on-policy 상태가 있을 때 의미가 있고, reward가 거칠면 SFT보다 나빠질 수 있다.

따라서 바로 큰 RL로 가지 말고, `reasoning_loss_weight`, trajectory filtering, mixed/replay, student-rollout correction을 순서대로 분리 실험하는 것이 가장 싸고 안전하다.

확인 깊이: 이 표는 원문 서지/초록 확인을 기본으로 한다. TRACE만 별도 문서에 본문 methods/table 검토를 기록했다. 나머지를 본문 정독·재현 완료로 해석하지 않는다.

## 핵심 논문

| 우선 | 논문 | 확인한 내용 | 현재 적용 |
|---|---|---|---|
| P0 | [ReAct: Synergizing Reasoning and Acting in Language Models](https://arxiv.org/abs/2210.03629), 2022/ICLR 2023 | reasoning trace와 action을 interleave하여 계획 갱신과 외부 정보 수집을 결합한다. | RCA 데이터를 answer-only로 만들면 agent 능력의 핵심 표면을 버린다. tool/action/observation 보존 필요. |
| P0 | [FireAct: Toward Language Agent Fine-tuning](https://arxiv.org/abs/2310.05915), 2023 | GPT-4 생성 agent trajectory로 Llama2를 fine-tune했고, diverse task/method trajectory가 중요하다고 보고한다. | teacher trajectory SFT는 타당한 시작점이다. 단일 prompt/method만 모방하는지는 별도 점검해야 한다. |
| P0 | [AgentTuning: Enabling Generalized Agent Abilities for LLMs](https://arxiv.org/abs/2310.12823), 2023 | AgentInstruct trajectory와 일반 instruction 데이터를 섞어 agent 능력을 올리면서 일반 능력 유지 목표를 둔다. | RCA domain만 순차 주입하면 망각 위험이 있다. domain-balanced mix/replay가 저비용 대조군이다. |
| P0 | [ToolLLM: Facilitating Large Language Models to Master 16000+ Real-world APIs](https://arxiv.org/abs/2307.16789), 2023 | ToolBench를 만들고, solution path annotation과 API retrieval을 결합해 ToolLLaMA를 학습한다. | 도구 선택/호출 경로를 학습 데이터의 1급 label로 둬야 한다. “정답 텍스트”만 SFT하면 부족하다. |
| P0 | [Reflexion: Language Agents with Verbal Reinforcement Learning](https://arxiv.org/abs/2303.11366), 2023/NeurIPS | 실패 후 feedback을 verbal memory로 바꾸어 다음 trial을 개선한다. | 실패 trajectory도 폐기 대상이 아니라, 잘 라벨링하면 recovery data가 된다. 다만 validation 실패를 train target으로 쓰면 안 된다. |
| P0 | [Agent Lightning: Train ANY AI Agents with Reinforcement Learning](https://arxiv.org/abs/2508.03680), 2025 | agent 실행과 RL training을 분리하고 trajectory transition credit assignment를 제안한다. | 현 SFT 코드와 바로 같지 않다. 나중에 RL로 갈 때 “agent trace interface + credit assignment” 설계 참고. |
| P1 | [ToolRL: Reward is All Tool Learning Needs](https://arxiv.org/abs/2504.13958), 2025/NeurIPS | tool-use RL에서 reward 설계가 핵심이며 coarse answer reward만으로는 충분하지 않다고 주장한다. | Sol mechanism 점수만 reward로 넣는 것은 위험하다. root/evidence/tool validity/stop timing 보상 분리가 필요하다. |
| P1 | [Reinforcement Learning for Long-Horizon Interactive LLM Agents](https://arxiv.org/abs/2502.01600), 2025 | long-horizon interactive environment에서 instruction-tuned model을 직접 환경에 맞춰 RL 학습한다. | RCA도 off-policy teacher imitation만으로는 student가 실제 방문하는 실패 상태를 못 배울 수 있다. |
| P1 | [TRACE: Training Reasoning Agents for Causal Exploration with Synthesized Rewards](https://arxiv.org/abs/2609.10315), 2026 | 광고 진단 task에서 hidden intervention oracle 기반 reward를 합성해 SFT+RL을 비교한다. no-signal 오탐 악화도 보고된다. | RCA 직접 증거는 아니지만, reward design이 좋아야 RL이 산다는 최신 반례다. 상세는 [TRACE](recent-diagnostic-rl.md). |
| P1 | [STaR: Bootstrapping Reasoning With Reasoning](https://arxiv.org/abs/2203.14465), 2022 | 정답을 활용해 rationale을 bootstrap하는 방법을 제안한다. | 사후 rationale 생성 자체는 연구적으로 가능한 축이다. 하지만 faithful process label로 자동 승격하면 안 된다. |

## 현재 설정으로 바로 가능한 ablation

| 실험 | 단일 변경 | 고정할 것 | 성공 신호 |
|---|---|---|---|
| R0 rationale loss | `reasoning_loss_weight` 0 / 0.25 / 1 | 같은 action target, seed, step/token budget, LoRA 설정 | root와 mechanism이 유지되면서 action/tool policy가 덜 망가지면 rationale loss가 문제였다는 신호 |
| R1 trajectory quality filter | teacher trajectory 전체 vs evidence-grounded 상위 subset | 총 optimizer step 또는 supervision token 예산 | 작은 데이터라도 held-out strict가 오르면 quantity보다 quality 신호 |
| R2 mixed/replay | 순차 `TT4->OB4->O2->Polestar` vs balanced mix/replay | 총 token/update 수, dataset hash | 앞 단계 domain 퇴보가 줄면 순차 망각 신호 |
| R3 student-state correction | teacher-only vs student rollout 실패 상태에 대한 train-only correction | validation 금지, 같은 신규 token budget | 실패 후 복구율과 repeated tool call 감소 |
| R4 reward pilot | SFT fixed vs root+evidence+tool validity reward의 제한된 on-policy run | reward version, tool schema, train/heldout 분리 | reward 상승뿐 아니라 heldout root/strict/evidence 동반 개선 |

## 적용 해석

- 현재 `base strict21/36`, `ckpt94 strict18/36`은 SFT가 절대 나쁘다는 증거가 아니라, 현재 데이터/손실/순차 구성에서 base의 탐색 능력을 보존하지 못했을 가능성을 보여주는 smoke signal이다.
- FireAct/AgentTuning 계열은 “trajectory SFT 자체”를 지지하지만, diverse trajectory와 일반 능력 보존을 같이 본다.
- Agent Lightning/ToolRL/TRACE 계열은 “RL을 하라”보다 “reward와 credit assignment 없이는 RL도 망한다”는 쪽에 더 가깝다.
- Reflexion/STaR은 실패·rationale을 자산으로 만들 수 있다는 근거지만, 현재처럼 `next_call`을 알고 쓴 사후 rationale은 행동 원인 기록이 아니다.

## 재사용 문장

우리 다음 자체 모델 성과는 큰 학습 한 방보다 `trajectory 품질`, `rationale loss`, `domain mix/replay`, `student 실패상태 보정`을 분리해 base 대비 같은 budget에서 root/strict/evidence를 올리는 방식으로 만들어야 한다.
