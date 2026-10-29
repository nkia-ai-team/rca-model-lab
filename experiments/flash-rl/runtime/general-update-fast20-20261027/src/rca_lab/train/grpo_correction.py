"""Optional decoupled GRPO surrogate; not enabled by importing this module.

Tokenwise importance truncation is a biased approximation for long trajectories,
not a repair for broken model arithmetic. Preserve immutable sampled rollout
logps, frozen per-batch proximal logps, and fixed reference logps separately.
"""
from dataclasses import dataclass
from math import isfinite

import torch
from torch import Tensor

from .grpo import grpo_loss


@dataclass(frozen=True)
class CorrectedGRPOResult:
    loss: Tensor
    policy_loss: Tensor
    sampled_reference_regularizer: Tensor
    episode_losses: Tensor
    diagnostics: dict[str, Tensor]


def corrected_grpo_loss(
    policy_logps: Tensor,
    proximal_logps: Tensor,
    rollout_logps: Tensor,
    reference_logps: Tensor,
    assistant_mask: Tensor,
    advantages: Tensor,
    *,
    importance_cap: float = 2.0,
    clip_epsilon: float = 0.2,
    kl_beta: float = 0.02,
) -> CorrectedGRPOResult:
    """Episode-mean PPO(current/proximal), weighted by capped proximal/rollout.

    The cap is an experimental hyperparameter, not an admission threshold.
    Caller must admit complete groups upstream; this function cannot infer group
    membership. No token rejection or batch normalization is performed.
    The reference term is a sampled regularizer, not a measured unbiased KL.
    Only policy_logps receive gradients. For turn microbatches accumulate each
    turn loss times its token count / episode token count before optimizer.step.
    """
    if not isfinite(importance_cap) or importance_cap <= 0:
        raise ValueError("importance_cap must be finite and positive")
    # Reuse the core's full shape, masking, finite-value and parameter contract.
    core = grpo_loss(policy_logps, proximal_logps, reference_logps,
                     assistant_mask, advantages, clip_epsilon=clip_epsilon,
                     kl_beta=kl_beta)
    if rollout_logps.shape != policy_logps.shape or rollout_logps.device != policy_logps.device:
        raise ValueError("rollout logps must share shape/device")
    if not rollout_logps.is_floating_point():
        raise ValueError("rollout logps must be floating point")
    valid_rollout = rollout_logps[assistant_mask]
    if not torch.isfinite(valid_rollout).all() or (valid_rollout > 1e-5).any():
        raise ValueError("invalid rollout policy-token log probabilities")

    def masked(value: Tensor) -> Tensor:
        return value.float().masked_fill(~assistant_mask, 0)

    current = masked(policy_logps)
    proximal = masked(proximal_logps.detach())
    behavior = masked(rollout_logps.detach())
    delta = proximal - behavior
    raw = delta.exp()
    if not torch.isfinite(raw).all():
        raise ValueError("rollout importance overflow; batch not admitted")
    weight = raw.clamp(max=importance_cap).detach()
    ratio = (current - proximal).exp()
    adv = advantages.detach().float()[:, None]
    surrogate = torch.minimum(ratio * adv,
                              ratio.clamp(1 - clip_epsilon, 1 + clip_epsilon) * adv)
    counts = assistant_mask.sum(dim=1)

    def means(value: Tensor) -> Tensor:
        return value.masked_fill(~assistant_mask, 0).sum(dim=1) / counts

    policies = means(-weight * surrogate)
    ref_delta = masked(reference_logps.detach()) - current
    regularizers = means(torch.expm1(ref_delta) - ref_delta)
    losses = policies + kl_beta * regularizers
    if not torch.isfinite(losses).all():
        raise ValueError("nonfinite corrected objective")

    # Compute diagnostics in float64: squared FP32 weights can overflow even
    # when individual ratios are finite. Never feed diagnostics into gradients.
    r = raw[assistant_mask].detach().double()
    w = weight[assistant_mask].double()
    d = delta[assistant_mask].double()
    def ess(values: Tensor) -> Tensor:
        # Rescaling preserves ESS and avoids overflow/underflow of squares.
        scaled = values / values.max().clamp_min(torch.finfo(values.dtype).tiny)
        return scaled.sum().square() / scaled.square().sum().clamp_min(torch.finfo(values.dtype).tiny)
    diagnostics = {
        "raw_ratio_mean": r.mean(), "raw_ratio_variance": r.var(unbiased=False),
        "raw_ratio_min": r.min(), "raw_ratio_p001": torch.quantile(r, .001),
        "raw_ratio_p99": torch.quantile(r, .99),
        "raw_ratio_p999": torch.quantile(r, .999), "raw_ratio_max": r.max(),
        "raw_ess": ess(r), "raw_normalized_ess": ess(r) / r.numel(),
        "capped_ess": ess(w), "capped_normalized_ess": ess(w) / w.numel(),
        "cap_fraction": (r > importance_cap).double().mean(),
        "truncated_weight_mass": (r - w).sum(),
        "truncated_weight_mass_fraction": (r - w).sum() / r.sum().clamp_min(torch.finfo(r.dtype).tiny),
        "signed_logratio_mean": d.mean(), "absolute_logratio_max": d.abs().max(),
        "episode_logratio_sum": delta.masked_fill(~assistant_mask, 0).sum(dim=1),
        "ppo_clip_fraction": core.clip_fraction,
        "ppo_ratio_mean": means(ratio.detach()).mean(),
    }
    return CorrectedGRPOResult(losses.mean(), policies.mean().detach(),
                               regularizers.mean().detach(), losses.detach(),
                               {key: value.detach() for key, value in diagnostics.items()})
