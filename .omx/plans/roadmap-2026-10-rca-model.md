# RCA 특화 30B 모델 로드맵 (2026-08-31 ~ 2026-10-31)

## 1. 목표

10월 31일까지 사업팀과 영업팀이 다음 문장을 검증 수치와 실제 사례를 근거로 사용할 수 있게 한다.

> 오픈 웨이트 30B급 베이스 모델을 자사 RCA 시나리오 데이터로 파인튜닝했으며, 동일한 RCA 하네스와 동일한 평가 조건에서 기존 모델보다 근본원인 판정 성능이 향상됐다.

이 목표는 독자 아키텍처나 처음부터 학습한 파운데이션 모델을 요구하지 않는다. 기존 RCA 하네스 내부의 LLM만 교체하며, 파인튜닝 전후의 차이를 측정한다.

## 2. 합의된 제약과 범위

- 일정: 2026-08-31부터 2026-10-31까지 9주.
- 인력: 2명, 다른 업무와 병행. 1인당 하루 약 2~3시간, 팀 합산 주 20~30시간을 계획 기준으로 삼는다.
- 인프라: H200 2장.
- 실행 방식: Codex/Claude 계열 AI를 코드 구현·테스트·리뷰, 교사 데이터 생성, 실험 분석, 평가 보조 judge에 적극 사용한다.
- 데이터 정책: 자사 시나리오와 관측 데이터는 승인된 외부 Claude/Codex 서비스에 교사 생성·평가 목적으로 전송할 수 있다. 단, API 키·비밀번호·토큰·개인정보 등 비밀값은 전송 전에 자동 제거한다.
- 모델: 30B급 오픈 웨이트 모델 한 종을 최종 선택.
- 학습: LoRA 또는 QLoRA 기반 SFT를 기본 경로로 한다. 전체 파라미터 학습과 GRPO는 이번 일정의 필수 범위에서 제외한다.
- 데이터: 9월 말 약 50개 RCA 시나리오를 예상하며, 이후 추가되는 시나리오는 가능한 한 블라인드 테스트에 사용한다.
- 최종 산출물: 검증 수치, 대표 성공·실패 사례, 모델 카드, 사업·영업용 요약 자료. 실시간 고객 데모와 운영 배포는 필수가 아니다.

## 3. 현재 상태와 출발점

- 프로젝트가 의도한 흐름은 `scenarios -> synth -> data -> train -> eval`이며, 시나리오 단위 train/eval 분리를 이미 원칙으로 정했다 (`src/rca_lab/__init__.py:4-8`, `README.md:42-45`).
- 학습 스택은 TRL + PEFT로 결정되어 있고 필요한 패키지는 `train` extra로 분리되어 있다 (`README.md:40`, `pyproject.toml:13-20`, `docs/decisions.md:5,10`).
- 베이스 모델은 아직 확정되지 않았고 config 한 줄로 교체하는 실험을 의도한다 (`docs/decisions.md:11`).
- 현재 데이터·모델·출력 디렉터리는 골격만 있으며, CI도 테스트 0개를 임시로 허용한다 (`README.md:32`, `.github/workflows/ci.yml:16`). 따라서 첫 2주는 모델 학습보다 데이터 계약과 평가 기준선 구축에 우선 투자해야 한다.
- 시나리오 러너는 `expected_rca_root_cause`, anomalies, clusters, incidents 등의 ground truth 필드를 이미 제공한다 (`../rca-scenario-runner/backend/app/models.py:101-126`, `../rca-scenario-runner/backend/app/scenarios.py:112-165`). 실행 결과도 `result.json`, `state.json`, `decisions.json`, `timeline.json` 등으로 저장된다 (`../rca-scenario-runner/backend/app/production_runtime.py:459-476`).
- 기존 RCA 하네스는 환경변수로 vLLM endpoint와 모델명을 교체하며 OpenAI 호환 `/chat/completions`를 사용한다 (`../lucida-rca-agent/config/base.py:66-84`, `../lucida-rca-agent/src/rca/llm/adapter.py:350-365,468-529`). 따라서 하네스 로직을 고정하고 모델 endpoint만 바꾸는 A/B 평가가 가능하다.
- 기존 평가기는 반복 실행, causal judgment, evidence calibration, guardrail 및 fatal flag 집계를 지원한다 (`../lucida-rca-agent/src/rca/agent_eval/runner.py:339-378,389-395`). 새 평가 체계는 이를 대체하지 말고 시나리오 ground truth와 연결해 확장한다.

