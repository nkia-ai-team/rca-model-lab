# Muse 하네스 비교 — 2026-10-08

## GPU 0도 assistant로 전환 (13:49 KST 추가 요청)

사용자가 두 GPU 모두 assistant를 붙여 하나의 24회 평가를 분담하도록 요청했다. 완료 점수는 재사용한다. 실행 중인 GPU 0 사례를 잃지 않도록 신규 배정만 막고, 기존 사례가 종료된 뒤 `thinkfl-muse-gpu0-assistant-upgrade.service`가 검증된 GPU 2 컨테이너 설정을 GPU 0·18100 포트로 복제한다. `upgrade_gpu0.py`는 GPU 0의 진행 프로세스가 없고 배정 사례의 두 점수가 모두 저장됐는지 확인한 뒤에만 기존 서버를 중단한다. 새 서버의 실제 guided tool 호출이 통과해야 스케줄러가 GPU 0 신규 배정을 재개한다.

진행 상태: `outputs/muse-assistant-harness-20261008/serving-gpu0/upgrade-state.json`. 동적 배정 설정: 같은 디렉터리 상위의 `gpu-control.json`. 미래 assistant 작업은 GPU별 동시 2개로 제한한다. 이미 실행 중인 GPU 2의 3개는 중단하지 않는다. assistant KV cache가 71,846 tokens이고 3개 요청에서 사용률이 약 89%까지 증가한 관측을 반영했다. 기존 draftless 사례와 전환 후 assistant 사례는 `serving-by-case.json`에 구분해서 남긴다. 이번 변경은 평가 건수를 추가하지 않는다.

## 최신 범위 변경: raw 폐기, 하네스 평가에 GPU 2 재배정

사용자 요청으로 하네스 없는 Muse 평가를 폐기했다. raw 평가 프로세스와 임시 replay DB는 중단·정리했으며 기존 로그는 보존한다. 더 이상 raw 24회 완료나 raw 기전 채점을 기다리지 않는다. 아래의 두 조건 비교 계획은 과거 기록이다.

GPU 0에서 이미 실행 중인 하네스 3개 사례는 계속 진행한다. GPU 2는 같은 Muse FP8 기반 모델에 `meta-models/Muse-Glimmer-30B-assistant` DFlash를 추가하여 남은 하네스 사례를 처리하도록 전환한다. assistant revision은 `e8192f3a8f617f74be2ce220360c89ef4789f39f`, speculative token 수는 공식 모델 블록 크기에 맞춘 15다. 준비·실행 증거는 `outputs/muse-assistant-harness-20261008/`에 저장한다. 합계 목표는 기존 12개 사례 × 2회 = 24회이며, 이미 진행 중인 사례를 새로 중복 실행하지 않는다. assistant 유무는 사례별 실행 출처로 보존하며 서로 다른 실행의 wall time을 통제된 속도 비교처럼 해석하지 않는다.

당시 최종 하네스로 학습한 기존 체크포인트 비교에서는 `mcp-v2bs checkpoint-94`가 checkpoint-50보다 높았다. 다만 **검증셋** 6개 사례 × 6회에서 원인 적중 21/36(58.3%), Codex `gpt-6.1-sol` 기전 포함 만점 18/36(50.0%)이며, 같은 검증셋의 base 23/36·21/36보다 낮다. 이 체크포인트의 봉인 테스트 결과는 없다. 출처: `outputs/model-progress-20261007/existing-evaluation-report.md`, `configs/sft/muse-glimmer-30b-mcp-v2bs.yaml`. 과거 다른 하네스의 v1i-40 봉인 테스트 7/36을 이 모델의 테스트 성능으로 대체하지 않는다.

### Assistant 전환 확인 (13:34 KST)

GPU 2의 `rca-muse-assistant-harness-20261008`에서 DFlash 15-token 설정, `/v1/models`, 실제 guided tool 호출 `echo_tool({"text":"hello"})`를 확인했다. 모델 가중치는 33.53GiB, KV cache는 71,846 tokens다. 기존 GPU 0과 동일한 이미지 ID `sha256:6ad1a9b5a34ea96c5a6c3b958703d3ccff0af2703acdf8f16bfbd54a8a484207`를 사용한다. 실제 검증은 `serving/smoke-success.json`, `serving/runtime.json`에 남겼다.

`thinkfl-muse-assistant-harness.service`가 기존 GPU 0의 3개 진행 사례를 보존하고, GPU 2에서 F04-R·F05-H·F06-P 사례의 replay 복원을 시작했다. 하네스 완료 결과 3회를 재사용했다. assistant로 줄어든 KV cache 때문에 동시 요청의 preemption 여부를 관찰해야 하며, 아직 통제된 가속 배수는 측정하지 않았다. 원인·기전 채점은 24회 rollout 완료 후 `gpt-6.1-sol`로 진행한다.

## 실행 정정: UUID 별칭 복원 및 동시 사례 3개

