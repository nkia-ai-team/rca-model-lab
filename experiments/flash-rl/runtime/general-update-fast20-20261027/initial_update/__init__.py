"""Staged initial-base accumulation/release core. No CLI, rollout, or GPU launch.

Same-forward proximal equality is by construction, NOT independent parity proof.
Only an unchanged initial policy and exactly one optimizer update are supported.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import torch

import grpo_train
from grpo_train import BASE_REVISION, sha256_file, validate_group
from runtime_identity import digest, require_matching_profile
from rca_lab.train import grpo, grpo_correction, grpo_drift

SCHEMA = "flash-initial-update-v1"
DEFAULTS = dict(learning_rate=1e-6, clip_epsilon=.2, kl_beta=.02,
                std_floor=1., max_grad_norm=1.)


def tensor_digest(tensors):
    h = hashlib.sha256()
    for name, value in sorted(tensors.items()):
        h.update(name.encode())
        if value is None:
            h.update(b"none")
        else:
            value = value.detach().cpu().contiguous()
            h.update(str((str(value.dtype), tuple(value.shape))).encode())
            h.update(value.reshape(-1).view(torch.uint8).numpy().tobytes())
    return h.hexdigest()


def atomic_save(value, path):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    torch.save(value, temporary)
    temporary.replace(path)
    return sha256_file(path)


class InitialUpdate:
    """Caller owns verified checkpoint/split loading and completion forward.

    `bindings` supplies immutable checkpoint, loader, and verified split hashes;
    collect the actual runtime profile in the forward interpreter. Base parameter
    version counters prohibit in-process mutation; checkpoint identity covers the
    frozen base across restart. Only resume may restore exact initial A/B values.
    """

    def __init__(self, policy, group_bytes, profile, bindings, *, training=None,
                 initial_parameters=None, gradient_scopes=None):
        self.policy = policy
        self.group = json.loads(group_bytes)
        self.episodes = validate_group(self.group, BASE_REVISION)
        if len(self.episodes) != 4:
            raise ValueError("Exactly four complete episodes required")
        if not self.group.get("serving_backend_sha256"):
            raise ValueError("Fingerprint of actual serving backend required")
        self.profile = profile
        profile_artifact = dict(probability_profile=profile,
                                probability_profile_sha256=digest(profile))
        require_matching_profile(profile_artifact, profile)
        required = {"checkpoint_identity_sha256", "loader_sha256", "split_sha256"}
        if not required.issubset(bindings) or any(
                not isinstance(bindings[k], str) or len(bindings[k]) != 64
                or any(c not in "0123456789abcdef" for c in bindings[k]) for k in required):
            raise ValueError("Missing verified checkpoint/loader/split identity")
        if bindings["split_sha256"] != self.group.get("split_sha256"):
            raise ValueError("Split identity mismatch")
        self.training = {**DEFAULTS, **(training or {})}
        if set(self.training) != set(DEFAULTS) or any(
                not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v)
                for v in self.training.values()):
            raise ValueError("Invalid training configuration")
        if (any(self.training[k] <= 0 for k in ("learning_rate", "std_floor", "max_grad_norm"))
                or not 0 < self.training["clip_epsilon"] < 1 or self.training["kl_beta"] < 0):
            raise ValueError("Invalid training bounds")
        self.parameters = {n: p for n, p in policy.named_parameters() if p.requires_grad}
        if (not self.parameters or not any(n.endswith("lora_B") for n in self.parameters)
                or any(not n.endswith(("lora_A", "lora_B")) for n in self.parameters)):
            raise ValueError("Only initial A/B adapter parameters supported")
        self.gradient_scopes = gradient_scopes or {"adapter": list(self.parameters)}
        scope_names = [n for names in self.gradient_scopes.values() for n in names]
        if (any(not names for names in self.gradient_scopes.values()) or len(scope_names) != len(set(scope_names))
                or set(scope_names) != set(self.parameters)):
            raise ValueError("Gradient scopes must partition every trainable parameter")
        for module in policy.modules():
            if isinstance(module, torch.nn.Dropout) and module.p:
                raise ValueError("Dropout invalidates frozen initial policy")
        if initial_parameters is not None:
            self._validate_tensors(initial_parameters, allow_none=False)
            if any(v.ne(0).any() for n, v in initial_parameters.items() if n.endswith("lora_B")):
                raise ValueError("Resume initial B is not zero")
            with torch.no_grad():
                for n, p in self.parameters.items():
                    p.copy_(initial_parameters[n])
        if any(not torch.isfinite(p).all() or (n.endswith("lora_B") and p.ne(0).any())
               for n, p in self.parameters.items()):
            raise ValueError("Only finite initial zero-B adapters supported")
        self.initial = {n: p.detach().cpu().clone() for n, p in self.parameters.items()}
        self.identity = {"schema": SCHEMA, **profile_artifact, "bindings": bindings,
                         "group_sha256": hashlib.sha256(group_bytes).hexdigest(),
                         "training": self.training, "initial_parameters_sha256": tensor_digest(self.initial),
                         "gradient_scopes": self.gradient_scopes,
                         "source_sha256": {name: sha256_file(module.__file__) for name, module in
                                           (("grpo", grpo), ("correction", grpo_correction),
                                            ("drift", grpo_drift), ("group_validator", grpo_train))},
                         "core_sha256": sha256_file(__file__),
                         "proximal_contract": "same-differentiable-forward-detached-initial-base"}
        self.versions = {n: (id(t), t._version) for n, t in self._state_tensors()}
        self.order = [(i, j) for i, ep in enumerate(self.episodes) for j in range(len(ep["turns"]))]
        rewards = torch.tensor([ep["reward"] for ep in self.episodes], dtype=torch.float32)
        self.advantages = grpo.group_advantages(rewards, torch.zeros(4, dtype=torch.long),
                                               std_floor=self.training["std_floor"])
        if not self.advantages.ne(0).any():
            raise ValueError("Flat group has no learning signal")
        for p in self.parameters.values():
            p.grad = None
        self.rows, self.loss_total, self.sealed = [], 0., None
        self.released = False
        self.invalid = False

    def _state_tensors(self):
        return list(self.policy.named_parameters()) + [("buffer:" + n, t) for n, t in self.policy.named_buffers()]

    def _unchanged(self):
        if self.invalid or self.released or {n: (id(t), t._version) for n, t in self._state_tensors()} != self.versions:
            raise ValueError("Initial policy changed; accumulated gradients cannot be released")

    def _validate_tensors(self, values, *, allow_none):
        if values.keys() != self.parameters.keys():
            raise ValueError("Incomplete adapter tensor coverage")
        for n, p in self.parameters.items():
            value = values[n]
            if value is None and allow_none:
                continue
            if (not isinstance(value, torch.Tensor) or value.shape != p.shape or value.dtype != p.dtype
                    or not torch.isfinite(value).all()):
                raise ValueError("Invalid adapter tensor: " + n)

    def accumulate(self, episode_index, turn_index, current):
        self._unchanged()
        if self.sealed is not None or len(self.rows) >= len(self.order) or (episode_index, turn_index) != self.order[len(self.rows)]:
            raise ValueError("Expected the next complete turn, exactly once")
        ep = self.episodes[episode_index]
        turn = ep["turns"][turn_index]
        if (current.shape != (1, len(turn["completion_token_ids"])) or not current.requires_grad
                or not torch.isfinite(current).all() or (current > 1e-5).any()):
            raise ValueError("Invalid complete differentiable log probabilities")
        proximal = current.detach().float().clone()
        reference = proximal.clone()
        old = torch.tensor(turn["old_logprobs"], dtype=torch.float32, device=current.device)[None]
        result = grpo_correction.corrected_grpo_loss(
            current, proximal, old, reference, torch.ones_like(current, dtype=torch.bool),
            self.advantages[episode_index:episode_index + 1].to(current.device),
            importance_cap=2., clip_epsilon=self.training["clip_epsilon"], kl_beta=self.training["kl_beta"])
        weight = current.numel() / ep["generated_tokens"] / 4
        try:
            (result.loss * weight).backward()
            self._unchanged()
        except BaseException:
            # A failed backward may already have written partial gradients.
            # Only restoring a previously saved atomic prefix can recover it.
            self.invalid = True
            raise
        self.rows.append(dict(episode=episode_index, turn=turn_index,
                              proximal=proximal.cpu(), reference=reference.cpu()))
        self.loss_total += result.loss.detach().item() * weight

    def _state(self):
        self._unchanged()
        gradients = {n: None if p.grad is None else p.grad.detach().cpu().clone() for n, p in self.parameters.items()}
        self._validate_tensors(gradients, allow_none=True)
        return dict(identity=self.identity, optimizer_steps=0, initial_parameters=self.initial,
                    rows=self.rows, gradients=gradients, loss_total=self.loss_total)

    def save_prefix(self, path):
        if not self.rows:
            raise ValueError("No complete backward turn")
        return atomic_save(self._state(), path)

    @classmethod
    def resume(cls, policy, group_bytes, profile, bindings, path, expected_sha256, *, training=None,
               gradient_scopes=None):
        if sha256_file(path) != expected_sha256:
            raise ValueError("Prefix artifact hash mismatch")
        state = torch.load(path, map_location="cpu", weights_only=True)
        require_matching_profile(state["identity"], profile)
        instance = cls(policy, group_bytes, profile, bindings, training=training,
                       initial_parameters=state["initial_parameters"], gradient_scopes=gradient_scopes)
        instance._validate_state(state)
        for n, p in instance.parameters.items():
            g = state["gradients"][n]
            p.grad = None if g is None else g.to(p.device).clone()
        instance.rows, instance.loss_total = state["rows"], state["loss_total"]
        if "drift_report" in state or "controls" in state:
            report = instance._report(instance.rows, state["controls"])
            if report != state["drift_report"]:
                raise ValueError("Sealed report differs from complete accumulation")
            instance.sealed = (Path(path), dict(accumulation_sha256=expected_sha256,
                                               identity_sha256=digest(instance.identity),
                                               drift_report_sha256=digest(report)))
        return instance

    def _validate_state(self, state):
        if state["identity"] != self.identity or state.get("optimizer_steps") != 0:
            raise ValueError("Stale accumulation identity")
        rows = state["rows"]
        if not rows or len(rows) > len(self.order) or [(r["episode"], r["turn"]) for r in rows] != self.order[:len(rows)]:
            raise ValueError("Incomplete or reordered turn prefix")
        for row in rows:
            turn = self.episodes[row["episode"]]["turns"][row["turn"]]
            p, r = row["proximal"], row["reference"]
            if (not isinstance(p, torch.Tensor) or p.shape != (1, len(turn["completion_token_ids"]))
                    or not p.is_floating_point() or not torch.isfinite(p).all() or (p > 1e-5).any()
                    or not isinstance(r, torch.Tensor) or not torch.equal(p, r)):
                raise ValueError("Invalid proximal/reference coverage")
        if not math.isfinite(state["loss_total"]) or tensor_digest(state["initial_parameters"]) != self.identity["initial_parameters_sha256"]:
            raise ValueError("Corrupt initial state or loss totals")
        self._validate_tensors(state["gradients"], allow_none=True)

    def _report(self, rows, controls):
        if len(rows) != len(self.order):
            raise ValueError("Partial group cannot be audited for release")
        by_turn = {(r["episode"], r["turn"]): r for r in rows}
        samples = [dict(episode_id=ep["episode_id"], turns=[dict(
            prompt_tokens=len(t["prompt_token_ids"]), completion_token_ids=t["completion_token_ids"],
            old_logprobs=t["old_logprobs"],
            proximal_logprobs=by_turn[(i, j)]["proximal"].reshape(-1).tolist())
            for j, t in enumerate(ep["turns"])]) for i, ep in enumerate(self.episodes)]
        if (controls.get("status") != "complete" or controls.get("group_sha256") != self.identity["group_sha256"]
                or controls.get("serving_backend_sha256") != self.group.get("serving_backend_sha256")):
            raise ValueError("Serving controls identity mismatch")
        report = grpo_drift.build_drift_report(samples, controls=controls["controls"], importance_cap=2.)
        for ep in report["episodes"]:
            required = {0, len(ep["turns"]) // 2, len(ep["turns"]) - 1}
            verified = {t["turn_index"] for t in ep["turns"] if t.get("serving_control", {}).get("token_identity_verified") is True}
            if not required <= verified:
                raise ValueError("Missing verified early/middle/last serving controls")
        return report

    def seal(self, path, controls):
        state = self._state()
        self._validate_state(state)
        report = self._report(state["rows"], controls)
        state.update(drift_report=report, controls=controls)
        path = Path(path)
        if path.exists():
            raise ValueError("Refusing to overwrite sealed accumulation")
        artifact_sha = atomic_save(state, path)
        self.sealed = (path, dict(accumulation_sha256=artifact_sha,
                                  identity_sha256=digest(self.identity), drift_report_sha256=digest(report)))
        return self.sealed[1], report

    def release(self, review, current_profile, output):
        """One AdamW step only after complete unchanged-budget audit and bound review.

        Output reservation prevents accidental reuse after process interruption.
        Caller must perform real contextual review; this API validates its binding.
        """
        self._unchanged()
        require_matching_profile(self.identity, current_profile)
        if tensor_digest(self.parameters) != self.identity["initial_parameters_sha256"]:
            raise ValueError("Initial adapter values changed")
        if self.sealed is None:
            raise ValueError("No complete sealed accumulation")
        path, binding = self.sealed
        if sha256_file(path) != binding["accumulation_sha256"]:
            raise ValueError("Sealed accumulation changed")
        if (review.get("decision") != "approved_one_update" or review.get("binding") != binding
                or review.get("importance_cap") != 2. or any(not isinstance(review.get(k), str) or not review[k].strip()
                for k in ("reviewer", "rationale")) or any(not isinstance(review.get("reviews", {}).get(k), str)
                or not review["reviews"][k].strip() for k in ("systematic_drift", "context_growth", "serving_controls"))):
            raise ValueError("Missing or stale explicit one-update review")
        state = torch.load(path, map_location="cpu", weights_only=True)
        self._validate_state(state)
        report = self._report(state["rows"], state["controls"])
        if report != state["drift_report"] or not report["all_experimental_budgets_pass"]:
            raise ValueError("Complete episode drift budgets failed; no update")
        for scope, names in self.gradient_scopes.items():
            if not any(state["gradients"][n] is not None and state["gradients"][n].ne(0).any() for n in names):
                raise ValueError("No nonzero gradient in required scope: " + scope)
        output = Path(output)
        if output.exists():
            raise ValueError("Optimizer result already exists")
        reservation = output.with_suffix(output.suffix + ".reserved")
        with reservation.open("x") as stream:
            json.dump(dict(binding=binding, status="one_update_reserved"), stream)
        for n, p in self.parameters.items():
            g = state["gradients"][n]
            p.grad = None if g is None else g.to(p.device).clone()
        norm = torch.nn.utils.clip_grad_norm_(list(self.parameters.values()), self.training["max_grad_norm"], error_if_nonfinite=True)
        optimizer = torch.optim.AdamW(self.parameters.values(), lr=self.training["learning_rate"], weight_decay=0.)
        optimizer.step()
        self.released = True
        updated = {n: p.detach().cpu().clone() for n, p in self.parameters.items()}
        self._validate_tensors(updated, allow_none=False)
        if tensor_digest(updated) == self.identity["initial_parameters_sha256"]:
            raise ValueError("Optimizer did not change adapter values")
        result = dict(status="passed", optimizer_steps=1, state_dict=updated, identity=self.identity,
                      admission=review, loss_total=self.loss_total, preclip_gradient_norm=float(norm))
        atomic_save(result, output)
        return result
