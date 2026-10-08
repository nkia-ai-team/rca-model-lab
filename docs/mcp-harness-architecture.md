# rca-mcp 하네스 구조 — 도구·컨텍스트·데이터 경로

2026-09-21 기준. 학생(Muse Glimmer 30B)·교사(Claude Opus 5)가 같은 도구 표면 위에서 조사하고, 그 궤적을 학습 데이터로 바꾸는 전체 구조. 코드 정본은 `src/rca_lab/mcp/`, 실험 기록은 `mcp-student-pipeline.md`.

인터랙티브 다이어그램(archify, 자체 포함 HTML): [파이프라인 데이터 흐름](diagrams/rca-harness-dataflow.html) · [학생 루프 한 턴 시퀀스](diagrams/student-turn-sequence.html) · [도구 표면 23종과 데이터 원천](diagrams/rca-tool-surface.html). 원본 spec 은 같은 폴더의 `*.dataflow.json` / `*.sequence.json` / `*.architecture.json` (archify `deliver` 로 재생성). 도구별 원천(PG/CH/VM)은 `tools/rca-mcp/tools/registry.go` 의 생성자 인자에서 그대로 옮긴 것이다.

## 1. 전체 흐름

```
/data/eval-cases/case-*            (캡처: PG dump + CH + VM export + topology, 읽기 전용)
        │ restore.sh (compose 스택 1개/케이스, ~4분, 동적 포트)
        ▼
  격리 DB 세트 (PG/CH/VM)  ◄──── tools/rca-mcp (Go MCP stdio, 23 도구, -blind)
        │
   seed 생성 (meta.json 의 t1/t2 만 → 창 안 open 알람 ≤50) → -sanitize-stdin
        │
   ┌────┴─────────────────────────────────────┐
   │ 학생 루프 (student.py, vLLM)            │ 교사 (teacher.py, claude -p + MCP)
   │  system 규율 + seed → 추론→호출 반복    │  같은 규율·seed, 빌트인 도구 전부 차단
   │  → submit_rca                           │  → rca-answer MCP 로 submit_rca
   └────┬─────────────────────────────────────┘
        ▼
   trajectory.jsonl / result.json / score.json   (골든 대조 채점, 리뷰 게이트)
        │ rationalize (추론 재작성 + 판정) → export (턴 단위 예제) → SFT
```

## 2. 도구 표면 (tools/rca-mcp, 23종)

전부 읽기 전용. `*` = 필수 인자. `tools/list` 순서 = 아래 표 순서(`registry.go` 등록 순, 2026-09-22 부터 결정적 — 이전엔 map 순회라 프로세스마다 달랐음). 응답은 공통 봉투(`status: normal|anomalous|no_data`, `findings`, `refs`, `assessment_basis`, `truncated`)이고 학생 쪽은 6,000자에서 절단된다.

