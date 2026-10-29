# RCA/ThinkFL 관련 논문 조사 - 2026-10-07

요청 유형: 포괄 연구. 범위는 ThinkFL 기반 자체 RCA 모델 개발에 바로 영향을 주는 논문으로 제한했다. 검색 기준일은 2026-10-07이며, 날짜가 이 이후인 자료는 제외했다.

## 직접 결론

현재 문헌의 큰 흐름은 "큰 LLM을 ReAct로 길게 돌리는 RCA"에서 "구조화된 도구 사용, 증거 기반 검증, 작은 모델의 RCA 전용화"로 이동 중이다. ThinkFL과 FoundRoot는 이 방향의 가장 직접적인 근거다. 단, 둘 다 "그냥 SFT/RL을 붙이면 된다"가 아니다. ThinkFL은 단순 MRR 보상 GRPO가 reward hacking과 짧은 응답 붕괴를 보인 뒤, format-aware priming, exploration augmentation, multi-factor grader를 단계화했다. FoundRoot도 warm-up SFT 뒤 DAPO/RL로 structured deep thinking을 강화했다. 반대로 OpenRCA와 AIOpsLab은 현재 LLM agent가 실제 장애 RCA에서 아직 낮은 정확도와 비효율을 보인다는 경고 근거다.

우리의 base 21/36, ckpt94 18/36, ckpt50 11/36 결과만으로 학습이 망가졌다고 단정하면 안 된다. 프로젝트 가설로는 SFT가 "도구 선택, 멈춤 판단, 증거-기전 연결"을 약화시켰는지 확인해야 한다. 문헌에서 직접 지지되는 안전한 다음 실험은 teacher answer 재현만 보지 말고, tool-format 준수, reasoning path, hallucination/중복, final root rank를 분리 평가하는 것이다.

## 우선순위 논문 인벤토리

| 우선 | 논문 | 날짜/venue | 근거 깊이 | 우리 작업 관련성 |
|---|---|---:|---|---|
| P0 | ThinkFL: Self-Refining Failure Localization for Microservice Systems via Reinforcement Fine-Tuning | arXiv 2025; 2026 v2, TOSEM accepted | PDF methods/ablation, GitHub, OpenRLHF fork 확인 | 자체 소형 RCA 모델 학습의 가장 직접적인 선례 |
| P0 | FoundRoot: Towards Foundation Model for Root Cause Analysis via Structured Deep Thinking | ICSE 2026 | camera-ready PDF methods/table/ablation, ICSE program, GitHub 확인 | "RCA foundation model + structured deep thinking" 축 |
| P0 | OpenRCA: Can Large Language Models Locate the Root Cause of Software Failures? | ICLR 2025 | ICLR proceedings, OpenReview PDF, GitHub 확인 | 실제 RCA 벤치에서 agent 성능 한계 확인 |
| P0 | Flow-of-Action: SOP Enhanced LLM-Based Multi-Agent System for Root Cause Analysis | WWW Companion 2025 | arXiv, ACM DOI/PDF 확인 | ReAct 대비 SOP/도구 흐름 제약이 크게 유효 |
| P0 | RCAEval: A Benchmark for Root Cause Analysis of Microservice Systems with Telemetry Data | WWW Companion 2025 | arXiv, ACM/OpenReview PDF, GitHub/HF 확인 | 현재 평가셋/외부 벤치마크 기준점 |
| P1 | RCAgent: Cloud Root Cause Analysis by Autonomous Agents with Tool-Augmented Large Language Models | arXiv 2023 | arXiv 확인 | tool-augmented RCA agent의 초기 산업형 기준 |
| P1 | mABC: Multi-Agent Blockchain-Inspired Collaboration for Root Cause Analysis in Micro-Services Architecture | Findings of EMNLP 2024 | arXiv, ACL Anthology PDF 확인 | 다중 agent, voting, workflow 표준화 |
| P1 | LATS-RCA: Language Agent Tree Search for Root Cause Analysis in Microservices | arXiv 2026, SEAA 2026/Springer | arXiv/DOI/replication package 확인 | 단일 경로 추론 대신 후보 tree search |
| P1 | AIOpsLab: A Holistic Framework to Evaluate AI Agents for Enabling Autonomous Clouds | MLSys 2025 | arXiv, OpenReview, MS GitHub 확인 | live cloud agent evaluation, tool API 반복 실패 근거 |
| P1 | eARCO: Efficient Automated Root Cause Analysis with Prompt Optimization | arXiv 2025 | arXiv, author page 확인 | prompt optimization + SLM RCA 추천 |
| P2 | Cloud Atlas: Efficient Fault Localization for Cloud Systems using Language Models and Causal Insight | arXiv 2024 | arXiv, Stanford MAST page 확인 | causal graph 생성과 telemetry validation |
| P2 | COCA: Generative Root Cause Analysis for Distributed Systems with Code Knowledge | ICSE 2025 | IEEE, arXiv, DBLP 확인 | telemetry 부족 시 code knowledge 보강 |
| P2 | BARO: Robust Root Cause Analysis for Microservices via Multivariate Bayesian Online Change Point Detection | FSE 2024 | arXiv, OpenReview, Zenodo artifact 확인 | LLM 이전/비LLM 강한 baseline |
| P2 | Agentic Root Cause Analysis through Evidence-Grounded Reasoning | arXiv 2026 | arXiv 확인 | 산업 센서 RCA에서 evidence-grounded hypothesis loop |

