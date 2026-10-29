"""Multi-group GRPO update from the current adapter (2026-10-15, RL loop automation).

Generalizes the initial-base core (initial_update) for repeated on-policy updates:
  * starts from the previous release's adapter values and AdamW state (or zero-B base adapters),
  * accepts several complete groups sampled from that exact policy version (one step per fresh batch, so the
    current/proximal ratio is same-forward by construction, as in the initial core),
  * KL reference optional (user 2026-10-15: kl_beta = 0, DAPO-style; reference = proximal then),
  * groups whose rewards are all equal carry zero advantage and are skipped before any compute,
  * per-group drift report under the user-approved revised budget (raw ESS >= .90, truncated mass <= .01) gates release,
  * episodes are weighted equally across all active groups (each turn: tokens / episode tokens / active episodes).
Rollout tokens/log-probs are immutable; no turns or tokens are dropped from an active episode.

2026-10-24 (user, fast17): optional per-group steps. With per_group_steps the optimizer steps after each active group's
last episode (drift gate of that group first), so one update makes one step per active group at the same compute. Later
groups are scored by the already-stepped adapter: proximal = that adapter (same forward), the behavior ratio stays capped
(importance_cap) and the per-group gate measures it. Gradient prefixes cannot resume across a step and are not saved.

2026-10-26 (user, fast18): step_groups(gs) steps once on the summed gradient of several completed groups (runner
--groups-per-step N), so each step averages more cases; if any group of the batch fails its drift gate the whole step is
dropped (the gradients are already summed and cannot be split).
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import torch

import grpo_train
from grpo_train import sha256_file
from initial_update import atomic_save, tensor_digest
from runtime_identity import digest, require_matching_profile
from rca_lab.train import grpo, grpo_correction, grpo_drift

SCHEMA = "flash-general-update-v1"
DEFAULTS = dict(learning_rate=1e-5, clip_epsilon=.2, kl_beta=0., std_floor=1., max_grad_norm=1.,
                adam_beta1=.9, adam_beta2=.999, adam_eps=1e-8, importance_cap=2.)
REVISED_BUDGET = {"min_raw_ess": .90, "max_truncated_mass": .01}


class GeneralUpdate:
    def __init__(self, policy, groups_bytes, profile, bindings, *, policy_version, start_state=None, training=None,
                 gradient_scopes=None, per_group_steps=False):
        self.policy = policy
        self.policy_version = policy_version
        self.training = {**DEFAULTS, **(training or {})}
        if set(self.training) != set(DEFAULTS) or any(not isinstance(v, (int, float)) or isinstance(v, bool)
                                                      or not math.isfinite(v) for v in self.training.values()):
            raise ValueError("Invalid training configuration")
        if self.training["kl_beta"] != 0:
            raise ValueError("kl_beta > 0 needs a base-reference forward per turn; not wired in this runner")
        self.groups = []
        # 2026-10-15 (user): groups sampled by the start policy or by its parent (one optimizer step stale) are admitted;
        # the loss already importance-weights against the behavior logprobs (cap 2) and the release gate measures the
        # resulting truncated mass per group.
        parent = (start_state or {}).get("parent_policy_version")
        allowed = {policy_version} | ({parent} if parent else set())
        for raw in groups_bytes:
            group = json.loads(raw)
            behavior = group.get("policy_version")
            if behavior not in allowed:
                raise ValueError(f"Group behavior policy {behavior} is neither the start policy nor its parent")
            episodes = grpo_train.validate_group(group, behavior)
            self.groups.append(dict(group=group, episodes=episodes, sha256=hashlib.sha256(raw).hexdigest(),
                                    behavior_policy_version=behavior, stale_steps=0 if behavior == policy_version else 1))
        if not self.groups:
            raise ValueError("At least one complete group required")
        self.parameters = {n: p for n, p in policy.named_parameters() if p.requires_grad}
        if not self.parameters or any(not n.endswith(("lora_A", "lora_B")) for n in self.parameters):
            raise ValueError("Only LoRA A/B parameters may be trainable")
        self.gradient_scopes = gradient_scopes or {"adapter": list(self.parameters)}
        names = [n for v in self.gradient_scopes.values() for n in v]
        if len(names) != len(set(names)) or set(names) != set(self.parameters):
            raise ValueError("Gradient scopes must partition every trainable parameter")
        self.start_step = 0
        self.start_optimizer = None
        if start_state is not None:
            values = start_state["state_dict"]
            if values.keys() != self.parameters.keys():
                raise ValueError("Start adapter does not cover the trainable parameters")
            with torch.no_grad():
                for n, p in self.parameters.items():
                    if values[n].shape != p.shape:
                        raise ValueError("Start adapter shape mismatch: " + n)
                    p.copy_(values[n])
            self.start_step = int(start_state.get("optimizer_steps", 0))
            self.start_optimizer = start_state.get("optimizer")
        elif any(n.endswith("lora_B") and p.ne(0).any() for n, p in self.parameters.items()):
            raise ValueError("Without a start adapter, B must be zero (base policy)")
        self.initial_digest = tensor_digest(self.parameters)
        rewards, gid = [], []
        for g, item in enumerate(self.groups):
            for ep in item["episodes"]:
                rewards.append(float(ep["reward"])); gid.append(g)
        adv = grpo.group_advantages(torch.tensor(rewards), torch.tensor(gid), std_floor=self.training["std_floor"])
        k = 0
        for item in self.groups:
            item["advantages"] = adv[k:k + len(item["episodes"])]; k += len(item["episodes"])
            item["active"] = bool(item["advantages"].ne(0).any())
        self.active = [(g, i) for g, item in enumerate(self.groups) if item["active"] for i in range(len(item["episodes"]))]
        if not self.active:
            raise ValueError("All groups are flat: no learning signal")
        self.order = [(g, i, j) for g, i in self.active for j in range(len(self.groups[g]["episodes"][i]["turns"]))]
        self.identity = {"schema": SCHEMA, "probability_profile": profile, "probability_profile_sha256": digest(profile),
                         "bindings": bindings, "policy_version": policy_version,
                         "group_sha256": [item["sha256"] for item in self.groups],
                         "group_behavior_policy_versions": [item["behavior_policy_version"] for item in self.groups],
                         "group_stale_steps": [item["stale_steps"] for item in self.groups],
                         "active_groups": [g for g, item in enumerate(self.groups) if item["active"]],
                         "training": self.training, "initial_parameters_sha256": self.initial_digest,
                         "start_optimizer_steps": self.start_step, "gradient_scopes": self.gradient_scopes,
                         "core_sha256": sha256_file(__file__)}
        self.per_group_steps = bool(per_group_steps)
        self.identity["per_group_steps"] = self.per_group_steps
        for p in self.parameters.values():
            p.grad = None
        self.rows, self.loss_total = [], 0.
        self.steps, self.group_gate, self.group_norms, self._optimizer = 0, {}, {}, None

    def episode(self, g, i):
        return self.groups[g]["episodes"][i]

    def _turn_loss(self, g, i, j, current):
        """Weighted corrected-GRPO loss of one turn (not yet backpropagated) and its audit row."""
        ep = self.episode(g, i)
        turn = ep["turns"][j]
        if (current.shape != (1, len(turn["completion_token_ids"])) or not current.requires_grad
                or not torch.isfinite(current).all() or (current > 1e-5).any()):
            raise ValueError("Invalid complete differentiable log probabilities")
        proximal = current.detach().float().clone()
        old = torch.tensor(turn["old_logprobs"], dtype=torch.float32, device=current.device)[None]
        result = grpo_correction.corrected_grpo_loss(
            current, proximal, old, proximal, torch.ones_like(current, dtype=torch.bool),
            self.groups[g]["advantages"][i:i + 1].to(current.device), importance_cap=self.training["importance_cap"],
            clip_epsilon=self.training["clip_epsilon"], kl_beta=0.)
        weight = current.numel() / ep["generated_tokens"] / len(self.active)
        return result.loss * weight, dict(group=g, episode=i, turn=j, proximal=proximal.cpu())

    def accumulate(self, g, i, j, current):
        if len(self.rows) >= len(self.order) or (g, i, j) != self.order[len(self.rows)]:
            raise ValueError("Expected the next complete turn, exactly once")
        loss, row = self._turn_loss(g, i, j, current)
        loss.backward()
        self.rows.append(row)
        self.loss_total += loss.detach().item()

    def accumulate_episode_tree(self, g, i, make_tree):
        """All turns of episode (g, i) through the exact prefix-sharing schedule (tree_forward.EpisodeTree): the
        accumulated gradient is the sum of the per-turn gradients. Rows are appended in turn order."""
        n = len(self.episode(g, i)["turns"])
        if self.order[len(self.rows):len(self.rows) + n] != [(g, i, j) for j in range(n)]:
            raise ValueError("Expected the next complete episode, exactly once")
        results = {}
        def turn_loss(j, current):
            if j in results:
                raise ValueError("Turn scored twice by the tree schedule")
            loss, row = self._turn_loss(g, i, j, current)
            results[j] = (row, loss.detach().item())
            return loss
        tree = make_tree(self.episode(g, i)["turns"])
        tree.run(turn_loss)
        if sorted(results) != list(range(n)):
            raise ValueError("Tree schedule did not score every turn")
        for j in range(n):
            row, value = results[j]
            self.rows.append(row)
            self.loss_total += value
        return tree.stats

    def _optimizer_for(self, params, ordered):
        if self._optimizer is None:
            self._optimizer = torch.optim.AdamW(params, lr=self.training["learning_rate"], weight_decay=0.,
                                                betas=(self.training["adam_beta1"], self.training["adam_beta2"]), eps=self.training["adam_eps"])
            if self.start_optimizer is not None:
                if self.start_optimizer.get("names") != ordered:
                    raise ValueError("Optimizer state parameter order mismatch")
                self._optimizer.load_state_dict(self.start_optimizer["state"])
            for group in self._optimizer.param_groups:   # load_state_dict restores the saved lr; use the configured one
                group["lr"] = self.training["learning_rate"]
        return self._optimizer

    def group_done(self, g):
        """True once every turn of active group g has been accumulated."""
        done = {(r["group"], r["episode"], r["turn"]) for r in self.rows}
        return all(key in done for key in self.order if key[0] == g)

    def _group_report(self, g):
        item = self.groups[g]
        by_turn = {(r["group"], r["episode"], r["turn"]): r for r in self.rows if r["group"] == g}
        samples = [dict(episode_id=ep["episode_id"], turns=[dict(
            prompt_tokens=len(t["prompt_token_ids"]), completion_token_ids=t["completion_token_ids"],
            old_logprobs=t["old_logprobs"], proximal_logprobs=by_turn[(g, i, j)]["proximal"].reshape(-1).tolist())
            for j, t in enumerate(ep["turns"])]) for i, ep in enumerate(item["episodes"])]
        return grpo_drift.build_drift_report(
            samples, controls=None, expected_group_size=len(samples), importance_cap=self.training["importance_cap"],
            min_raw_ess=REVISED_BUDGET["min_raw_ess"], max_truncated_mass=REVISED_BUDGET["max_truncated_mass"])

    @staticmethod
    def _gate(r):
        eps = [e["summary"] for e in r["episodes"]]
        tokens = sum(e["tokens"] for e in eps)
        fraction = sum(e["truncated_mass"] for e in eps) / tokens
        alarms = [dict(episode=e2.get("episode_id"), raw_normalized_ess=e["raw_normalized_ess"],
                       truncated_fraction=e["truncated_mass"] / e["tokens"])
                  for e2, e in zip(r["episodes"], eps)
                  if e["raw_normalized_ess"] < REVISED_BUDGET["min_raw_ess"] or e["truncated_mass"] / e["tokens"] > REVISED_BUDGET["max_truncated_mass"]]
        return dict(group_truncated_fraction=fraction, passed=fraction <= REVISED_BUDGET["max_truncated_mass"], alarms=alarms)

    def step_groups(self, gs):
        """One AdamW step on the summed gradient of completed groups gs, each gated on its own drift report first."""
        gs = list(gs)
        if not self.per_group_steps or not gs or any(not self.group_done(g) or g in self.group_gate for g in gs):
            raise ValueError("step_groups needs per_group_steps and complete, not yet stepped groups")
        gates = {}
        for g in gs:
            report = self._group_report(g)
            gate = self._gate(report)
            gate["report"] = report
            gates[g] = gate
        passed = all(gt["passed"] for gt in gates.values())
        ordered = sorted(self.parameters)
        params = [self.parameters[n] for n in ordered]
        norm = None
        if passed:
            norm = torch.nn.utils.clip_grad_norm_(params, self.training["max_grad_norm"], error_if_nonfinite=True)
            self._optimizer_for(params, ordered).step()
            self.steps += 1
        for g, gt in gates.items():
            gt["step"] = self.steps if passed else None
            gt["step_groups"] = gs
            if not passed:
                gt["passed"] = False
            self.group_gate[g] = gt
            if passed:
                self.group_norms[g] = float(norm)
        for q in params:
            q.grad = None
        return passed

    def step_group(self, g):
        """Per-group step: gate group g on its own drift report, then one AdamW step on its gradient (or drop it)."""
        if not self.per_group_steps or not self.group_done(g) or g in self.group_gate:
            raise ValueError("step_group needs per_group_steps and a complete, not yet stepped group")
        report = self._group_report(g)
        gate = self._gate(report)
        gate["report"] = report
        self.group_gate[g] = gate
        ordered = sorted(self.parameters)
        params = [self.parameters[n] for n in ordered]
        if gate["passed"]:
            norm = torch.nn.utils.clip_grad_norm_(params, self.training["max_grad_norm"], error_if_nonfinite=True)
            self._optimizer_for(params, ordered).step()
            self.steps += 1
            self.group_norms[g] = float(norm)
        for q in params:
            q.grad = None
        return gate["passed"]

    def _release_stepped(self, output_dir):
        """Release after per-group steps: the adapter after every passed group's step (blocked if none passed)."""
        if len(self.rows) != len(self.order) or set(self.group_gate) != set(self.identity["active_groups"]):
            raise ValueError("Every active group must be accumulated and gated before release")
        gate = {self.groups[g]["group"]["group_id"]: {k: v for k, v in r.items() if k != "report"} for g, r in self.group_gate.items()}
        reports = {self.groups[g]["group"]["group_id"]: r["report"] for g, r in self.group_gate.items()}
        if self.steps == 0:
            return dict(status="blocked_drift_budget", failed_groups=list(gate), gate=gate, reports=reports, optimizer_steps=self.start_step)
        updated = {n: q.detach().cpu().clone() for n, q in self.parameters.items()}
        if tensor_digest(updated) == self.initial_digest:
            raise ValueError("Optimizer did not change adapter values")
        ordered = sorted(self.parameters)
        state = dict(state_dict=updated, optimizer=dict(names=ordered, state=self._optimizer.state_dict()),
                     optimizer_steps=self.start_step + self.steps, parent_policy_version=self.policy_version, identity=self.identity)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        adapter_sha = atomic_save(state, output_dir / "adapter.pt")
        result = dict(status="passed", optimizer_steps=self.start_step + self.steps, steps_this_update=self.steps,
                      effective_learning_rate=self._optimizer.param_groups[0]["lr"],
                      preclip_gradient_norm=max(self.group_norms.values()), preclip_gradient_norms=self.group_norms,
                      loss_total=self.loss_total, adapter_sha256=adapter_sha, policy_version="adapter-" + adapter_sha[:16],
                      active_groups=self.identity["active_groups"], gate=gate, reports=reports,
                      failed_groups=[gid for gid, g in gate.items() if not g["passed"]])
        (output_dir / "release-result.json").write_text(json.dumps(result, indent=2, default=str))
        return result

    def state(self):
        gradients = {n: None if p.grad is None else p.grad.detach().cpu().clone() for n, p in self.parameters.items()}
        return dict(identity=self.identity, rows=self.rows, gradients=gradients, loss_total=self.loss_total)

    def save_prefix(self, path):
        return atomic_save(self.state(), path)

    def resume(self, path, expected_sha256):
        if sha256_file(path) != expected_sha256:
            raise ValueError("Prefix hash mismatch")
        state = torch.load(path, map_location="cpu", weights_only=True)
        if state["identity"] != self.identity:
            raise ValueError("Stale prefix identity")
        rows = state["rows"]
        if [(r["group"], r["episode"], r["turn"]) for r in rows] != self.order[:len(rows)]:
            raise ValueError("Incomplete or reordered prefix")
        for n, p in self.parameters.items():
            g = state["gradients"][n]
            p.grad = None if g is None else g.to(p.device).clone()
        self.rows, self.loss_total = rows, state["loss_total"]

    def drift_reports(self):
        if len(self.rows) != len(self.order):
            raise ValueError("Partial accumulation cannot be audited")
        by_turn = {(r["group"], r["episode"], r["turn"]): r for r in self.rows}
        reports = {}
        for g, item in enumerate(self.groups):
            if not item["active"]:
                continue
            samples = [dict(episode_id=ep["episode_id"], turns=[dict(
                prompt_tokens=len(t["prompt_token_ids"]), completion_token_ids=t["completion_token_ids"],
                old_logprobs=t["old_logprobs"], proximal_logprobs=by_turn[(g, i, j)]["proximal"].reshape(-1).tolist())
                for j, t in enumerate(ep["turns"])]) for i, ep in enumerate(item["episodes"])]
            reports[item["group"]["group_id"]] = grpo_drift.build_drift_report(
                samples, controls=None, expected_group_size=len(samples), importance_cap=self.training["importance_cap"],
                min_raw_ess=REVISED_BUDGET["min_raw_ess"], max_truncated_mass=REVISED_BUDGET["max_truncated_mass"])
        return reports

    def release(self, output_dir, current_profile):
        require_matching_profile(self.identity, current_profile)
        if self.per_group_steps:
            return self._release_stepped(output_dir)
        if tensor_digest(self.parameters) != self.initial_digest:
            raise ValueError("Adapter values changed during accumulation")
        reports = self.drift_reports()
        # 2026-10-15 user decision: block only on the group truncated-mass fraction; raw ESS and per-episode truncated
        # mass are alarms (dominated by a few extreme-ratio tokens; the capped ESS of those groups stays ~0.96).
        gate = {}
        for gid, r in reports.items():
            eps = [e["summary"] for e in r["episodes"]]
            tokens = sum(e["tokens"] for e in eps)
            fraction = sum(e["truncated_mass"] for e in eps) / tokens
            alarms = [dict(episode=e2.get("episode_id"), raw_normalized_ess=e["raw_normalized_ess"],
                           truncated_fraction=e["truncated_mass"] / e["tokens"])
                      for e2, e in zip(r["episodes"], eps)
                      if e["raw_normalized_ess"] < REVISED_BUDGET["min_raw_ess"] or e["truncated_mass"] / e["tokens"] > REVISED_BUDGET["max_truncated_mass"]]
            gate[gid] = dict(group_truncated_fraction=fraction, passed=fraction <= REVISED_BUDGET["max_truncated_mass"], alarms=alarms)
        failed = [gid for gid, g in gate.items() if not g["passed"]]
        output_dir = Path(output_dir)
        if failed:
            return dict(status="blocked_drift_budget", failed_groups=failed, gate=gate, reports=reports, optimizer_steps=self.start_step)
        for scope, names in self.gradient_scopes.items():
            if not any(self.parameters[n].grad is not None and self.parameters[n].grad.ne(0).any() for n in names):
                raise ValueError("No nonzero gradient in required scope: " + scope)
        ordered = sorted(self.parameters)
        params = [self.parameters[n] for n in ordered]
        norm = torch.nn.utils.clip_grad_norm_(params, self.training["max_grad_norm"], error_if_nonfinite=True)
        optimizer = torch.optim.AdamW(params, lr=self.training["learning_rate"], weight_decay=0.,
                                      betas=(self.training["adam_beta1"], self.training["adam_beta2"]), eps=self.training["adam_eps"])
        if self.start_optimizer is not None:
            if self.start_optimizer.get("names") != ordered:
                raise ValueError("Optimizer state parameter order mismatch")
            optimizer.load_state_dict(self.start_optimizer["state"])
        for group in optimizer.param_groups:   # 2026-10-26: load_state_dict restored the saved lr; use the configured one
            group["lr"] = self.training["learning_rate"]
        optimizer.step()
        updated = {n: p.detach().cpu().clone() for n, p in self.parameters.items()}
        if tensor_digest(updated) == self.initial_digest:
            raise ValueError("Optimizer did not change adapter values")
        state = dict(state_dict=updated, optimizer=dict(names=ordered, state=optimizer.state_dict()),
                     optimizer_steps=self.start_step + 1, parent_policy_version=self.policy_version, identity=self.identity)
        output_dir.mkdir(parents=True, exist_ok=True)
        adapter_sha = atomic_save(state, output_dir / "adapter.pt")
        result = dict(status="passed", optimizer_steps=self.start_step + 1, preclip_gradient_norm=float(norm),
                      loss_total=self.loss_total, adapter_sha256=adapter_sha, policy_version="adapter-" + adapter_sha[:16],
                      active_groups=self.identity["active_groups"], gate=gate, reports=reports)
        (output_dir / "release-result.json").write_text(json.dumps(result, indent=2, default=str))
        return result