## 4. 성공 기준

### 필수 성능 기준

1. 최종 블라인드 테스트에서 `근본원인 판정 성공률`이 기존 하네스 모델보다 절대 10%p 이상 향상된다.
2. `근거 부족 상황에서 잘못 CONCLUSIVE로 확정한 비율`은 기준선보다 악화되지 않는다.
3. 각 블라인드 시나리오를 최소 3회 반복하고, 평균뿐 아니라 시나리오별 결과와 변동성을 함께 공개한다.
4. 하네스, 프롬프트, 도구, 입력 데이터, 샘플링 설정과 평가기는 A/B 양쪽에서 동일하고 LLM endpoint와 모델명만 달라야 한다.
5. 도전 목표는 근본원인 판정 성공률 절대 15%p 향상이다.

### 전달물 기준

1. 재현 가능한 baseline 및 final evaluation config가 저장소에 존재한다.
2. 모델 adapter/checkpoint, 베이스 모델 revision, 데이터셋 revision, 코드 commit, W&B run이 서로 추적 가능하다 (`README.md:37,44,49`).
3. 사업·영업용 자료에는 전체 평균, 서비스군/장애유형별 결과, 대표 개선 사례 3개 이상, 실패 또는 한계 사례 2개 이상이 포함된다.
4. 외부 문구는 “자사 데이터로 파인튜닝한 RCA 특화 모델”로 제한하고, 독자 아키텍처나 범용 성능 향상을 주장하지 않는다.
5. AI가 만든 코드·샘플·점수에는 사용 모델, prompt/config revision, 원본 시나리오, 생성 시각과 사람 검수 상태가 기록되어야 한다.

## 4-A. AI 증강 운영 모델

AI 도움을 사람의 가용 시간에 포함된 단순 자동완성으로 보지 않고, 다음 세 실행 레인으로 운영한다.

### 구현 에이전트 레인

- loader, schema, scorer, training CLI, config, 테스트와 문서 초안을 독립된 작은 작업 단위로 생성한다.
- 한 에이전트가 설계·구현·승인을 모두 하지 않는다. 생성한 diff는 다른 AI reviewer와 담당 사람이 검토한다.
- AI가 완료했다고 보고한 작업은 targeted test, lint/typecheck, 실제 소규모 smoke run 중 해당 작업에 맞는 검증을 통과해야만 완료로 처리한다.
- 병렬화로 얻은 시간은 GRPO나 기능 확장보다 평가 반복, 데이터 품질 개선, 실패 분석에 우선 사용한다.

### 교사 데이터 레인

- Claude/Codex를 이용해 한 시나리오에서 여러 양질의 trajectory, tool-call, 최종 RCA 답변 후보를 생성한다. 프로젝트도 교사 모델 기반 합성을 원래 책임으로 정의한다 (`src/rca_lab/__init__.py:5`, `README.md:4-5`).
- 교사 입력에는 blind test의 ground truth를 포함하지 않는다. train 시나리오에서도 ground truth를 그대로 복사한 답변이 아니라 관측 증거로부터 도출된 설명인지 검사한다.
- 생성 샘플은 schema 준수, 근거-결론 정합성, unsupported claim, 중복, 비밀정보 포함 여부를 자동 필터링하고 사람이 계층별 표본 검수한다.
- 교사 모델·prompt·temperature·원본 scenario revision을 lineage에 기록해 데이터셋을 재생성할 수 있게 한다.

