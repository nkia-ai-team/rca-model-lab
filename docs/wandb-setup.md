# W&B 세팅

학습 run 은 전부 팀 `nkia-ai` 의 프로젝트 `rca-model-lab` 에 모은다: https://wandb.ai/nkia-ai/rca-model-lab

## 1. 계정 · 팀 합류
1. https://wandb.ai 가입 (GitHub 로그인 가능)
2. 팀 `nkia-ai` 초대 메일 수락. 초대가 없으면 팀 관리자에게 요청.

## 2. API 키
https://wandb.ai/authorize 에서 복사. **개인 키**다 — 본인 계정을 인증하는 것이고, 팀 소속이면 이 키로 팀 프로젝트에 기록된다. 공유하지 않는다.

## 3. `.env`
```
WANDB_API_KEY=<자기 키>
WANDB_ENTITY=nkia-ai         # .env.example 기본값
WANDB_PROJECT=rca-model-lab  # .env.example 기본값
```

## 4. 동작 확인 (학습 스택 없이)
```bash
set -a; source .env; set +a
uv run --with wandb python -c "
import wandb
r = wandb.init(project='rca-model-lab', name='smoke-test')
for i in range(5): wandb.log({'loss': 1/(i+1)})
r.finish()
"
```
`View run at https://wandb.ai/nkia-ai/rca-model-lab/runs/...` 링크가 뜨고 loss 곡선이 보이면 끝. 테스트 run 은 대시보드에서 지워도 된다.

## 규칙
- run 이름 = config 파일 이름 (`configs/sft/qwen3-8b-v0.yaml` → `qwen3-8b-v0`). 대시보드에서 yaml 로 바로 찾기 위해.
- 키·엔티티·프로젝트는 `.env` 의 `WANDB_*` 로만. 코드에 박지 않는다.
- 무인 자동화(CI, 스케줄 잡)가 생기면 개인 키 대신 팀 서비스 계정 키를 쓴다. 지금은 불필요.
