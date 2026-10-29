#!/usr/bin/env python3
"""Export a Flash GRPO LoRA checkpoint (adapter.pt from flash_train.py) into a
PEFT-style LoRA directory that vLLM (0.32, Qwen4Exp) loads and applies to every
adapted module, routed experts included.

    python export_vllm_lora.py ADAPTER_PT OUT_DIR [--dtype float32|bfloat16]
                               [--scale 1.0] [--model-config CONFIG_JSON]
                               [--verify-vllm] [--force]

Training semantics (flash_train.py DenseLoRA / PackedLoRA):
    y = base(x) + (x @ A^T) @ B^T * scale        (scale = 1.0, no alpha/r)
PEFT/vLLM semantics:
    y = base(x) + (x @ A^T) @ B^T * (lora_alpha / r)
so the exporter writes, per module family, r = rank(A) and lora_alpha = scale * r
(top-level r/lora_alpha for the dense rank, rank_pattern/alpha_pattern for any
family whose rank differs). vLLM's PEFTHelper.get_rank_and_scaling honours both.

Name mapping (training -> PEFT key, prefix "base_model.model."):
    backbone.layers.{i}.<path>.lora_A                    -> model.language_model.layers.{i}.<path>.lora_A.weight
    backbone.layers.{i}.mlp.experts.experts.{e}.<proj>.* -> model.language_model.layers.{i}.mlp.experts.{e}.<proj>.lora_*.weight
vLLM then renames "model.language_model." -> "language_model.model." (Qwen3VL
hf_to_vllm_mapper, rename-only) and packs:
    self_attn.{q,k,v}_proj          -> self_attn.qkv_proj      (3 slices; indexer kept separate when LoRA is on)
    self_attn.o_proj                -> self_attn.o_proj
    linear_attn.in_proj_qkv         -> linear_attn.in_proj_qkvz slices q,k,v (A replicated, B split by rows)
    linear_attn.in_proj_z           -> linear_attn.in_proj_qkvz slice z
    linear_attn.in_proj_{b,a}       -> linear_attn.in_proj_ba  slices b,a
    linear_attn.out_proj            -> linear_attn.out_proj
    mlp.shared_expert.{gate,up}_proj-> mlp.shared_expert.gate_up_proj
    mlp.shared_expert.down_proj     -> mlp.shared_expert.down_proj
    mlp.experts.{e}.{gate,down,up}_proj -> mlp.experts (FusedMoEWithLoRA, w1/w2/w3 per expert)
Anything else (router, indexer, embeddings, lm_head, PLE, hyper-connections,
...) is refused: the exporter exits non-zero instead of dropping it.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import sys
import time
from pathlib import Path

import torch
from safetensors.torch import load_file, save_file

DEFAULT_MODEL = Path("/home/work/models/qwen-flash-next-w4a16-autoround-4c67bf68")
SRC_PREFIX = "backbone."
PEFT_PREFIX = "base_model.model."
HF_PREFIX = "model.language_model."

# family -> (regex on the training name after "backbone.", allowed layer type)
FAMILIES: dict[str, tuple[str, str | None]] = {
    "self_attn.q_proj": (r"layers\.(\d+)\.self_attn\.q_proj", "full_attention"),
    "self_attn.k_proj": (r"layers\.(\d+)\.self_attn\.k_proj", "full_attention"),
    "self_attn.v_proj": (r"layers\.(\d+)\.self_attn\.v_proj", "full_attention"),
    "self_attn.o_proj": (r"layers\.(\d+)\.self_attn\.o_proj", "full_attention"),
    "linear_attn.in_proj_qkv": (r"layers\.(\d+)\.linear_attn\.in_proj_qkv", "linear_attention"),
    "linear_attn.in_proj_z": (r"layers\.(\d+)\.linear_attn\.in_proj_z", "linear_attention"),
    "linear_attn.in_proj_b": (r"layers\.(\d+)\.linear_attn\.in_proj_b", "linear_attention"),
    "linear_attn.in_proj_a": (r"layers\.(\d+)\.linear_attn\.in_proj_a", "linear_attention"),
    "linear_attn.out_proj": (r"layers\.(\d+)\.linear_attn\.out_proj", "linear_attention"),
    "shared_expert.gate_proj": (r"layers\.(\d+)\.mlp\.shared_expert\.gate_proj", None),
    "shared_expert.up_proj": (r"layers\.(\d+)\.mlp\.shared_expert\.up_proj", None),
    "shared_expert.down_proj": (r"layers\.(\d+)\.mlp\.shared_expert\.down_proj", None),
    "experts.gate_proj": (r"layers\.(\d+)\.mlp\.experts\.experts\.(\d+)\.gate_proj", None),
    "experts.up_proj": (r"layers\.(\d+)\.mlp\.experts\.experts\.(\d+)\.up_proj", None),
    "experts.down_proj": (r"layers\.(\d+)\.mlp\.experts\.experts\.(\d+)\.down_proj", None),
}
# vLLM runtime module each family lands in (for the manifest / report).
VLLM_TARGET = {
    "self_attn.q_proj": "self_attn.qkv_proj[q]",
    "self_attn.k_proj": "self_attn.qkv_proj[k]",
    "self_attn.v_proj": "self_attn.qkv_proj[v]",
    "self_attn.o_proj": "self_attn.o_proj",
    "linear_attn.in_proj_qkv": "linear_attn.in_proj_qkvz[q,k,v]",
    "linear_attn.in_proj_z": "linear_attn.in_proj_qkvz[z]",
    "linear_attn.in_proj_b": "linear_attn.in_proj_ba[b]",
    "linear_attn.in_proj_a": "linear_attn.in_proj_ba[a]",
    "linear_attn.out_proj": "linear_attn.out_proj",
    "shared_expert.gate_proj": "mlp.shared_expert.gate_up_proj[gate]",
    "shared_expert.up_proj": "mlp.shared_expert.gate_up_proj[up]",
    "shared_expert.down_proj": "mlp.shared_expert.down_proj",
    "experts.gate_proj": "mlp.experts (FusedMoEWithLoRA w1)",
    "experts.up_proj": "mlp.experts (FusedMoEWithLoRA w3)",
    "experts.down_proj": "mlp.experts (FusedMoEWithLoRA w2)",
}
EXPERT_RANK_PATTERN_KEY = r"mlp\.experts\.\d+\.(gate_proj|up_proj|down_proj)"


def sha256_file(path: Path, chunk: int = 1 << 24) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()


def expected_dims(tc: dict) -> dict[str, tuple[int, int]]:
    """(in_features, out_features) of each adapted HF projection."""
    h = tc["hidden_size"]
    hd = tc["head_dim"]
    nq = tc["num_attention_heads"]
    nkv = tc["num_key_value_heads"]
    kd = tc["linear_key_head_dim"] * tc["linear_num_key_heads"]
    vd = tc["linear_value_head_dim"] * tc["linear_num_value_heads"]
    nv = tc["linear_num_value_heads"]
    se = tc["shared_expert_intermediate_size"]
    mi = tc["moe_intermediate_size"]
    return {
        # Qwen4Exp full attention packs a sigmoid output gate next to Q.
        "self_attn.q_proj": (h, nq * hd * 2),
        "self_attn.k_proj": (h, nkv * hd),
        "self_attn.v_proj": (h, nkv * hd),
        "self_attn.o_proj": (nq * hd, h),
        "linear_attn.in_proj_qkv": (h, 2 * kd + vd),
        "linear_attn.in_proj_z": (h, vd),
        "linear_attn.in_proj_b": (h, nv),
        "linear_attn.in_proj_a": (h, nv),
        "linear_attn.out_proj": (vd, h),
        "shared_expert.gate_proj": (h, se),
        "shared_expert.up_proj": (h, se),
        "shared_expert.down_proj": (se, h),
        "experts.gate_proj": (h, mi),
        "experts.up_proj": (h, mi),
        "experts.down_proj": (mi, h),
    }


def classify(module: str) -> tuple[str, int, int | None] | None:
    for fam, (rx, _) in FAMILIES.items():
        m = re.fullmatch(rx, module)
        if m:
            layer = int(m.group(1))
            expert = int(m.group(2)) if m.lastindex and m.lastindex >= 2 else None
            return fam, layer, expert
    return None


def peft_module_name(fam: str, layer: int, expert: int | None, module: str) -> str:
    if expert is not None:
        proj = fam.split(".")[1]
        return f"{HF_PREFIX}layers.{layer}.mlp.experts.{expert}.{proj}"
    return HF_PREFIX + module


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("adapter", type=Path, help="adapter.pt written by flash_train.py")
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--dtype", choices=["float32", "bfloat16"], default="float32",
                    help="tensor dtype in adapter_model.safetensors (vLLM casts to its lora_dtype at load)")
    ap.add_argument("--scale", type=float, default=1.0,
                    help="training LoRA scale (DenseLoRA/PackedLoRA .scale); lora_alpha = scale * r")
    ap.add_argument("--model-config", type=Path, default=DEFAULT_MODEL / "config.json")
    ap.add_argument("--base-model", default=str(DEFAULT_MODEL))
    ap.add_argument("--verify-vllm", action="store_true",
                    help="re-parse the written dir with vLLM's PEFTHelper / LoRA name parser")
    ap.add_argument("--force", action="store_true", help="overwrite a non-empty out_dir")
    a = ap.parse_args()

    if a.out_dir.exists() and any(a.out_dir.iterdir()) and not a.force:
        print(f"ERROR: {a.out_dir} exists and is not empty (use --force)", file=sys.stderr)
        return 2

    raw_cfg = json.loads(a.model_config.read_text())
    tc = raw_cfg.get("text_config", raw_cfg)
    layer_types = tc["layer_types"]
    n_layers = tc["num_hidden_layers"]
    n_experts = tc["num_experts"]
    dims = expected_dims(tc)

    t0 = time.time()
    ckpt = torch.load(a.adapter, weights_only=True, map_location="cpu", mmap=True)
    sd = ckpt["state_dict"] if isinstance(ckpt, dict) and "state_dict" in ckpt else ckpt
    meta = {k: v for k, v in ckpt.items() if k != "state_dict"} if isinstance(ckpt, dict) else {}

    # ---- group A/B pairs by module ------------------------------------------------
    pairs: dict[str, dict[str, torch.Tensor]] = collections.defaultdict(dict)
    refused: list[str] = []
    for name, t in sd.items():
        if not name.startswith(SRC_PREFIX) or not (name.endswith(".lora_A") or name.endswith(".lora_B")):
            refused.append(name)
            continue
        module, ab = name[len(SRC_PREFIX):].rsplit(".", 1)
        pairs[module][ab] = t

    errors: list[str] = []
    per_family = collections.Counter()
    fam_ranks: dict[str, set[int]] = collections.defaultdict(set)
    expert_sets: dict[tuple[int, str], set[int]] = collections.defaultdict(set)
    out: dict[str, torch.Tensor] = {}
    dtype = getattr(torch, a.dtype)
    nonzero_b = collections.Counter()

    for module, ab in sorted(pairs.items()):
        c = classify(module)
        if c is None:
            refused.append(module)
            continue
        fam, layer, expert = c
        if set(ab) != {"lora_A", "lora_B"}:
            errors.append(f"{module}: missing {({'lora_A', 'lora_B'} - set(ab))}")
            continue
        A, B = ab["lora_A"], ab["lora_B"]
        if not (0 <= layer < n_layers):
            errors.append(f"{module}: layer {layer} out of range")
            continue
        want_type = FAMILIES[fam][1]
        if want_type is not None and layer_types[layer] != want_type:
            errors.append(f"{module}: family {fam} on a {layer_types[layer]} layer")
            continue
        if expert is not None and not (0 <= expert < n_experts):
            errors.append(f"{module}: expert {expert} out of range")
            continue
        din, dout = dims[fam]
        r = A.shape[0]
        if A.dim() != 2 or B.dim() != 2 or A.shape != (r, din) or B.shape != (dout, r):
            errors.append(f"{module}: A{tuple(A.shape)} B{tuple(B.shape)} != A({r},{din}) B({dout},{r})")
            continue
        if not (torch.isfinite(A).all() and torch.isfinite(B).all()):
            errors.append(f"{module}: non-finite values")
            continue
        fam_ranks[fam].add(r)
        per_family[fam] += 1
        if expert is not None:
            expert_sets[(layer, fam)].add(expert)
        if bool((B != 0).any()):
            nonzero_b[fam] += 1
        key = PEFT_PREFIX + peft_module_name(fam, layer, expert, module)
        out[key + ".lora_A.weight"] = A.to(dtype).contiguous()
        out[key + ".lora_B.weight"] = B.to(dtype).contiguous()

    if refused:
        errors.append("UNREPRESENTABLE / unknown adapter entries (refusing to drop them silently): "
                      + ", ".join(sorted(set(refused))[:50]) + (" ..." if len(set(refused)) > 50 else ""))

    # ---- structural checks vLLM relies on -----------------------------------------
    for fam in FAMILIES:
        if len(fam_ranks[fam]) > 1:
            errors.append(f"{fam}: mixed ranks {sorted(fam_ranks[fam])} (one rank per family supported)")
    # FusedMoE pack_moe asserts every expert of a layer has w1/w2/w3.
    expert_layers = sorted({l for (l, _f) in expert_sets})
    for layer in expert_layers:
        for fam in ("experts.gate_proj", "experts.up_proj", "experts.down_proj"):
            got = expert_sets.get((layer, fam), set())
            if len(got) != n_experts:
                errors.append(f"layer {layer} {fam}: {len(got)}/{n_experts} experts adapted "
                              "(vLLM FusedMoE LoRA needs all experts of an adapted layer)")
    # GDN in_proj_qkvz: vLLM expands [in_proj_qkv, in_proj_z]; a z-only adapter cannot be split.
    for layer, lt in enumerate(layer_types):
        if lt != "linear_attention":
            continue
        has_qkv = f"layers.{layer}.linear_attn.in_proj_qkv" in pairs
        has_z = f"layers.{layer}.linear_attn.in_proj_z" in pairs
        if has_z and not has_qkv:
            errors.append(f"layer {layer}: in_proj_z adapted without in_proj_qkv (vLLM expand_packed_lora cannot place it)")

    if errors:
        print("EXPORT FAILED:", file=sys.stderr)
        for e in errors:
            print("  -", e, file=sys.stderr)
        return 1

    ranks = {fam: next(iter(v)) for fam, v in fam_ranks.items()}
    r_top = max(ranks.values())
    rank_pattern: dict[str, int] = {}
    alpha_pattern: dict[str, float] = {}
    for fam, r in sorted(ranks.items()):
        if r == r_top:
            continue
        if fam.startswith("experts."):
            key = EXPERT_RANK_PATTERN_KEY
        else:
            sub = {"self_attn": "self_attn", "linear_attn": "linear_attn", "shared_expert": "mlp\\.shared_expert"}[fam.split(".")[0]]
            key = sub + r"\." + fam.split(".")[1]
        if key in rank_pattern and rank_pattern[key] != r:
            print(f"EXPORT FAILED: conflicting ranks for pattern {key}", file=sys.stderr)
            return 1
        rank_pattern[key] = r
        alpha_pattern[key] = a.scale * r
    target_modules = sorted({fam.split(".")[1] for fam in ranks})

    config = {
        "peft_type": "LORA",
        "task_type": "CAUSAL_LM",
        "base_model_name_or_path": a.base_model,
        "r": r_top,
        "lora_alpha": a.scale * r_top,
        "rank_pattern": rank_pattern,
        "alpha_pattern": alpha_pattern,
        "target_modules": target_modules,
        "lora_dropout": 0.0,
        "bias": "none",
        "fan_in_fan_out": False,
        "use_rslora": False,
        "use_dora": False,
        "init_lora_weights": True,
        "modules_to_save": None,
        "inference_mode": True,
    }

    a.out_dir.mkdir(parents=True, exist_ok=True)
    st_path = a.out_dir / "adapter_model.safetensors"
    save_file(out, str(st_path), metadata={"format": "pt", "source": str(a.adapter)})
    (a.out_dir / "adapter_config.json").write_text(json.dumps(config, indent=2) + "\n")

    # ---- round-trip check: bit-exact vs source (in the chosen dtype) -------------
    back = load_file(str(st_path))
    assert set(back) == set(out), "key set changed on round trip"
    for module, ab in pairs.items():
        fam, layer, expert = classify(module)
        key = PEFT_PREFIX + peft_module_name(fam, layer, expert, module)
        for src_ab, dst in (("lora_A", ".lora_A.weight"), ("lora_B", ".lora_B.weight")):
            if not torch.equal(back[key + dst], ab[src_ab].to(dtype)):
                print(f"EXPORT FAILED: round-trip mismatch {key}{dst}", file=sys.stderr)
                return 1

    # ---- counts ------------------------------------------------------------------
    rows = []
    print(f"{'family':28s} {'modules':>8s} {'rank':>5s} {'alpha':>6s} {'B!=0':>6s}  -> vLLM module")
    for fam in FAMILIES:
        if per_family[fam]:
            r = ranks[fam]
            print(f"{fam:28s} {per_family[fam]:8d} {r:5d} {a.scale * r:6g} {nonzero_b[fam]:6d}  -> {VLLM_TARGET[fam]}")
            rows.append({"family": fam, "modules": per_family[fam], "rank": r, "lora_alpha": a.scale * r,
                         "scaling": a.scale, "modules_with_nonzero_B": nonzero_b[fam],
                         "vllm_target": VLLM_TARGET[fam]})
    n_mod = sum(per_family.values())
    print(f"total modules {n_mod}, tensors {len(out)}, expert layers {len(expert_layers)}, "
          f"file {st_path.stat().st_size / 2**30:.2f} GiB, {time.time() - t0:.1f}s")

    manifest = {
        "schema": "flash-lora-vllm-export-v1",
        "created_unix": time.time(),
        "source": str(a.adapter),
        "source_sha256": sha256_file(a.adapter),
        "source_meta": {k: meta[k] for k in ("status", "optimizer_steps", "loss_total", "preclip_gradient_norm") if k in meta},
        "source_identity": (meta.get("identity") or {}).get("probability_profile", {}).get("base_policy_version")
        if isinstance(meta.get("identity"), dict) else None,
        "adapter_model_sha256": sha256_file(st_path),
        "dtype": a.dtype,
        "training_scale": a.scale,
        "families": rows,
        "total_modules": n_mod,
        "total_tensors": len(out),
        "rank_pattern": rank_pattern,
        "alpha_pattern": alpha_pattern,
        "vllm_max_lora_rank_needed": r_top,
        "key_rule": "backbone.X.lora_{A,B} -> base_model.model.model.language_model.X.lora_{A,B}.weight; "
                    "mlp.experts.experts.{e} -> mlp.experts.{e}",
    }

    if a.verify_vllm:
        manifest["vllm_verify"] = verify_with_vllm(a.out_dir, out, ranks, a.scale, layer_types, n_experts)

    (a.out_dir / "export-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {a.out_dir}")
    return 0


def verify_with_vllm(out_dir: Path, out: dict, ranks: dict, scale: float, layer_types, n_experts) -> dict:
    """Re-parse with vLLM's own PEFTHelper and LoRA name parser + the Qwen4Exp
    rename mapper, and check every module lands on an existing vLLM module path
    with the training scaling."""
    from vllm.lora.peft_helper import PEFTHelper
    from vllm.lora.utils import parse_fine_tuned_lora_name
    from vllm.model_executor.models.utils import WeightsMapper
    try:  # the real mapper of the served architecture (rename part only)
        from vllm.models.qwen4_exp.nvidia.model import Qwen4ExpForConditionalGeneration
        mapper = Qwen4ExpForConditionalGeneration.hf_to_vllm_mapper.get_rename_mapper()
        mapper_src = "Qwen4ExpForConditionalGeneration.hf_to_vllm_mapper.get_rename_mapper()"
    except Exception as e:  # pragma: no cover
        mapper = WeightsMapper(orig_to_new_prefix={"model.language_model.": "language_model.model."})
        mapper_src = f"fallback prefix map ({type(e).__name__}: {e})"
    helper = PEFTHelper.from_local_dir(str(out_dir), None)
    packed = {  # vLLM runtime leaf -> adapter leaves (packed_modules_mapping with LoRA on)
        "qkv_proj": ["q_proj", "k_proj", "v_proj"], "gate_up_proj": ["gate_proj", "up_proj"],
        "in_proj_qkvz": ["in_proj_qkv", "in_proj_z"], "in_proj_ba": ["in_proj_b", "in_proj_a"],
    }
    leaf_to_runtime = {c: p for p, cs in packed.items() for c in cs}
    runtime_modules = set()
    for i, lt in enumerate(layer_types):
        base = f"language_model.model.layers.{i}"
        if lt == "linear_attention":
            runtime_modules |= {f"{base}.linear_attn.{m}" for m in ("in_proj_qkvz", "in_proj_ba", "out_proj")}
        else:
            runtime_modules |= {f"{base}.self_attn.{m}" for m in ("qkv_proj", "o_proj")}
        runtime_modules |= {f"{base}.mlp.shared_expert.gate_up_proj", f"{base}.mlp.shared_expert.down_proj",
                            f"{base}.mlp.experts"}
    bad, scal_bad, hit = [], [], set()
    for key in out:
        if not key.endswith(".lora_A.weight"):
            continue
        vname, is_a = parse_fine_tuned_lora_name(key, mapper)
        peft_name, _ = parse_fine_tuned_lora_name(key)
        r, s = helper.get_rank_and_scaling(peft_name)
        if abs(s - scale) > 1e-12 or r != out[key].shape[0]:
            scal_bad.append((peft_name, r, s))
        if ".mlp.experts." in vname:
            runtime = vname.rsplit(".experts.", 1)[0] + ".experts"
        else:
            parent, leaf = vname.rsplit(".", 1)
            runtime = f"{parent}.{leaf_to_runtime.get(leaf, leaf)}"
        if runtime not in runtime_modules:
            bad.append((key, vname))
        else:
            hit.add(runtime)
    return {
        "mapper": mapper_src,
        "adapter_modules_not_on_a_vllm_module": bad[:20],
        "n_not_on_vllm_module": len(bad),
        "scaling_mismatches": scal_bad[:20],
        "n_scaling_mismatches": len(scal_bad),
        "vllm_runtime_modules_hit": len(hit),
        "vllm_runtime_modules_total": len(runtime_modules),
        "vllm_runtime_modules_not_hit": sorted(runtime_modules - hit)[:20],
    }


if __name__ == "__main__":
    sys.exit(main())
