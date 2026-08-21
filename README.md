# rca-model-lab

RCA(근본원인분석) 특화 모델을 만드는 실험실.
`rca-scenario-runner` 의 장애 시나리오(ground truth 포함)를 재료로 프런티어 모델(Claude / Codex CLI)에게
SFT 데이터를 합성시키고, 그걸로 오픈 모델을 LoRA 파인튜닝한 뒤, 이후 GRPO 로 확장한다.

## 시작하기

```bash
git clone git@github.com:nkia-ai-team/rca-model-lab.git && cd rca-model-lab
make setup
```

필요한 것: `uv`, 로그인된 `claude` / `codex` CLI, 옆 디렉토리에 `rca-scenario-runner` 체크아웃.
학습은 클라우드 GPU 에서 `make sync-train` 후 `make sft`. 나머지 타겟은 `make help`.

## 디렉토리

| 경로 | 역할 | git |
|---|---|---|
| `src/rca_lab/scenarios` | service-spec.yaml → `Scenario` (ground truth) | ✔ |
| `src/rca_lab/synth` | 교사 CLI 래퍼(`teachers.py`) + 합성 루프(`generate.py`) | ✔ |
| `src/rca_lab/data` | 공통 스키마 `RcaSample`, JSONL IO, synth→processed 빌드 | ✔ |
| `src/rca_lab/train` | `sft.py` (TRL), `grpo.py` (자리) | ✔ |
| `src/rca_lab/eval` | 채점기 `reward.py` (SFT 평가·RL reward 공용, 자리) | ✔ |
| `prompts/` | 교사용/학생용 프롬프트 템플릿 | ✔ |
| `configs/{synth,sft,grpo}` | 실험 설정 yaml. 새 실험 = 새 yaml | ✔ |
| `data/{raw,synth,processed}`, `models/`, `outputs/` | 데이터·가중치·실험 산출물 (로컬) | ✘ (.gitkeep 만) |

## 규칙

- **큰 파일은 git 에 넣지 않는다.** `data/ models/ outputs/` 는 gitignore. 팀 간 데이터셋/가중치 공유가 필요해지면 HF Hub private repo 를 붙인다.
- **시크릿은 `.env`** 에만. `.env.example` 을 갱신해 키 이름만 공유.
- **샘플 포맷은 `RcaSample` 하나.** 합성·정제·학습·평가 전부 이 스키마로 통한다. 필드 추가는 `data/schema.py` 에서.
- **실험은 yaml 로 구분.** 코드를 복사하지 말고 `configs/` 에 yaml 을 추가한다. 산출물 경로는 yaml 의 `output_dir`.
- train/eval split 은 **시나리오 단위**. 같은 장애의 변형이 양쪽에 섞이면 평가가 무의미하다.
- 브랜치: `main` 보호, 작업은 `feature/<topic>` 또는 개인 브랜치 → PR.