### 평가 보조 judge 레인

- deterministic scorer를 1차 기준으로 두고, 서술형 인과 품질은 별도의 AI judge가 보조한다. 기존 agent-eval도 causal judgment와 evidence calibration을 구분해 집계한다 (`../lucida-rca-agent/src/rca/agent_eval/runner.py:339-378`).
- baseline/fine-tuned 답변의 모델명을 가리고 순서를 무작위화해 judge 편향을 줄인다.
- 가능하면 교사와 다른 모델 또는 독립 prompt를 judge로 사용한다.
- 최종 blind 결과의 최소 20% 또는 10건 중 큰 수를 두 사람이 표본 검수하고, AI judge와 사람의 일치율을 함께 기록한다.
- AI judge 점수만으로 +10%p 성공을 선언하지 않는다. 구조화 ground truth와 사람 검수 결과가 방향상 일치해야 한다.

## 5. 평가 설계

### 데이터 분할

- 목표 50개 기준 초기 가이드: train 원천 30개, validation 8개, blind test 12개.
- 무작위 샘플 분할을 금지하고 서비스군, 장애 원인, negative/inconclusive 유형을 기준으로 family-level split을 수행한다. 같은 장애의 실행 변형이 train과 test에 동시에 들어가면 안 된다 (`README.md:45`).
- 9월 18일까지 분할 규칙을 동결하고, 9월 30일까지 train/validation 목록을 동결한다.
- 10월 1일 이후 추가되는 적격 시나리오는 학습에 사용하지 않고 blind test 또는 후속 일반화 테스트에만 사용한다.
- ground truth가 불완전한 시나리오는 학습·평가에 넣기 전에 `root cause`, expected verdict, 핵심 증거, 금지 오판을 사람이 검수한다. 현재 시나리오 스키마의 ground truth 필드는 optional이다 (`../rca-scenario-runner/backend/app/models.py:106-126`).

### 지표

- Primary: root-cause success rate. 원인 도메인, 핵심 메커니즘, 사건/대상 식별이 rubric을 충족한 run의 비율.
- Safety: false-conclusive rate. 정답이 INCONCLUSIVE/NEEDS_MORE_EVIDENCE인 케이스에서 근본원인을 잘못 확정한 비율.
- Diagnostic: causal judgment, evidence calibration, case-use, fatal flags, timeout/skip, tool-call/JSON 실패율. 기존 집계 필드를 재사용한다 (`../lucida-rca-agent/src/rca/agent_eval/runner.py:339-378`).
- Operational, 비게이팅: 평균/p95 지연, GPU 메모리, 토큰 사용량. 이번 릴리스의 성공 여부를 결정하지는 않지만 모델 카드에 기록한다.

### 공정성 통제

- temperature, seed, max token, tool schema를 고정한다. 기존 adapter는 이 값을 config로 제공한다 (`../lucida-rca-agent/src/rca/llm/adapter.py:350-365`).
- 자동 채점 가능한 구조화 필드를 우선 사용하고, 서술형 판단은 블라인드 evaluator와 사람 표본 검수로 교차 확인한다.
- teacher가 본 ground truth 문구가 평가 입력이나 blind test 학습 샘플로 유출되지 않도록 데이터 lineage 검사를 둔다.

## 5-A. 핵심 경로: 데이터 팩토리 (W1~W5)

데이터 작업은 W2의 일회성 단계가 아니라 W1부터 W5까지 이어지는 프로젝트의 핵심 경로다. W2는 데이터를 만드는 주간이 아니라, 이후 매일 데이터를 만들고 검수할 수 있는 자동화 파이프라인을 완성하는 주간이다.

### 데이터 규모 가이드

