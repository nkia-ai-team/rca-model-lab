# Blind RCA 입력과 관측 응답 경계

2026-09-10. 기존 배치에 노출된 F03-H 파드명과 F05-P syslog 실험 명령을
보완한다. 원본 캡처·기존 trajectory는 수정하지 않는다.

## 초기 입력

`scripts/build_observed_seed.py`는 metadata의 시간 경계만 읽고,
lucida_events_local Parquet에서 실제 관측 알람을 추출한다. 케이스·시나리오
ID, cause, 설명, 주입 내용, 정답 파일은 public seed에 포함하지 않는다.

- [t1,t2) 안의 non-cleared 알람을 시각·event_id 순서로 최대 50개 선택한다.
- event_id, 발생시각, detector, 종류, severity, 대상 UUID, 서비스, reason,
  transition, 관측 metric/direction/숫자 baseline/observed만 전달한다.
- affected_targets/services는 선택된 알람에서만 추출한다. 실제 장애 영향
  대상을 확정한 목록이 아니라 조사 시작점이다.
- 원천 전체 수, 창 내 수, 적격 수, 선택 수, 절단 여부를 기록한다.
- 알람이 없으면 입력 생성을 실패시킨다. 시나리오 설명으로 증상을 만들지 않는다.

이 입력은 **관측 알람 seed**이며 제품의 IncidentSeed 재현은 아니다.
기존 45개 복원 로그의 incidents=0 조건 때문에 제품 incident를 가장하지 않는다.
시간창은 여전히 t1/t2를 이용한 사후 선택이며 실제 자동 탐지 시각 평가와 다르다.

## 모델 출력 경계 필터

`internal/blind.SanitizeJSON`을 MCP의 `-blind` 모드와 public seed 작성에 공유한다.

- scenario/case 이름 전체를 안정적인 opaque 식별자로 치환한다. 설명형 접미사도
  남기지 않으며 UUID는 유지한다.
- 실험 식별자 또는 주입·정리 표시가 포함된 명시적 COMMAND/sudo 문자열과
  scenario-runs 경로, hidden metadata 필드를 숨긴다.
- 중첩 JSON, raw 사본, refs, 오류 응답, tool schema에도 같은 규칙을 적용한다.
- 정상 SQL LOCK, OOMKilled, HTTP 429, 일반 stress-ng 관측은 보존한다.
- 응답 직렬화/필터 실패 시 원문으로 폴백하지 않는다. 변경된 도구 봉투에는
  blind_filter 통계를 붙이고 seed 필터 통계는 별도 파일에 남긴다.

치환된 이름을 포함한 ref는 표시용 인용 좌표다. 동일 이름의 일관성은 유지되나
원천 이름을 역변환하는 조회기는 아직 없으므로 원문 selector로 재사용하면 안 된다.
이 필터는 확인된 명시적 패턴을 차단하며 임의 자연어의 정답 단서를 모두 제거했다고
증명하지 않는다. 운영 데이터의 실제 원인 증거까지 삭제하지 않도록 범위를 제한했다.

## CLI 실행기

runner는 케이스 이름이 없는 /tmp/rca-blind-* 작업 폴더와 실행파일 경로를 사용한다.
public prompt에는 정제된 관측 seed만 포함한다. 원본 seed와 모델 설정은 외부 run
폴더에 저장한다. 기본 file/shell 도구와 스킬을 비활성화하고 strict MCP 설정으로
RCA 도구만 허용한다. 사용자·프로젝트 설정 소스를 끄고 명시적인 system prompt를
사용한다. 이 설정을 OS 수준의 파일 접근 격리나 완전한 누출 방지 인증으로 부르지 않는다.
CLI 인증은 기존 로그인 상태를 사용한다(`--bare` 사용 안 함).

기존 trajectory가 있으면 덮어쓰기를 거부한다. grade stdout은 grade.log로 보내
채점기의 grade.json을 덮어쓰지 않는다. 설정에 실제 지정하지 않은 temperature=0
주장과 golden_exposed=false 보증은 제거했다.

## 검증 결과

- 45개 캡처 모두 관측 알람 seed 생성 성공. F03-H 165개 중 50개,
  F05-P 233개 중 50개 선택.
- 기존 45개 trajectory의 tool_result를 새 필터로 오프라인 재처리하여
  알려진 시나리오 식별자 패턴 잔존 0건 확인. F03-H 치환 9회,
  F05-P 문자열 숨김 18회. 원본 해시와 파일 내용 보존 확인.
- 실제 F03-H로 runner 준비 모드를 실행해 public prompt의 ID 제거,
  관측 알람/대상 제공, opaque MCP 경로와 -blind 플래그 확인.
- Go 필터/응답 경계 테스트, 합성 Parquet seed 테스트 5개 및 전체 Go 테스트 통과.

오프라인 결과: `reports/claude-evaluation/blind-input-validation.json`.
이번 검증은 Claude 재평가 결과가 아니며 정확도 개선 수치는 아직 없다.

## 재현

```sh
go build -o /tmp/rca-blind-check ./cmd/rca-mcp
python3 scripts/validate_blind_inputs.py --binary /tmp/rca-blind-check --out reports/claude-evaluation/blind-input-validation.json
python3 -m unittest discover -s scripts -p test_build_observed_seed.py
# 준비만 실행: 격리 backend 환경변수 지정 후 새로운 run 디렉터리 사용
RCA_PREPARE_ONLY=1 scripts/run_claude_case.sh /data/eval-cases/<case> runs/<new-run>
```
