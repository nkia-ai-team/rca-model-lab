# 추가 직접 관련 연구

기준일 2026-10-07. 아래 3편은 **서지·초록 확인** 수준이다. 본문 실험 통제·재현 자산 검증은 미완료이며 핵심 정독 논문과 구분한다.

| 논문 | 버전·출처 | 확인된 내용 | 적용 가설·제약 |
|---|---|---|---|
| How Far Can Root Cause Analysis Go on Real-World Telemetry Data? | Gopal, Krishnan; 2026-07-15 v1. [원문](https://arxiv.org/abs/2607.13548) | 정답을 아는 reverse reasoning 분석으로 필요한 증거의 부재와 증거 활용 실패를 구분한다. 저자는 reasoning/domain knowledge가 주요 제약이라고 주장한다. | 도구 개선만으로 해결된다고 단정하지 말 것. reverse reasoning은 사후 감사이며 실제 blind rollout 능력과 구분. 우리 train 오류를 evidence-missing / evidence-unused로 나눠 보는 가설 |
| OpsAgent: An Evolving Multi-agent System for Incident Management in Microservices | Luo et al.; 2025-10 최초, 2026-05-12 v3. [원문](https://arxiv.org/abs/2510.24145v3) | 관측 데이터의 구조화, multi-agent 협업, 모델 갱신과 외부 경험 축적을 결합한다. | 모델 가중치 효과와 외부 memory/scaffold 효과를 별도 ablation해야 함. 초기 제목 From Observability Data to Diagnosis와 같은 paper ID로 중복 계산하지 않음 |
| Rethinking the Evaluation of Microservice RCA with a Fault Propagation-Aware Benchmark | Fang et al.; 2025-10 최초, 2025-12-23 v2; arXiv에 FSE'26 수락 명시. [원문](https://arxiv.org/abs/2510.04711) | 공개 벤치마크의 단순성을 점검하고 복잡한 전파와 사용자 SLI 영향을 확인한 장애 데이터셋을 제시한다. | 높은 공개셋 점수만으로 운영 일반화 주장 금지. OpenRCA 2.0 논문과 별도 paper ID이나 저자·데이터 계보를 고려해 독립 재현 증거로 과대 집계하지 않음 |

이 표의 적용 열은 조사자 제안이며 저자 연구로 우리 코드에 대해 검증된 결론이 아니다.