## 핵심 근거

1. ThinkFL은 "lightweight LLM + progressive multi-stage GRPO + multi-factor failure localization grader + recursion-of-thought actor"를 제안한다. 본문은 먼저 SFT 후 MRR-only GRPO를 직접 적용했을 때 LLaMA3-8B에서 reward와 response length가 줄고, 모델이 긴 tool chain을 피하는 reward hacking을 보였다고 분석한다. 최종 ThinkFL은 작은 SFT로 format/tool invocation을 priming하고, diversity/recall grader로 exploration을 키운 뒤, multi-factor FL grader로 rank-oriented refinement를 한다. Llama3.2-3B ablation은 평균 MRR이 Stage 1 48.51%, Stage 2 54.42%, Stage 3 62.93%로 올랐고, Qwen2.5-3B도 37.02%에서 52.91%로 올랐다고 보고한다. 즉 근거는 "RL 자체"가 아니라 "RCA에 맞춘 단계형 reward/trajectory 설계"다.  
출처: https://arxiv.org/abs/2504.18776, https://github.com/LLM4AIOps/ThinkFL, https://github.com/LLM4AIOps/OpenRLHF-ThinkFL

2. OpenRCA는 335개 failure case와 68GB 이상의 logs/metrics/traces로 평가하며, ICLR 공식 페이지는 특별 설계된 RCA-agent에서도 Claude 3.5의 해결률이 11.34%였다고 보고한다. 즉 실제 RCA는 "긴 telemetry를 읽을 수 있음"만으로 풀리지 않고, query 구조, evidence extraction, component/reason/time 요소 채점이 병목이다.  
출처: https://proceedings.iclr.cc/paper_files/paper/2025/hash/d29b8d53678015079e1d245c023e49d2-Abstract-Conference.html, https://openreview.net/pdf?id=M4qNIzQYpd, https://github.com/microsoft/OpenRCA

3. Flow-of-Action은 ReAct가 RCA에서 irrelevant action과 hallucination에 취약하다고 보고, SOP flow와 보조 agent를 넣어 평균 정확도를 ReAct 35.50%에서 64.01%로 올렸다고 보고한다. 우리 학습 로그에서 tool call 수를 줄이는 방향은 속도에는 좋지만, SOP/중간 검증 없이 줄이면 root는 맞아도 mechanism 설명이 약해질 위험이 있다.  
출처: https://arxiv.org/abs/2502.08224, https://tieyingzhang.github.io/home/files/www25_agent.pdf

