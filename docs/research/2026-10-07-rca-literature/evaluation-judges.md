# 평가/채점 관련 문헌: LLM judge, process reward, rationale faithfulness

조사 기준일: 2026-10-07 KST. 범위는 ThinkFL RCA 학습/평가에서 `gpt-6.1-sol` judge 점수, root-hit/mechanism 채점, 반복 실행 통계를 해석하는 데 직접 필요한 논문이다. 원문/학회 서지 페이지와 초록으로 확인된 주장만 수치로 적었다. 아래 문헌 확인 깊이는 주로 abstract/metadata page 기준이며, full-paper 재현 검토가 아니다.

## 바로 결론

현재 Sol 계열 judge는 빠른 선별용으로 쓸 수 있지만, 단독 oracle로 쓰면 위험하다. 다만 두 judge 계약은 분리해야 한다. `src/rca_lab/mcp/judge.py`의 `id/reasoning/actual_call` 입력은 **학습용 rationale judge**이고, `src/rca_lab/mcp/rl_reward.py`의 `judge_mechanism(reference, conclusion)`은 **reference cause와 candidate conclusion의 mechanism 동등성 평가**다. 따라서 strict mechanism 점수를 "사후 reasoning-call plausibility"라고 설명하면 틀리다. 별도로, 둘 다 관측 로그가 결론을 실제로 지지하는지까지 직접 검증하는 evidence-support judge는 아니다.

현재 6개 case x 6 run 결과도 36개 독립 표본처럼 해석하면 안 된다. case가 같은 반복 관측이므로 case-level paired summary, cluster/bootstrap over case, disagreement audit로 봐야 한다. 6 case 단계에서는 "방향성/오류 유형 발견"까지가 안전하고, "모델 A가 우월" 같은 성능 주장은 너무 세다.

우선 실행은 다음 순서가 맞다.

1. observation 포함 evidence-support audit 소표본을 만든다. 기존 mechanism 점수를 대체하지 말고, 결론이 실제 관측 증거로 지지되는지를 별도 축으로 본다.
2. human audit를 train-only/heldout-disagreement 층화 표본으로 작게 붙인다. 임의 목표 일치율을 만들지 말고 Sol-human 불일치 유형을 수집한다.
3. 평가표는 root hit, mechanism 0/.5/1, evidence support, tool cost를 분리한다. 합산 점수 하나로 덮지 않는다.
4. training reward에는 사후 rationale을 process label로 바로 쓰지 않는다. observation-grounded step/action verifier가 성능에 도움이 되는지 별도 가설로 검증한다.
5. 보고서는 36회 평균보다 case별 승패/동률과 신뢰구간을 우선한다.

## 핵심 논문 12개

