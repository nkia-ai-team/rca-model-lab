"""Train-only Gemma FP8-aware distillation; never falls back to post-training quantization.

Teacher probabilities are cached per supervised turn before the student is loaded. This
keeps one full model on GPU1. Cache uses FP16 log probabilities (renormalized for KL);
student forward uses ModelOpt E4M3 channel weights / dynamic token activations.
"""

from __future__ import annotations

import argparse
import fnmatch
import gc
import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2))
    tmp.replace(path)


def state(cfg, phase, **extra):
    write(
        Path(cfg["runtime"]) / "state.json",
        {"phase": phase, "pid": os.getpid(), "utc": time.time(), **extra},
    )


def validate_train_rows(rows, cfg):
    allowed = set(cfg["train_case_ids"])
    forbidden = set(cfg["forbidden_case_ids"])
    if not allowed or allowed & forbidden:
        raise ValueError("invalid train / heldout case partition")
    actual = {row["case_id"] for row in rows}
    if actual != allowed or actual & forbidden:
        raise ValueError("QAD data must contain exactly the bound training cases, no heldout cases")
    for row in rows:
        match = re.match(r"^case-(f[0-9]+)(?:-|$)", row["case_id"], re.IGNORECASE)
        scenario = row.get("scenario_id") or (
            match.group(1).upper() if match else row["case_id"].split("-")[0]
        )
        if scenario in cfg.get("forbidden_scenario_ids", ["F40"]):
            raise ValueError("excluded training scenario in QAD data")


def serving_recipe(reference):
    quant = json.loads((Path(reference) / "config.json").read_text())["quantization_config"]
    groups = quant["config_groups"]
    if len(groups) != 1 or quant.get("kv_cache_scheme") is not None:
        raise ValueError("unsupported serving quantization groups / KV scheme")
    group = next(iter(groups.values()))
    for key, strategy, dynamic in [
        ("weights", "channel", False),
        ("input_activations", "token", True),
    ]:
        spec = group[key]
        if any(
            spec.get(k) != v
            for k, v in {
                "num_bits": 8,
                "type": "float",
                "strategy": strategy,
                "dynamic": dynamic,
                "symmetric": True,
            }.items()
        ):
            raise ValueError("serving recipe is not FP8_DYNAMIC W-channel/A-token")
    return {
        "name": "FP8_DYNAMIC",
        "weights": "E4M3_per_output_channel",
        "activations": "E4M3_dynamic_per_token",
        "kv_cache": None,
        "ignore": quant["ignore"],
        "reference_config_sha256": sha(Path(reference) / "config.json"),
        "reference_recipe_sha256": sha(Path(reference) / "recipe.yaml"),
    }


def quant_config(recipe):
    # Exact base exclusions, not ModelOpt's broader default model exclusions.
    entries = [{"quantizer_name": "*", "enable": False}]
    for pattern in ["*weight_quantizer", "*weight_quantizers.*"]:
        entries.append({"quantizer_name": pattern, "cfg": {"num_bits": (4, 3), "axis": 0}})
    entries.append(
        {
            "quantizer_name": "*input_quantizer",
            "cfg": {"num_bits": (4, 3), "axis": None, "type": "dynamic", "block_sizes": {-1: None}},
        }
    )
    entries.extend({"quantizer_name": name + ".*", "enable": False} for name in recipe["ignore"])
    return {"quant_cfg": entries, "algorithm": "max"}


def masked_kl(student_logits, teacher_log_probs, temperature=1.0):
    import torch

    if temperature != 1.0:
        raise ValueError("teacher cache is bound to temperature 1")
    target = teacher_log_probs.float()
    target = target - torch.logsumexp(target, dim=-1, keepdim=True)
    student = student_logits.float().log_softmax(-1)
    return (target.exp() * (target - student)).sum(-1)


def projected_logits(model, hidden):
    import torch
    from torch.nn import functional as F

    cfg = getattr(model.config, "text_config", model.config)
    logits = F.linear(
        hidden.float() * float(getattr(cfg, "output_multiplier", 1.0)), model.lm_head.weight.float()
    )
    cap = float(getattr(cfg, "final_logit_softcapping", 0.0) or 0.0)
    return torch.tanh(logits / cap) * cap if cap else logits


def selected_hidden(model, example, device):
    import torch

    ids = torch.tensor([example["input_ids"]], dtype=torch.long, device=device)
    labels = torch.tensor(example["labels"][1:], device=device)
    hidden = model.model(
        input_ids=ids, attention_mask=torch.ones_like(ids), use_cache=False
    ).last_hidden_state
    return hidden[0, :-1][labels != -100]


