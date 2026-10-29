# Flash RL 하네스 스냅샷

Qwen3.8-Flash-Next(w4a16) RCA GRPO LoRA 루프의 소스 사본입니다.
실행 중인 루프는 `outputs/` 아래 원본을 읽으므로, 이 디렉터리를 고쳐도 실행에는 반영되지 않습니다.
원본을 고친 뒤 아래 명령으로 다시 복사해 커밋합니다.

```bash
uv run python experiments/flash-rl/sync_from_outputs.py
```

| 경로 | 원본 (`outputs/` 기준) | 내용 |
|---|---|---|
| `loop/` | `flash-rl-loop-20261015/` | `loop.py`(rollout→채점→학습→export→평가 루프), `config.json`, 감시·W&B 동기화, 데이터 분할 v3~v5, `diag/` 진단·스윕·대조 스크립트와 판정 문서 |
| `rollout/` | `flash-next-rl-pilot-20261014/rollout/` | 학생 rollout(`run.py`), 사전 채점(`prejudge.py`), 보상 확정(`finalize.py`) |
| `runtime/general-update-fast18-20261026/` | `flash-runtime-reconstructed-20261027/…` | KT 학습 런타임. 루프 `config.json` 의 `data.runtime` 이 가리키는 버전 |
| `runtime/general-update-fast20-20261027/` | 같은 곳 | 최신 런타임(`--optimizer adamw\|muon\|hybrid`, 스윕용) |
| `runtime/lora-export-20261015/` | 같은 곳 | 학습 어댑터 → vLLM LoRA 변환 |
| `runtime/RECONSTRUCTION.md`, `runtime/_reconstruction_tools/` | 같은 곳 | 2026-10-27 KT 세션 유실 후 런타임 재구성 기록 |

- 2026-10-29 확인: KT `/home/work/flash-rl-quant-probe-20261014` 의 fast18·lora-export 파일 72개가 이 사본과 md5 일치.
- 복사하지 않는 것: `state.json`, `loop.log`, `u*/`(rollout·학습 산출물), `eval/`, W&B 로컬 파일, 백업(`*.bak*`, `*.pre-*`, `*original*`), 300KB 초과 파일.
- 점수 비교는 같은 epoch(프롬프트 rev·도구 태그·채점 rubric·컨텍스트 정책) 안에서만 유효합니다(`loop.py` 의 `epoch_key`).
