# P0 확장: 최종 행동을 포함한 승인 검증

2026-10-07. [계획](execution-20261007-evidence-audit.md), [모델별 계약](base-model-contract-20261007.md).

## 실행

기존 pilot6사례를 제외하고 완료된 TT4 train12사례에서 마지막 submit_rca12턴과 별도 legacy거절5턴을 수집했다. 총17턴이며 final12턴 중1개도 기존거절이었다. 명시 split, sourceview, caseidentity, sourceSHA를 검증하고 원본 snapshot을 고정했다. 크기는 compact per-item JSON26,737~101,561자이며 토큰 수가 아니다.

고정 rubric `mcp-evidence-admission-v1`, Codex `gpt-6.1-sol`로 rationale support / action support / consistency를 독립 평가했다. 기존 score와 case경로는 prompt에 넣지 않았다. 인간 감사는 미실시.

## 결과

| 과거 판정 | 새 승인 | 새 보류 | 새 제외 후보 | 합계 |
|---|---:|---:|---:|---:|
| 기존 통과 | 0 | 1 | 10 | 11 |
| 기존 거절 | 0 | 0 | 6 | 6 |
| 합계 | 0 | 1 | 16 | 17 |

제외 후보는 명시적 관측 모순 또는 추론문/행동 불일치다. 보류는 근거 부족이다. 모든 root정답이 틀렸다는 뜻이 아니다. 소규모 비무작위 TT4표본이며 전체 데이터 오염률이나 Sol정확도로 해석하지 않는다. 원본 rationale의 supervise flag와 export시 추가규칙이 있으므로, 감사에서 거절된17턴이 모두 실제 학습에 포함됐다고도 단정하지 않는다.

사례 `00-36`: 추론문이 contention/propagation 미입증을 인정하지만 제출은 confirmed로 단정. `06-50`: 다른 서비스 metric shift가 관측됐는데 제출은 없다고 주장하며, saturation미입증을 인정한 추론과 확정행동이 어긋남. 모든 항목의 근거 ref와 사유는 교정 대기 목록에 보존했다.

## 구현 및 재개

- `evidence_admission.py`: 관측에 의해 뒷받침되는 rationale+action 및 서로 일치할 때만 승인. 추가 budget생략/미완료/부족은 보류. inherited rolling절단만으로 자동 거절하지 않는다.
- `mcp_judge_evidence.py`: item SHA/rubric/model별 cache, 프로세스 lock, atomic결과저장, 성공 재사용, quota/timeout/malformed는 pending. 입력hash불일치 거부.
- `mcp_evidence_audit.py`: final및legacy거절 표본 선정, 기존pilot 제외 옵션.
- 근거 부족 판정 자체가 관측 부재를 이유로 드는 것은 허용한다. 잘린 관측의 부재로 supported/contradicted를 확정하는 것은 거부한다.
- 1건은 absence_based와 contradiction을 혼합해 validator에 걸렸다. 원응답 보존 후 검증 오류를 알려 동일rubric으로 다시 요청했고 유효응답을 얻었다. 평가 기준을 완화하거나 수동으로 점수를 고치지 않았다.
- 최종 재실행은17/17 cache재사용, pending0. 기존mechanism판정/원본데이터/학습서비스 변경 없음.

```bash
uv run python scripts/mcp_judge_evidence.py \
  --input outputs/evidence-audit-20261007/expanded/items.jsonl \
  --output outputs/evidence-audit-20261007/expanded-judged \
  --limit 17 --timeout 180
```

## 다음 결정

이번 표본에서 새 승인target이 없으므로 P1 GPU학습을 추가기동하지 않았다. 다음은 원본 관측을 바탕으로 finalaction과 rationale을 함께 교정하거나 감독에서 제외하는 독립 데이터버전이다. 근거 없는 확정원인은 단순 문장 미화로 해결하지 않는다. 교정후동일rubric 재감사, 기존오류재발검사, modelgradient/runtimeprefix gate를 통과해야 SFT비교로 간다. 기존recovery chain은 별도로 진행중이며 이 감사의 승인완료 모델로 간주하지 않는다.

산출물: [요약](../outputs/evidence-audit-20261007/expanded-judged/summary.json), [승인판정](../outputs/evidence-audit-20261007/expanded-judged/admission.json), [교정대기목록](../outputs/evidence-audit-20261007/expanded-judged/repair-queue.json), [고정rubric](../outputs/evidence-audit-20261007/expanded/rubric.txt).
