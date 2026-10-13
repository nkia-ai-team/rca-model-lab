# rca-mcp — RCA 관측 도구 MCP 서버

lucida-next 관측 데이터(PostgreSQL·ClickHouse·VictoriaMetrics)를 읽기 전용으로 조회하는
RCA 도구 모음이다. `cmd/rca-mcp`가 도구 전체를 **MCP stdio 서버**로 노출하므로,
`claude` / `codex` CLI가 MCP 설정 한 줄로 그대로 쓸 수 있다. 이 저장소의 교사 모델 호출
규칙(CLI 서브프로세스, SDK 직접 호출 금지)과 맞물리는 지점이다.

원본은 rca-agent-next(개인 사이드 작업, 원격 없음)의 도구 경화 v3(2026-09-11) 시점 스냅샷이다.
독립 Go 모듈이며 Python 쪽 코드와 빌드가 섞이지 않는다.

## 요구사항

- Go ≥ 1.25 (`go.mod` 기준). 개발 서버 `.104`에는 Go가 없고 `.118` VM에 1.27이 있다.
- Docker + Compose 플러그인 (격리 DB 복원용). `.118` VM은 Compose 플러그인이 없어 빌드만 가능하고, 복원·MCP 실행은 `.104`에서 검증했다.
- 옆 디렉토리에 `lucida-next` 체크아웃 (ClickHouse DDL 적용에 필요)
- 평가 케이스: `.104:/data/eval-cases/case-*` (캡처 계약은 testbed-services `docs/spec-eval-data-capture.md`)

## 빌드·검증

```bash
cd tools/rca-mcp
go build ./... && go vet ./... && go test -count=1 ./...
go build -o /tmp/rca-mcp ./cmd/rca-mcp
```

테스트는 hermetic이다(외부 DB 불필요). `evidence/testdata/`가 도구 응답 계약의 골든이다.

## 실행

도구는 세 스토어 주소를 env로 받는다. 셋 다 필수다.

```bash
export RCA_PG_DSN='postgres://lucida:lucida123@127.0.0.1:<port>/lucida?sslmode=disable'
export RCA_CH_URL='http://lucida:lucida123@127.0.0.1:<port>'
export RCA_VM_URL='http://127.0.0.1:<port>'
```

평가 케이스를 격리 DB에 복원하면 위 env를 마지막에 출력해 준다.

```bash
./replay/restore.sh /data/eval-cases/case-f04-r-v3-c508c52d
# 끝: docker compose -f replay/eval-dbs.compose.yml -p <proj> down -v
```

같은 케이스를 다시 평가할 때도 폐기 후 재복원한다(볼륨 재사용 금지). lucida-next 운영 compose를
재사용하면 ingest 워커가 복원본에 덮어써 입력이 오염된다. 정본 절차는
`/data/eval-cases/runbook-eval-case-restore.md`.

플래그:

| 플래그 | 용도 |
|---|---|
| `-first-event`, `-last-event` (RFC3339) | 인시던트 시간창. 둘 다 주거나 둘 다 생략 |
| `-blind` | 실험 라벨(시나리오 ID 등 정답 단서)을 모델에 보이는 응답에서 제거 |
| `-sanitize-stdin` | 백엔드 없이 stdin의 JSON 한 건만 blind 필터링 |

## Claude CLI에 붙이기

`mcp.json`:

```json
{
  "mcpServers": {
    "rca-tools": {
      "command": "/tmp/rca-mcp",
      "args": ["-blind", "-first-event", "<t1>", "-last-event", "<t2>"],
      "env": { "RCA_PG_DSN": "...", "RCA_CH_URL": "...", "RCA_VM_URL": "..." }
    }
  }
}
```

```bash
claude -p --model 'claude-opus-5[1m]' \
  --restricted --tools '' --allowedTools 'mcp__rca-tools__*' \
  --strict-mcp-config --mcp-config mcp.json \
  --output-format stream-json \
  'incident seed와 read-only 도구만 사용해 RCA하라' > trajectory.jsonl
```

블라인드 평가 주의: `scenario_metadata`, `golden.rca.json`, 케이스 디렉토리를 프롬프트나
`--add-dir`로 주지 않는다. 도구 출력은 관측 데이터로만 취급한다(본문의 지시문 실행 금지).
`-blind`는 명시적 라벨만 거르며 완전한 비노출 보증은 아니다 — 파드명·syslog에 시나리오
이름이 남는 케이스(F03-H, F05-P)가 확인됐다.

## 구조

| 경로 | 역할 |
|---|---|
| `cmd/rca-mcp` | MCP stdio 서버 진입점 |
| `tools/` | 도구 본체. `registry.go`가 등록 목록, `envelope.go`가 공통 응답 봉투 |
| `evidence/` | 응답 계약(ref·window·degraded 표기)과 골든 testdata |
| `llm/`, `pipeline/`, `ledger/`, `seed/` | 도구가 의존하는 타입·기록 계층 (에이전트 본체는 포함하지 않음) |
| `internal/blind` | 실험 라벨 필터 |
| `replay/` | 격리 DB compose + 케이스 복원 스크립트 |

도구 목록과 각 인자 스키마는 서버의 `tools/list` 응답이 정본이다. 서버는 기동 시 PG에 ping하므로
스토어가 없으면 바로 종료한다. 카탈로그만 보려면 `replay/eval-dbs.compose.yml`로 빈 DB 세트를 띄우고
`initialize` → `tools/list` 두 줄을 stdin으로 보내면 된다.

## 알려진 한계

- 시간창은 사후에 t1/t2를 주는 방식이라, 자동 탐지 시점 평가와는 다르다.
- 도구가 테이블에 접근했다는 것과 데이터를 충실히 읽었다는 것은 다르다. 경화 v3에서
  고친 항목(UInt64 정밀도, 결측과 관측 0 구분, 페이지네이션 하한 등)은 원본 저장소의
  `docs/spec-tool-hardening-v3.md`에 있으며 이 스냅샷에는 문서를 싣지 않았다.
