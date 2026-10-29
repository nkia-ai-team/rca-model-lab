"""Release one update under the user-revised drift budget (2026-10-15).

The preregistered budget (raw normalized ESS >= .95 and truncated mass <= .01 per episode) failed on the complete
g0002 audit (ESS .948/.931/.942/.953, truncated mass <= .01 everywhere). The user approved revising the release
criterion to truncated mass <= .01 as the primary guard (the cap-2 truncated importance correction stays nearly
unbiased) with ESS >= .90 as a gross-mismatch alarm. This keeps the core file (and the accumulation identity bound
to it) unchanged: the subclass verifies the sealed original report exactly, recomputes the revised report from the
same rows/controls, and requires the review to carry the revised budget. Both reports are saved with the result.
"""
from __future__ import annotations

import json
from pathlib import Path

import torch

from initial_update import InitialUpdate, atomic_save, require_matching_profile, sha256_file, tensor_digest
from rca_lab.train import grpo_drift

REVISED = {"min_raw_ess": 0.90, "max_truncated_mass": 0.01}


class RevisedInitialUpdate(InitialUpdate):
    def _revised_report(self, rows, controls):
        by_turn = {(r["episode"], r["turn"]): r for r in rows}
        samples = [dict(episode_id=ep["episode_id"], turns=[dict(
            prompt_tokens=len(t["prompt_token_ids"]), completion_token_ids=t["completion_token_ids"],
            old_logprobs=t["old_logprobs"], proximal_logprobs=by_turn[(i, j)]["proximal"].reshape(-1).tolist())
            for j, t in enumerate(ep["turns"])]) for i, ep in enumerate(self.episodes)]
        return grpo_drift.build_drift_report(samples, controls=controls["controls"], importance_cap=2., **REVISED)

    def release(self, review, current_profile, output):
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
                or review.get("importance_cap") != 2. or review.get("revised_budget") != REVISED
                or any(not isinstance(review.get(k), str) or not review[k].strip() for k in ("reviewer", "rationale", "budget_revision_approval"))
                or any(not isinstance(review.get("reviews", {}).get(k), str) or not review["reviews"][k].strip()
                       for k in ("systematic_drift", "context_growth", "serving_controls"))):
            raise ValueError("Missing or stale explicit one-update review with the revised budget")
        state = torch.load(path, map_location="cpu", weights_only=True)
        self._validate_state(state)
        original = self._report(state["rows"], state["controls"])
        if original != state["drift_report"]:
            raise ValueError("Sealed original report differs from complete accumulation")
        revised = self._revised_report(state["rows"], state["controls"])
        if not revised["all_experimental_budgets_pass"]:
            raise ValueError("Revised drift budget failed; no update")
        for scope, names in self.gradient_scopes.items():
            if not any(state["gradients"][n] is not None and state["gradients"][n].ne(0).any() for n in names):
                raise ValueError("No nonzero gradient in required scope: " + scope)
        output = Path(output)
        if output.exists():
            raise ValueError("Optimizer result already exists")
        reservation = output.with_suffix(output.suffix + ".reserved")
        with reservation.open("x") as stream:
            json.dump(dict(binding=binding, status="one_update_reserved", revised_budget=REVISED), stream)
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
                      admission=review, loss_total=self.loss_total, preclip_gradient_norm=float(norm),
                      original_drift_report=original, revised_drift_report=revised, revised_budget=REVISED)
        atomic_save(result, output)
        return result