4. RCAEval은 3개 microservice system에서 735 failure case와 11 fault type, metrics/logs/traces를 포함한다고 밝힌다. 이 benchmark는 coarse-grained와 fine-grained RCA를 모두 평가한다. 우리 val36이 6개 case x 6회 반복이라면, 논문식 성능 주장으로 쓰기에는 아직 분산과 fault coverage가 작다.  
출처: https://arxiv.org/abs/2412.17015, https://openreview.net/pdf/f0ad03673af1f847033420d0bceb6a2df29872ef.pdf, https://github.com/Tenkey666/RCAEval-trace, https://huggingface.co/datasets/phamquiluan/RCAEval

5. AIOpsLab은 agent-cloud interface로 detection, localization, RCA, mitigation을 live environment에서 평가한다. OpenReview PDF는 agent들이 같은 API를 반복 호출하거나 없는 API를 생성하는 식으로 step을 낭비한다고 보고한다. 이것은 우리 자체 모델 평가에서도 "정답 여부"와 별개로 반복 tool call, invalid tool, stop timing을 별도 metric으로 봐야 한다는 근거다.  
출처: https://arxiv.org/abs/2501.06706, https://openreview.net/pdf?id=3EXBLwGxtq, https://github.com/microsoft/AIOpsLab

6. RCAgent와 mABC는 둘 다 prompt-only보다 workflow, self-consistency, context management, voting, role specialization이 중요하다고 본다. mABC는 7개 specialized agents와 blockchain-inspired voting을 사용하며, RCAgent는 industrial setting에서 ReAct보다 root cause, solution, evidence, responsibility 측면이 낫다고 보고한다. 우리 적용 가설은 검증 가능한 중간 trajectory를 학습 신호로 추가하면 도움이 되는지 비교하는 것이다.  
출처: https://arxiv.org/abs/2310.16340, https://arxiv.org/abs/2404.12135, https://aclanthology.org/2024.findings-emnlp.232.pdf

7. LATS-RCA는 RCA를 단일 선형 추론이 아니라 reflection-guided tree search로 본다. arXiv 초록은 LO2 open benchmark 91.3%와 production incidents 65.1%를 보고하며, production에서는 multi-factor root cause, 불완전한 observability, 복잡한 polyglot stack 때문에 정확도가 내려간다고 설명한다. 이 차이는 synthetic/open 데이터에서 잘 된 모델이 polestar식 실제 시나리오에서 흔들릴 수 있다는 경고다.  
출처: https://arxiv.org/abs/2605.03505, https://doi.org/10.1007/978-3-032-36590-3_14, https://github.com/kottinov/lats-rca

8. eARCO는 Microsoft의 180K historical incident data로 domain-adapted SLM을 만들고, PromptWizard 기반 prompt optimization이 RAG LLM과 fine-tuned SLM 모두에서 정확도를 올렸다고 보고한다. 이는 "모델 자체 학습"만이 아니라 inference prompt와 retrieval example 선택도 독립적인 성과 축이 될 수 있음을 뜻한다.  
출처: https://arxiv.org/abs/2504.11505, https://goeldrishti.github.io/

9. Cloud Atlas와 COCA는 RCA가 telemetry-only 문제가 아니라 causal graph와 code knowledge 문제이기도 하다는 근거다. Cloud Atlas는 LLM으로 system documentation/telemetry/deployment feedback에서 causal graph를 만들고 validation을 붙인다. COCA는 issue report만으로 부족할 때 code snippet과 execution path를 보강해 root cause localization과 summarization을 개선한다고 보고한다.  
출처: https://arxiv.org/abs/2407.08694, https://mast.stanford.edu/pubs/cloud_atlas_efficient_fault_localization_for_cloud_systems_using_language_models_and_causal_insight/, https://arxiv.org/abs/2503.23051, https://ieeexplore.ieee.org/document/11029720/