- 9월 말 약 50개 시나리오 중 약 30개를 train 원천으로 사용한다.
- train 시나리오마다 실행 조건·증거 조합·표현을 달리한 10~20개 full trajectory 후보를 생성해, 총 300~600개 검수 통과 trajectory를 목표로 한다.
- 하네스의 supervisor, sub-agent, reasoner, verifier, action-guide, render 단계별 대화를 분리해 약 1,500~3,000개의 SFT conversation 후보로 확장한다. 기존 하네스는 profile별 출력 정책을 구분한다 (`../lucida-rca-agent/src/rca/llm/adapter.py:253-269,371-377`).
- 숫자 자체를 성공 기준으로 삼지 않는다. root-cause family, service, CONCLUSIVE/INCONCLUSIVE, tool-call, long-context 분포를 coverage matrix로 관리한다.

### 주차별 데이터 산출물

| 주차 | 데이터 작업 | 주간 산출물 | 품질 게이트 |
|---|---|---|---|
| W1 | 소스·정답·실패 유형 inventory, 공통 schema와 rubric 정의 | scenario catalog, label completeness report, data contract v0 | 평가 가능한 필수 ground truth와 금지 누출 필드를 명시 |
| W2 | loader·teacher generation·validation·lineage 자동화 | 재실행 가능한 data factory와 dataset v0 | schema/lineage 100%, blind scenario 접근 차단, 자동 필터 통과 |
| W3 | train family 전체에 teacher 생성, 중복 제거, 계층별 사람 표본 검수 | dataset v1, data card, coverage matrix, SFT v0 입력 | 각 핵심 strata에서 사람 검수; 표본 합격률 90% 미만이면 해당 strata 재생성 |
| W4 | v0 실패 기반 hard positive/negative, 불확실성·오확정 방지 사례 보강 | dataset v2, error-to-data changelog, SFT v1 입력 | 새 샘플마다 어떤 실패 유형을 해결하는지 연결 |
| W5 | 신규 시나리오 반영, coverage gap 보완, train/validation 최종 동결 | dataset v3 release candidate, frozen split manifest | train/validation/blind hash와 revision 고정, 이후 blind 결과 기반 데이터 수정 금지 |

### 사람 검수 원칙

- train 데이터는 service, root-cause family, verdict type별 층화 표본을 검수한다. 최소 10% 또는 100개 중 큰 수를 사람이 확인한다.
- validation과 blind 시나리오의 ground truth/rubric은 100% 사람이 검수한다.
- AI 자동 필터는 schema, tool-call, 중복, 비밀값, unsupported claim을 검사한다. 사람은 증거가 결론을 실제로 지지하는지 판단한다.
- W6 이후 추가되는 시나리오는 기본적으로 학습에 넣지 않고 blind/confirmation set으로 보존한다. corrective SFT가 필요하면 별도의 untouched confirmation set을 다시 확보한다.

## 6. 주차별 로드맵

### 1주차 — 8/31~9/4: 평가 기준선과 교체 계약 고정

- 전체 시나리오 source inventory와 label completeness report를 만들고, 공통 data contract v0를 정의한다.
- 기존 하네스의 모델 endpoint, tool-call, structured output, context/token 요구사항을 contract test로 기록한다.
- 현재 모델로 평가 가능한 시나리오를 최소 10개 연결해 baseline smoke run을 수행한다.
- primary/safety rubric 초안을 만들고 두 사람이 동일한 10개 결과를 독립 채점해 불일치를 조정한다.
- 30B 후보 2~3개의 라이선스, 한국어, 긴 컨텍스트, tool-call/JSON 호환성을 표로 비교한다. 모델 성능 비교는 작은 smoke set으로 제한한다.
- AI 구현 에이전트가 loader/schema/eval contract-test 초안을 병렬로 만들고, 두 사람은 인터페이스와 rubric 승인에 집중한다.

**Gate A:** 동일 config로 baseline을 재실행할 수 있고, 모델 endpoint만 교체해도 하네스가 동작해야 한다.

