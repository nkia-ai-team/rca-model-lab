# RCA 벤치마크·인과/증거 문헌

기준일: 2026-10-07 KST. 목적은 공개 benchmark와 causal/evidence 평가 문헌을 현재 ThinkFL RCA 학습 세션에 연결하는 것이다. 수치와 dataset 규모는 원문/공식 페이지에서 확인 가능한 경우만 적고, benchmark 간 점수는 직접 비교하지 않는다.

## 결론

RCA 문헌은 최종 root label만 맞히는 평가에서 벗어나고 있다. 최신 RCA agent/benchmark는 원인 서비스, fault type, fault propagation path, evidence coverage, tool/action trajectory를 분리한다. 현재 `6 case x 6 run`은 regression smoke로는 유용하지만, 모델 개발 성과를 주장하려면 최소한 system/fault-family holdout과 case-level 통계가 필요하다.

검증된 core RCA benchmark/process paper는 현재 **8개**다: RCAEval, OpenRCA, OpenRCA 2.0, Cloud-OpsBench, cloud RCA agent failure analysis, TraceBench, fault-propagation-aware RCA benchmark, trajectory-level RCA agent study. ThinkFL은 자체 모델 학습 선례로 P0지만, 엄밀히는 RCA benchmark가 아니라 failure localization 방법 논문이다.

확인 깊이: OpenRCA 2.0은 본문 방법·평가 항목까지 확인했다. 나머지는 별도 본문 검토가 명시된 RCA 보고서를 제외하면 서지·초록·공식 artifact 설명 수준으로 취급한다. 공개 여부는 재현 완료를 뜻하지 않는다.

## 핵심 benchmark / dataset

