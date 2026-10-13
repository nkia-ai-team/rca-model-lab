# RCA 하네스 구조

2026-10-13 소스 기준. [HTML 다이어그램](rca-harness-architecture.html) · [시작하기](../README.md)

## 조사 흐름

```text
관측 알람 + 시간 범위 → seed
  → Python student 조사 루프 ↔ 모델 API / Codex 연결
  → MCP stdio → Go 관측 도구 23종 → PG / CH / VM
  → 관측 응답·근거 refs → 다음 조사 또는 submit_rca
  → trajectory / result / score → 기전 채점·보고서·학습 데이터
```

정본은 `src/rca_lab/mcp/student.py`, `tools/rca-mcp/tools/registry.go`입니다.
`submit_rca`와 structured 정책의 `read_observation`은 Python 하네스 도구로, Go 23종 수에 포함하지 않습니다.
모델 공급자는 실험별로 선택합니다. Muse 전용 문법 강제는 모든 모델에 적용하는 공통 규칙이 아닙니다.

## 데이터와 도구

| 조회 | 도구 | 주 데이터 원천 |
|---|---|---|
| 데이터 종류·대상·관계 | `describe_data_sources`, `search_targets`, `describe_target`, `expand_topology`, `get_data_coverage` | PG 자산·K8s, CH 관측, VM 수집 근거 |
| 지표·기준선·또래 비교 | `scan_metrics`, `read_timeseries`, `compare_peers` | VM + PG 대상 정보 |
| 로그·이벤트·변경 | `sample_logs`, `list_events`, `list_changes`, `search_host_data` | CH + PG, 일부 VM |
| 트레이스 | `get_trace_summaries`, `get_trace_spans`, `get_trace_activity`, `breakdown_endpoints` | CH |
| DB | `db_slow_queries`, `db_blocking` | CH |
| 호스트·K8s | `get_processes`, `get_process_snapshot`, `get_k8s_state`, `get_cluster_projections` | CH + PG |
| 네트워크 | `get_snmp_traps` | CH |

도구 응답은 `status`, `findings`, `refs`, 조회 범위·제약을 담습니다. `no_data`는 장애가 없다는 뜻이 아닙니다.
입력은 알람과 시간 범위로 만들며, 정답 메타데이터는 채점 영역에 둡니다. blind 필터만으로 누출 방지가 완성되는 것은 아닙니다.

## 서버에서 서비스 찾기

1. `search_targets`로 서버 이름/IP를 UUID로 확정합니다.
2. `expand_topology(target=<서버 UUID>, from=…, to=…)`에서 역방향 `hosted_on` 연결로 서비스 후보를 찾습니다. 간선 자체는 기존처럼 앱 → 호스트 방향입니다.
3. 서비스의 추가 의존 관계는 반환된 대상 ID로 후속 조회합니다.
4. 실행 프로세스와 연결 포트는 `get_process_snapshot`·`search_host_data(kind="connections")`에서 확인합니다.

직접 관계는 `targets.meta.host_target_id`가 가리키는 **application → server** 유형을 검증해 연결합니다. 이 경로는 트레이스가 없어도 동작하며 시간 근거는 `current_inventory;not_historical`입니다. CH·파드 조회가 실패해도 직접 확인된 관계는 보존하고 실패한 원천을 degraded로 표시합니다.

추가 관측 관계는 시간창 내 트레이스의 `service`·`host_name`을 PG K8s 파드 이름 → `data.nodeName` → 호스트 `resource_key`와 연결합니다. `lucida.target_id`로 서비스 UUID를 확인하며 미확정 서비스는 `service:<이름>` 노드로 남깁니다. 중복 파드/노드 이름은 모호한 연결을 만들지 않도록 제외합니다. `hosted_on` 연결은 APM hop을 소모하지 않으며 기존 크기 제한·truncated 표기를 유지합니다.

시간 근거는 `span_in_window;pod_placement=current_inventory`입니다. 트레이스는 지정 시간창이지만 파드 배치는 현재 자산 스냅샷이므로 과거 배치까지 보증하지 않습니다.

