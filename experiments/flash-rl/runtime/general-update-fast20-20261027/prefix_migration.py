"""Continue a single-GPU gradient prefix under the two-GPU split runtime (2026-10-15, user: "14턴까지 했으면 이어서").

The committed prefix binds the single-GPU runner hash and runtime profile, so `InitialUpdate.resume` refuses it under
the split runner. Device placement does not change arithmetic (pipeline_split moves storage only), so the only
legitimate identity differences are:
  * bindings.runner_sha256                      (runner gained the --pipeline-split-at option)
  * probability_profile.pipeline_split_at       (absent before, N now)
  * probability_profile.runtime_identity.implementations.pipeline_split (new source file)
  * the two digests derived from the profile.
Everything else (checkpoint, loader, split, controls, group, training, initial A/B digest, scopes, core and loss
sources, proximal contract) must be identical, or migration is refused. Before continuing, turn (0, 0) is recomputed
on the split model and compared against the prefix's stored log probabilities. The prefix file is not modified; a
migration record is written next to the new output and every later prefix carries the new identity.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import torch

ALLOWED_PROFILE_KEYS = {"pipeline_split_at"}


def _strip(identity: dict) -> dict:
    ident = copy.deepcopy(identity)
    ident.pop("probability_profile_sha256", None)
    profile = ident.get("probability_profile", {})
    for key in ALLOWED_PROFILE_KEYS:
        profile.pop(key, None)
    runtime = profile.get("runtime_identity", {})
    runtime.get("implementations", {}).pop("pipeline_split", None)
    profile.pop("runtime_identity_sha256", None)
    ident.get("bindings", {}).pop("runner_sha256", None)
    return ident


def _diff_keys(a, b, prefix=""):
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            out += _diff_keys(a.get(k), b.get(k), f"{prefix}.{k}" if prefix else str(k))
        return out
    return [] if a == b else [prefix]


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def migrate(update_cls, policy, group_bytes, profile, bindings, prefix_path, expected_sha256, *, gradient_scopes,
            episodes, completion_logps, logit_chunk_size, output: Path):
    if sha256_file(prefix_path) != expected_sha256:
        raise ValueError("Prefix artifact hash mismatch")
    state = torch.load(prefix_path, map_location="cpu", weights_only=True)
    instance = update_cls(policy, group_bytes, profile, bindings, initial_parameters=state["initial_parameters"],
                          gradient_scopes=gradient_scopes)
    old_identity, new_identity = state["identity"], instance.identity
    residual = _diff_keys(_strip(old_identity), _strip(new_identity))
    if residual:
        raise ValueError(f"Prefix differs beyond the device split: {residual[:8]}")
    changed = _diff_keys(old_identity, new_identity)
    migrated = dict(state, identity=new_identity)
    instance._validate_state(migrated)
    # Same full-model recompute of the first committed turn on the split model.
    first = state["rows"][0]
    turn = episodes[first["episode"]]["turns"][first["turn"]]
    with torch.enable_grad():
        current = completion_logps(policy, turn, "cuda:0", logit_chunk_size)
    recomputed = current.detach().float().cpu()
    del current
    stored = first["proximal"].float()
    delta = (recomputed - stored).abs()
    check = {"turn": [first["episode"], first["turn"]], "tokens": int(stored.numel()),
             "max_abs": float(delta.max()), "mean_abs": float(delta.mean()), "exact": bool(torch.equal(recomputed, stored))}
    if not check["max_abs"] <= 1e-3:
        raise ValueError(f"Split model does not reproduce the stored turn: {check}")
    for n, p in instance.parameters.items():
        g = state["gradients"][n]
        p.grad = None if g is None else g.to(p.device).clone()
    instance.rows, instance.loss_total = state["rows"], state["loss_total"]
    record = {"source_prefix": str(prefix_path), "source_prefix_sha256": expected_sha256,
              "source_runner_sha256": old_identity["bindings"].get("runner_sha256"),
              "new_runner_sha256": new_identity["bindings"].get("runner_sha256"),
              "changed_identity_fields": changed, "completed_turns": len(instance.rows),
              "first_turn_recompute": check, "optimizer_steps": 0}
    (output / "prefix-migration.json").write_text(json.dumps(record, indent=2))
    return instance, record
