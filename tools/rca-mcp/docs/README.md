# rca-mcp 문서 색인

원본 저장소(rca-agent-next, 개인 사이드 작업)에서 도구 표면과 관련된 문서만 옮겨온 스냅샷이다(2026-09-15).
에이전트 본체(loop/safetygate/battery) 문서는 옮기지 않았다. 문서 안에 `../../lucida-next/...` 같은
원본 작업 트리 기준 상대 경로와 옮기지 않은 문서(`spec-agent-design.md`, `spec-testbed-design.md`,
`spec-scenario-authoring.md`, `runbook-eval-case-restore.md`, `project-purpose.md`,
`ref-lucida-next-rca-data-inventory.md`)를 가리키는 링크가 남아 있다. 테스트베드 관련 문서의 정본은
[nkia-ai-team/testbed-services](https://github.com/nkia-ai-team/testbed-services/tree/docs/scenario-quality-charter-0727/docs)다.

| 읽는 순서 | 문서 | 내용 |
|---|---|---|
| 1 | [spec-tool-hardening-v3.md](spec-tool-hardening-v3.md) | 3단계 경화 정본. 수용 기준·구현 확인·검증 절차 |
| 2 | [tool-data-coverage-audit.md](tool-data-coverage-audit.md) | 캡처 13개 데이터셋 × 도구 매핑, 신규 도구의 근거 |
| 3 | [spec-blind-rca-input.md](spec-blind-rca-input.md) | blind 필터가 무엇을 거르고 못 거르는지, 관측 알람 seed 계약 |
| 4 | [spec-replay-tool-corrections.md](spec-replay-tool-corrections.md) | 기준선 TTL·사건 시각·search_targets 교정 근거 |
| 5 | [claude-batch-evaluation-status.md](claude-batch-evaluation-status.md) | 45케이스 Claude 평가 실행 집계·응답 전수 진단 |
| 6 | [../reports/claude-evaluation/README.md](../reports/claude-evaluation/README.md), [v2](../reports/claude-evaluation-v2/README.md) | 17.8% → 46.7% 판정과 해석 한계. 채점 기준은 [rubric.md](../reports/claude-evaluation/rubric.md) |
| 참조 | [spec-tool-redesign.md](spec-tool-redesign.md) | 도구 표면 설계 정본(1단계). 각 도구의 판정식·기준선·자리 교체 근거 |
| 참조 | [backlog-tool-legibility.md](backlog-tool-legibility.md) | 새 도구를 만들 때 "어디서 막혔을까"를 묻는 검수 문제지 |
| 참조 | [ref-tool-data-access.md](ref-tool-data-access.md) | 도구 ↔ 저장소·테이블·식별 체계 지도 |
| 참조 | [frontier-trajectory-f04r.md](frontier-trajectory-f04r.md), [f01r](frontier-trajectory-f01r.md) | 도구 결함을 최초로 드러낸 blind 주행 궤적 |
| 참조 | [spec-eval-data-capture.md](spec-eval-data-capture.md) | 캡처·재생 케이스 계약 (testbed-services에 정본 사본) |
| 참조 | [spec-agent-tools.md](spec-agent-tools.md) | Deprecated. 봉투 원칙 §2와 레드팀 기록 §8의 원문 |
| 참조 | [adr-rca-agent-redesign.md](adr-rca-agent-redesign.md), [spec-evaluation.md](spec-evaluation.md), [ref-lucida-next-data-collection.md](ref-lucida-next-data-collection.md), [ref-lucida-next-ai-features.md](ref-lucida-next-ai-features.md) | 위 문서들이 참조하는 배경 문서 |

검증 스크립트 [../scripts/verify_tool_hardening.py](../scripts/verify_tool_hardening.py)는 격리 복원한 케이스에 실제 `rca-mcp`로
호출을 걸고 원천 Parquet을 pyarrow로 직접 읽어 값을 대조한다. 원본 실행 시 인자·경로는 스크립트 상단 참조.
