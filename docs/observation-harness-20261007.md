# 관측 전달 하네스 개선 — 2026-10-07

## 목표와 범위

기존 원본 보존 결정(08-27), top-N 후보 누락(09-08), summary 절단(10-06)을 계승한다. 새 학습이나 범용 샌드박스에 앞서, 모델에게 제시하지 못한 관측을 식별하고 실제로 다시 읽을 수 있게 한다. 기존 회복 서비스와 점수는 변경하지 않는다.

## 구현 계약

- 비교용 `structured-v1` 정책을 추가한다. 기존 `legacy` 기본 경로는 재현용으로 유지한다.
- blind MCP 응답을 UUID alias 처리한 뒤, 문자열 절단 전에 run별 저장소에 보관한다. 저장소는 DB 원본 전체가 아니라 **도구가 반환한 응답 전문**이다. 도구 내부 top-N·본문 절단·미수집 데이터는 복원했다고 주장하지 않는다.
- 모델 화면은 유효한 JSON이며 원본 ID/hash/길이, 출처 상태와 표시 생략을 분리한다. 사실은 전체 필드 단위로 선택한다. 요약을 모델이 새로 만들어 사실로 대체하지 않는다.
- `read_observation`은 현재 실행에서 발급한 짧은 ID(O1 등)만 받아 유한 페이지를 반환한다. `json_pointer`로 정확한 필드·배열 원소를 선택할 수 있다. 문자 offset은 선택한 값 안의 위치다. 같은 저장 응답을 재조회하므로 DB 변경에 따른 결과 불일치를 피한다.
- rolling view와 컨텍스트 초과 복구도 구조화 관측의 ID와 재조회 정보를 보존한다. 기존 legacy 화면은 그대로 둔다.
- 새 실행은 실제 wire request(도구 catalog 포함)를 `observations/requests.jsonl`에 보관해 이후 prefix 검증에 사용한다. 오류로 거부된 요청도 포함되므로 성공 여부는 실행 결과와 함께 해석한다.
- 학습·추론의 공통 rolling_view를 유지한다. 새 정책으로 기존 reasoning이나 verdict를 자동 재사용하지 않는다. 새 데이터 학습은 별도 parity/admission gate 이후다.
- 새 정책의 ref 점수는 실제 화면 또는 읽은 페이지에 노출된 근거만 인정한다.

## 검증 순서

1. 원본 exact roundtrip, 예산, Unicode, 미발급 ID 거부, no_data/source-truncated 보존, 과거 관측 재조회 계약 테스트.
2. 기존 train-only 고정 snapshot에서 legacy/structured 필드 보존과 페이지 복원 비교. SHA를 기록하고 원본 수정 금지.
3. 같은 고정 관측으로 기반모델/기존 adapter의 제한된 사실 조회 실험. 정답은 원본 필드, 판정은 exact-match이며 RCA 정확도나 새 LLM 채점으로 보고하지 않는다.
4. full-episode 2×2 RCA 비교는 별도 사례/seed/모델/도구 바이너리/예산을 고정해야 한다. 현재 기존 평가가 같은 서버에서 진행 중이므로 작은 직렬 probe부터 검증한다. 새 mechanism 채점 시 GPT-6.1 Sol만 사용한다.
5. 관측에 맞는 행동·추론 공동 교정과 admission을 통과한 뒤 새 하네스 학습 데이터를 만든다. 일반화 확인용 holdout은 개발에 사용하지 않는다.

## 완료 보고의 구분

구현 완료, 결정론적 근거 접근 검증, 실제 모델 사용 검증, full RCA 성능 검증을 각각 기록한다. 앞의 성공을 뒤의 성공으로 확대하지 않는다.


## 구현·검증 결과

- `observations.py`: run-scoped O1 핸들, sanitized/aliased 응답 전문 보관, whole-field JSON 화면, 정확한 페이지 조회와 JSON Pointer 선택 구현. 화면 생략과 원천 truncated/no_data 분리. 페이지의 JSON escape까지 포함해 문자 예산 검사.
- `student.py` / `context.py`: `structured-v1` 옵션, local reader dispatch, rolling/emergency compact, 실제 모델 요청 기록 연결. 근거 점수는 성공한 모델 요청에 실제 전달된 ref만 집계한다. 아직 응답하지 않은 같은 배치의 도구 결과는 근거로 인정하지 않는다.
- `mcp_case_run.py`: `--observation-policy structured-v1`, 원본 저장 경로, source/binary SHA 기록, 다른 정책 또는 코드의 캐시 혼합 거부.
- SFT·RL export 모두 새 정책 데이터를 기존 정적 도구 catalog에 섞지 않고 명시적으로 거부한다. 새 정책의 학습 지원은 별도 admission/runtime prefix 검증 후 활성화한다.

