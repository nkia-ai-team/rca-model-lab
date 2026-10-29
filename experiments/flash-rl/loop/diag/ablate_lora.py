"""Expert-only / dense-only copies of an exported vLLM LoRA (2026-10-24 diagnosis): zero lora_B of the other family.

  python ablate_lora.py SRC_LORA_DIR OUT_ROOT   ->  OUT_ROOT/experts (routed experts kept), OUT_ROOT/dense (attention + shared expert kept)
"""
import json
import shutil
import sys
from pathlib import Path

import torch
from safetensors.torch import load_file, save_file

src, out_root = Path(sys.argv[1]), Path(sys.argv[2])
weights = load_file(str(src / "adapter_model.safetensors"))
for keep in ("experts", "dense"):
    out = out_root / keep
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src / "adapter_config.json", out / "adapter_config.json")
    ablated, zeroed = {}, 0
    for k, v in weights.items():
        is_expert = ".mlp.experts." in k
        drop = k.endswith("lora_B.weight") and (is_expert if keep == "dense" else not is_expert)
        ablated[k] = torch.zeros_like(v) if drop else v
        zeroed += drop
    save_file(ablated, str(out / "adapter_model.safetensors"), metadata={"format": "pt", "source": str(src), "kept": keep})
    (out / "ablation.json").write_text(json.dumps({"source": str(src), "kept": keep, "zeroed_lora_B": zeroed}))
    print(keep, "zeroed", zeroed, "of", sum(k.endswith("lora_B.weight") for k in weights))