최초 `muse-ablation-20261008` 실행은 잘못된 `--no-alias` 설정으로 중단했다. 기존 Muse 런타임은 UUID 복사 실패 때문에 `T<n>` 변환을 기본으로 사용한다. 하네스 첫 실행의 28개 오류 중 27개, raw의 22개 오류 중 13개가 UUID 관련이었다. 첫 실행은 각각 54분50초·34분17초가 걸렸다. 이 결과는 보존하되 정상 하네스 비교 결과로 섞지 않는다.

수정 실행은 `outputs/muse-ablation-aliases-par3-20261008/`에 별도로 저장한다. 두 조건 모두 UUID 별칭을 켜며, raw SQL에서는 문자열 리터럴 내부의 대상 별칭만 복원한다. SQL 식별자·테이블 별칭·주석은 대상 ID로 변환하지 않는다.

GPU 0 = 하네스, GPU 2 = raw를 유지하고, 각 GPU에 독립 사례 3개씩을 동시 실행한다. 동일한 사례를 두 번 푸는 반복은 해당 사례 작업 안에서 순차 실행한다. 사례별 DB 프로젝트·설정·로그를 분리하며, 조건별 점수 디렉터리는 사례 디렉터리 링크로 집계한다. 생성 예산과 채점 조건은 유지한다. 동시 부하가 달라졌으므로 이전 단일 요청 실행과 wall time을 직접 비교하지 않는다.

아래 UUID 원문·초기 경로 설명은 최초 실행의 기록이며 위 수정 실행에 의해 대체된다.

목표: 기존 Opus 비교에 사용한 12개 Polestar 캡처를 Muse 기반 모델로 각각 2회 실행하여, RCA 하네스와 원본 SQL/PromQL 조회 도구의 차이를 측정한다. 학습 중인 어댑터의 성능 평가가 아니라 **기반 모델의 하네스 효과 비교**다.

- 실행 위치: 104번 서버 L40S GPU 0 = 하네스, GPU 2 = 직접 조회. 각각 TP=1 독립 모델 서버. 기존 GPU 1 서비스 및 KT GPU 1 SFT와 분리.
- 모델: `RedHatAI/Muse-Glimmer-30B-FP8-block`, revision `8ed2e29141d4fef439b9a0e15e0a2678bc190a82`.
- 조건별 24회, 총 48회. 두 조건에 동일 사례, seed, 생성 설정, 시간창, 블라인드 필터, 정답 계약 적용. sealed 사례 없음.
- 하네스: 현재 `rca-mcp` 바이너리. 직접 조회: 기존 read-only raw MCP의 PostgreSQL/ClickHouse SQL 및 VictoriaMetrics 도구.
- 두 조건 모두 UUID 원문 사용. SQL 문자열 내부 별칭 변환을 피하고 관측 대상 표기를 맞춘다.
- 최대 조사 60턴, prompt 40,000 tokens, 도구 결과 6,000자, 기존 `legacy` 컨텍스트 정책. 이것은 **평가의 조사 예산**이며 SFT의 모든 assistant 턴 사용 방침과 별개다.
- temperature 1.0, top_p 0.95, top_k 64, 반복별 seed 1000/2000. 힌트 없음.
- 원인 대상 일치율은 기존 결정적 채점. 기전까지 맞는 strict 지표는 새 결과에만 Codex `gpt-6.1-sol` rubric v2를 적용한다. 미채점은 오답으로 집계하지 않는다.

## 이전 결과의 출처

기존 HTML: [Opus 5.5 · RCA 하네스 비교 결과](../outputs/reports/opus55-harness-20261007/report.html). 17→19 차이는 F11-R 두 답안에서 영향 대상 cart를 필수 원인에서 제외하고 발원지 Redis를 인정한 결정적 재계산이며, 추가 모델 실행이 아니다.

`outputs/mcp-eval/ablation-harness-final`의 개별 점수는 22/24, `ablation-raw`의 저장된 개별 점수는 17/24다. `docs/mcp-student-pipeline.md`에는 정답 기준 통일 후 raw 19/24라는 재산출 기록이 있다. 기존 궤적·점수 파일은 수정하지 않는다. 현재 Muse 비교는 plan에 해시를 고정한 현재 정답 계약과 현재 하네스를 사용하므로, Opus 당시의 런타임까지 동일한 재현이라고 주장하지 않는다.

## 실행·결과

- 고정 계획: `outputs/muse-ablation-20261008/plan.json`
- 재개 가능한 실행기: `outputs/muse-ablation-20261008/run.py`
- 상태: `outputs/muse-ablation-20261008/state.json`
- 실행 로그: 같은 디렉터리의 `harness.log`, `raw.log`, `judge.log`
- 원인 일치 결과: `root-results.json`; 기전 포함 결과: `report.json`
- 개별 궤적: `outputs/mcp-eval/muse-ablation-{harness,raw}-20261008/`

각 조건의 24개 점수 키가 모두 존재해야 rollout 완료다. 코드가 정상 종료하더라도 일부 사례가 실패하면 완료로 처리하지 않는다. 재개 시 기존 완료 궤적과 유효한 기전 채점 캐시를 재사용한다.
