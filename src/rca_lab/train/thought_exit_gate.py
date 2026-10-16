"""Thought-exit regression gate run inside the trainer at every checkpoint save.

For a fixed probe subset (base natural exits N, training exits T; prompt + thought prefix token IDs
built from the frozen eval tokenizer), measure
  P(close) = p(<channel|> | x) + p("\\n" | x) * p(<channel|> | x + "\\n")
with one no-grad forward over x + "\\n" (last two positions only). Step 0 is the untouched base
(LoRA B = 0). A healthy native-format run keeps the N-set median near the base value.
"""
from __future__ import annotations

import json
import math
import os
import statistics
import time
from pathlib import Path
from typing import Any

CLOSE, NEWLINE = 101, 107


def load_probes(path: Path, per_set: int) -> list[dict[str, Any]]:
    probes = [json.loads(line) for line in path.open(encoding="utf-8")]
    chosen: list[dict[str, Any]] = []
    for name in ("N", "T"):
        subset = sorted((p for p in probes if p["set"] == name), key=lambda p: p["id"])
        chosen.extend(subset[:per_set])
    return chosen


def measure(model: Any, probes: list[dict[str, Any]], torch_module: Any) -> dict[str, Any]:
    torch = torch_module
    was_training = model.training
    model.eval()
    device = next(p for p in model.parameters()).device
    rows = []
    started = time.time()
    with torch.no_grad():
        for probe in probes:
            ids = torch.tensor([probe["ids"] + [NEWLINE]], device=device)
            out = model(input_ids=ids, use_cache=False, logits_to_keep=2)
            logp = torch.log_softmax(out.logits[0, -2:].float(), dim=-1)
            direct, newline, after = (math.exp(float(logp[0, CLOSE])), math.exp(float(logp[0, NEWLINE])), math.exp(float(logp[1, CLOSE])))
            rows.append({"id": probe["id"], "set": probe["set"], "p_close": direct + newline * after, "p_direct": direct, "p_newline": newline})
    if was_training:
        model.train()
    summary: dict[str, Any] = {"seconds": round(time.time() - started, 1)}
    for name in ("N", "T"):
        subset = [row for row in rows if row["set"] == name]
        if subset:
            summary[name] = {key: round(statistics.median(row[key] for row in subset), 4) for key in ("p_close", "p_direct", "p_newline")}
    return {"summary": summary, "rows": rows}


def make_callback(probe_path: Path, out_path: Path, per_set: int, torch_module: Any) -> Any:
    from transformers import TrainerCallback

    probes = load_probes(probe_path, per_set)

    class ThoughtExitGate(TrainerCallback):
        def _run(self, step: int, model: Any) -> None:
            result = measure(model, probes, torch_module)
            with out_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"step": step, **result}) + "\n")
            print(f"[thought_exit_gate] step={step} {json.dumps(result['summary'])}", flush=True)

        def on_train_begin(self, args, state, control, model=None, **kwargs):
            if model is not None and state.global_step == 0 and os.environ.get("THOUGHT_EXIT_SKIP0") != "1":
                self._run(0, model)

        def on_save(self, args, state, control, model=None, **kwargs):
            if model is not None:
                self._run(state.global_step, model)

    return ThoughtExitGate()
