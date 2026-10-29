# Opus 5.5 structured-v1 평가 하락 분석

2026-10-08 저장된 48개 결과·trajectory·실제 request를 읽기 전용으로 비교했다. 새 평가/재채점은 실행하지 않았다.

## 결과

| 조건 | 원인 대상 적중 | 대상+기전 완전 통과 |
|---|---:|---:|
| 기존 ablation-harness-final | 22/24 | 20/24 |
| opus55-structured-opus12-20261008 | 19/24 | 17/24 |

하락은 정확히 아래 3회에 집중한다. 다른 21회의 원인 적중 여부는 같다. F03-H 두 회는 양쪽 모두 대상 적중/기전0.5이고 F20-R 두 회는 양쪽 모두 대상 실패다.

| 캡처 | 기존 대상/기전 | structured 대상/기전 | 실행상 차이 |
|---|---|---|---|
| F09-H run1 | 1 / 1 | 0 / 대상 미적중으로 기전 미채점 | 기존22턴·20조회 → 신규8턴·12조회, 강제 제출 |
| F09-H run2 | 1 / 1 | 0 / 대상 미적중으로 기전 미채점 | 기존21턴·19조회 → 신규8턴·11조회, 강제 제출 |
| F06-R run2 | 1 / 1 | 0 / 대상 미적중으로 기전 미채점 | 기존13턴·12조회 → 신규7턴·11조회, 강제 제출 |

## 1. F09-H: 읽었던 힙 설정 값이 제출 직전에 문맥에서 제거됨

정답은 commerce-order의 JAVA_TOOL_OPTIONS에 -Xmx96m/-Xms96m이 추가된 힙 축소다. 기존 답안은 설정 변화와 JVM CPU/메모리·checkout 지연을 연결했다.

신규 run1은 [trajectory 8행](../outputs/mcp-eval/opus55-structured-opus12-20261008/case-f09-h-v3-0c795f2b/run1/trajectory.jsonl)에서 read_observation으로 실제 before/after 값을 읽었고, 10행에서 모델이 “-Xmx96m 힙 제한을 추가”했다고 말했다. run2도 7행에서 값을 읽고 9행에서 같은 단서를 인정했다. 따라서 원문 저장/재조회 자체가 실패한 사례는 아니다.

하지만 **두 run 모두 requests.jsonl의 turn7에는 해당 페이지 6000자가 존재하고, 최종 turn8에는 589자 메타데이터로 대체되어 -Xmx96m 원문이 사라진다.** 이전 assistant 발화에는 설정 값이 남아 있으므로 모델이 모든 정보를 완전히 잃었다고 단정할 수는 없다. 그래도 최종 result는 두 번 모두 “변경 값을 확인하지 않았다”고 서술하고 causes=[]를 제출한다. 관측 이력과 결론이 명백히 모순된다.

증거:
- [run1 실제 요청](../outputs/mcp-eval/opus55-structured-opus12-20261008/case-f09-h-v3-0c795f2b/run1/observations/requests.jsonl): 7·8행
- [run2 실제 요청](../outputs/mcp-eval/opus55-structured-opus12-20261008/case-f09-h-v3-0c795f2b/run2/observations/requests.jsonl): 7·8행
- [run1 최종 답](../outputs/mcp-eval/opus55-structured-opus12-20261008/case-f09-h-v3-0c795f2b/run1/result.json)
- [run2 최종 답](../outputs/mcp-eval/opus55-structured-opus12-20261008/case-f09-h-v3-0c795f2b/run2/result.json)
- [기존 run1](../outputs/mcp-eval/ablation-harness-final/case-f09-h-v3-0c795f2b/run1/result.json)
- [기존 run2](../outputs/mcp-eval/ablation-harness-final/case-f09-h-v3-0c795f2b/run2/result.json)

## 2. F06-R run2: payment 상세를 다시 읽기 전에 조사 종료

모델은 order→payment 호출이 약10초 뒤 502라는 사실을 찾았다. 17행에서 다음 홉 payment를 조사했고 18행의 응답은 status=normal 요약을 제공했지만, findings 18,922자는 생략됐다. 이 요약 자체에도 “건강하다는 뜻이 아니다”라는 주의가 있다. 19행 로그 응답의 findings764자도 생략됐다. 곧바로 20행에 하네스가 조사 예산 종료를 삽입하여 read_observation을 호출할 기회를 차단했다.

최종 답은 “payment 쪽 이상이 보이지 않았다”면서 발원지 payment 대신 피해 서비스 order를 원인으로 제출했다. 동일 캡처 run1은 payment egress 원문을 조회하여 외부 PG 10초 SocketTimeout을 찾고 정답이었다. 다만 run1/run2 조회 시간창이 각각22:24~22:33 / 22:22~22:33으로 달라 기준선과 도구 요약도 달랐다. 단지 요약 표시만으로 모든 차이를 설명할 수 없다.

