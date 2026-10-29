# 최신 인접 연구: TRACE

조사 기준일: 2026-10-07 KST. 마이크로서비스 RCA 직접 증거와 구분한다.

## TRACE: Training Reasoning Agents for Causal Exploration with Synthesized Rewards

- Rui Sun, Zhan Shi, Bing He; 2026-09-09, arXiv preprint v1. [서지](https://arxiv.org/abs/2609.10315), [원문](https://arxiv.org/html/2609.10315v1).
- 확인 수준: 본문, Table 2–4, Appendix B–C. 광고 진단 시뮬레이터에서 숨긴 개입 원인·영향 구간을 Python/SQL로 찾는 과제. 마이크로서비스 실험이 아니다.
- 저자 보고: 235개 held-out episode의 FullAttr@1, Qwen3.5-35B base 0.159 → SFT 0.637 → SFT+RL 0.757. graded reward만 사용한 SFT+RL은 0.596. 보상 설계에 따라 SFT보다 악화될 수도 있다. [Table 2–3](https://arxiv.org/html/2609.10315v1#S4).
- 반대 증거: no-signal 정확도는 SFT 0.76 → SFT+RL 0.49. 전체 점수 상승이 무근거 원인 지목 감소를 뜻하지 않는다. evidence 설명은 학습 reward에 포함되지 않는다. [§3.1, Table 4](https://arxiv.org/html/2609.10315v1#S3).
- 재현 한계: 검토한 본문에서 저자 코드·데이터 저장소 링크는 확인하지 못했다. API/open 모델의 실행 한도도 동일하지 않다. 독립 재현은 수행하지 않았다. [Appendix C](https://arxiv.org/html/2609.10315v1#A3).

## 현재 프로젝트 적용: 조사자 제안, 논문에서 검증된 결과 아님

1. 장애 주입의 구조화된 정답과 모델이 관찰 가능한 데이터를 분리한다. 주입 성공만 확인하지 말고, 관측으로 구별 가능한 사건인지 점검한다.
2. root-hit 보상, 기전 완결성, 증거 타당성, 무장애/정보 부족 상황의 오탐을 별도 측정한다. 단일 합계로 숨기지 않는다.
3. 대규모 RL 전, 고정된 소규모 train-only replay에서 verifier와 reward를 감사한다. 현재 Sol 설명 채점을 객관적 oracle과 동일시하지 않는다.
4. SFT → RL 개선은 가설이다. 현재 30B 모델·LoRA·운영 도구·데이터셋으로 수치를 전이 예측하지 않는다.
