"""Clipped GRPO primitives for complete RCA episodes (never weighted CE).

Rows are complete sampled episodes: concatenate *all* assistant turns and retain
context/observation positions with a false policy mask. ``old_logps`` must be the
log probabilities of the behavior policy that actually sampled those tokens;
``reference_logps`` are from a fixed reference, not the current adapter. Sampling
must use the same distribution as these log probabilities (no unaccounted top-p,
temperature, or speculative distribution changes).

This module implements the objective, not a rollout engine or optimizer loop.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Sequence

import torch
from torch import Tensor


@dataclass(frozen=True)
class EpisodeOutcome:
    """Only completed, successfully judged trajectories may enter an update.

    Infrastructure/judge failures must be retried upstream, never assigned zero.
    Token/turn costs must count the full episode, including failed tool attempts.
    """

    judge_status: str
    root_correct: bool
    mechanism_correct: bool
    generated_tokens: int
    investigation_turns: int
    infrastructure_error: bool = False
    root_precision: float = 0.0
    root_recall: float = 0.0
    model_failure: bool = False
    mechanism_score: float | None = None
    evidence_judge_status: str | None = None
    path_score: float | None = None
    hallucination_score: float | None = None
    causal_depth: float | None = None  # rca_lab.mcp.causal_depth: supported root-chain links / 3 (joint successes only)
    major_claims: int | None = None  # evidence judge: unsupported/fabricated claims of severity "major"


MAJOR_CLAIM_PENALTY_MAX = 0.3


def trajectory_rewards(
    outcomes: Sequence[EpisodeOutcome],
    *,
    token_budget: int,
    turn_budget: int,
    efficiency_weight: float = 0.0,
    token_cost_share: float = 0.7,
    path_weight: float = 0.0,
    hallucination_weight: float = 0.0,
    success_hallucination_weight: float | None = None,
    partial_root_weight: float = 0.2,
    partial_mechanism_weight: float = 0.1,
    depth_weight: float = 0.0,
    fabricated_success_cap: float | None = None,
    major_claim_penalty: float = 0.0,
) -> Tensor:
    """Joint correctness first; bounded efficiency bonus on successes only.

    Budgets normalize cost; they are NOT trajectory truncation limits. Start with
    efficiency_weight=0 for the accuracy-only control. With weight <= .2, a
    correct episode earns [0.8, 1], partial answers <= .3, model failures -.2. These
    coefficients are explicit experimental hyperparameters, not validated optima.
    """
    if not outcomes:
        raise ValueError("empty outcomes")
    if token_budget <= 0 or turn_budget <= 0:
        raise ValueError("cost normalization budgets must be positive")
    if not isfinite(efficiency_weight) or not 0 <= efficiency_weight <= 0.2:
        raise ValueError("efficiency_weight must be finite and in [0, .2]")
    if not isfinite(token_cost_share) or not 0 <= token_cost_share <= 1:
        raise ValueError("token_cost_share must be finite and in [0, 1]")
    if not isfinite(path_weight) or not 0 <= path_weight <= 0.05:
        raise ValueError("path_weight must be finite and in [0, .05]")
    if not isfinite(hallucination_weight) or not 0 <= hallucination_weight <= 0.1:
        raise ValueError("hallucination_weight must be finite and in [0, .1]")
    # rca-evidence-v3 (2026-10-19, user): a heavier fabrication penalty on joint successes only (half of them carried a
    # major fabricated claim at -0.1). With <= .3 a success still earns >= .5 > the .35 partial ceiling, and partial
    # answers keep -0.1 so a fabricated wrong answer never falls below a non-submission (-.2).
    shw = hallucination_weight if success_hallucination_weight is None else success_hallucination_weight
    if not isfinite(shw) or not 0 <= shw <= 0.3:
        raise ValueError("success_hallucination_weight must be finite and in [0, .3]")
    # rca-evidence-v4 (2026-10-19, user: "기전 비율을 좀더 높여"): a partial answer is quality * (root + mechanism * score);
    # v2/v3 use .2 + .1 (root-led), v4 .1 + .2 (mechanism-led). Their sum stays <= .3 so every success (>= .5) still
    # outranks every partial answer.
    for name, w in (("partial_root_weight", partial_root_weight), ("partial_mechanism_weight", partial_mechanism_weight)):
        if not isfinite(w) or w < 0:
            raise ValueError(f"{name} must be finite and >= 0")
    if partial_root_weight + partial_mechanism_weight > 0.3 + 1e-9:
        raise ValueError("partial_root_weight + partial_mechanism_weight must be <= .3")
    # rca-evidence-v5 (2026-10-19, user: explain the target and its cause precisely): joint successes earn
    # depth_weight * causal_depth (grounded root-chain links). Successes only, so a detailed wrong answer earns nothing.
    if not isfinite(depth_weight) or not 0 <= depth_weight <= 0.1:
        raise ValueError("depth_weight must be finite and in [0, .1]")
    # rca-evidence-v6 (2026-10-20, user): a joint success carrying a major unsupported/fabricated claim is capped at
    # this value (.35) before the evidence terms, so it lands in [.05, .40]: below every clean success (>= .5); a lightly
    # fabricated one stays above an honest partial (~.25), a heavily fabricated one falls below it. u28-u30: fabricated
    # successes (hallucination .8, reward .79) beat honest partials (.19).
    if fabricated_success_cap is not None and (not isfinite(fabricated_success_cap) or not 0 <= fabricated_success_cap <= 0.35):
        raise ValueError("fabricated_success_cap must be finite and in [0, .35]")
    # rca-evidence-v7 (2026-10-20, user): instead of the v6 cap (a judge audit found "major" flags on 2/3 of root-correct
    # runs, half of them peripheral details), joint successes lose major_claim_penalty per major unsupported/fabricated
    # claim, at most MAJOR_CLAIM_PENALTY_MAX: clean successes rank first while flagged successes keep a positive signal.
    if not isfinite(major_claim_penalty) or not 0 <= major_claim_penalty <= 0.1:
        raise ValueError("major_claim_penalty must be finite and in [0, .1]")
    rewards = []
    for outcome in outcomes:
        if outcome.judge_status != "completed" or outcome.infrastructure_error:
            raise ValueError("unresolved judge/infrastructure outcome; retry before training")
        if type(outcome.root_correct) is not bool or type(outcome.mechanism_correct) is not bool:
            raise ValueError("correctness must be explicit boolean judgments")
        if (
            type(outcome.generated_tokens) is not int or outcome.generated_tokens <= 0
            or type(outcome.investigation_turns) is not int or outcome.investigation_turns < 0
        ):
            raise ValueError("invalid full-episode token/turn accounting")
        if any(not isfinite(v) or not 0 <= v <= 1
               for v in (outcome.root_precision, outcome.root_recall)):
            raise ValueError("root precision/recall must be finite in [0,1]")
        mechanism_score = (float(outcome.mechanism_correct) if outcome.mechanism_score is None
                           else outcome.mechanism_score)
        if mechanism_score not in (0.0, 0.5, 1.0):
            raise ValueError("mechanism_score must be 0, .5, or 1")
        if (mechanism_score == 1.0) != outcome.mechanism_correct:
            raise ValueError("mechanism score and pass judgment disagree")
        evidence_enabled = path_weight > 0 or hallucination_weight > 0 or shw > 0
        if evidence_enabled and not outcome.model_failure:
            if outcome.evidence_judge_status != "completed":
                raise ValueError("unresolved evidence judge outcome; retry before training")
            if (
                outcome.path_score is None
                or outcome.hallucination_score is None
                or not isfinite(outcome.path_score)
                or not isfinite(outcome.hallucination_score)
                or not 0 <= outcome.path_score <= 1
                or not 0 <= outcome.hallucination_score <= 1
            ):
                raise ValueError("evidence scores must be finite in [0,1]")
        cost = token_cost_share * outcome.generated_tokens / (outcome.generated_tokens + token_budget)
        cost += ((1 - token_cost_share) * outcome.investigation_turns
                 / (outcome.investigation_turns + turn_budget))
        success = outcome.root_correct and outcome.mechanism_correct
        quality = outcome.root_recall * outcome.root_precision ** 2
        partial = partial_root_weight * quality + partial_mechanism_weight * quality * mechanism_score
        if outcome.model_failure and success:
            raise ValueError("model failure cannot also be judged successful")
        if outcome.model_failure:
            reward = -0.2
        elif success:
            reward = 1 - efficiency_weight * cost
            if depth_weight > 0:
                depth = outcome.causal_depth
                if depth is None or isinstance(depth, bool) or not isfinite(depth) or not 0 <= depth <= 1:
                    raise ValueError("depth_weight requires a causal_depth in [0,1] on every joint success")
                reward += depth_weight * float(depth)
            if major_claim_penalty > 0:
                if type(outcome.major_claims) is not int or outcome.major_claims < 0:
                    raise ValueError("major_claim_penalty requires a major_claims count on every joint success")
                reward -= min(MAJOR_CLAIM_PENALTY_MAX, major_claim_penalty * outcome.major_claims)
            if fabricated_success_cap is not None:
                if type(outcome.major_claims) is not int or outcome.major_claims < 0:
                    raise ValueError("fabricated_success_cap requires a major_claims count on every joint success")
                if outcome.major_claims > 0:
                    reward = min(reward, fabricated_success_cap)
        else:
            reward = partial
        if evidence_enabled and not outcome.model_failure:
            reward += path_weight * float(outcome.path_score)
            reward -= (shw if success else hallucination_weight) * float(outcome.hallucination_score)
        rewards.append(reward)
    return torch.tensor(rewards, dtype=torch.float32)


def group_advantages(rewards: Tensor, group_ids: Tensor, *, std_floor: float = 1.0) -> Tensor:
    """Center within same-prompt rollout groups without magnifying tiny costs.

    A floor of 1 preserves absolute reward scale for [0,1] rewards. Ordinary
    unit-variance normalization would amplify arbitrarily tiny length differences
    in all-success groups into a full-strength policy update. Flat groups give 0.
    """
    if rewards.ndim != 1 or rewards.numel() == 0 or group_ids.shape != rewards.shape:
        raise ValueError("rewards and group_ids must be nonempty equal-length vectors")
    if not rewards.is_floating_point() or not torch.isfinite(rewards).all():
        raise ValueError("rewards must be finite floating-point values")
    if group_ids.dtype not in (torch.int32, torch.int64) or group_ids.device != rewards.device:
        raise ValueError("group_ids must be integers on rewards device")
    if not isfinite(std_floor) or std_floor <= 0:
        raise ValueError("std_floor must be positive and finite")
    rewards = rewards.detach().float()
    advantages = torch.empty_like(rewards)
    for group in torch.unique(group_ids):
        selected = group_ids == group
        values = rewards[selected]
        if values.numel() < 2:
            raise ValueError("each prompt group needs at least two completed rollouts")
        advantages[selected] = (values - values.mean()) / values.std(unbiased=False).clamp_min(
            std_floor
        )
    return advantages.detach()


@dataclass(frozen=True)
class GRPOResult:
    loss: Tensor
    policy_loss: Tensor
    reference_kl: Tensor
    clip_fraction: Tensor
    episode_losses: Tensor


def grpo_loss(
    policy_logps: Tensor,
    old_logps: Tensor,
    reference_logps: Tensor,
    assistant_mask: Tensor,
    advantages: Tensor,
    *,
    clip_epsilon: float = 0.2,
    kl_beta: float = 0.02,
) -> GRPOResult:
    """PPO-clipped GRPO surrogate plus nonnegative k3 reference KL estimator.

    Inputs logps/mask have shape [episode, token]; advantages have shape [episode].
    Each episode has equal weight independent of its token count. Only current
    policy logps receive gradients. Masked observation positions may contain NaN
    padding: they are removed BEFORE subtraction/exponentiation. Overflow on a
    policy token fails closed rather than silently clamping/changing the objective.
    """
    if policy_logps.ndim != 2 or policy_logps.numel() == 0:
        raise ValueError("policy_logps must be nonempty [episodes,tokens]")
    for value in (old_logps, reference_logps, assistant_mask):
        if value.shape != policy_logps.shape or value.device != policy_logps.device:
            raise ValueError("logps and mask must share shape/device")
    if assistant_mask.dtype != torch.bool:
        raise ValueError("assistant_mask must be boolean")
    if advantages.shape != (policy_logps.shape[0],) or advantages.device != policy_logps.device:
        raise ValueError("advantages must have one value per episode on policy device")
    if not advantages.is_floating_point() or not torch.isfinite(advantages).all():
        raise ValueError("advantages must be finite floats")
    if not isfinite(clip_epsilon) or not 0 < clip_epsilon < 1:
        raise ValueError("clip_epsilon must be finite and in (0,1)")
    if not isfinite(kl_beta) or kl_beta < 0:
        raise ValueError("kl_beta must be finite and nonnegative")
    counts = assistant_mask.sum(dim=1)
    if (counts == 0).any():
        raise ValueError("every episode needs assistant policy tokens")
    for value in (policy_logps, old_logps, reference_logps):
        if not value.is_floating_point() or not torch.isfinite(value[assistant_mask]).all():
            raise ValueError("policy-token log probabilities must be finite floats")
        if (value[assistant_mask] > 1e-5).any():
            raise ValueError("log probabilities cannot be positive")
    policy = policy_logps.float().masked_fill(~assistant_mask, 0)
    old = old_logps.detach().float().masked_fill(~assistant_mask, 0)
    reference = reference_logps.detach().float().masked_fill(~assistant_mask, 0)
    adv = advantages.detach().float()[:, None]
    ratios = (policy - old).exp()
    surrogate = torch.minimum(
        ratios * adv, ratios.clamp(1 - clip_epsilon, 1 + clip_epsilon) * adv
    )
    # expm1 is accurate for tiny log-ratios; exp(delta)-delta-1 estimates KL(pi||ref).
    delta = reference - policy
    kl = torch.expm1(delta) - delta
    if not torch.isfinite(ratios).all() or not torch.isfinite(kl).all():
        raise ValueError("importance ratio/KL overflow; reject stale or invalid batch")
    def episode_mean(values: Tensor) -> Tensor:
        return values.masked_fill(~assistant_mask, 0).sum(dim=1) / counts
    policies = episode_mean(-surrogate)
    kls = episode_mean(kl)
    losses = policies + kl_beta * kls
    if not torch.isfinite(losses).all():
        raise ValueError("nonfinite objective")
    clipped = ((ratios - 1).abs() > clip_epsilon).float()
    return GRPOResult(
        loss=losses.mean(), policy_loss=policies.mean().detach(),
        reference_kl=kls.mean().detach(), clip_fraction=episode_mean(clipped).mean().detach(),
        episode_losses=losses.detach(),
    )