### 2주차 — 9/7~9/11: 데이터·평가 파이프라인 최소 구현

- `src/rca_lab/scenarios`에 scenario-runner read-only loader를 구현한다. runner는 inline 및 per-scenario YAML을 모두 지원한다 (`../rca-scenario-runner/backend/app/scenarios.py:183-193`).
- `src/rca_lab/data`에 하나의 versioned sample schema와 lineage 필드를 정의한다.
- `src/rca_lab/eval`에 ground truth와 하네스 결과를 연결하는 deterministic scorer 및 judge bundle exporter를 구현한다.
- `src/rca_lab/synth`에 versioned teacher prompt, 병렬 생성, validation, lineage 저장 경로를 구현하고 train 후보에서 데이터 v0를 생성한다.
- 매일 새 시나리오를 추가해도 같은 명령으로 raw -> synth -> validated -> processed dataset을 갱신할 수 있게 한다.
- loader, schema round-trip, family split, scorer에 단위 테스트를 추가하고 CI의 “테스트 0개 허용” 상태를 제거한다 (`.github/workflows/ci.yml:16`).

**Gate B:** 최소 20개 시나리오를 같은 명령으로 적재·검증·평가할 수 있고 잘못된 split/누락 label이 자동으로 실패해야 한다.

### 3주차 — 9/14~9/18: 학습 데이터 v1, 모델 결정, SFT v0

- train family 전체에서 하네스의 실제 호출 구조를 보존한 300~600개 teacher trajectory 후보를 생성한다.
- 데이터 품질 규칙을 적용한다: tool schema 준수, 근거-결론 정합성, unsupported claim 제거, 중복 제거.
- 단계별 conversation을 추출하고 coverage matrix와 층화 사람 검수 결과를 포함한 dataset v1/data card를 만든다.
- family-level train/validation/blind split 규칙을 동결한다.
- 30B 베이스 모델 한 종과 정확한 revision을 고정한다. H200 2장 기준 LoRA/QLoRA 설정을 결정한다.
- 작은 subset으로 end-to-end SFT smoke run을 완료하고, 이어서 첫 SFT v0를 실행한다.
- validation에서 baseline과 v0를 비교해 tool-call 형식 오류와 데이터 품질 문제를 조기에 분리한다.

**Gate C:** 검수 통과한 학습 샘플과 데이터 카드가 존재하고, blind test의 입력·정답은 학습 파이프라인에서 접근할 수 없으며, SFT v0의 end-to-end 결과가 재현되어야 한다.

### 4주차 — 9/21~9/25: 실패 보강과 SFT v1

- v0 실패를 데이터 부족, tool-call 형식 오류, 잘못된 확정, 원인 혼동, 하네스 통합 오류로 분류한다.
- AI가 실패군별 hard positive/negative 후보를 생성하고, 사람 검수 후 데이터 v2에 반영한다.
- 각 추가 샘플을 v0의 실패 유형과 연결하는 error-to-data changelog를 남긴다.
- SFT v1을 실행해 기존 모델 및 v0와 동일한 validation set에서 비교한다.

**Gate D:** validation에서 primary metric이 기준선보다 개선되고 false-conclusive rate가 악화되지 않거나, 3영업일 안에 수정 가능한 명확한 실패 원인이 있어야 한다. 둘 다 아니면 모델 크기 확대나 GRPO로 우회하지 않고 데이터·평가 설계를 재검토한다.

### 5주차 — 9/28~10/2: Dataset v3와 최종 제한 SFT

- 9월 말까지 추가된 시나리오를 검수하고 train/validation 약 38개, blind 약 12개 구성을 목표로 dataset v3를 동결한다.
- service/root-cause/verdict/tool-call/long-context coverage gap을 보완하고 최종 split manifest와 hash를 고정한다.
- v0 실패 유형을 중심으로 hard positive/negative 샘플을 보강한다.
- 학습 샘플 수, epoch, rank, learning rate 등 소수의 핵심 변수만 비교하고 모든 run을 W&B와 config로 남긴다 (`README.md:44,49`).

