# rca-model-lab

RCA(근본원인분석) 특화 모델을 만드는 실험실.
`rca-scenario-runner` 의 장애 시나리오(ground truth 포함)를 재료로 프런티어 모델(Claude / Codex CLI)에게
SFT 데이터를 합성시키고, 그걸로 오픈 모델을 파인튜닝한 뒤, 이후 강화학습으로 확장한다.

현재 모델의 개념, 학습 파이프라인, 예시, 진행 상황은
[RCA 모델 개발 현황과 학습 파이프라인](docs/rca-model-development-overview.md)에 정리되어 있다.
브라우저에서 바로 볼 수 있는 [HTML 설명서](docs/rca-model-development-overview.html)에는
Student에게 제공한 27개 도구의 입력·출력·사용 예시도 포함되어 있다.

## 시작하기

```bash
git clone git@github.com:nkia-ai-team/rca-model-lab.git && cd rca-model-lab
make setup
```

필요한 것: `uv`, 로그인된 `claude` / `codex` CLI, 옆 디렉토리에 `rca-scenario-runner` 체크아웃,
W&B 팀 `nkia-ai` 초대 수락 후 `.env` 에 자기 `WANDB_API_KEY` — [docs/wandb-setup.md](docs/wandb-setup.md).
HF org `nkia-ai-lab` 초대 수락 후 `.env` 에 자기 `HF_TOKEN` (write, https://huggingface.co/settings/tokens).
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
| `src/rca_lab/harness` | student 실행 계약 — 증거 환경, capability registry, typed ledger/proof/scorer | ✔ |
| `prompts/` | 교사용/학생용 프롬프트 | ✔ |
| `configs/` | 실험 설정 yaml | ✔ |
| `data/{raw,synth,processed}`, `models/`, `outputs/` | 데이터·가중치·실험 산출물 | ✘ (.gitkeep 만) |

## 규칙

- **큰 파일은 git 에 넣지 않는다.** `data/ models/ outputs/` 는 gitignore.
- **데이터셋·가중치 공유는 HF Hub private, org `nkia-ai-lab`.** 데이터셋은 `nkia-ai-lab/rca-sft`, `rca-eval`, 모델은 `rca-sft-<base>-<ver>`.
  버전은 태그로 찍고 학습 코드는 `revision=` 으로 읽는다. git LFS 는 쓰지 않는다. 토큰은 `.env` 의 `HF_TOKEN`.
- **시크릿은 `.env`** 에만. 키 이름을 추가하면 `.env.example` 도 갱신.
- **학습 스택은 TRL + PEFT.** SFT → GRPO 를 한 스택으로 잇기 위해서다.
- **교사 모델은 `claude` / `codex` CLI 로 호출한다.** API 키를 따로 관리하지 않는다.
- **샘플 포맷은 하나로 통일한다.** 합성·정제·학습·평가가 같은 스키마를 쓴다. 스키마는 `src/rca_lab/data` 에 두고, 바꿀 때는 PR 로.
- **새 실험은 코드 복사 대신 `configs/` 에 yaml 추가.**
- **학습 모니터링은 W&B.** 모든 run 은 팀 프로젝트 `rca-model-lab` 하나에 모은다 (SFT·GRPO 를 같은 곳에서 비교하기 위해). run 이름은 config 파일 이름과 같게. 키·엔티티는 `.env` 에서 읽고 코드에 박지 않는다.
- **train/eval split 은 시나리오 단위.** 같은 장애의 변형이 양쪽에 섞이면 평가가 무의미하다.
- **브랜치**: `main` 하나, 수명 짧은 작업 브랜치(trunk-based). `develop`/`release` 없음.
  `feature/<이슈번호>-<topic>` → PR → CI 통과 → squash 머지 → 브랜치 삭제. 며칠 넘게 살아 있으면 쪼갠다.
  `main` 직접 푸시 금지 (무료 플랜 private 레포라 branch protection 을 못 건다 — 약속으로 지킨다).
- **실험은 브랜치가 아니다.** 모델·하이퍼파라미터를 바꾸는 건 `configs/` yaml 추가 + W&B run. 브랜치는 코드를 바꿀 때만.
- **커밋 메시지**: 팀 컨벤션(Linear 이슈 번호 + type) 을 따른다.
- **결정은 `docs/decisions.md` 에 한 줄씩.**

## Student harness

`src/rca_lab/harness` 가 ThinkFL student의 안전 경계다. 모델의 추론 문장은 제한하지 않되,
실행 가능한 action/metric/target, 관측, 증거 참조, 최종 답, reward는 Pydantic 계약으로 검증한다.

- 원본 증거는 `EvidenceEnvironment`에 전부 보존한다. `prompt_view()`와 `query.limit`만 제한한다.
- action은 정적 enum과 episode별 `CapabilityRegistry`를 모두 통과해야 한다.
- probe 결과는 `Observation` typed ledger에 원문 payload와 구조화 fact로 영속화한다.
- `confirmed`는 `ProofType`별 결정적 규칙을 만족해야 한다.
- 평가와 RL reward는 `TypedScorer` 하나를 공유한다.

현재 코드는 `lucida-next` Go 하네스의 핵심 계약을 Python 네이티브로 이식한 1단계다.
scenario adapter, Codex teacher runner, episode 실행 루프는 이 경계 위에 순서대로 연결한다.
