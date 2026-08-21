"""TRL SFTTrainer + PEFT LoRA. 실행: accelerate launch -m rca_lab.train.sft --config configs/sft/default.yaml"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml
from datasets import load_dataset
from peft import LoraConfig
from transformers import AutoTokenizer
from trl import SFTConfig, SFTTrainer

from rca_lab.settings import REPO_ROOT


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, required=True)
    cfg = yaml.safe_load(ap.parse_args().config.read_text())

    ds = load_dataset("json", data_files=str(REPO_ROOT / cfg["dataset"]), split="train")
    ds = ds.select_columns(["messages"])  # RcaSample → TRL conversational 포맷

    tok = AutoTokenizer.from_pretrained(cfg["model_name"])
    lora = cfg["lora"]
    peft_config = LoraConfig(
        r=lora["r"],
        lora_alpha=lora["alpha"],
        lora_dropout=lora["dropout"],
        target_modules=lora["target_modules"],
        task_type="CAUSAL_LM",
    )
    args = SFTConfig(
        output_dir=str(REPO_ROOT / cfg["output_dir"]),
        max_length=cfg.get("max_length", 4096),
        **cfg["train"],
    )
    trainer = SFTTrainer(
        model=cfg["model_name"],
        args=args,
        train_dataset=ds,
        processing_class=tok,
        peft_config=peft_config,
    )
    trainer.train()
    trainer.save_model()


if __name__ == "__main__":
    main()
