# RCA 특화 모델 개발 WBS

> 기간: 2026-09-01 ~ 2026-11-30  
> 목표: 10월 초까지 30B급 SFT 모델의 하네스 연동·비교 결과를 먼저 확보하고, 11월 말까지 데이터 확대와 후속학습·블라인드 평가를 완료해 정확성 비악화와 조사 과정 개선을 증명한다.

## 1. 운영 원칙

- 기능별 고정 담당자를 두지 않는다.
- 작업 유형은 `공동 기반`, `독립 실험`, `공동 검증`으로 구분한다.
- 데이터셋, 평가기, validation/blind split은 공동 자산으로 관리한다.
- 독립 실험은 하나의 가설과 config 단위로 등록하고, 같은 실험을 중복 실행하지 않는다.
- 모든 실험은 같은 validation set과 평가 지표로 비교한다.
- 공통 데이터·평가 기준 변경은 두 사람이 합의한다.
- 1차 결과는 개발·검증셋으로 산출하며 최종 블라인드셋은 사용하지 않는다.
- 최종 블라인드셋은 2차 최종 후보 동결 전까지 실행하지 않는다.
- AI는 코드·테스트·교사 데이터·분석 초안을 병렬 생성하고, 사람은 기준·표본 품질·승격·대외 주장을 승인한다.

### 작업 유형

| 유형 | 의미 |
|---|---|
| 공동 기반 | 두 사람의 모든 실험이 공유하는 데이터·코드·평가 계약 |
| 독립 실험 | 서로 다른 가설·모델·데이터 조합·학습 방법을 병렬 탐색 |
| 공동 검증 | 동일 조건으로 결과를 비교하고 다음 기준 후보를 결정 |

## 2. 단계별 목표

| 구분 | 1차: SFT 조기 검증 | 2차: 최종 모델·성과 |
|---|---|---|
| 기간 | 9/1 ~ 10/2 | 10/5 ~ 11/30 |
| 핵심 목적 | SFT 모델을 기존 하네스에 연결해 빠르게 개선 가능성을 확인 | 데이터 확대와 후속학습으로 최종 성능을 확보하고 대외 근거 완성 |
| 시나리오 | 약 30개: 개발·학습 24, 1차 검증 6 | 50개 이상: train 35, validation 5, blind 10 이상 |
| SFT 목표 | 30B급 LoRA/QLoRA SFT v1, 하네스 연동, tool-call·JSON 성공률 95% 이상 | 신규·실패 시나리오를 반영한 SFT v2와 안정적인 후속학습 출발점 |
| 성능 목표 | 기존 모델 대비 RCA 성공률 +5%p 또는 과정 지표 10% 이상 개선 | 기존 모델 대비 정확성 +10%p 도전 또는 정확성 비악화와 과정 지표의 유의미한 개선 |
| 방어 조건 | 최종 정확성·false-conclusive rate 비악화 | false-conclusive rate 비악화, 최종 blind 결과 기반 재학습 금지 |
| 주요 결과 | SFT v1 checkpoint, 하네스 비교 결과, 대표 사례 2~3개 | 최종 checkpoint, blind 보고서, 모델 카드, 사업·영업 자료 |

### 학습 단계별 목표

- **SFT:** 자사 시나리오에서 검증된 RCA 분석 절차와 판단 패턴을 학습시켜 초기 정확도와 조사 품질을 높이고, 후속학습을 위한 안정적인 기준 모델을 확보한다. Tool-call·JSON 형식 준수와 하네스 연동은 학습 목적이 아니라 별도 품질 조건으로 검증한다.
- **후속학습:** 최종 정답을 유지·향상하면서 올바른 도구 선택, 필수 증거 수집, 불필요 호출 감소를 직접 최적화한다. 방법은 사전에 고정하지 않고 비교 후 선택한다.

## 3. WBS

