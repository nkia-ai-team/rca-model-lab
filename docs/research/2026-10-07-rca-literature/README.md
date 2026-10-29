# ThinkFL·RCA 자체 모델 개발 문헌 조사

기준일: **2026-10-07 KST**. 목적: 현재 RCA 모델의 성능 저하를 설명할 검증 가능한 가설, 직접 비교할 선행 연구, 다음 실험의 우선순위.

**중복 제거 52편**을 정리했다. [전체 논문·원문 목록](catalog.md). 핵심 ThinkFL·FoundRoot·OpenRCA 2.0·TRACE는 방법/평가 본문까지 확인했으며, 나머지의 확인 깊이는 개별 보고서에 구분했다.

## 먼저 볼 결론

1. **현재 실행은 ThinkFL 재현 실험이 아니다.** 복구된 teacher 행동과 사후 추론문을 학습하는 SFT chain이다. ThinkFL의 단계별 RL·reward·도구·benchmark와 구분해야 한다.
2. **grounding 감사가 가장 먼저다.** 현재 rationale judge에는 실제 관측이 입력되지 않는다. `grounded=true`만으로 관측 근거가 검증됐다고 해석할 수 없다.
3. **원인 서비스 정답, 기전 설명, 관측 증거 수집은 다른 목표다.** 관련 논문의 localization 점수를 우리 strict 성능으로 환산하지 않는다.
4. **RL은 마지막 선택이 아니라 검증 순서의 뒤쪽이다.** 먼저 데이터·초기 SFT·검증 가능한 reward를 확보한다. RL 개선과 오탐 악화를 함께 보고해야 한다.
5. **현재 36회는 6개 사례의 반복이다.** 기반 모델 strict21/36, checkpoint94 strict18/36은 현재 관측. 하락의 원인이나 일반화 성과를 입증하지 않는다.

코드에 대한 근거: [구현 대조](implementation-crosswalk.md). 실험 순서·통제·성과 기준: [실험 계획](experiment-plan.md). TRACE의 보상 설계 반례: [최신 인접 연구](recent-diagnostic-rl.md).

## 조사 묶음

| 묶음 | 내용 |
|---|---|
| [RCA 모델·에이전트](rca-agents.md) | ThinkFL, 직접 비교 가능한 RCA 연구, 학습과 prompting 구분 |
| [벤치마크·인과 분석](benchmarks-causality.md) | 공개 데이터·분할·도구 환경·원인/기전 지표와 일반화 |
| [에이전트 학습](agent-training.md) | trajectory SFT, distillation, on-policy correction, RL |
| [판정·평가 신뢰도](evaluation-judges.md) | LLM judge, rationale faithfulness, process reward, 반복 실행 통계 |
| [TRACE](recent-diagnostic-rl.md) | 2026-09 광고 진단 RL, RCA에는 간접 증거 |
| [추가 직접 관련 연구](additional-primary-papers.md) | 본문 정독 전 서지·초록 확인 수준의 최신 RCA 논문 |
| [현재 구현 대조](implementation-crosswalk.md) | 실제 코드에 확인된 계약과 논문 적용의 간극 |
| [실험 계획](experiment-plan.md) | P0–P4, 단일 변수·예산 통제·성과 기준 |

## 조사 방법과 범위

- 2026-10-07까지 공개된 연구를 대상으로 RCA 직접 연구를 우선하고, 필요한 일반 agent 학습·평가의 기반 논문을 확장 조사했다.
- 4개 독립 조사 축에서 논문 원문(arXiv, 학회/출판사, 저자 PDF)과 저자 공개 저장소를 확인했다. 검색 요약·블로그만으로 수치나 재현성을 확정하지 않았다.
- 검색 축: ThinkFL / LLM root cause analysis / RCAEval / OpenRCA / diagnostic agent benchmark / agent trajectory SFT distillation RL / LLM judge faithfulness process reward.
- 논문별 확인 깊이(본문/일부/초록), 공개 자료, 한계를 기록했다. 동명 연구와 버전 변경은 가능한 한 canonical paper ID 기준으로 구분한다.
- 이 문서는 광범위한 목적 지향 조사다. 모든 출판물 누락이 없음을 보증하는 체계적 전수 문헌고찰은 아니다. 유료 원문 접근·비공개 데이터·독립 재현이 필요한 부분은 미확인으로 남긴다.
- 논문 저자의 보고, 코드에서 직접 확인한 사실, 조사자의 적용 가설을 구분한다. 비교 대상·metric·데이터가 다른 논문 수치를 성능 순위로 합치지 않는다.
- 코드/모델/데이터의 공개 링크 확인은 실제 학습 재현·라이선스 적합성 검증 완료와 다르다. 이번 조사에서 새 모델 다운로드·학습·서비스 변경은 하지 않았다.

## 먼저 읽을 논문과 결정할 실험

| 논문 | 개발에 가져올 판단 |
|---|---|
| [ThinkFL](https://arxiv.org/abs/2504.18776) | 단계별 보상과 탐색을 포함한 학습 계약을 현재 SFT와 구분 |
| [FoundRoot](https://netman.aiops.org/wp-content/uploads/2026/01/foundroot_camera_ready.pdf) | 구조화된 SFT 이후 RL 비교 설계 |
| [OpenRCA 2.0](https://arxiv.org/abs/2606.27154) | 결과와 인과 경로 평가를 분리 |
| [Cloud-OpsBench](https://arxiv.org/abs/2603.00468) | 정답률과 관측 증거 충족률을 분리 |
| [AgentTuning](https://arxiv.org/abs/2310.12823) | 순차 학습 대비 혼합/replay 대조 실험 |
| [CoT faithfulness](https://arxiv.org/abs/2307.13702) | 사후 추론문을 충실한 사고 기록으로 가정하지 않기 |
| [TRACE](https://arxiv.org/abs/2609.10315) | RL 전체 점수와 무장애 오탐을 동시에 확인 |