| 그룹 | 도구 | 역할 |
|---|---|---|
| 발견 | `describe_data_sources(domain)` | 캡처된 데이터셋·담당 도구 목록 |
| | `search_targets(query, type, limit, offset)` | 이름/유형/주소로 대상 UUID 찾기 |
| | `describe_target(target*, from, to)` | 정체·소속(호스트/K8s/서비스그룹)·한도 카탈로그 |
| | `expand_topology(target*, hops, edge_kinds, from, to, limit)` | 트레이스·DB·호스트 간선 즉석 조립. 양쪽 관측 병치 |
| | `get_data_coverage(targets*, from*, to*)` | 수집기 상태 vs 실제 관측 수 (0건 ≠ 배제 근거) |
| 지표 | `scan_metrics(target*, from*, to*, baseline_*)` | 전 지표 존재 4분류(disappeared/appeared/shifted/steady) |
| | `read_timeseries(targets*, metrics*, from*, to*, group_by, baseline_*)` | 버킷 시계열 + onset·선후·한도% |
| | `compare_peers(target*, metric*, …)` | 혼자 이탈 vs 또래도 이탈 |
| 로그·이벤트 | `sample_logs(target*, mode*=map|grep, template_id, query, severity_min, context_lines, …)` | map=종류 지도(급증/소멸/신규) + 총량 변동 `volume_shift`(흐름 끊김/폭주), grep=원문 |
| | `list_events(target*, from*, to*)` | 이상감지 에피소드·외부 알람·K8s 이벤트 |
| | `list_changes(from, to, target, include_audit_activity)` | 정책 배포·구성 변경·자산 편집 (기본 24h 소급) |
| | `search_host_data(kind*, from*, to*, host/target, query, …)` | syslog·소켓 관측 |
| 트레이스 | `get_trace_summaries(target*, kind*, from*, to*, signature)` | 경로/오류 요약 (count-only, HTTP 코드·peer 그룹) |
| | `get_trace_spans(trace_id*, from*, to*)` | 원본 스팬 |
| | `get_trace_activity(target*, from*, to*, bucket_seconds)` | 직전 창 대비 스팬 수, 사라진 연산 |
| | `breakdown_endpoints(target*, from, to, sections)` | 서비스 내부 구간별 지연·실패 |
| DB | `db_slow_queries(db*, from, to, top_n, plan_for)` | top-SQL, 24h 기준선 대비 |
| | `db_blocking(db*, from, to)` | 블로킹 사건(루트 세션·객체) |
| 호스트·K8s | `get_processes(host*, from*, to*, sort_by)` | 상위 프로세스 |
| | `get_process_snapshot(target*, from*, to*, pid/proc_key)` | 과거 프로세스 스냅샷 |
| | `get_k8s_state(target*, from*, to*)` | 재시작·CrashLoop·OOMKilled·waiting. pod finding 에 `target_id`(등록 pod)·`owner_target_id`(replicaset/deployment/statefulset, 이름에서 도출) 동봉(2026-09-22) — 이름만 주면 조사자가 그 pod 로 못 넘어감 |
| | `get_cluster_projections(cluster, from*, to*)` | 이벤트 클러스터 투영 |
| 네트워크 | `get_snmp_traps(device*, from*, to*)` | SNMP trap |
| **결론** | `submit_rca(status*, summary*, causes*, external_causes)` | 하네스 의사 도구. 아래 §5 |

**blind 모드** (`-blind`, `-sanitize-stdin`): 응답과 seed 에서 실험 라벨을 제거한다 — 키 `scenario_metadata/injection_summary/distinguishing_evidence/golden*/case_id/scenario_id` 삭제, `scenario-*`/`case-*`/`F07-H` 류 토큰과 `memhog/inject/cleanup` 단어 마스킹. 패턴 방어일 뿐 임의 텔레메트리가 원인을 누설하지 않는다는 증명은 아니다(패키지 주석 그대로).

교사가 본 도구 결함 (Youngdong 이슈 후보): `sample_logs` map 이 로그 볼륨 붕괴(기준선 867건 → 창 1건)를 `status: normal` 로 표시하고 희소 템플릿 소멸을 접는다 — 외부 hang 으로 스레드가 멈춘 케이스(F06-R)의 결정 증거가 숨는다. **2026-09-22 수정**: map 응답에 `volume_shift` 구획(현재/기준선 분당 비율, ×비율, kind=collapsed|surged) 추가, status 에 반영. 기준선 기대 건수 30 미만이면 비교 안 함(희소 로그 오탐 방지). 문턱(×0.1 붕괴, ×5 폭주)은 다른 판정식과 같이 임시.

## 3. seed (입력)

`seed.py`: meta.json 에서 **t1/t2 만** 읽고, `lucida_events_local` 에서 창 안 `transition=open`·비-cleared 알람을 시각순 ≤50 개 뽑는다. 2026-09-22 부터 알람 폭풍 억제: 동일 (대상·종류·지표…) 알람은 첫 발생만 남기고 `repeat_count`/`last_at` 로 접으며, 대상당 ≤8건 → 그 뒤 시각순 50건(`selection` 에 접힌 수·상한 탈락 수 기록). 필드: `event_id, occurred_at, target_id, service, kind, class, detector, reason, severity, metric, baseline, observed, change(above/below_baseline)`. 여기에 `affected_services`, `affected_targets`(UUID), `time_window(first_event/last_event)`. 시나리오 제목·기전은 절대 안 들어간다(8/19 제목 누출 사고 이후). `-sanitize-stdin` 을 한 번 더 통과.

## 4. 학생 루프 (student.py)

**프롬프트**: system = 조사·결론 규율(`prompts.py` SYSTEM_PROMPT, 범주 수준 규칙만) + 핸들 표기 안내. user = seed JSON + "도구 호출은 최대 N회, 끝나면 submit_rca". 힌트(사다리)는 **system 의 라이브 사본에만** 붙고 저장 궤적엔 없다(user 에 두면 학생이 되뇌어 학습 타깃에 샌다 — 실측).