| WBS | 작업 | 유형 | 산출물 | 완료 조건 | 선행 작업 | 예상 기간 |
|---|---|---|---|---|---|---|
| **1.0** | **데이터셋 구축** | 공동 기반 | SFT와 후속 학습이 공유하는 canonical dataset | 전체 trajectory, ground truth, reward metadata, split, lineage를 하나의 계약으로 관리 | - | 9월 전반, 이후 지속 개선 |
| 1.1 | Canonical schema 정의 | 공동 기반 | `src/rca_lab/data`의 versioned schema | scenario/input/evidence/tool-call/observation/final answer/rubric/lineage를 표현하고 SFT·preference·RL view를 파생 가능 | - | 2~3일 |
| 1.2 | 시나리오·정답 적재 | 공동 기반 | `src/rca_lab/scenarios` loader와 scenario catalog | runner의 inline/per-scenario YAML 및 optional RCA ground truth를 누락 없이 검증 (`../rca-scenario-runner/backend/app/scenarios.py:112-165,183-193`, `../rca-scenario-runner/backend/app/models.py:106-126`) | 1.1 | 3~4일 |
| 1.3 | 전체 trajectory 수집 | 공동 기반 | 하네스 실행의 messages, tool calls, observations, decisions, final report 묶음 | 한 실행을 재생·분석할 수 있고 원본 run/config/model revision으로 역추적 가능 | 1.1, 5.1 | 4~5일 |
| 1.4 | 교사 데이터 생성 | 독립 실험 | `src/rca_lab/synth`의 prompt별 trajectory 후보 | Claude/Codex 생성 결과가 schema·tool 계약을 통과하고 생성 모델/prompt/revision이 기록됨 (`src/rca_lab/__init__.py:5`, `README.md:4-5`) | 1.1, 1.2 | 1~2주, 반복 |
| 1.5 | 데이터 품질·검수 | 공동 검증 | 자동 검사 결과, 층화 사람 검수 기록, coverage matrix | 중복·비밀값·형식 오류·근거 없는 결론을 탐지하고 핵심 strata의 품질 기준을 통과 | 1.3, 1.4 | 3~5일, 반복 |
| 1.6 | Split·버전 동결 | 공동 기반 | train/validation/blind manifest와 dataset revision | 같은 장애 family가 split을 넘지 않고 blind 정답이 학습 경로에서 차단됨 (`README.md:45`) | 1.2, 1.5 | 2일 |
| 1.7 | 학습 view 생성 | 공동 기반 | SFT target view, preference view, reward/rollout view | 하나의 canonical revision에서 각 view를 재현 가능하며 split과 lineage가 유지됨 | 1.6 | 2~3일 |
| **2.0** | **평가·Reward 체계** | 공동 기반 | 정확성과 조사 과정을 함께 측정하는 평가 시스템 | 기존·SFT·후속학습 모델을 같은 조건에서 비교 가능 | 1.1 | 9월 전반, 이후 보정 |
| 2.1 | Baseline 측정 | 공동 검증 | 기존 하네스 모델의 기준 결과 | 동일 config와 반복 횟수로 재실행 가능하고 raw result가 보존됨 | 1.2, 5.1 | 3~4일 |
| 2.2 | 최종 RCA 평가 | 공동 기반 | root-cause correctness와 false-conclusive scorer | ground truth 대비 원인·대상·메커니즘·결론 상태를 일관되게 채점 | 1.1, 1.2 | 3~4일 |
| 2.3 | 조사 과정 평가 | 공동 기반 | 도구 선택, 증거 충족, 중복 호출, 비용·지연 scorer | 전체 trajectory에서 과정 지표를 자동 산출하고 사람이 표본 검증 가능 | 1.3 | 4~5일 |
| 2.4 | Reward contract 정의 | 공동 기반 | reward components, weights, penalties, version | 최종 정확성·증거·도구·효율·오확정 항목을 분리 기록하며 특정 학습 방법에 종속되지 않음 | 2.2, 2.3 | 3~4일 |
| 2.5 | Judge 교정 | 공동 검증 | AI judge와 사람 판정의 calibration report | 모델 정체를 가린 표본에서 불일치 원인을 분류하고 허용 기준을 합의 | 2.2, 2.3 | 2~3일 |
| 2.6 | 성능 리포트 자동화 | 공동 기반 | 실험별 비교표와 실패 사례 리포트 | 기존 평가기의 causal/evidence/guardrail 집계와 신규 scorer 결과를 함께 출력 (`../lucida-rca-agent/src/rca/agent_eval/runner.py:339-378`) | 2.1~2.5 | 2~3일 |
| **3.0** | **SFT 모델 개발** | 독립 실험 | 기존 하네스에서 동작하는 30B급 SFT 기준 모델과 1차 비교 결과 | 10월 초까지 checkpoint·config·하네스 비교 리포트 확정 | 1.7, 2.0, 5.0 | 9월~10월 2일 |
| 3.1 | 베이스 모델 후보 실험 | 독립 실험 | 후보별 호환성·초기 성능 기록 | 라이선스, context, 한국어, tool-call/JSON, H200×2 적합성을 같은 smoke set으로 비교 | 2.1, 5.2 | 2~3일 |
| 3.2 | SFT 파이프라인 구축 | 공동 기반 | `src/rca_lab/train` CLI와 config | TRL+PEFT 기반 학습을 dataset/model/config revision으로 재실행 (`README.md:40,44,49`, `pyproject.toml:13-20`) | 1.7 | 3~4일 |
| 3.3 | SFT 초기 실험 | 독립 실험 | 모델·데이터·하이퍼파라미터별 run | 각 run이 하나의 가설을 검증하고 동일 validation report를 생성 | 3.1, 3.2 | 약 1주 |
| 3.4 | 실패 기반 SFT 반복 | 독립 실험 | 보강 dataset revision과 후속 SFT run | 실패 유형이 추가 데이터와 연결되고 기준 모델 대비 정확성 또는 과정 지표가 개선 | 1.5, 2.6, 3.3 | 1~2주 |
| 3.5 | SFT 1차 모델·결과 확정 | 공동 검증 | SFT checkpoint, config, dataset revision, 하네스 비교 리포트 | 정확성 비악화·형식 안정성·하네스 회귀를 확인하고 대표 사례 2~3개를 정리 | 3.3, 3.4, 5.4 | 9/28~10/2 |
| **4.0** | **강화학습·후속학습 모델 개발** | 독립 실험 | 조사 과정까지 개선된 최종 후보 | 방법을 미리 고정하지 않고 소규모 비교 후 한 경로를 선택 | 2.4, 3.5 | 10월 5일~11월 중순 |
| 4.1 | 학습 방법 bake-off | 독립 실험 | GRPO/RLOO/preference 계열 등 후보 비교 | reward 신뢰도, validation 개선, 학습 안정성, GPU 효율을 같은 소규모 set으로 비교 | 2.4, 3.5 | 약 1주 |
| 4.2 | 학습 환경 검증 | 독립 실험 | live/replay/hybrid 후보와 reward sanity report | 선택 환경에서 rollout 재현성·처리량·도구 행동 채점이 가능 | 1.3, 2.4, 4.1 | 3~5일 |
| 4.3 | 후속학습 초기 실험 | 독립 실험 | 방법·reward config별 모델 후보 | 정확성 비악화를 유지하면서 하나 이상의 조사 과정 지표 개선 신호 확보 | 4.1, 4.2 | 약 1주 |
| 4.4 | Reward·데이터 보정 반복 | 독립 실험 | 실패 분석, reward revision, 후속 run | reward hacking·퇴행·도구 편향을 기록하고 보정 결과를 동일 validation으로 재검증 | 2.6, 4.3 | 1~2주 |
| 4.5 | 최종 모델 확정 | 공동 검증 | 최종 checkpoint, reward/config/dataset revision | SFT 및 후속학습 후보를 비교해 한 모델을 선택하고 이후 학습 변경을 중단 | 4.3, 4.4, 5.4 | 2일 |
| **5.0** | **하네스·모델 연동** | 공동 기반 | 모델만 교체 가능한 안정적 실행 경로 | 하네스·prompt·tools를 고정하고 endpoint/model만 바꿔 비교 | - | 전 기간 병행 |
| 5.1 | 기준 실행 계약 고정 | 공동 기반 | endpoint, model name, sampling, context, tool schema contract | 기존 하네스의 OpenAI-compatible vLLM 호출 설정을 재현 (`../lucida-rca-agent/config/base.py:66-84`, `../lucida-rca-agent/src/rca/llm/adapter.py:350-365,468-529`) | - | 2~3일 |
| 5.2 | 모델별 호환성 검사 | 공동 검증 | tool-call/JSON/profile contract tests | supervisor/reasoner/verifier 등 주요 profile에서 형식 오류 없이 동작 | 5.1 | 2~3일, 모델별 반복 |
| 5.3 | 실험·artifact 추적 | 공동 기반 | config, W&B run, dataset/model revision 연결 | 모든 결과를 코드·데이터·모델·평가기 revision으로 역추적 (`README.md:37,44,49`) | 3.2 | 2~3일 |
| 5.4 | 하네스 전체 회귀검증 | 공동 검증 | SFT/후속학습 후보별 regression report | 동일 시나리오와 설정에서 timeout·tool-call·schema·결과 회귀를 비교 | 5.2, 후보 모델 | 3~4일, 후보별 반복 |
| **6.0** | **최종 검증** | 공동 검증 | 단 한 번의 블라인드 비교 결과 | 기존·SFT·최종 모델의 정확성과 조사 과정 개선 여부 확정 | 4.5, 5.4 | 11월 16~20일 |
| 6.1 | 블라인드 평가 준비 | 공동 기반 | 봉인된 split, 실행 config, scorer revision | 최종 후보 동결 후에도 blind 입력·정답·평가 기준이 변경되지 않음 | 1.6, 2.6, 4.5 | 1~2일 |
| 6.2 | 블라인드 평가 실행 | 공동 검증 | 모델별 반복 실행 raw result | 기존·SFT·최종 모델을 동일 조건으로 평가하고 결과를 본 뒤 재학습하지 않음 | 6.1 | 3~5일 |
| 6.3 | 결과 분석 | 공동 검증 | 전체·유형별 지표와 신뢰구간, 성공·실패 사례 | 정확성 비악화 여부와 과정 개선 항목을 분리해 보고 | 6.2 | 2~3일 |
| 6.4 | 대외 주장 확정 | 공동 검증 | 승인된 성능 문구와 금지 주장 | 수치로 직접 뒷받침되는 범위만 표현 | 6.3 | 1일 |
| **7.0** | **성과 전달** | 공동 검증 | 사업·영업팀이 사용할 근거 패키지 | 11월 말까지 검증 수치·사례·제한사항 전달 | 6.4 | 11월 23~30일 |
| 7.1 | 모델 카드 | 공동 기반 | base model, 데이터, 학습, 평가, 한계 문서 | 모든 revision과 알려진 한계를 포함 | 4.5, 6.3 | 1~2일 |
| 7.2 | 기술 검증 보고서 | 공동 검증 | 방법·지표·결과·실패 분석 | 원시 결과와 scorer로 모든 표를 재현 가능 | 6.3 | 2일 |
| 7.3 | 사업·영업 자료 | 공동 검증 | 1쪽 요약, 5~7장 자료, 대표 사례 | 오픈 웨이트 기반 파인튜닝임을 명시하고 과장 없는 승인 문구 사용 | 6.4, 7.2 | 2일 |
| 7.4 | FAQ·리허설 | 공동 검증 | 예상 질문, 제한사항, 발표 피드백 | 사업·영업팀 리허설 1회와 기술 문구 최종 확인 | 7.3 | 1일 |

