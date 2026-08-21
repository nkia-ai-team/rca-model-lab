# rca-model-lab

RCA(근본원인분석) 특화 모델을 만드는 실험실.
`rca-scenario-runner` 의 장애 시나리오(ground truth 포함)를 재료로 프런티어 모델(Claude / Codex CLI)에게
SFT 데이터를 합성시키고, 그걸로 오픈 모델을 파인튜닝한 뒤, 이후 강화학습으로 확장한다.

## 시작하기

```bash
git clone git@github.com:nkia-ai-team/rca-model-lab.git && cd rca-model-lab
make setup
```

필요한 것: `uv`, 로그인된 `claude` / `codex` CLI, 옆 디렉토리에 `rca-scenario-runner` 체크아웃,
W&B 팀 `nkia-ai` 초대 수락 후 `.env` 에 자기 `WANDB_API_KEY` — [docs/wandb-setup.md](docs/wandb-setup.md).
학습은 클라우드 GPU 에서 `make sync-train`. 나머지 타겟은 `make help`.

## 디렉토리

구조는 잡혀 있고 구현은 비어 있다. 각 자리의 책임만 정해져 있다.

| 경로 | 책임 | git |
|---|---|---|
| `src/rca_lab/scenarios` | rca-scenario-runner 의 시나리오(ground truth) 읽기 | ✔ |
| `src/rca_lab/synth` | 교사 모델(claude / codex CLI)로 학습 샘플 합성 | ✔ |
| `src/rca_lab/data` | 샘플 스키마, 합성→학습셋 정제·분할 | ✔ |
| `src/rca_lab/train` | SFT (TRL + PEFT), 이후 GRPO | ✔ |
| `src/rca_lab/eval` | 채점기 — SFT 평가와 RL reward 가 공유 | ✔ |
| `prompts/` | 교사용/학생용 프롬프트 | ✔ |
| `configs/` | 실험 설정 yaml | ✔ |
| `data/{raw,synth,processed}`, `models/`, `outputs/` | 데이터·가중치·실험 산출물 | ✘ (.gitkeep 만) |

## 규칙

- **큰 파일은 git 에 넣지 않는다.** `data/ models/ outputs/` 는 gitignore. 팀 간 공유가 필요해지면 그때 방법(HF Hub 등)을 정한다.
- **시크릿은 `.env`** 에만. 키 이름을 추가하면 `.env.example` 도 갱신.
- **학습 스택은 TRL + PEFT.** SFT → GRPO 를 한 스택으로 잇기 위해서다.
- **교사 모델은 `claude` / `codex` CLI 로 호출한다.** API 키를 따로 관리하지 않는다.
- **샘플 포맷은 하나로 통일한다.** 합성·정제·학습·평가가 같은 스키마를 쓴다. 스키마는 `src/rca_lab/data` 에 두고, 바꿀 때는 PR 로.
- **새 실험은 코드 복사 대신 `configs/` 에 yaml 추가.**
- **학습 모니터링은 W&B.** 모든 run 은 팀 프로젝트 `rca-model-lab` 하나에 모은다 (SFT·GRPO 를 같은 곳에서 비교하기 위해). run 이름은 config 파일 이름과 같게. 키·엔티티는 `.env` 에서 읽고 코드에 박지 않는다.
- **train/eval split 은 시나리오 단위.** 같은 장애의 변형이 양쪽에 섞이면 평가가 무의미하다.
- **브랜치**: `main` 직접 푸시 금지. `feature/<topic>` 또는 개인 브랜치 → PR.
- **결정은 `docs/decisions.md` 에 한 줄씩.**