**UUID 핸들** (`alias.py`): 대상 UUID 를 에피소드 안에서 `T<n>` 으로 양방향 치환. 베이스 Muse 가 UUID 꼬리를 못 베끼던(`a0a6668c-…-Bleu`) 문제 해결. 나가는 텍스트는 alias, 들어오는 인자는 resolve, 모르는 핸들은 오류 응답.

**문법 강제** (`muse_grammar.py`, xgrammar structural tag): 매 턴 `<|start|>assistant to=self<|message|>추론<|eom|><|start|>assistant to=<tool><|message|><atem:function_calls>…</atem:function_calls><|eom|>` — 추론 1개 → 도구 호출 정확히 1개. 없으면 Harmony 토큰이 본문에 섞이고 여러 호출이 나와 파싱 실패. 강제 답변 턴엔 `submit_rca` 만 허용.

**한 턴**: 서버에 롤링 뷰(§6) 전송 → 응답의 tool_calls 중 첫 `max_calls_per_turn=4` 개 실행(초과분은 오류 응답) → 결과를 6,000자로 절단해 tool 메시지로 추가 → `refs` 수집(근거 검증용). 도구 호출이 없으면 "호출하거나 제출하라" 넛지, 3회 초과 시 `no_tool_call` 종료.

**예산·가드**: `max_turns`(현재 50) 또는 prompt_tokens ≥ 40,000(서버 49,152 − 완료 4,096 − 봉투 1개) 이면 "예산 끝, 지금까지 관측으로 submit_rca" 를 붙이고 tool_choice 를 submit 으로 고정. 서버가 컨텍스트 초과를 반환하면 가장 큰 관측 3개를 800자로 줄이고 강제 답변. 종료 사유: `submitted | forced_answer_rejected | no_tool_call | max_turns`.

**샘플링**: T=1.0, top_p 0.95, top_k 64, seed=턴별 고정. 평가는 케이스당 3~6런.

## 5. submit_rca (결론 계약, answer.py)

```
status: confirmed | provisional | insufficient
summary: 2~4문장
causes[]: {target(UUID/핸들), mechanism, support_refs[]}      ← 발원지만
external_causes[]: {id: external:<이름>, kind: external_dependency|kafka|redis|network|capacity_limit,
                    name, boundary_target(UUID), evidence_refs[]}
```
검증: target 은 이 에피소드에서 관측된 UUID 여야 하고, refs 는 도구 응답의 refs 문자열 그대로여야 한다(실패·no_data 응답 ref 인용 금지). 위반은 오류 응답으로 돌려주고 다시 시도하게 한다. 교사는 같은 스키마를 `rca-answer` MCP 서버(`answer_server.py`)로 받는다.

## 6. 컨텍스트 관리 (context.py — 롤아웃·rationalize·학습 공통)

저장 궤적은 모든 도구 결과를 전문(≤6,000자)으로 보관한다. 모델이 실제로 보는 건 **롤링 뷰**:
- 최근 8개 도구 결과는 전문, 그 이전은 1,200자로 절단 + "필요하면 같은 인자로 다시 조회" 표식.
- 이전 턴의 `reasoning_content` 는 전부 제거 (서버로 안 보낸다).
- 같은 함수를 세 곳(롤아웃 wire, rationalize 문맥, 학습 tokenize)에서 써서 학습 프롬프트 = 추론 프롬프트를 토큰 단위로 맞춘다.

실측 크기: 30~50 호출 에피소드가 롤링 뷰로 15k~49k 토큰. 이 정책 없이는 30 호출에 46k 로 창을 넘겼다.

## 7. 교사 (teacher.py)

`claude -p --model claude-opus-5 --output-format stream-json --strict-mcp-config --mcp-config <rca-tools + rca-answer> --tools "" --allowedTools mcp__rca-tools__*,mcp__rca-answer__* --disallowedTools Bash,Read,… --max-turns 60 --system-prompt <같은 규율>`, 프롬프트는 stdin, cwd 는 빈 임시 디렉터리. `--tools ""` 만으론 CLI 가 큰 도구 출력을 읽으려고 Bash 를 제안했기 때문에 빌트인을 명시 차단(그래도 거부된 Bash 시도가 stream 에 남아 export 에서 제거).

교사 채택 = 자동 채점(대상) + **채점자 리뷰** `review.json {"accepted", "note"}`(기전을 meta 와 대조; 대상 맞고 기전 틀린 런 기각). 무힌트 실패는 **힌트 사다리** ① 오답 배제 → ② 증상 → ③ 내비게이션 순으로 라이브 프롬프트에만 붙여 재런. 정답 단어는 끝까지 비노출.