**Gate E:** dataset v3와 split manifest가 동결되고, 최소 한 후보가 validation에서 +10%p에 근접하며 false-conclusive rate가 악화되지 않아야 한다.

### 6주차 — 10/5~10/9: 후보 모델 고정

- SFT v1/v2 중 최종 후보 하나를 선택한다.
- 하네스의 supervisor/reasoner/verifier 등 각 LLM profile에서 tool call과 JSON schema 회귀를 확인한다. 기존 adapter가 profile별 정책과 tool-call auto 모드를 사용하므로 형식 안정성이 필수다 (`../lucida-rca-agent/src/rca/llm/adapter.py:253-269,371-377,458-529`).
- 학습 데이터, 모델 adapter, config, code revision을 release candidate로 태깅한다.

**Gate F:** 모델 선택 이후에는 blind test 결과를 보고 재학습하지 않는다. 치명적 통합 오류가 아닌 한 후보를 변경하지 않는다.

### 7주차 — 10/12~10/16: 블라인드 최종 평가

- blind test 시나리오별 최소 3회씩 baseline과 fine-tuned model을 실행한다.
- primary/safety/diagnostic/operational metric과 confidence interval 또는 bootstrap interval을 계산한다.
- 자동 채점 결과의 일부를 두 사람이 블라인드 수동 검수한다.
- 실패 사례와 제한점을 숨기지 않고 원인별로 분류한다.

**Gate G:** 필수 목표(+10%p, false-conclusive 비악화)를 충족하면 외부 커뮤니케이션 준비로 이동한다. 미달이면 표현을 “특정 시나리오군에서 향상”으로 축소하거나 10월 23일까지 한 번만 corrective SFT를 허용한다. 이 경우 별도의 untouched confirmation set으로 재검증한다.

### 8주차 — 10/19~10/23: 검증 보고서와 모델 카드

- baseline 대비 결과표, 서비스군/장애유형별 breakdown, 반복 변동성, 실패 사례를 정리한다.
- 모델 카드에 베이스 모델/revision, 파인튜닝 방식, 데이터 범위, 평가 방식, 알려진 한계, 금지 주장을 기록한다.
- 재현 명령과 artifact 위치를 문서화하고 다른 팀원이 최소 1회 재실행한다.

**Gate H:** 숫자 하나마다 config, raw result, scorer version으로 역추적할 수 있어야 한다.

### 9주차 — 10/26~10/31: 사업·영업 패키지 확정

- 1쪽 executive summary, 5~7장 발표 자료, 상세 기술 부록을 만든다.
- 대표 개선 사례 3개 이상과 한계 사례 2개 이상을 전후 비교 형식으로 정리한다.
- 사업·영업팀 대상 리허설을 하고 예상 질문에 대한 FAQ를 만든다.
- 대외 표현을 기술팀이 최종 검토한다.

**최종 Gate:** 승인된 문구, 수치, 사례, 모델 카드, 재현 가능한 평가 artifact가 모두 있어야 “자사 RCA 특화 모델” 발표 준비 완료로 간주한다.

## 7. 2인 역할 배분

### 담당 A — 데이터·평가 오너

- scenario loader, schema, split, label QA, evaluator, benchmark report를 담당한다.
- blind test 접근과 최종 점수 산출을 통제한다.

### 담당 B — 모델·통합 오너

- 베이스 모델 bake-off, SFT config, 학습 run, checkpoint, vLLM serving, 하네스 contract test를 담당한다.

### 공동 책임

