"""Read-only residual-drift audit. Passing budgets never approves an update."""
from math import isfinite
from typing import Any

import torch


def _values(values: Any, name: str) -> torch.Tensor:
    if not isinstance(values, (list, tuple)) or not values:
        raise ValueError(f'{name}: expected nonempty logprob sequence')
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not isfinite(v) or v > 1e-5
           for v in values):
        raise ValueError(f'{name}: invalid log probabilities')
    return torch.tensor(values, dtype=torch.float64)


def _stats(delta: torch.Tensor, cap: float) -> dict:
    raw = delta.exp()
    if not torch.isfinite(raw).all() or (raw == 0).any():
        raise ValueError('importance overflow/underflow')
    capped = raw.clamp(max=cap)
    def ess(x):
        scaled = x / x.max()
        return (scaled.sum().square() / scaled.square().sum()).item()
    def quantiles(x):
        return {str(q): torch.quantile(x, q).item() for q in (.0, .5, .99, .999, 1.)}
    # Reject unrepresentable aggregate mass instead of fabricating diagnostics.
    removed_fraction = (1 - capped.sum() / raw.sum()).item()
    if not isfinite(removed_fraction):
        raise ValueError('importance aggregate overflow')
    return dict(tokens=delta.numel(), signed_mean=delta.mean().item(),
                signed_quantiles=quantiles(delta), absolute_quantiles=quantiles(delta.abs()),
                raw_ratio_quantiles=quantiles(raw), raw_ratio_mean=raw.mean().item(),
                raw_ess=ess(raw), raw_normalized_ess=ess(raw)/delta.numel(),
                capped_ess=ess(capped), capped_normalized_ess=ess(capped)/delta.numel(),
                cap_fraction=(raw > cap).double().mean().item(),
                truncated_mass=(raw - capped).sum().item(),
                truncated_mass_fraction=max(0., removed_fraction))


def build_drift_report(episodes: list[dict], controls: list[dict] | None = None, *,
                 expected_group_size: int = 4, importance_cap: float = 2.,
                 min_raw_ess: float = .95, max_truncated_mass: float = .01) -> dict:
    """Audit complete supplied episodes; upstream must verify trajectory manifests.

    Episode schema: episode_id, turns with old_logprobs, proximal_logprobs,
    prompt_token_ids (or integer prompt_tokens), optional completion_token_ids.
    Controls: episode_id, turn_index, generation_logprobs, prefill_logprobs,
    optional completion_token_ids. Control generation logps must exactly match
    the saved rollout. Token identity requires matching completion_token_ids in
    both records; without these a control is explicitly unverified.
    Context bins use prompt length, not generated-token position.
    """
    if (type(expected_group_size) is not int or expected_group_size < 2
            or not isfinite(importance_cap) or importance_cap <= 0
            or not isfinite(min_raw_ess) or not 0 <= min_raw_ess <= 1
            or not isfinite(max_truncated_mass) or not 0 <= max_truncated_mass <= 1):
        raise ValueError('invalid audit configuration')
    if len(episodes) != expected_group_size:
        raise ValueError('incomplete rollout group')
    ids = [ep['episode_id'] for ep in episodes]
    if len(set(ids)) != len(ids):
        raise ValueError('duplicate episode id')
    control_map = {}
    for control in controls or []:
        key = (control['episode_id'], control['turn_index'])
        if key in control_map:
            raise ValueError('duplicate control')
        control_map[key] = control
    reports, bins, seen = [], {}, set()
    for ep in episodes:
        if not ep['turns']:
            raise ValueError('episode has no turns')
        turns, deltas = [], []
        for index, turn in enumerate(ep['turns']):
            old = _values(turn['old_logprobs'], 'rollout')
            prox = _values(turn['proximal_logprobs'], 'proximal')
            if old.shape != prox.shape:
                raise ValueError('incomplete token coverage')
            tokens = turn.get('completion_token_ids')
            if tokens is not None and (len(tokens) != old.numel() or any(type(t) is not int or t < 0 for t in tokens)):
                raise ValueError('invalid completion token ids')
            if 'prompt_token_ids' in turn:
                prompt_ids = turn['prompt_token_ids']
                if not isinstance(prompt_ids, list) or any(type(t) is not int or t < 0 for t in prompt_ids):
                    raise ValueError('invalid prompt token ids')
                prompt = len(prompt_ids)
                if 'prompt_tokens' in turn and turn['prompt_tokens'] != prompt:
                    raise ValueError('prompt coverage mismatch')
            else:
                prompt = turn['prompt_tokens']
            if type(prompt) is not int or prompt <= 0:
                raise ValueError('invalid prompt token count')
            label = '0-8k' if prompt < 8192 else '8k-32k' if prompt < 32768 else '32k-64k' if prompt < 65536 else '64k+'
            delta = prox - old
            stats = _stats(delta, importance_cap)
            entry = dict(turn_index=index, prompt_tokens=prompt, context_bin=label, **stats)
            key = (ep['episode_id'], index)
            if key in control_map:
                seen.add(key)
                control = control_map[key]
                if 'prompt_tokens' in control and control['prompt_tokens'] != prompt:
                    raise ValueError('control prompt context mismatch')
                generation = _values(control['generation_logprobs'], 'control generation')
                prefill = _values(control['prefill_logprobs'], 'control prefill')
                if generation.shape != old.shape or prefill.shape != old.shape or not torch.equal(generation, old):
                    raise ValueError('control does not cover identical rollout probabilities')
                control_tokens = control.get('completion_token_ids')
                verified = tokens is not None and control_tokens is not None
                if verified and control_tokens != tokens:
                    raise ValueError('control sampled token identity mismatch')
                entry['serving_control'] = dict(
                    token_identity_verified=verified,
                    serving_self=_stats(prefill-generation, importance_cap),
                    trainer_vs_serving_prefill=_stats(prox-prefill, importance_cap),
                    comparison_status='pending_review')
            turns.append(entry)
            deltas.append(delta)
            bins.setdefault(label, []).append(delta)
        summary = _stats(torch.cat(deltas), importance_cap)
        budget_pass = (summary['raw_normalized_ess'] >= min_raw_ess and
                       summary['truncated_mass_fraction'] <= max_truncated_mass)
        reports.append(dict(episode_id=ep['episode_id'], turns=turns,
                            summary=summary, experimental_budgets_pass=budget_pass))
    if seen != set(control_map):
        raise ValueError('control references unknown episode or turn')
    return dict(status='pending_review', optimizer_approved=False,
                experimental_budgets=dict(importance_cap=importance_cap,
                    min_raw_normalized_ess=min_raw_ess,
                    max_truncated_mass_fraction=max_truncated_mass),
                all_experimental_budgets_pass=all(ep['experimental_budgets_pass'] for ep in reports),
                supplied_token_coverage='complete',
                trajectory_manifest_coverage='requires_upstream_verification',
                systematic_and_context_growth_review='pending_review',
                controls_supplied=len(control_map), episodes=reports,
                context_bins={k: _stats(torch.cat(v), importance_cap) for k, v in bins.items()})