| 우선 | 자료 | 확인한 내용 | 현재 적용 |
|---|---|---|---|
| P0 | [RCAEval: A Benchmark for Root Cause Analysis of Microservice Systems with Telemetry Data](https://arxiv.org/abs/2412.17015), 2024/WWW Companion 2025 | microservice telemetry 기반 RCA benchmark. 공개 GitHub/HF artifact가 있다. | 현재 외부 benchmark 후보. coarse/fine RCA와 telemetry modality 분리 기준을 가져온다. |
| P0 | [OpenRCA: Can Large Language Models Locate the Root Cause of Software Failures?](https://openreview.net/forum?id=M4qNIzQYpd), ICLR 2025 | logs/metrics/traces가 포함된 software failure RCA benchmark와 LLM agent 평가. | root 하나 맞히기보다 component, reason, time, evidence 식의 부분 채점 구조를 참고한다. |
| P0 | [OpenRCA 2.0: From Outcome Labels to Causal Process Supervision](https://arxiv.org/abs/2606.27154), 2026 | PAVE step-wise causal annotation. 3개 system(TrainTicket, OpenTelemetry Demo, DeathStarBench Hotel Reservation), 500 evaluable instance, 27 chaos primary kind, instance당 평균 7.5 verified causal edge. exact root-cause-set 평균 20.7%, any correct service 76.0%, verified path grounding 61.5%. | `root`와 `mechanism/propagation`을 분리 채점해야 하는 가장 직접적인 근거. outcome-only 평가는 ungrounded diagnosis를 숨긴다. |
| P0 | [Cloud-OpsBench: A Reproducible Benchmark for Agentic Root Cause Analysis in Cloud Systems](https://arxiv.org/abs/2603.00468), 2026 | 754 runtime-verified case, 57 fault type, OnlineBoutique/TrainTicket, application + Kubernetes platform fault. Joint RCA Accuracy와 Evidence Closure Rate를 분리한다. strongest JRA는 0.76/0.68이지만 ECR은 0.38/0.15로 낮게 보고된다. | 최종 답 정답률이 evidence-grounded diagnosis 능력을 과대평가할 수 있다는 강한 근거. |
| P0 | [AIOpsLab: A Holistic Framework to Evaluate AI Agents for Enabling Autonomous Clouds](https://arxiv.org/abs/2501.06706), 2025/MLSys | fault injection, workload, telemetry export, agent-cloud interface로 cloud operation agent를 평가한다. | 실제 tool API 반복/invalid call/mitigation까지 보는 agent harness 설계 참고. |
| P0 | [Rethinking the Evaluation of Microservice RCA with a Fault Propagation-Aware Benchmark](https://arxiv.org/abs/2510.04711), 2025/2026 artifact | fault propagation-aware benchmark와 artifact가 공개되어 있다. Zenodo artifact는 FSE 2026 paper package를 제공한다. | root entity뿐 아니라 propagation path/edge를 평가하는 기준으로 참고. |
| P0 | [Why Do AI Agents Systematically Fail at Cloud Root Cause Analysis?](https://arxiv.org/abs/2602.09937), 2026 | OpenRCA 전체를 5개 LLM으로 실행해 1,675 agent run을 만들고, reasoning/communication/agent-environment interaction의 12 pitfall type을 분류한다. hallucinated data interpretation과 incomplete exploration을 반복 pitfall로 보고한다. | checkpoint 회귀를 설명할 때 failure taxonomy로 사용한다. 단, 로컬 회귀 원인이 architecture라고 단정하지 말고 로컬 evidence로 확인해야 한다. |
| P1 | [TraceBench: Controlled Evaluation of LLM Agents for Time-Series Root-Cause Attribution](https://arxiv.org/abs/2608.27182), 2026 | simulation 기반 controlled RCA attribution task. 3개 interpretable mechanical system, dataset/agent trajectory/result/leaderboard 공개. | time-series/trajectory RCA 평가 아이디어. microservice RCA와 직접 점수 비교는 금지. |
| P1 | [Beyond Fault Localization: A Trajectory-Level Study of LLM Agents for Microservice Root Cause Analysis](https://arxiv.org/abs/2608.21310), 2026 | 3,500 diagnostic trajectory를 분석하고, 정답 localization과 diagnostic quality가 분리될 수 있음을 보인다. DiagGuard가 held-out setting에서 Acc@1 43.5% -> 52.5%를 보고한다. | 현재 root+mechanism 분리 채점과 evidence-support audit의 직접 근거. |
| P1 | [RCABench: Open Benchmarking Platform for Root Cause Analysis](https://arxiv.org/abs/2303.05029), 2023 | 원래는 crash RCA benchmark platform 계열. 이름이 microservice RCABench와 혼동될 수 있다. | 문헌/도구명 인용 시 `RCABench`가 어떤 계열인지 명시한다. |

## RCA 방법/인과 기반

| 우선 | 논문 | 확인한 내용 | 현재 적용 |
|---|---|---|---|
| P0 | [ThinkFL: Self-Refining Failure Localization for Microservice Systems via Reinforcement Fine-Tuning](https://arxiv.org/abs/2504.18776), 2025 | lightweight LLM, progressive multi-stage GRPO, multi-factor failure localization grader, recursion-of-thought actor. | 자체 모델 방향의 가장 직접적인 선례. 단, 현재 구현은 SFT라 동일 실험이 아니다. |
| P0 | [Automatic Root Cause Analysis via Large Language Models for Cloud Incidents](https://arxiv.org/abs/2305.15778), EuroSys 2024 | RCACopilot. Microsoft incident data에서 alert handler matching, diagnostic info aggregation, root category prediction, explanation을 결합한다. | production incident RCA는 retrieval/aggregation/handler matching이 같이 가야 한다는 근거. |
| P1 | [Automated Root Causing of Cloud Incidents using In-Context Learning with GPT-4](https://arxiv.org/abs/2401.13810), 2024 | 100,000+ production incidents에서 ICL GPT-4 RCA를 비교하고, fine-tuning 비용/유지 문제를 강조한다. | “무조건 fine-tune”보다 retrieval/example selection과 baseline 비교 필요. |
| P1 | [PACE-LM: Prompting and Augmentation for Calibrated Confidence Estimation with GPT-4 in Cloud Incident Root Cause Analysis](https://arxiv.org/abs/2309.05833), 2023 | RCA prediction의 confidence calibration을 다룬다. | 자체 모델 결론에도 confidence/abstain 또는 evidence 부족 판정을 분리한다. |
| P1 | [MicroRCA: Root Cause Localization of Performance Issues in Microservices](https://doi.org/10.1109/NOMS47738.2020.9110353), NOMS 2020 | attributed graph로 application symptoms와 resource utilization을 연결한다. | LLM baseline 외 비LLM telemetry baseline/feature 정의 참고. |
| P1 | [MicroHECL: High-Efficient Root Cause Localization in Large-Scale Microservice Systems](https://arxiv.org/abs/2103.01782), ICSE-SEIP 2021 | service call graph, anomaly propagation chains, correlation ranking, pruning을 사용한다. | propagation-chain과 pruning은 tool query budget 설계에 직접 참고 가능. |
| P1 | [MicroRank: End-to-End Latency Issue Localization with Extended Spectrum Analysis in Microservice Environments](https://doi.org/10.1145/3442381.3449905), WWW 2021 | normal/abnormal trace와 spectrum ranking으로 latency root cause를 찾는다. | trace-heavy fault에서 LLM이 놓치는 deterministic evidence baseline으로 참고. |
| P1 | [CloudRanger: Root Cause Identification for Cloud Native Systems](https://research.ibm.com/publications/cloudranger-root-cause-identification-for-cloud-native-systems), CCGrid 2018 | dynamic causal relationship analysis와 second-order random walk로 culprit service를 찾는다. | causal/impact graph 기반 후보 prior를 LLM 도구에 제공하는 방향 참고. |

## 평가 설계로 바꾼 요구사항

| 평가 축 | 문헌 근거 | 현재 필요한 산출 |
|---|---|---|
| Root entity | RCAEval, OpenRCA, ThinkFL | 서비스/컴포넌트 정답률. 기존 root-hit 유지. |
| Fault type / category | RCAEval, OpenRCA | root가 맞아도 장애 유형을 틀리면 별도 감점. |
| Mechanism / propagation | Beyond Fault Localization, fault-propagation-aware benchmark, MicroHECL | symptom이 root에서 어떻게 전파됐는지 0/.5/1 또는 edge/node F1. |
| Evidence support | OpenRCA, AIOpsLab, trajectory-level study | 결론을 지지하는 observation id/time/window/tool result 존재 여부. |
| Tool/process | ReAct lineage, AIOpsLab, trajectory-level study | invalid/repeated tool call, useful observation rate, stop timing, cost. |
| Calibration | PACE-LM, LLM judge literature | confidence, insufficient-evidence, abstain/unknown 처리. |

## split / leakage 체크리스트

- 같은 incident를 run repeat으로 늘린 값을 독립 표본처럼 쓰지 않는다.
- system holdout: TT4/OB4/O2/Polestar 중 전체 system을 heldout으로 둔다.
- fault-family holdout: resource, network, exception, dependency, data-corruption 등을 family 단위로 분리한다.
- service identity leakage: train과 validation에 같은 service name/fault template이 반복되는지 본다.
- telemetry artifact leakage: 같은 datapack, timestamp, injection manifest가 train/eval 양쪽에 들어가지 않게 한다.
- judge leakage: validation conclusion/rationale을 teacher correction에 넣지 않는다.

## 재사용 문장

공개 RCA benchmark 흐름상 자체 모델 성과는 `root label`, `fault type`, `mechanism/propagation`, `evidence support`, `tool process`를 분리해 case-level로 보고해야 한다. 지금 val36은 빠른 회귀 탐지용이고, 성과 주장은 system/fault-family heldout과 관측 증거 감사가 붙은 뒤에 가능하다.