### 고정 train 관측 감사

[감사 결과](../outputs/observation-harness-20261007/summary.json): 18사례, 35감사 항목이 참조하는 관측 411건. 중복 참조를 제거한 source SHA는 274개다. 독립된 411사례가 아니다.

- 저장된 source의 exact 재조회 411/411, SHA 불일치 0, 페이지 581개 모두 6,000자 이하.
- 최근 화면과 이전 1,200자 화면 각각 411/411 유효 JSON, 화면 예산 초과 0.
- 저장 당시 legacy 절단 표시가 이미 있는 111건은 원본 뒷부분이 복구되지 않는다. 위 411/411은 **현재 snapshot에 남아 있는 문자열**의 재조회다.
- 원천JSON에서 확인 가능한 조회 범위 필드 82건: 같은 1,200자 화면에서 legacy 20건, structured 63건이 정확한 값을 유지한다. 새 6,000자 초기 화면에서는 82건 모두 유지.
- status 286건은 두 old-view 모두 유지. summary는 legacy 286건, structured 285건 유지. 새 화면도 모든 사실을 항상 담는 것은 아니며, 생략된 값은 원본 ID/JSON Pointer로 재조회해야 한다.
- source/binary가 이미 다른 과거 RCA 점수에 이 수치를 합쳐 성능 개선으로 보고하지 않는다.

### 실제 모델의 지정된 사실 조회

[첫 실험](../outputs/observation-probe-20261007/result.json): 같은 관측의 `findings[1].axis`를 묻는 실험. 두 legacy 조건은 unknown. 문자 offset 재조회는 두 모델 모두 실행했으나 base는 1,024 출력 토큰 소진, adapter는 다른 axis 값 선택. 첫 결과는 보존했다.

[JSON Pointer 후속](../outputs/observation-probe-20261007/pointer-v2/result.json): 같은 source와 같은 정답, 초기화면 예산 1,200자, 출력 1,024토큰, seed 7107. 네 조건 모두 새로 실행, 총 6요청.

| 모델 | legacy | structured-v1 |
|---|---|---|
| muse-glimmer-base | unknown | `/findings/1/axis` 조회 후 `framework_segment` 정확히 응답 |
| mcp-v2bs-checkpoint-94 | unknown | `/findings` 조회 후 `framework_segment` 정확히 응답 |

이 실험은 정답 경로를 지정한 assisted lookup이다. 자율 원인 탐색, RCA 정확도, 학습 모델의 우위 또는 일반화 입증이 아니다. legacy의 unknown은 보이지 않는 값을 추측하지 않은 적절한 응답이다. 후속은 경로 보존 조회 개선 후의 개발 실험이며 첫 실패를 숨기지 않는다.

[최종 코드 재현 검증](../outputs/observation-probe-20261007/pointer-v2/verification.json): 최종 관측 모듈의 렌더링 및 두 재조회 응답 SHA가 실제 모델 실험과 일치(3/3). 추가 모델 호출 없음. 서버 모델 메타데이터는 실험 후 관측으로 별도 저장했으며 weights 해시 검증으로 취급하지 않는다.

### 후속 RCA pilot

같은 train 사례 `case-rcaeval-tt4-v3-158f2019`, 두 모델 × 두 정책, 동일 20턴으로 full-episode 비교를 완료했다. 설정 seed는 7107이며 run1 보정과 턴 번호를 더하므로 첫 wire request seed는 8108이다. 출력 예산은 요청당 4096토큰, temperature=1.0, top_p=0.95, top_k=64다. 기존 평가·복구 서비스와 DB는 변경하지 않고 별도 Docker project로 캡처를 복구했다. 산출물은 `outputs/observation-rca-pilot-20261007/`에 격리한다. 단일 학습 사례 결과는 일반화 성과가 아니다.

서버는 기존 평가와 공유한다. 실행 도중 다른 요청 및 대기를 확인했으므로 소요 시간은 정책 간 속도 비교 근거로 사용하지 않는다.

[전체 조사 결과](../outputs/observation-rca-pilot-20261007/summary.json): 단일 train 사례, 네 조건 모두 원인 F1=0, strict 성공 0/4. 판정은 기존 정답 계약에 따른 결정론적 평가이며 새 LLM 기전 채점은 하지 않았다.

| 모델 | 하네스 | 최종 결과 | 재조회 호출 | 도구 오류 |
|---|---|---|---:|---:|
| muse-glimmer-base | legacy | insufficient | 0 | 0 |
| muse-glimmer-base | structured-v1 | insufficient | 4 | 0 |
| mcp-v2bs-checkpoint-94 | legacy | insufficient | 0 | 0 |
| mcp-v2bs-checkpoint-94 | structured-v1 | forced_answer_rejected / 답변 없음 | 2 | 2 |

