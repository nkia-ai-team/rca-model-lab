# rca-model-lab — 에이전트 작업 지침

목적: RCA 특화 모델 파인튜닝 실험실. 디렉토리 책임과 규칙은 README.md 가 정본이다. 여기엔 에이전트가 틀리기 쉬운 것만 적는다.

## 환경
- Python 3.12, `uv` 관리. 실행은 항상 `uv run ...`. 전역 pip 설치 금지.
- 학습은 클라우드 GPU 에서 (`make sync-train`). 로컬은 데이터 작업만 — 학습 의존성을 기본 설치하지 않는다.
- `data/ models/ outputs/` 아래는 절대 git add 하지 않는다 (`.gitkeep` 제외). 공유는 HF Hub `nkia-ai-lab` org 로 (README 규칙).
- 시나리오 ground truth 는 `../rca-scenario-runner` 에서 읽기만 한다. 그 레포를 수정하지 않는다.

## 코드 규칙
- 학습 스택은 TRL + PEFT. 다른 프레임워크를 쓰려면 먼저 사용자에게 확인.
- 교사 모델 호출은 `claude` / `codex` CLI 서브프로세스. API SDK 직접 호출을 추가하지 않는다.
- 샘플은 `src/rca_lab/data` 의 공용 스키마로만 주고받는다. ad-hoc dict 금지.
- 새 실험 = `configs/` 에 yaml 추가. 스크립트에 값을 하드코딩하지 않는다.
- 학습 로깅은 W&B (`report_to=wandb`). 프로젝트·엔티티·키는 `.env` 의 `WANDB_*` 에서. run 이름 = config 파일 이름.
- 외부 CLI·GPU 가 필요한 테스트는 만들지 않는다 (fixture 로 대체).
- **아직 정해지지 않은 구현을 추측으로 채우지 않는다.** 비어 있는 모듈은 담당자가 채운다. 필요하면 사용자에게 묻는다.

## 하지 말 것
- `claude`/`codex` CLI 를 `--dangerously-*` 옵션으로 호출하는 코드.
- `data/ models/ outputs/` 아래 삭제 — 재생성에 시간이 드는 산출물이다. 삭제 전 사용자 확인.
