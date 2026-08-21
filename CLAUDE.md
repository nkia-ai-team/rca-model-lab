# rca-model-lab — 에이전트 작업 지침

목적: RCA 특화 모델 파인튜닝 실험실. 파이프라인/디렉토리/규칙은 README.md 가 정본이다. 여기엔 에이전트가 자주 틀리는 것만 적는다.

## 환경
- Python 3.12, `uv` 관리. 실행은 항상 `uv run ...`. 전역 pip 설치 금지.
- 학습은 클라우드 GPU 에서 (`make sync-train`). 로컬은 데이터 합성/정제만 — 로컬에서 학습 의존성을 기본 설치하지 않는다.
- `data/ models/ outputs/` 는 gitignore. 그 아래 파일은 절대 git add 하지 않는다 (`.gitkeep` 제외).
- 시나리오 ground truth 는 `../rca-scenario-runner` 에서 읽기만 한다. 그 레포를 수정하지 않는다.

## 코드 규칙
- 학습 스택은 TRL + PEFT 고정 (SFT → GRPO 연속성). 다른 프레임워크 제안 전에 사용자에게 확인.
- 샘플은 `rca_lab.data.schema.RcaSample` 로만 주고받는다. ad-hoc dict 금지.
- 교사 모델 호출은 `rca_lab.synth.teachers.Teacher` 구현체로만. API SDK 직접 호출 추가 금지(CLI 세션 사용이 원칙).
- 새 실험 = `configs/` 에 yaml 추가. 학습 스크립트에 하드코딩된 값 넣지 않는다.
- 테스트: `make test`. 외부 CLI·GPU 가 필요한 테스트는 만들지 않는다(fixture 로 대체).

## 하지 말 것
- `claude`/`codex` CLI 를 `--dangerously-*` 옵션으로 호출하는 코드.
- `data/ models/ outputs/` 아래 삭제 — 재생성에 시간이 드는 산출물이다. 삭제 전 사용자 확인.