학습모델의 structured 조건은 `scan_metrics`에 실제 target ID 대신 `ts-db`를 넣었고, 최종 확정·잠정 답변에 원인 목록이 없어 제출이 거부됐다. 이 조건을 정상적인 insufficient 답변으로 처리하지 않는다. reader 2회 중 한 번은 원본 길이와 같은 offset으로 EOF를 읽어 빈 페이지를 받았다. 즉 도구 호출 자체와 유용한 근거 회수를 구분해야 한다.

첫 실행은 SIGTERM(143)으로 중단됐고 학습모델 legacy 조건에는 완료 결과가 없었다. 완료된 기반모델 두 조건은 보존하고 나머지만 재개했다. 첫 실행 wrapper SHA는 root가 실행 중 읽었던 값으로 `first-run-provenance-recovered.json`에 별도 기록했다. 최초 capture manifest는 없었고 resume 시 기록했으므로, 재개 provenance가 최초 실행 당시 모든 파일의 동일성을 증명한다고 주장하지 않는다. 이 파일럿은 개발 진단이며 정식 성능 벤치마크로 사용하지 않는다.

**결론:** 관측 보존·재조회는 구현됐지만 이번 단일 사례의 RCA 성공이나 자체 모델 우위는 확인되지 않았다. 반복 대상 탐색, 유효 target 선택, 관측에 맞는 가설 전환, 유효한 최종 제출을 다음 행동 교정 대상으로 삼는다.

### 완료된 기반모델 조사에서 확인한 행동

두 정책 모두 `insufficient`로 제출했고 원인 F1은 0이다. legacy는 `search_targets`를 7회, structured는 6회 사용했으며 양쪽 모두 MongoDB/database 대상 검색을 반복했다. structured는 `read_observation`을 4회 성공적으로 사용했다. 따라서 이번 사례에서 재조회 도구의 사용 가능성은 확인됐지만, 조회 행동이 원인 규명으로 이어지지는 않았다. 다음 교정 후보는 수집되지 않은 DB 대상 검색을 반복하는 대신 coverage에 맞는 다른 가설·관측으로 전환하는 행동이다. 이것이 유일한 실패 원인이라는 주장은 아니며 정답이나 교정 추론을 실험 입력에 주입하지 않았다.

### 후속 개선의 판단 기준

- 도구 응답에 필요한 사실이 있지만 모델 화면에서 사라진 경우: 이번 저장·재조회 계약으로 해결 여부를 검증한다.
- 도구 응답 자체에 필요한 사실이 없는 경우: 조회 범위, pagination, 집계 및 coverage 계약을 먼저 확인한다. 저장소에 없는 DB 원본을 저장소 reader로 찾을 수는 없다.
- 사실을 조회할 수 있는데 모델이 적절한 도구·대상·필드를 선택하지 못한 경우: 실제 관측에 맞는 행동과 추론을 함께 교정하고 admission으로 검증한다.
- 고정 조회·집계 도구로 필요한 분석을 표현할 수 없다는 사례가 확인되면 제한된 코드 실행을 추가한다. 자유 코드 실행을 먼저 도입했다고 RCA 성과가 입증되는 것은 아니다.
- 자체 모델 성과는 같은 하네스·예산의 기반모델 대비 학습모델 성능으로 측정한다. 개발 사례의 접근성 개선과 holdout RCA 성능을 분리하며, 새 mechanism 채점이 필요하면 GPT-6.1 Sol을 사용한다.

### 검증과 참고 문서

Python 전체 테스트 269통과 / 1skip(torch 환경 부재), 기존 변경 Python 12파일 Ruff 통과. 추가 파일럿 실행기도 Ruff 및 py_compile 통과. 네 조건 완료 후 실행 기록 불변성·설정 및 소스 변경 거부 테스트 4개를 추가했고 전체 테스트를 다시 통과했다. 이 사후 실행기 수정은 이미 완료된 모델 호출에 적용됐다고 주장하지 않는다. 실제 GPU 학습이나 모델 가중치 변경 없음. 관측 모듈과 학습 유입 경로의 독립 리뷰 통과.

Spine에서 INDEX와 `Wiki/research/2026-09-18_rca-mcp-student-distillation-and-lora-overshoot.md`를 참고했다. 9월 문서의 옵티마이저 설명은 10월 2일 gradient 차단 발견으로 정정됐으므로, 최신 로컬 시행착오 기록을 우선했다.