10. FoundRoot는 ICSE 2026 research track의 camera-ready 논문으로 확인했다. 방법은 metric scanning, propagation analysis, reflection, ranking의 네 substep을 한 번의 continuous output 안에 학습시키는 structured deep thinking이다. 학습은 DeepSeek-R1-Distill-Qwen2.5-14B 기반 warm-up SFT 후 DAPO/RL이며, reward는 format, JSON validity, MRR 기반 RCA accuracy, structured thinking 준수의 weighted sum이다. Table 2는 네 dataset에서 baseline과 비교한다. 데이터·baseline이 다른 개선율을 우리 strict 점수로 환산하지 않는다. Figure 8 ablation은 WarmUp SFT Only와 SFT+SFT보다 SFT+RL이 낫다고 보고한다. 다만 A/B처럼 복잡하고 observability/metric quality가 낮은 dataset에서는 절대 정확도가 여전히 낮다고 논문이 직접 caveat를 둔다.  
출처: https://conf.researchr.org/track/icse-2026/icse-2026-research-track, https://netman.aiops.org/publications/, https://github.com/NetManAIOps, https://xiez22.github.io/, https://netman.aiops.org/wp-content/uploads/2026/01/foundroot_camera_ready.pdf

## 자체 모델 개발 가설

가설 A: 현재 SFT checkpoint가 base보다 낮은 것은 "정답 형식 학습"이 "증거 탐색 정책"을 약화시켰기 때문일 수 있다. 이것은 우리 프로젝트 가설이지, 문헌이 우리 결과를 설명한다고 단정하는 것은 아니다. 검증은 root-correct 사례와 root-wrong 사례를 나눠 tool sequence length, invalid call, repeated call, stop timing, evidence citation count를 비교하면 된다. ThinkFL, Flow-of-Action, AIOpsLab 근거와 맞닿아 있다.

평가 원칙 B: 비어 있던 mechanism 점수는 채워야 strict 비교가 가능하다. 채점 완료가 모델 자체 성능 향상을 뜻하지는 않는다. root 선택과 mechanism 설명은 다른 능력이다. OpenRCA가 root cause element 단위로 평가하고, RCAEval이 coarse/fine 평가를 분리하는 이유와 같다. 따라서 다음 리포트는 root accuracy, mechanism faithfulness, tool evidence coverage를 분리해야 한다.

가설 C: 데이터 복구/teacher 생성은 "좋은 최종 답변"보다 "좋은 반례와 중간 판단"을 우선해야 한다. mABC/LATS-RCA/AgentRCA 모두 후보 비교, voting, hypothesis tracking을 쓴다. 바로 할 수 있는 실험은 Sol judge로 trajectory를 3등급으로 나누고, high-confidence trajectory만 SFT 또는 DPO/GRPO seed로 쓰는 ablation이다.

## 바로 병렬화 가능한 실험 축

1. 평가 축: val36을 root/mechanism/tool-policy 세 지표로 재집계하고, checkpoint별 confusion matrix를 case 단위로 만든다.

2. 데이터 축: recovery된 teacher trajectory를 최종 답변 품질이 아니라 evidence-grounding 기준으로 필터링한다.

3. 학습 축: SFT-only, SFT+ranked rejection, 정식 RL pilot, inference-time reward reranking을 별개 조건으로 비교한다. RL 실험은 보상 검증 뒤에 진행한다.

4. 프롬프트 축: eARCO식 prompt optimization 또는 SOP flow를 base와 ckpt94 양쪽에 적용해서 모델 학습 효과와 prompt 효과를 분리한다.

5. 벤치 축: RCAEval/OpenRCA의 공개 포맷 중 작은 subset을 이 프로젝트 포맷으로 변환해서 val36 과적합 여부를 확인한다.

## 불확실성

- "관련 논문 전부"는 실제로 불가능하므로, 이 문서는 ThinkFL 자체 모델 개발에 직접 연결되는 14편으로 컷했다.
- FoundRoot는 camera-ready PDF까지 확인했지만, 표/그림이 많은 최신 2026 논문이라 추후 DOI/최종 ACM metadata는 변동 가능성이 있다.
- RCAgent는 arXiv 원문 기준으로만 인용한다.
- 2026 논문은 현재 날짜 기준 최근 자료라 DOI/출판 메타데이터가 일부 변동될 수 있다.

## 보조 논문 원문 링크

- [BARO](https://arxiv.org/abs/2405.09330): 비LLM baseline.
- [Agentic Root Cause Analysis through Evidence-Grounded Reasoning](https://arxiv.org/abs/2607.22385): 산업 센서/디지털 트윈 환경. 초록 확인 수준이며 cloud RCA와 직접 점수 비교하지 않는다.
