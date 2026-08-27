# Student harness 이관 상태

## 기준

- 새 정본: `src/rca_lab/harness`
- 이전 구현 참조: `lucida-next/backend/services/ai/features/operator/rca/{agentloop,rlmenv}`
- 학습 데이터·평가 결과·모델 가중치: git 제외, HF Hub private에서 버전 관리

## 이식 완료

| 계약 | 구현 |
|---|---|
| 원본 증거 무손실 저장 | `EvidenceEnvironment` |
| prompt/query 결과만 cap | `prompt_view`, `EvidenceQuery.limit` |
| action/proof/fact enum | `models.py` |
| 실제 target/metric/source/kind registry | `registry.py` |
| 모든 probe의 typed ledger | `Observation`, `Fact`, `Episode` |
| support/counter ref 검증 | `HarnessValidator` |
| 원인별 confirmed proof | `HarnessValidator.evaluate_proof` |
| eval/reward 단일 scorer | `TypedScorer` |
| RL episode 필드 | prompt/response/token/logprob/ledger/reward 포함 `Episode` |

## 다음 연결 순서

1. `rca-scenario-runner` read-only adapter
2. tool executor와 idempotent scenario reset/health/checksum
3. Codex CLI blind teacher + HHD navigation correction
4. student loop의 비용/진행 기반 동적 종료
5. rollout → scorer → advantage → LoRA update

Gold label은 scorer 입력에서만 사용한다. actor prompt, tool observation, teacher correction에는 노출하지 않는다.