- 주 2회 30분 checkpoint: 월요일 범위 고정, 금요일 gate 판정.
- label과 최종 사례는 두 사람 교차 검토.
- 한 사람이 휴가 또는 본업 이슈로 빠져도 재실행할 수 있도록 모든 작업을 명령/config 중심으로 남긴다.
- 주간 총 작업량은 20~30시간을 넘기지 않는다. 각 주는 필수 gate 작업 약 16~22시간, 검수·문서화 약 4~8시간으로 제한하고, 초과 작업은 다음 주가 아니라 명시적 비범위로 이동한다.

## 8. 우선순위와 명시적 비범위

### 반드시 한다

1. 같은 하네스에서 공정한 baseline/fine-tuned A/B 평가.
2. 데이터 lineage와 family-level split.
3. SFT/LoRA 계열 30B 모델 한 종.
4. 블라인드 반복 평가와 영업용 근거 패키지.

### 10월 이후로 미룬다

- GRPO/RL 최적화.
- 전체 파라미터 학습.
- 다중 베이스 모델 장기 비교.
- production HA, autoscaling, API 보안, SLA.
- 고객 실시간 데모 UI.
- 모든 RCA 도메인에 대한 범용성 주장.

## 9. 주요 위험과 완화

| 위험 | 조기 신호 | 완화 |
|---|---|---|
| 50개 시나리오가 학습에 부족 | v0가 train만 상승하고 validation 정체 | 시나리오 수가 아니라 고품질 trajectory와 hard negative를 늘리고, 주장 범위를 특정 시나리오군으로 축소 |
| train/test 누출 | 유사 시나리오에서만 급격한 상승 | family-level split, lineage hash, blind set 접근 제한 |
| ground truth 불완전 | 사람 채점 불일치가 큼 | 2인 rubric 교정, 필수 label completeness gate, ambiguous 케이스 제외 |
| 30B 모델의 tool-call 불안정 | JSON/tool 실패가 성능 개선을 상쇄 | 1주차 contract smoke test를 모델 선택의 필수 조건으로 사용 |
| AI 교사 데이터가 정답 문구를 복사 | train 성능만 비정상 상승 | evidence-to-answer 검증, lineage 기록, blind ground truth 접근 차단, 사람 표본 검수 |
| AI judge가 특정 문체나 모델을 선호 | 자동 점수와 사람 판단이 불일치 | 모델명 마스킹, 답변 순서 무작위화, 별도 judge, 최소 20% 사람 교차검수 |
| 병행 업무로 일정 지연 | 2주 연속 gate 미달 | GRPO·서빙·데모 범위를 즉시 제거하고 baseline/eval/SFT 한 경로에 집중 |
| 블라인드 결과 미달 | validation uplift가 +10%p에 못 미침 | 10/9 이전 후보 폐기 판단; 이후에는 한 번의 corrective SFT와 untouched confirmation만 허용 |
| 대외 표현 과장 | “자체 개발 모델”의 의미가 사람마다 다름 | 모델 카드와 승인 문구에 오픈 웨이트 기반 파인튜닝임을 명시 |

## 10. 10월 말 권장 메시지

성공 기준 충족 시:

> 당사는 오픈 웨이트 30B급 모델을 자체 RCA 시나리오 데이터로 파인튜닝했습니다. 동일한 RCA 하네스와 블라인드 평가 조건에서 기존 모델 대비 근본원인 판정 성공률이 X%p 향상됐으며, 근거가 부족한 상황에서의 잘못된 확정은 증가하지 않았습니다.

성공 기준 일부만 충족 시:

> 당사는 특정 RCA 시나리오군에 특화된 30B급 파인튜닝 모델을 개발했고, 해당 범위에서 기존 모델 대비 개선 가능성을 확인했습니다. 일반화 범위는 추가 검증 중입니다.

## 11. 다음 공동 결정 사항

1. 두 담당자의 실제 이름과 역할 배분.
2. 1주차 bake-off에 넣을 30B급 베이스 모델 후보 2~3개.
3. 9월 18일 기준 train/validation/blind scenario family 목록.
4. 사업·영업 자료의 회사 승인 절차와 최종 검토자.