같은 서버라는 이유만으로 서비스 간 호출 관계를 만들지 않습니다. 수집된 배치 관계와 관측 근거를 구분해야 합니다.
서버 조회는 실시간 SSH 탐색이 아니며 미수집·미등록 서비스까지 완전하게 열거하지 않습니다.

## 컨텍스트 정책

정본: `context.py`, `observations.py`, `student.py`.

| 정책 | 모델에 전달하는 응답 | 이전 응답 | 원문 재조회 |
|---|---|---|---|
| `legacy` | 설정 길이까지 앞부분 절단; 기본 6,000자 | 최근 8개를 제외하면 기본 1,200자까지 절단 | 같은 도구를 좁은 범위로 다시 호출; 별도 원문 reader 없음 |
| `structured-v1` | 전체 응답을 보관하고 크기 제한 JSON 봉투 전달 | 구조를 유지한 축약 | `read_observation` |
| `structured-v2` | 전체 응답을 보관하고 필드·누락 정보를 전달 | 이미 전달된 v2 관측 봉투를 유지 | `read_observation` |

`read_observation`은 현재 조사에 등록된 `observation_id`만 받습니다. 파일 경로를 받아 읽는 도구가 아닙니다.
`json_pointer`로 필드를 고르고 `offset`·`limit`·`next_offset`으로 페이지를 읽습니다.
보관된 “원문”은 **도구가 반환한 전체 응답**이지, 데이터베이스 전체나 도구 내부 limit으로 제외된 행까지 뜻하지 않습니다.
따라서 원문 저장만으로 중요한 증거가 반드시 조회·추론된다는 보장은 없습니다.
이전 `reasoning_content` 제거 등 롤링 문맥 정책은 추론·추론 재작성·학습에서 공유합니다.

## 예산·결론·평가

- `max_turns`, `max_prompt_tokens`, `max_tokens`는 별도 설정입니다. `max_prompt_tokens=0`은 강제 제출을 유발하는 프롬프트 예산만 해제합니다.
- 입력+출력의 물리적 상한은 모델 서버 컨텍스트입니다. 초과 시 처리는 정책별로 다르며 무제한 입력이 되지 않습니다.
- `submit_rca`는 상태·요약·원인 대상·기전·근거 refs를 제출합니다. 관측하지 않은 대상/근거 등 계약 위반은 검증 오류로 반환합니다.
- 원인 대상 채점과 기전 채점을 분리합니다. 기전 정답률 비교에는 채점자·정답 계약도 고정해야 합니다.
- 평가 결과에는 모델/토크나이저 revision, 양자화, thinking, 추측 디코딩, MCP 바이너리 해시, 정책, 예산, 서버 컨텍스트, 반복 수, split을 기록합니다.
- API 시간 기준 tok/s와 도구 포함 전체 풀이 tok/s는 다릅니다. 계측 정의가 다른 수치를 직접 비교하지 않습니다.

## 학습과 제품 연결

교사 풀이 → 근거·기전 채택 → 필요 시 무힌트 추론 재작성 → 학습 데이터 export → SFT 순서입니다.
턴별 학습 예제는 해당 시점까지의 문맥과 현재 assistant 출력을 사용합니다. 턴 단위 손실이 에피소드를 임의로 6턴만 남긴다는 뜻은 아닙니다.
학습·검증·봉인셋은 시나리오 단위로 분리하며, 검증셋으로 선택한 체크포인트를 최종 평가합니다.

Go MCP 도구는 제품 RCA에서도 재사용할 수 있는 경계입니다. 다만 이 저장소의 격리 캡처 복원·평가 루프는 제품 배포 자체가 아닙니다.
제품 연결 시 인증·테넌트 범위·시간 범위·읽기 권한·도구 응답 계약을 제품의 실행 경계에서 보장해야 합니다.

**최신 소스와 실행 바이너리는 별개입니다.** 고정 바이너리를 쓰는 진행 중 평가는 소스 수정으로 바뀌지 않습니다.
새 기능을 적용하려면 빌드·검증 후 새 실행 이름과 바이너리 해시로 시작하고 이전 결과와 구분합니다.
