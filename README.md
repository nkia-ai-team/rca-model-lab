# rca-model-lab

관측 데이터로 장애 원인과 기전을 조사하는 RCA 하네스, 교사 풀이 생성, 모델 학습·평가 저장소입니다.
Python 조사 루프가 모델과 Go MCP 도구를 연결하고, PostgreSQL·ClickHouse·VictoriaMetrics에서 근거를 조회합니다.

**구조 보기:** [HTML 아키텍처](docs/rca-harness-architecture.html) · [상세 구조](docs/mcp-harness-architecture.md) · [MCP 빌드·연결](tools/rca-mcp/README.md)

## 하네스 구조

```text
알람·조사 시간 범위
  → Python 조사 루프 ↔ 모델 API / Codex 어댑터
  → Go MCP 읽기 전용 도구 23종
      ├ PostgreSQL: 자산·K8s·관계
      ├ ClickHouse: 로그·트레이스·프로세스·소켓·DB 관측
      └ VictoriaMetrics: 지표
  → submit_rca: 대상·기전·근거 참조 검증
  → 풀이 저장 → 원인 및 기전 채점 → 학습 데이터 / 평가 보고서
```

서버의 서비스는 `search_targets`로 서버 UUID를 찾고 `expand_topology`로 배치 관계를 조회합니다.
등록된 application의 `meta.host_target_id` → server 관계는 트레이스 없이도 조회하며, 트레이스·K8s 배치 정보로 추가 관계를 확인합니다.
프로세스와 포트는 `get_process_snapshot`, `search_host_data(kind="connections")`로 추가 확인합니다.
조회는 수집된 관측·자산 기록 기준이며 서버의 모든 실행 프로세스를 실시간 탐색한다는 뜻은 아닙니다.

`legacy`는 길이 제한과 이전 응답 절단을 사용하는 기존 정책입니다. `structured-v1/v2`는 원문을 별도 보관하고 `read_observation`으로 재조회합니다.
정책명만 같아도 실행 조건이 같지는 않습니다. 모델·템플릿·MCP 바이너리·턴/출력 한도·서버 컨텍스트도 함께 고정해야 합니다.
**진행 중 평가의 고정 바이너리는 소스 변경만으로 교체되지 않습니다.** 최신 기능의 평가에는 새 실행과 버전 기록이 필요합니다.

## 시작하기

Python 3.12, `uv`가 필요합니다. MCP 빌드는 Go ≥ 1.25, 격리 데이터 복원에는 Docker Compose와 캡처 데이터가 필요합니다.

```bash
git clone git@github.com:nkia-ai-team/rca-model-lab.git
cd rca-model-lab
make setup
make test
make lint
```

```bash
cd tools/rca-mcp
go build ./...
go vet ./...
go test -count=1 ./...
go build -o bin/rca-mcp ./cmd/rca-mcp
```

평가 실행기의 실제 옵션을 먼저 확인합니다. 저장소 루트에서 실행합니다.

```bash
uv run python scripts/mcp_case_run.py --help
uv run python scripts/mcp_judge_runs.py --help
uv run python scripts/mcp_export_sft.py --help
```

모델 서버의 엔드포인트·모델명, 데이터 위치, split/정답 계약, 바이너리는 실행별로 명시합니다.
`--max-prompt-tokens 0`은 조사 예산만 해제하며 서버 컨텍스트를 무한하게 만들지 않습니다.
Gemma·Qwen처럼 Muse 전용 문법을 쓰지 않는 모델은 `--no-guided`와 해당 모델의 서버 템플릿·thinking 설정을 사용합니다.
학습 환경 의존성 설치는 `make sync-train`입니다.

## 코드와 데이터

| 경로 | 책임 |
|---|---|
| `src/rca_lab/mcp/` | 조사 루프, 모델 연결, 컨텍스트, 원문 저장, 결론 검증·채점 |
| `tools/rca-mcp/` | Go MCP stdio 서버와 관측 도구 |
| `src/rca_lab/train/` | SFT·LoRA, 모델별 토큰화·손실 처리 |
| `src/rca_lab/data/` | 학습 샘플 스키마·정제·분할 |
| `scripts/mcp_case_run.py` | 케이스 복원, seed 생성, 반복 평가 |
| `scripts/mcp_export_sft.py`, `scripts/mcp_rationalize.py` | 풀이 선별·추론 재작성·학습 데이터 준비 |
| `configs/` | 데이터 분할·정답 계약·학습 설정 |
| `docs/` | 설계·실험 문서 |
| `data/`, `models/`, `outputs/` | 로컬 데이터·가중치·실험 산출물; Git에 넣지 않음 |

## 실험 원칙

- 원인 대상 적중과 **원인+기전 통과**를 구분합니다. 도구 호출 수, 생성 토큰, 시간도 함께 기록합니다.
- 학습·검증·봉인셋은 시나리오 단위로 분리합니다. 검증으로 체크포인트를 고르고 봉인셋은 최종 평가에 사용합니다.
- 학습용 힌트와 정답은 평가 입력에 섞지 않습니다. blind 필터는 보조 방어이며 비누출을 보증하지 않습니다.
- 모델 공급자는 설정으로 선택합니다. 기존 Claude 경로와 Codex 연결, OpenAI 호환 모델 서버 경로가 공존합니다.
- 원본 데이터·가중치·접속 정보·API 키를 커밋하지 않습니다. 환경 변수는 `.env.example`을 참고합니다.
- 실험 조건은 설정과 실행 manifest로 남깁니다. 운영 제품 배포와 연구용 평가 실행은 별개입니다.
- 변경은 작업 브랜치에서 검증하고 PR로 공유합니다. `main` 직접 푸시는 하지 않습니다.