def quant_coverage(model, recipe, expected_experts=30):
    from modelopt.torch.quantization.nn import TensorQuantizer

    enabled = {
        name: mod
        for name, mod in model.named_modules()
        if isinstance(mod, TensorQuantizer) and mod.is_enabled
    }
    experts = [
        (name, mod)
        for name, mod in model.named_modules()
        if hasattr(mod, "gate_up_proj_weight_quantizers")
    ]
    if len(experts) != expected_experts:
        raise RuntimeError(f"quantized expert coverage {len(experts)} != {expected_experts}")
    for name, mod in experts:
        for attr in ["gate_up_proj_weight_quantizers", "down_proj_weight_quantizers"]:
            if len(getattr(mod, attr)) != mod.num_experts or not all(
                q.is_enabled for q in getattr(mod, attr)
            ):
                raise RuntimeError("missing enabled expert quantizers: " + name)
    for name, mod in model.named_modules():
        if (
            type(mod).__name__.endswith("Linear")
            and name not in recipe["ignore"]
            and hasattr(mod, "weight")
            and (not hasattr(mod, "weight_quantizer") or not mod.weight_quantizer.is_enabled)
        ):
            raise RuntimeError("unquantized serving Linear: " + name)
    if any(any(name.startswith(skip + ".") for skip in recipe["ignore"]) for name in enabled):
        raise RuntimeError("excluded module was quantized")
    return {
        "enabled_quantizers": len(enabled),
        "expert_layers": len(experts),
        "linear_module_names": sorted(
            name for name, mod in model.named_modules() if type(mod).__name__.endswith("Linear")
        ),
        "quantizer_names_sha256": hashlib.sha256("\n".join(sorted(enabled)).encode()).hexdigest(),
    }