- [실패 trajectory](../outputs/mcp-eval/opus55-structured-opus12-20261008/case-f06-r-v3-6ace8a7d/run2/trajectory.jsonl): 17~21행
- [실패 답안](../outputs/mcp-eval/opus55-structured-opus12-20261008/case-f06-r-v3-6ace8a7d/run2/result.json)
- [동일 캡처 성공 답안](../outputs/mcp-eval/opus55-structured-opus12-20261008/case-f06-r-v3-6ace8a7d/run1/result.json)

## 3. 구현상 확인된 공통 병목

1. [student.py 374~376행](../src/rca_lab/mcp/student.py): prompt_tokens >= max_prompt_tokens 또는 턴 상한이면 강제 제출한다. 세 실패는7~8턴이므로60턴 상한이 아닌40000 prompt-token 상한 경로다. 저장 runtime 설정은40000/60을 확인해 준다. 각 호출의 이전 usage 원본을 run별로 저장하지 않아 정확한 초과 토큰 수는 이 자료만으로 특정하지 않았다.
2. [context.py 15~35행](../src/rca_lab/mcp/context.py): 최근8개 도구 응답 외에는1200자로 압축한다. [observations.py 217~254행](../src/rca_lab/mcp/observations.py)의 페이지 압축은 page.text를 완전히 제외하고 재조회 메타데이터만 남긴다.
3. [proxy.py 23~33행](../outputs/opus55-structured-opus12-20261008/serving/proxy.py): 강제 제출 시 tools를 submit_rca 하나로 줄인다. 따라서 “없어진 정보는 원문을 재조회하라”는 정책과 “이제 재조회할 수 없다”는 최종 단계가 충돌한다.
4. [observations.py 146~160행](../src/rca_lab/mcp/observations.py): 개별 필드가700자를 넘으면 통째로 생략한다. 이는 전체6000자 예산과 별도 기준이다. F06-R 로그764자 findings도 생략되어 불필요한 추가 호출이 필요해졌다.
5. 원문은 보존되지만 read_observation 응답도6000자 봉투 예산을 적용하여 본문은 대략4800~5000자씩 전달한다. 큰 findings를 문자 페이지로 읽으면 조회 수·문맥 사용량이 늘어난다.

**확정:** 조기 강제 제출, 원문 페이지 제거, 재조회 차단은 실제 요청과 코드에서 확인했다. **미확정:** 이 정책만 고치면3건 모두 회복되는지, structured 정책이 동일 실행기 대비 순수하게 얼마나 불리한지는 재실험이 필요하다.

## 비교의 한계

같은 모델명·12캡처×2회지만 순수한 legacy/structured 단일 변수 비교가 아니다. 과거 score의 runner=teacher이며 Claude CLI stream.jsonl이 존재한다. 신규는 runner=student의 OpenAI 호환 API 루프, 대상 T핸들,40000 context 예산, rolling context와 강제 submit을 쓴다. 기존 teacher와 현재 student의 턴 정의·모델 기본 추론 설정도 동일하다고 가정할 수 없다. 기전 판정 모델은 과거Opus5.5, 신규GPT-6.1-sol로 다르다. 다만 이번3건은 기전 judge 이전의 결정적 대상 채점부터 실패하므로 judge 차이가 하락3건의 설명은 아니다.

## 수정 방향과 검증

- 조사 토큰 예산과 최종 제출을 분리한다. 문맥 예산이 차면 원문을 압축하고 근거를 재조회할 여유를 먼저 제공하며, 모델이 아직 미해결이라고 표시한 관측을 남겨둔 채 바로 submit만 강제하지 않는다.
- 읽은 근거의 구조화된 인덱스(observation ID, JSON pointer, 대상, 수치/변경값, 원문 참조)를 보존한다. 페이지 본문 제거 시에도 핵심 근거와 출처를 연결하고 제출 전에 다시 해석 가능한 상태로 제공한다. 특정 F09 정답 문자열을 하드코딩하지 않는다.
- 배열 전체 생략과700자 필드 제한 대신 예산 내 구조적 항목·경로 단위 전달을 사용한다. 이상 여부 요약과 실측 수치를 함께 제시하되 생략을 정상 판정으로 오해하지 않게 한다.
- 같은 API 실행기/도구 바이너리/프롬프트/추론 조건/예산/채점기로 legacy와 수정 structured를 비교한다. 우선3회 실패 경로의 회귀를 검증하고, 최종 결과는 동일12캡처×2회로 비교한다. 정확도뿐 아니라 강제 제출률·근거 재조회율·소요시간도 기록한다.

이 분석에서는 하네스와 평가 결과를 수정하지 않았다.
