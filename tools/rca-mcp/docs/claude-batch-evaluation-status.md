# Claude 배치 평가 검증 상태

2026-09-10. 원천: `runs/claude-opus5-all/summary.json`,
`runs/claude-opus5-all/exposure-audit.json`, 케이스별 trajectory/result.

## 실행 집계

- 45개 케이스 모두 최종 `result.json` 존재, terminal `end_turn` 확인.
- 현재 보존 파일 기준 도구 호출 1,613회, 미응답 호출 0건.
- 자기 보고: confirmed 20 / provisional 23 / insufficient 2.
- grade 파일 0개. 위 분포는 정답률이 아니다.
- backend_error 3건 모두 scan_metrics에서 발생.

## 정답 비노출 재검증

이전의 “hidden 문자열 0건이므로 정답 비노출 확인” 결론을 철회한다.
메타데이터 파일을 직접 전달하지 않아도 관측 로그에 실험 식별자와 명령이
포함될 수 있다. 이번 검사는 실제 tool_result만 대상으로 수행했다.

| 케이스 | 노출 경로 | 확인된 실험 단서 |
|---|---|---|
| F03-H | list_events, trajectory 174행 | Pod 이름 `scenario-f03-h-order-thread-pool-4jcn9` |
| F05-P | search_host_data, trajectory 55행 등 | syslog에 `cleanup F05-P memhog` 명령과 메모리·지속시간 인자 |

두 케이스는 오염 의심군으로 분리한다. 나머지 43개는 이번 리터럴 검사에서
일치하지 않았다는 의미이며, 누출 없음이 증명된 것은 아니다. 정상 운영에서도
관측 가능한 OOM/SQL/프로세스 정보 자체를 삭제해서는 안 된다. 실험 전용 이름과
주입·정리 명령을 운영 관측과 구분하는 검토가 필요하다.

모든 seed에는 case_id/scenario_id 및 주입 시간 t1/t2가 포함됐다. 실제 제품의
incident seed와 동일한 입력이 아니므로 이번 실험은 “알려진 장애 시간창에서의
탐색” 평가다. 최초 알람·증상 대상이 없는 조건도 조사 결과에 영향을 준다.

## 정답 기준

meta.json의 scenario_metadata.cause는 주입 의도 기준으로 별도 평가자가
읽을 수 있으나, 실제 관측 가능성과 동일하지 않다. 예를 들어 F01-H는 외부 PG
429가 기준 원인인데 결과는 payment의 실패 위치까지만 국소화했다. 이를 전체
정답으로 처리하면 안 되며 원인 메커니즘 적중과 부분 국소화를 구분해야 한다.
F18-P처럼 메타데이터의 bean 비활성 설명과 결과의 제어 테이블 설명이 다른
경우는 실행 원천을 더 확인해야 한다.

따라서 공식 정확도는 아직 미산출이다. 후속 채점은 원인 메커니즘, 원인 위치,
다중 원인 회수, 근거 일치, 단정 수준을 별도 판정하며 오염 의심군을 분리한다.
특히 데이터 없음의 원인을 입증하지 않은 상태에서 golden의 관측 상한을
confirmed/provisional로 임의 설정하지 않는다.

## 재현

```sh
python3 scripts/summarize_claude_batch.py runs/claude-opus5-all --out runs/claude-opus5-all/summary.json
python3 scripts/audit_claude_exposure.py runs/claude-opus5-all --out runs/claude-opus5-all/exposure-audit.json
```

원본 실행 파일은 이번 검증에서 수정하지 않았다. 이전 F15-G 재실행은 동일
디렉터리에 기록했으므로 최초 시도와 재시도를 포함한 총비용/호출 수는 현재
파일만으로 복원할 수 없다.

## 도구 응답 전수 진단

`reports/claude-evaluation/tool-diagnostics.json`을 별도 생성했다.

- tool_result 1,613개, JSON 봉투 1,583개, is_error 29개. 봉투가 아닌 나머지
  응답을 자동 성공으로 간주하지 않는다.
- 표시 절단 271회; unknown 148회; zero_observations 261회;
  not_collected 12회; backend_error 3회.
- 최종 evidence ref 668개 중 도구가 돌려준 refs와 문자열이 정확히 같은 것은
  591개(88.5%). 나머지 77개는 원천 재해석이 필요한 참조다. 이는 환각 확정도,
  근거의 의미적 지지 여부 검증도 아니다.
- 39개 케이스, breakdown_endpoints 211회에서 현재 날짜 기준 TTL 때문에
  기준선 비교가 비활성화됐다는 응답을 확인했다. 코드
  `tools/breakdownendpoints.go:382`는 time.Now()에서 3일을 빼므로, 과거 데이터를
  장기 보존한 복원 DB에서도 기준선을 조회하지 않을 수 있다. 후속 배치 전에
  replay 시간·보존 정책을 실제 가용 데이터에 맞춰야 한다.
- MCP 생성자는 firstEvent/lastEvent를 모두 zero time으로 전달한다. 호출자가
  기준선과 시간을 생략하면 캡처 seed 시각을 쓰지 않으므로 이번 평가의 또 다른
  재현성 제한이다.
- 초기 restore.log 45개 모두 incidents=0, members=0을 출력했다. 단순히
  eligibility 플래그만으로 제품 incident seed가 존재한다고 주장할 수 없다.
  캡처 손상/복원 누락/데이터 생성 정책 중 어느 원인인지는 별도 확인 대상이다.

도구 설계 평가와 모델 정답률을 해석할 때 이 실행 조건들을 함께 보고한다.