## 8. 채점 (golden.py / score.py)

골든(`configs/eval/train-family-v2.yaml`)은 케이스별 `expected_status` + roots. `expected_status` 는 시나리오 메타에 없는 우리 라벨 — 규칙 A(2026-09-21): confirmed = root 대상 이상→영향의 시각·경로 직접 관측이 가능한 시나리오, provisional = 외부 의존 root 또는 기전 미관측(규칙 본문은 yaml 머리 주석). 내부 root = 정식 target UUID 집합(서비스 + 그 K8s deployment/rs/pod/container/service/statefulset) + aliases; 외부 root = `pseudo_kind/pseudo_ids` + `proxy_target_ids`(테스트베드 mock 의 K8s id) + `boundary_target_ids`. 매칭은 **UUID 로만**(별칭은 문서용). 재캡처는 UUID 가 대부분 동일하고 Deployment 파드·RS 만 새 id → `scripts/mcp_golden_extend.py` 가 `meta.resource_kind + 정규화 resource_key` 로 대응.

score.json: `root_f1`(중복 무벌점), `status_correct`, `ref_grounding`(인용 ref 중 실제 관측 비율), `unsupported_confirmation`, `strict_correct`(f1=1 ∧ status ∧ grounding=1), `reward`(0.55·f1 + …), `tool_calls/errors/turns`. 검증 집계는 시나리오별 평균(`val_summary.py`), 12런 σ≈0.14 라 6런 이상 권장.

## 9. 학습 데이터 경로

1. **채택** `mcp_export_sft.py`: student/teacher 루트에서 `stop=submitted ∧ root_f1≥1.0 ∧ ref_grounding=1 ∧ (교사면 review 통과) ∧ 호출 ≤50` 인 런, 케이스당 짧은 순 2개. 카탈로그 밖 도구 턴(Bash) 제거, user 프롬프트 예산 문구를 학습·평가와 같은 값(50)으로 재기록. 검증·봉인 케이스는 거부.
2. **추론 재작성** `mcp_rationalize.py`: 행동열은 고정하고 각 턴의 추론만 무힌트 롤링 문맥에서 다시 쓴다. 작성자 = 학생(vLLM, 문법으로 호출 강제; 호출을 모르고 써서 행동과 불일치 11/12) 또는 Claude(`rationale_writer.py`, 고정 호출을 알고 씀; 일치 12/12, 학생 NLL 1.9). `judge.py`(claude -p)가 턴마다 `consistent`(추론의 다음 단계 = 실제 호출)·`grounded`(관측만 근거) 판정, 불일치 턴은 2라운드 재샘플, 끝까지 불일치면 `supervise:false`(문맥엔 남고 손실 제외). Claude 세션 한도는 `SessionLimitError` 로 중단·재개(실패 시 기존 텍스트 보존).
3. **토큰화** `train/mcp_sft.py`: 예제 = 어시스턴트 턴 1개, 프롬프트 = 그 시점 롤링 뷰 + 생성 헤더, 완성 = 추론 + 호출(종료 토큰 손실 제외). 선택적 CE(감독 위치 hidden state 만 lm_head 투영, 26만 어휘 전체 logits 는 31GB). 케이스 균형 가중, 추론 구간 가중(`reasoning_loss_weight`), LoRA-FA(`freeze_a`), `max_steps`.

## 10. 실행 경계

- 케이스 복원은 compose 프로젝트 `<prefix>-<family>` 로 격리 → 병렬 러너는 `--project-prefix` 를 달리한다. 같은 케이스 재평가는 폐기 후 재복원(볼륨 재사용 금지). `--reuse-db` 는 같은 `run-name/case` 의 connections.json 을 재사용.
- KT vLLM(GPU0:8002, 터널 18002)은 런타임 LoRA 로드(`/v1/load_lora_adapter`). 어댑터는 vision 텐서를 뺀 사본(`rca-adapters-lm/`)이어야 하고, **서버 생존 중 등록된 경로를 옮기면 엔진이 죽는다**.
- 봉인 9(+3) 케이스는 학습·힌트 설계·프롬프트 규칙 도출에 절대 사용 금지. 검증 4 케이스 실패로 프롬프트 규칙을 뽑으면 검증 오염(v2 의 +3/12 가 그 사례).