def validate_export_recipe(quant, recipe, linear_module_names):
    """Resolve ModelOpt wildcard exclusions against actual converted Linear names."""
    if (
        quant.get("quant_method") != "modelopt"
        or quant.get("quant_algo") != "FP8_PER_CHANNEL_PER_TOKEN"
        or quant.get("kv_cache_quant_algo") is not None
    ):
        raise ValueError("export algorithm/KV differs from exact FP8_DYNAMIC serving recipe")
    universe = set(linear_module_names)
    if not universe:
        raise ValueError("export exclusion check requires actual Linear module names")
    expected = universe & set(recipe["ignore"])
    patterns = quant.get("ignore")
    if not isinstance(patterns, list):
        raise TypeError("ModelOpt export has no structured ignore list")
    actual = {
        name for name in universe if any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)
    }
    if actual != expected:
        raise ValueError(
            f"export exclusion drift: missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )
    return {"quant_algo": quant["quant_algo"], "resolved_exclusions": sorted(actual)}


def trainable_student(model):
    names = []
    for name, p in model.named_parameters():
        active = (
            ".language_model.layers." in name
            and (".self_attn." in name or ".mlp." in name)
            and name.endswith(".weight")
            and ".experts." not in name
        )
        p.requires_grad_(active)
        if active:
            names.append(name)
    if not names:
        raise RuntimeError("no QAD attention/shared-MLP trainable weights")
    return names


def gpu_admission():
    import subprocess

    if os.environ.get("CUDA_VISIBLE_DEVICES") != "1":
        raise RuntimeError("QAD must run with CUDA_VISIBLE_DEVICES=1; GPU0 prohibited")
    usage = subprocess.check_output(
        ["nvidia-smi", "-i", "1", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
        text=True,
    )
    if int(usage.strip()) >= 1024:
        raise RuntimeError("GPU1 is occupied; do not interrupt existing process")


def run(cfg):
    import modelopt.torch.opt as mto
    import modelopt.torch.quantization as mtq
    import torch
    from modelopt.torch.export import export_hf_checkpoint
    from transformers import AutoModelForImageTextToText

    from rca_lab.train.mcp_sft import (
        build_examples,
        load_config,
        load_rows,
        load_training_tokenizer,
    )
    from rca_lab.train.precision import build_fp32_master_adamw

    runtime = Path(cfg["runtime"])
    runtime.mkdir(parents=True, exist_ok=True)
    gpu_admission()
    probe_file = Path("/home/work/gemma-qad-20261014/tiny-probe/result.json")
    if not probe_file.exists() or json.loads(probe_file.read_text()).get("status") != "passed":
        raise RuntimeError("Gemma4 MoE ModelOpt quantization/backward/export preflight not passed")
    if sha(cfg["remote_dataset"]) != cfg["dataset_sha256"]:
        raise ValueError("QAD dataset SHA mismatch")
    train_cfg = load_config(Path(cfg["train_config"]))
    if train_cfg.dataset_sha256 != cfg["dataset_sha256"] or train_cfg.final_rca_loss_weight != 3.0:
        raise ValueError("QAD must preserve selected mechanism SFT dataset/loss contract")
    rows = load_rows(train_cfg)
    validate_train_rows(rows, cfg)
    examples, preprocessing = build_examples(
        train_cfg, load_training_tokenizer(train_cfg), rows, return_manifest=True
    )
    recipe = serving_recipe(cfg["base_fp8_recipe_reference"])
    write(runtime / "preprocess.json", preprocessing)
    cache = runtime / "teacher-cache"
    cache.mkdir(exist_ok=True)
    teacher_path = Path(cfg["teacher_model_dir"])
    teacher_identity = {p.name: sha(p) for p in sorted(teacher_path.glob("*.safetensors"))}
    if not teacher_identity:
        raise ValueError("selected merged BF16 teacher missing")
    teacher_identity["config.json"] = sha(teacher_path / "config.json")
    teacher_id = hashlib.sha256(json.dumps(teacher_identity, sort_keys=True).encode()).hexdigest()
    teacher_config = json.loads((teacher_path / "config.json").read_text())
    vocab_size = teacher_config.get("text_config", teacher_config)["vocab_size"]
    estimated = sum(sum(t != -100 for t in x["labels"][1:]) for x in examples) * vocab_size * 2
    cache_bytes = sum(p.stat().st_size for p in cache.glob("*.pt"))
    if shutil.disk_usage(cache).free + cache_bytes < estimated * 1.15:
        raise RuntimeError(
            f"insufficient disk for exact full-vocabulary teacher cache: {estimated} bytes"
        )
    state(cfg, "teacher_cache", examples=len(examples), estimated_bytes=estimated)
    loader = {
        "dtype": torch.bfloat16,
        "device_map": {"": "cuda:0"},
        "attn_implementation": "sdpa",
        "local_files_only": True,
    }
    teacher = (
        AutoModelForImageTextToText.from_pretrained(str(teacher_path), **loader)
        .eval()
        .requires_grad_(False)
    )
    teacher.config.use_cache = False
    for index, example in enumerate(examples):
        identity = hashlib.sha256(
            json.dumps({"teacher": teacher_id, "example": example}, sort_keys=True).encode()
        ).hexdigest()
        target = cache / f"{index:05d}.pt"
        marker = cache / f"{index:05d}.json"
        if marker.exists() and target.exists():
            meta = json.loads(marker.read_text())
            if meta["identity"] == identity and meta["sha256"] == sha(target):
                continue
        with torch.no_grad():
            hidden = selected_hidden(teacher, example, "cuda:0")
            chunks = [
                projected_logits(teacher, block).log_softmax(-1).half().cpu()
                for block in hidden.split(16)
            ]
            logp = torch.cat(chunks)
        torch.save(logp, target.with_suffix(".tmp"))
        target.with_suffix(".tmp").replace(target)
        write(
            marker,
            {
                "identity": identity,
                "sha256": sha(target),
                "shape": list(logp.shape),
                "dtype": "float16_log_probabilities",
            },
        )
        state(cfg, "teacher_cache", completed=index + 1, total=len(examples))
        del hidden, chunks, logp
    del teacher
    gc.collect()
    torch.cuda.empty_cache()
    student = AutoModelForImageTextToText.from_pretrained(str(teacher_path), **loader)
    student.config.use_cache = False
    names = trainable_student(student)
    # Dynamic activations need no calibration corpus; static weight scales observe all weights.
    student = mtq.quantize(student, quant_config(recipe))
    coverage = quant_coverage(student, recipe)
    student.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    student.train()
    optimizer = build_fp32_master_adamw(
        torch_module=torch, parameters=student.parameters(), lr=1e-6, weight_decay=0.0
    )
    order = torch.randperm(len(examples), generator=torch.Generator().manual_seed(42)).tolist()
    accumulation = 4
    optimizer.zero_grad()
    updates = 0
    for position, index in enumerate(order):
        example = examples[index]
        target = torch.load(cache / f"{index:05d}.pt", map_location="cpu", weights_only=True)
        hidden = selected_hidden(student, example, "cuda:0")
        weights = torch.tensor(
            [w for y, w in zip(example["labels"][1:], example["token_weight"][1:]) if y != -100],
            device="cuda:0",
        )
        # Correct partial final accumulation group; no dropped assistant turns.
        group_size = min(accumulation, len(order) - (position // accumulation) * accumulation)
        detached_loss = 0.0
        hidden_leaf = hidden.detach().requires_grad_(True)
        for start in range(0, len(hidden), 16):
            end = min(start + 16, len(hidden))
            per_token = masked_kl(
                projected_logits(student, hidden_leaf[start:end]), target[start:end].to("cuda:0")
            )
            loss = (
                (per_token * weights[start:end]).sum()
                / weights.sum()
                * example["sample_weight"]
                / group_size
            )
            if not torch.isfinite(loss):
                raise FloatingPointError("QAD KL is non-finite")
            loss.backward()
            detached_loss += float(loss.detach()) * group_size
        hidden.backward(hidden_leaf.grad)
        if (position + 1) % accumulation == 0 or position + 1 == len(order):
            grads = [p.grad for p in student.parameters() if p.requires_grad and p.grad is not None]
            if (
                not grads
                or not all(torch.isfinite(g).all() for g in grads)
                or not any(torch.count_nonzero(g).item() for g in grads)
            ):
                raise RuntimeError("QAD gradient missing/nonfinite/all-zero")
            torch.nn.utils.clip_grad_norm_(
                [p for p in student.parameters() if p.requires_grad], 1.0
            )
            optimizer.step()
            optimizer.zero_grad()
            updates += 1
        state(
            cfg,
            "distilling",
            completed=position + 1,
            total=len(order),
            optimizer_steps=updates,
            kl=detached_loss,
        )
        del hidden, hidden_leaf, target
    student.eval()
    # Preserve quantizer state before export; export uses trained quantizers, not a new oneshot recipe.
    with torch.no_grad():
        probe_ids = examples[0]["input_ids"][:128]
        probe_hidden = student.model(
            input_ids=torch.tensor([probe_ids], device="cuda:0"), use_cache=False
        ).last_hidden_state[0, -1:]
        probe_logp = projected_logits(student, probe_hidden).log_softmax(-1)[0]
        values, indices = probe_logp.topk(20)
        write(
            runtime / "export-probe.json",
            {
                "input_ids": probe_ids,
                "top_log_probs": {str(int(i)): float(v) for i, v in zip(indices, values)},
            },
        )
    state_path = runtime / "qad-modelopt.pth"
    mto.save(student, str(state_path))
    output = Path(cfg["output_dir"])
    state(cfg, "exporting", optimizer_steps=updates)
    export_hf_checkpoint(
        student, dtype=torch.bfloat16, export_dir=str(output), save_modelopt_state=True
    )
    for name in [
        "tokenizer.json",
        "tokenizer_config.json",
        "chat_template.jinja",
        "generation_config.json",
        "processor_config.json",
    ]:
        source = Path(cfg["base_fp8_recipe_reference"]) / name
        if source.exists():
            shutil.copy2(source, output / name)
    # A serving reload/logits gate is intentionally mandatory, never replaced by PTQ fallback.
    write(
        runtime / "trained.json",
        {
            "status": "trained_export_pending_verification",
            "model_dir": str(output),
            "step": cfg["selected_step"],
            "adapter_model_sha256": cfg["adapter_model_sha256"],
            "dataset_sha256": cfg["dataset_sha256"],
            "actual_quantization_recipe": recipe,
            "teacher_identity": {"weights": teacher_identity, "sha256": teacher_id},
            "student_identity": {
                "initial_teacher_sha256": teacher_id,
                "modelopt_state_sha256": sha(state_path),
            },
            "quant_coverage": coverage,
            "trainable_parameter_names": names,
            "experts_trainable": False,
            "objective": {
                "kl_temperature": 1.0,
                "teacher_cache": "FP16 full-vocabulary log-probabilities renormalized in FP32",
                "assistant_only": True,
                "final_rca_loss_weight": 3.0,
                "learning_rate": 1e-6,
                "epochs": 1,
                "optimizer_steps": updates,
            },
        },
    )
    state(cfg, "awaiting_export_verification")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    cfg = json.loads(Path(args.config).read_text())
    try:
        run(cfg)
    except Exception as exc:
        state(cfg, "blocked", error=f"{type(exc).__name__}: {exc}")
        raise


if __name__ == "__main__":
    main()