## 4. 마일스톤

| 마일스톤 | 목표일 | 포함 WBS | 통과 조건 |
|---|---|---|---|
| M1 1차 SFT 하네스 결과 | 10월 2일 | 1.1~1.7, 2.1~2.6, 3.1~3.5, 5.1~5.4 | 약 30개 시나리오 기반 SFT v1·하네스 비교 결과·대표 사례 확보 |
| M2 데이터 확대·SFT v2 | 10월 16일 | 1.4~1.7, 2.4~2.6, 3.4~3.5 | 50개 이상 시나리오와 후속학습 가능한 데이터·reward 기반 확보 |
| M3 최종 학습 후보 | 11월 13일 | 4.1~4.5, 5.4 | 정확성 비악화와 과정 개선 신호를 가진 최종 후보 동결 |
| M4 블라인드 검증 | 11월 20일 | 6.1~6.4 | 10개 이상 봉인 시나리오의 단일 blind 평가로 주장 범위 확정 |
| M5 성과 전달 | 11월 30일 | 7.1~7.4 | 모델 카드·보고서·사업 자료·FAQ 완성 |

## 5. 핵심 의존 관계

```text
데이터 schema ─┬─> 데이터 생성/검수 ─> 학습 view ─> SFT ─> 후속학습 ─> 블라인드 평가 ─> 성과 전달
               └─> trajectory 수집 ──> 평가/Reward ───────────┘

하네스 실행 계약 ─> baseline/호환성 검사 ─> SFT·후속학습 회귀검증 ───────────┘
```

## 6. 프로젝트 성공 기준

- 최종 RCA 정확성과 false-conclusive rate가 기존 하네스 모델보다 악화되지 않는다.
- 1차에는 약 30개 시나리오로 SFT v1 하네스 결과를 10월 2일까지 확보한다.
- 2차에는 50개 이상 시나리오와 10개 이상 blind 시나리오로 11월 20일까지 최종 검증한다.
- 도구 선택·증거 충족률은 +10%p 이상을 목표로 하고, 불필요·중복 호출은 20% 이상 감소를 목표로 한다.
- 최종 정확성은 기존 모델 대비 +10%p를 도전 목표로 하되, 정확성 비악화와 과정 지표 개선을 최소 성공 조건으로 둔다.
- 향상 기준의 구체적 수치는 baseline 측정 후 고정하고 이후 변경하지 않는다.
- 최종 결과는 봉인된 블라인드셋에서 한 번만 평가한다.
- 모델·데이터·평가기·config·raw result가 서로 역추적 가능하다.
- 사업·영업팀이 사용할 수 있는 검증 수치와 대표 사례를 11월 30일까지 제공한다.