| 축 | 논문 | 확인 깊이 | 확인한 근거 | ThinkFL 적용 |
|---|---|---|---|---|
| LLM-as-judge 기본 | [Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena](https://arxiv.org/abs/2306.05685), Zheng et al., 2023, NeurIPS 2023 Datasets and Benchmarks | arXiv metadata/abstract | GPT-4 judge가 human preference와 높은 일치를 보일 수 있지만 position, verbosity, self-enhancement bias와 reasoning 한계를 명시한다. 초록상 80%+ agreement를 보고한다. | Sol judge 자체는 유효한 도구지만 bias check 없이 절대점수화 금지. pair/order/verbosity probe 권장. |
| LLM judge 프레임 | [G-Eval: NLG Evaluation using GPT-4 with Better Human Alignment](https://arxiv.org/abs/2303.16634), Liu et al., 2023 | arXiv metadata/abstract | CoT + form-filling 평가 프레임. 요약 task에서 GPT-4 G-Eval Spearman 0.514를 보고하고, LLM 생성물 선호 bias 가능성을 적는다. | rubric/form 점수화는 쓸 수 있다. 다만 "긴/잘 쓴 rationale" 선호를 evidence support로 착각하지 않게 별도 항목을 둔다. |
| 위치 편향/보정 | [Large Language Models are not Fair Evaluators](https://arxiv.org/abs/2305.17926), Wang et al., 2023/ACL 2024 | arXiv metadata/abstract | 응답 순서 변경만으로 평가가 크게 흔들릴 수 있음을 보이고, balanced position calibration, multiple evidence, human-in-loop를 제안한다. | 두 모델 비교형 judge를 만들면 swap/반복 집계를 넣는다. 단일 순서 win rate는 낮은 신뢰로 표시한다. |
| rubric judge 학습 | [Prometheus: Inducing Fine-grained Evaluation Capability in Language Models](https://arxiv.org/abs/2310.08491), Kim et al., 2023/ICLR 2024 | arXiv metadata/abstract | reference answer와 score rubric을 동반하면 open evaluator가 GPT-4급 평가 성능에 접근할 수 있다고 주장한다. 초록상 human Pearson 0.897, GPT-4 0.882, ChatGPT 0.392를 보고한다. | mechanism judge에는 reference cause와 rubric이 중요하다. evidence-support까지 보려면 reference evidence/observation 축을 별도 설계해야 한다. |
| 길이 편향 | [Length-Controlled AlpacaEval: A Simple Way to Debias Automatic Evaluators](https://arxiv.org/abs/2404.04475), Dubois et al., 2024/COLM 2024 | arXiv metadata/abstract | LLM auto-annotator가 긴 답을 선호하는 confounder를 다루고, length control로 AlpacaEval-Chatbot Arena Spearman 0.94 -> 0.98을 보고한다. | conclusion/rationale 길이와 점수 상관을 확인한다. strict mechanism judge가 길이를 보상한다는 직접 증거는 아니므로 probe로만 둔다. |
| judge ensemble | [Replacing Judges with Juries: Evaluating LLM Generations with a Panel of Diverse Models](https://arxiv.org/abs/2404.18796), Verga et al., 2024 | arXiv metadata/abstract | 여러 작은/다양한 judge panel이 단일 GPT-4 judge보다 일부 설정에서 좋고 비용이 7x+ 낮다고 보고한다. | Sol 단독 판정은 기준선으로 두되, 중요한 결론 전에는 cross-family judge 또는 사람 감사로 disagreement만 잡는다. |
| outcome verifier | [Training Verifiers to Solve Math Word Problems](https://arxiv.org/abs/2110.14168), Cobbe et al., 2021 | arXiv metadata/abstract | 여러 후보 풀이를 만들고 verifier가 correctness를 고르는 방식이 finetuning baseline보다 데이터 확장성이 좋다고 보고한다. GSM8K 8.5K도 도입한다. | RCA에서도 후보 결론/행동을 만들고 verifier로 선택하는 설계가 가설로 가능하다. 관측 입력의 효과는 별도 검증 대상이다. |
| process supervision | [Let's Verify Step by Step](https://arxiv.org/abs/2305.20050), Lightman et al., 2023 | arXiv metadata/abstract | outcome supervision보다 process supervision이 MATH에서 낫다고 보고한다. PRM800K 800,000 step-level human labels, representative subset 78% 해결을 명시한다. | 최종 root-hit만 reward로 주면 중간 탐색 품질을 놓칠 수 있다는 가설을 세울 수 있다. RCA step-level label 필요성은 직접 증거가 아니라 적용 제안이다. |
| rationale 불충실성 | [Language Models Don't Always Say What They Think: Unfaithful Explanations in Chain-of-Thought Prompting](https://arxiv.org/abs/2305.04388), Turpin et al., 2023/NeurIPS 2023 | arXiv metadata/abstract | CoT가 실제 결정 이유를 왜곡할 수 있고, bias feature를 설명에 언급하지 않은 채 합리화할 수 있음을 보인다. 초록상 일부 설정에서 accuracy drop up to 36%를 보고한다. | 현재 rationale은 posthoc이므로 행동 원인으로 취급하지 않는다. rationale loss ablation은 권장 실험이다. |
| CoT faithfulness 측정 | [Measuring Faithfulness in Chain-of-Thought Reasoning](https://arxiv.org/abs/2307.13702), Lanham et al., 2023 | arXiv metadata/abstract | CoT에 개입해 답이 바뀌는지 측정한다. task/model에 따라 CoT 의존도가 크게 다르고, 더 큰 모델에서 faithfulness가 낮아지는 경향을 보고한다. | reasoning을 학습 신호로 쓸 때 "reasoning 제거/교란 시 action이 바뀌는가"를 별도 평가하는 실험을 제안한다. |
| 작은 샘플 power | [With Little Power Comes Great Responsibility](https://aclanthology.org/2020.emnlp-main.745/), Card et al., EMNLP 2020 | ACL anthology metadata/abstract | 작은 test set과 human rating study가 작은 차이를 검출하기엔 underpowered인 경우가 많다고 정리한다. | 6 case 평가는 효과 크기 주장보다 후보 필터링/오류 유형 분석으로 제한한다. |
| 유의성 테스트 선택 | [The Hitchhiker's Guide to Testing Statistical Significance in Natural Language Processing](https://aclanthology.org/P18-1128/), Dror et al., ACL 2018 | ACL anthology metadata/abstract | NLP metric/task 구조에 따라 통계 테스트 선택이 달라진다고 설명하고, test selection protocol을 제시한다. | paired/run 구조를 무시한 단순 평균 비교 금지. case-clustered paired test 또는 bootstrap을 명시한다. |

## 현재 채점 설계에 대한 구체 권고

### Sol mechanism judge와 rationale judge

- 유지: root-hit와 mechanism 0/.5/1 분해는 계속 유용하다. mechanism judge는 reference cause와 candidate conclusion의 동등성 평가로 해석한다.
- 분리: rationale judge는 reasoning이 실제 다음 tool call을 정당화하는지 보는 학습 데이터 품질 검사다. strict mechanism eval과 섞어 말하지 않는다.
- 추가: observation snippet, evidence id/time/window, expected causal path를 보는 evidence-support audit를 별도 축으로 만든다.
- 감사: mechanism 점수와 evidence-support 점수가 갈리는 sample을 사람이 본다. 갈림은 training/eval 설계 개선 신호로 쓴다.
- 금지: Sol 점수를 바로 reward oracle로 쓰거나, 비어 있던 mechanism 점수만 채웠다고 데이터가 완전히 깨끗해졌다고 말하지 않는다.

### 사후 rationale

현재 구조에서는 writer가 conversation과 exact next call을 받고 reasoning을 쓴다. 이건 행동 이전의 사고 기록이 아니라 행동 조건부 설명이다. Turpin/Lanham 계열 결과상 그럴듯한 설명은 faithful process 보장이 아니다.

권장 실험은 `reasoning_loss_weight=0`, 현재값, action-only 평가를 같은 데이터/초기화/step budget으로 비교하는 것이다. mechanism 점수 개선이 root-hit 하락을 숨기지 않게 둘을 분리한다.

### 통계 보고

- 단위: 6 case가 1차 단위, run은 같은 case 안 반복 관측.
- 표: case별 base/ckpt root-hit, strict mechanism majority, 평균 tool cost, disagreement count.
- 추론: 6 case에서는 exploratory label을 붙인다. confidence interval은 넓게 나올 것이 정상이다.
- 비교: 같은 case/seed 대응이 보장될 때 paired; 보장 안 되면 "동일 조건 반복"이 아니라 "비동일 반복 관측"으로 낮춰 말한다.

## 우선순위 실험안

1. **Evidence-support audit:** 기존 mechanism 점수와 observation 포함 evidence-support 점수 30개 내외 비교. 목표는 점수 상승이 아니라 불일치 유형 목록이다.
2. **Case-clustered report:** 기존 6x6 결과를 case 단위로 재집계. `36 independent` 표현 제거.
3. **Rationale ablation:** 동일 action labels에서 rationale loss를 끄거나 낮춰 root/mechanism/tool cost를 비교.
4. **Judge bias probes:** 같은 답의 rationale 길이 축약/확장, answer order swap, model name blind 처리.
5. **Human audit seed set:** Sol-human 불일치와 mechanism 0.5 경계 사례를 모아 고정 calibration set으로 둔다.

## Caveats

- 위 논문들은 대부분 open-ended generation, math, instruction following 평가다. 마이크로서비스 RCA에서 그대로 수치 전이하면 안 된다.
- process supervision 논문은 step label 품질이 있을 때 강하다. 현재 posthoc rationale은 step label 후보로도 신중히 다뤄야 하며, 정답 process가 아니다.
- judge ensemble/PoLL은 비용과 latency를 늘린다. 지금은 full ensemble보다 disagreement-triggered audit가 더 현실적이다.
