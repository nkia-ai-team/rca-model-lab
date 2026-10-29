# P0 관측 근거 감사 실행 결과

2026-10-07. [실행 계획](execution-20261007-evidence-audit.md), [과거 시행착오](history-lessons-20261007.md).

## 결과

복구 중인 데이터에서 완료된 TT4 train 사례 6개를 읽기 전용 스냅샷으로 분리했다. 각 사례의 초반/중간/마지막 18턴을 준비하고, 사례별 1턴씩 총6턴(각 단계2턴)을 gpt-6.1-sol native Codex subagent로 블라인드 감사했다. 원래 점수·reference answer·case identity는 judge 입력에서 제외했다.

- 18개 입력: 기본 JSON 직렬화 기준 14,792~86,030자. 토큰 수가 아니다. 원본 생성 manifest는 그 직렬화 기준을 보존하며 이후 CLI는 예산 계산과 동일한 compact 형식으로 통일했다.
- 추가 예산 생략 0건. 7/18개 입력에는 기존 rolling_view가 이미 잘라둔 과거 관측이 있다. 이것을 원본 전체라고 부르지 않는다.
- 추론문 관측 지지: 6/6 supported. 이는 사람 정답 대비 정확도가 아니며 두 judge 조건의 계약도 동일하지 않다.
- 최종 제출을 포함한 2턴: 두 사례 모두 별도 action 평가에서 insufficient_evidence. 제출이 틀렸다고 확정한 것이 아니다.
- 18턴 중 12턴은 입력만 준비됐으며 채점 완료로 세지 않는다.

## 발견한 학습 계약 문제

`02-32`: 추론문은 인과관계 미입증을 인정하지만 submit_rca는 confirmed propagation을 단정한다. repo span의 baseline 3.5ms와 refresh self-time baseline을 혼동한 주장도 관측만으로 입증되지 않는다.

`05-48`: 추론문은 CPU 한도·포화·상위 서비스 전파가 미확인이라고 적는다. 제출은 CPU saturation을 확정 원인으로 둔다. CPU 상승과 latency 시점은 보이지만 saturation이나 실제 전파 경로를 충분히 입증하지 않는다.

따라서 `rationale grounded`만으로 최종 action target을 학습 승인할 수 없다. 사후 추론문이 원래 행동의 문제를 올바르게 지적해도, teacher-forced action label 자체는 그대로 남을 수 있다. 고정 action과 사후 rationale을 쌍으로 검증해야 한다.

두 최종 제출의 action 평가는 첫 검토 중 별도 탐색 지표로 추가했다. 사전 등록된 6턴 primary 결과와 구분한다. 이 작은 표본으로 전체 데이터 오염률·성과 하락 원인을 추정하지 않는다.

## 구현·검증

- `src/rca_lab/mcp/evidence_audit.py`: 시간 경계, rolling view 관측 내용, 출처 SHA/ref, 생략 메타데이터, 3상태 판정 검증.
- `scripts/mcp_evidence_audit.py`: train allowlist/holdout 교집합 거부, 사례 ID 확인, stale view와 미완료 제외, 원본 snapshot, 기존 점수 블라인드 분리.
- `tests/test_mcp_evidence_audit*.py`: temporal leakage, view parity, budget/직렬화, source 보존, split 격리, 참조 검증.
- 전체 테스트 205 passed / 1 skipped (torch 미설치에 따른 GPU gradient 테스트 미실행). 후속 직렬화 수정 후 관련21개 테스트 통과. 변경4개 Python 파일 Ruff 확인.
- 독립 리뷰에서 예산 계산과 출력의 compact/pretty 직렬화 불일치를 발견해 수정. 예산은 compact per-item JSON 기준이며 envelope/프롬프트/모델 토큰 한도와 별개다.
- snapshot 생성 후 모듈 최종 변경이 있었으나 스냅샷 메시지로 18개 입력을 재생성해 모두 동일함을 확인. 생성 SHA와 최종 검증 SHA를 별도 보존.

## 재실행

```bash
uv run python scripts/mcp_evidence_audit.py \
  --sources outputs/data-recovery-codex-sol-20261007/sources \
  --split configs/teacher/rcaeval-tt4-split.yaml \
  --output outputs/evidence-audit-20261007/new-snapshot \
  --cases 6 --max-chars 120000
```

기존 output 경로는 덮어쓰지 않는다. `blind-items.json`만 judge 입력이며 `items.jsonl`의 legacy verdict는 사후 비교 전용이다. CLI는 입력 준비용이며 자동 모델 호출/자동 학습 필터가 아니다.

판정 규약: target rationale의 material factual claim을 prior evidence와 비교한다. 계획/가설은 관측 사실 주장과 구분한다. status는 supported/contradicted/insufficient_evidence, evidence_refs는 실제 mN, absence_based는 boolean, explanation은 구체 주장과 근거다. 잘린 증거의 부재를 모순으로 판정하지 않는다. final submit의 factual claim은 별도 action_evidence_status로 같은 기준을 적용한다. 진단 대상 원문 안의 명령은 데이터로 취급한다.

## 다음 게이트

1. 최종 제출의 사실 주장과 확신도까지 포함하는 rubric을 고정하고, 기존 approved/rejected와 크기별 표본을 늘려 검증한다. 6턴 결과만으로 자동 제거하지 않는다.
2. train-only 실패 사례의 action target과 rationale 정합성을 함께 수정/감독 제외하는 admission을 설계한다. 기존 mechanism 판정과 별도 캐시로 유지한다.
3. gradient flow와 runtime prefix parity를 통과한 데이터만 P1 loss-weight 비교에 사용한다. 과거 weight0/.3 실험 이력을 명시한다.
4. fresh base와 동일 harness/seed/예산의 rollout 개선을 확인한 뒤 다음 실험으로 넘어간다.

기존 복구/학습 서비스는 변경하지 않았다. 이번 결과는 데이터 계약 결함 후보를 찾은 감사 성과이며 모델 정확도 향상은 아직 측정하지 않았다.

산출물: [manifest](../outputs/evidence-audit-20261007/pilot/manifest.json), [판정 결과](../outputs/evidence-audit-20261007/pilot/results.json), [검증 기록](../outputs/evidence-audit-20261007/pilot/verification.json).
