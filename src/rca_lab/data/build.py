"""data/synth/*.jsonl 들을 합쳐 학습용 processed 셋을 만든다 (중복 제거 + 시나리오 단위 split)."""

from __future__ import annotations

import random
from pathlib import Path

from rca_lab.settings import PROCESSED_DIR, SYNTH_DIR

from .io import read_jsonl, write_jsonl


def build(name: str = "sft_v0", eval_ratio: float = 0.2, seed: int = 42) -> tuple[Path, Path]:
    samples = {s.id: s for p in sorted(SYNTH_DIR.glob("*.jsonl")) for s in read_jsonl(p)}
    keys = sorted({s.scenario_key for s in samples.values()})
    rng = random.Random(seed)
    rng.shuffle(keys)
    n_eval = max(1, int(len(keys) * eval_ratio)) if len(keys) > 1 else 0
    eval_keys = set(
        keys[:n_eval]
    )  # 시나리오 단위로 나눠 같은 장애가 train/eval 에 동시에 들어가지 않게 한다

    train = [s for s in samples.values() if s.scenario_key not in eval_keys]
    evals = [s for s in samples.values() if s.scenario_key in eval_keys]
    tp, ep = PROCESSED_DIR / f"{name}.jsonl", PROCESSED_DIR / f"{name}.eval.jsonl"
    print(f"train {write_jsonl(tp, train)} → {tp}")
    print(f"eval  {write_jsonl(ep, evals)} → {ep}  (scenarios: {sorted(eval_keys)})")
    return tp, ep
