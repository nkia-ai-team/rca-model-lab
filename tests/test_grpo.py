import math

import pytest
import torch

from rca_lab.train.grpo import EpisodeOutcome, group_advantages, grpo_loss, trajectory_rewards
from rca_lab.mcp.rl_reward import (
    build_trajectory_evidence_item,
    validate_trajectory_evidence_verdict,
)


def batch(ratios, advantages):
    old = torch.full((len(ratios), 1), -3.0, requires_grad=True)
    policy = (old.detach() + torch.tensor(ratios).log()[:, None]).requires_grad_()
    ref = policy.detach().clone().requires_grad_()
    adv = torch.tensor(advantages, dtype=torch.float32, requires_grad=True)
    result = grpo_loss(policy, old, ref, torch.ones_like(policy, dtype=torch.bool), adv, kl_beta=0)
    return result, policy, old, ref, adv


def test_positive_advantage_upper_clipping():
    result, policy, old, ref, adv = batch([1.5], [1])
    assert result.loss.item() == pytest.approx(-1.2)
    result.loss.backward()
    assert policy.grad.item() == 0
    assert old.grad is None and ref.grad is None and adv.grad is None


def test_negative_advantage_lower_clipping():
    result, policy, *_ = batch([0.5], [-1])
    assert result.loss.item() == pytest.approx(0.8)
    result.loss.backward()
    assert policy.grad.item() == 0


def test_negative_advantage_high_ratio_is_not_clipped():
    result, policy, *_ = batch([1.5], [-1])
    assert result.loss.item() == pytest.approx(1.5)
    result.loss.backward()
    assert policy.grad.item() == pytest.approx(1.5)


def test_finite_kl_and_reference_frozen():
    policy = torch.tensor([[-2.]], requires_grad=True)
    reference = torch.tensor([[-3.]], requires_grad=True)
    result = grpo_loss(policy, policy.detach(), reference, torch.tensor([[True]]),
                       torch.tensor([0.]), kl_beta=1)
    assert result.reference_kl.item() == pytest.approx(math.exp(-1))
    result.loss.backward()
    assert policy.grad.item() == pytest.approx(1 - math.exp(-1))
    assert reference.grad is None


def test_masks_remove_observations_before_arithmetic_and_episode_weighting():
    policy = torch.tensor([[-3., float('nan'), float('nan')], [-3., -3., -3.]],
                          requires_grad=True)
    mask = torch.tensor([[True, False, False], [True, True, True]])
    result = grpo_loss(policy, policy.detach(), policy.detach(), mask, torch.tensor([1., 3.]))
    assert result.loss.item() == pytest.approx(-2)
    result.loss.backward()
    assert policy.grad[0, 1:].tolist() == [0., 0.]
    assert policy.grad[0, 0].item() == pytest.approx(-.5)
    assert policy.grad[1].sum().item() == pytest.approx(-1.5)


def test_flat_groups_and_tiny_success_cost_not_amplified():
    adv = group_advantages(torch.tensor([1., 1., .99, .991]), torch.tensor([0, 0, 1, 1]))
    assert adv[:2].tolist() == [0., 0.]
    assert adv[2:].abs().max().item() < .001
    with pytest.raises(ValueError):
        group_advantages(torch.tensor([1.]), torch.tensor([0]))


def test_reward_success_always_beats_partial_and_failure():
    outcomes = [EpisodeOutcome('completed', True, True, 100000, 100),
                EpisodeOutcome('completed', False, True, 1, 0, root_precision=.5, root_recall=1),
                EpisodeOutcome('completed', False, False, 1, 0, model_failure=True)]
    rewards = trajectory_rewards(outcomes, token_budget=100, turn_budget=10, efficiency_weight=.2)
    assert rewards.tolist() == pytest.approx([1 - .2 * (.7 * 100000 / 100100 + .3 * 100 / 110), .075, -.2])
    assert trajectory_rewards(outcomes, token_budget=100, turn_budget=10)[0].item() == 1


@pytest.mark.parametrize('status,infra', [('missing', False), ('error', False), ('completed', True)])
def test_unresolved_judge_never_fabricates_reward(status, infra):
    with pytest.raises(ValueError):
        trajectory_rewards([EpisodeOutcome(status, True, True, 1, 1, infra)],
                           token_budget=100, turn_budget=10)


def test_invalid_usage_and_nonfinite_fail_closed():
    with pytest.raises(ValueError):
        trajectory_rewards([EpisodeOutcome('completed', True, True, None, 1)],
                           token_budget=100, turn_budget=10, efficiency_weight=.2)
    with pytest.raises(ValueError):
        group_advantages(torch.tensor([1., float('nan')]), torch.tensor([0, 0]))
    with pytest.raises(ValueError):
        grpo_loss(torch.tensor([[float('nan')]]), torch.tensor([[-1.]]),
                  torch.tensor([[-1.]]), torch.tensor([[True]]), torch.tensor([1.]))
    with pytest.raises(ValueError):
        grpo_loss(torch.tensor([[-1.]]), torch.tensor([[-1.]]), torch.tensor([[-1.]]),
                  torch.tensor([[False]]), torch.tensor([1.]))


def test_overflow_not_silently_clamped():
    with pytest.raises(ValueError, match='overflow'):
        grpo_loss(torch.tensor([[-1.]]), torch.tensor([[-1000.]]), torch.tensor([[-1.]]),
                  torch.tensor([[True]]), torch.tensor([1.]))


def test_smooth_efficiency_distinguishes_long_successes_and_partial_mechanism():
    outcomes = [EpisodeOutcome('completed', True, True, n, 20) for n in (1000, 2000)]
    outcomes.append(EpisodeOutcome('completed', False, False, 100, 2,
                                   root_precision=.5, root_recall=1, mechanism_score=.5))
    rewards = trajectory_rewards(outcomes, token_budget=100, turn_budget=10, efficiency_weight=.2)
    assert .8 < rewards[1] < rewards[0] < 1
    assert rewards[2].item() == pytest.approx(.0625)
    with pytest.raises(ValueError):
        trajectory_rewards([EpisodeOutcome('completed', True, True, 10, 1, mechanism_score=.5)],
                           token_budget=100, turn_budget=10)


def test_evidence_reward_is_bounded_and_success_still_beats_partial():
    outcomes = [
        EpisodeOutcome('completed', True, True, 1000000, 1000000,
                       root_precision=1, root_recall=1, mechanism_score=1,
                       evidence_judge_status='completed', path_score=0, hallucination_score=1),
        EpisodeOutcome('completed', False, True, 1, 0,
                       root_precision=1, root_recall=1, mechanism_score=1,
                       evidence_judge_status='completed', path_score=1, hallucination_score=0),
    ]

    rewards = trajectory_rewards(outcomes, token_budget=1, turn_budget=1,
                                 efficiency_weight=.2, path_weight=.05,
                                 hallucination_weight=.1)

    assert rewards[0].item() == pytest.approx(.7)
    assert rewards[1].item() == pytest.approx(.35)
    assert rewards[0] > rewards[1]


def test_evidence_reward_fails_closed_without_completed_judge():
    outcome = EpisodeOutcome('completed', True, True, 10, 1,
                             root_precision=1, root_recall=1, mechanism_score=1,
                             evidence_judge_status='pending', path_score=.5,
                             hallucination_score=0)
    with pytest.raises(ValueError, match='evidence judge'):
        trajectory_rewards([outcome], token_budget=100, turn_budget=10,
                           path_weight=.05, hallucination_weight=.1)


def test_trajectory_evidence_item_excludes_correctness_and_post_submit_observations():
    messages = [
        {'role': 'user', 'content': 'seed'},
        {'role': 'assistant', 'tool_calls': [
            {'id': 'c1', 'function': {'name': 'read_timeseries', 'arguments': '{"target":"db"}'}}
        ]},
        {'role': 'tool', 'tool_call_id': 'c1', 'name': 'read_timeseries', 'content': 'db p95 rose'},
        {'role': 'assistant', 'tool_calls': [
            {'id': 'c2', 'function': {'name': 'submit_rca', 'arguments': '{}'}}
        ]},
        {'role': 'tool', 'tool_call_id': 'c2', 'name': 'submit_rca', 'content': 'hidden submit response'},
    ]

    item = build_trajectory_evidence_item(
        messages,
        {'summary': 'db caused it', 'causes': [], 'external_causes': []},
        {'stop_reason': 'submitted', 'root_precision': 0, 'root_recall': 0},
    )
    text = repr(item)

    assert 'root_precision' not in text
    assert 'root_recall' not in text
    assert 'hidden submit response' not in text
    assert item['trajectory_evidence']['refs'][1]['paired_call']['function']['name'] == 'read_timeseries'


def test_trajectory_evidence_item_uses_latest_submit_boundary_after_rejection():
    messages = [
        {'role': 'user', 'content': 'seed'},
        {'role': 'assistant', 'tool_calls': [
            {'id': 'c1', 'function': {'name': 'submit_rca', 'arguments': '{}'}}
        ]},
        {'role': 'tool', 'tool_call_id': 'c1', 'name': 'submit_rca', 'content': 'rejected'},
        {'role': 'assistant', 'tool_calls': [
            {'id': 'c2', 'function': {'name': 'sample_logs', 'arguments': '{"target":"payment"}'}}
        ]},
        {'role': 'tool', 'tool_call_id': 'c2', 'name': 'sample_logs', 'content': 'PG returned 429'},
        {'role': 'assistant', 'tool_calls': [
            {'id': 'c3', 'function': {'name': 'submit_rca', 'arguments': '{}'}}
        ]},
        {'role': 'tool', 'tool_call_id': 'c3', 'name': 'submit_rca', 'content': 'accepted'},
    ]

    item = build_trajectory_evidence_item(
        messages,
        {'summary': 'PG 429', 'causes': [], 'external_causes': []},
        {'stop_reason': 'submitted'},
    )
    text = repr(item)

    assert 'PG returned 429' in text
    assert 'accepted' not in text


def test_evidence_verdict_requires_claims_for_nonzero_scores():
    item = {
        'trajectory_evidence': {'refs': [{'ref_id': 'm1'}]},
    }
    with pytest.raises(ValueError, match='supported inference'):
        validate_trajectory_evidence_verdict(item, {
            'path_score': .1,
            'hallucination_score': 0,
            'supported_inferences': [],
            'unsupported_or_fabricated_claims': [],
            'reason': 'some support',
        })
    with pytest.raises(ValueError, match='unsupported/fabricated'):
        validate_trajectory_evidence_verdict(item, {
            'path_score': 0,
            'hallucination_score': .1,
            'supported_inferences': [],
            'unsupported_or_fabricated_claims': [],
            'reason': 'some hallucination',
        })


def test_evidence_verdict_allows_uncited_fabricated_claim_but_not_uncited_support():
    # Rubric: supported or contradicted judgments cite observations; a fabricated claim no observation supports has
    # nothing to cite. Claude Opus 5.5 returns [] there (2026-10-15 loop crash); refs that are given must still exist.
    item = {'trajectory_evidence': {'refs': [{'ref_id': 'm1'}, {'ref_id': 'm2'}]}}
    base = {'path_score': .5, 'hallucination_score': .4, 'reason': 'partly grounded',
            'supported_inferences': [{'claim': 'p95 rose', 'evidence_refs': ['m1'], 'explanation': 'tool output'}]}
    verdict = validate_trajectory_evidence_verdict(item, {**base, 'unsupported_or_fabricated_claims': [
        {'claim': 'config was changed', 'evidence_refs': [], 'severity': 'major', 'explanation': 'never observed'}]})
    assert verdict['unsupported_or_fabricated_claims'][0]['evidence_refs'] == []
    with pytest.raises(ValueError, match='nonempty evidence_refs'):
        validate_trajectory_evidence_verdict(item, {**base, 'supported_inferences': [
            {'claim': 'p95 rose', 'evidence_refs': [], 'explanation': 'tool output'}],
            'unsupported_or_fabricated_claims': [
            {'claim': 'x', 'evidence_refs': ['m2'], 'severity': 'minor', 'explanation': 'contradicted'}]})
    with pytest.raises(ValueError, match='unavailable evidence refs'):
        validate_trajectory_evidence_verdict(item, {**base, 'unsupported_or_fabricated_claims': [
            {'claim': 'x', 'evidence_refs': ['m9'], 'severity': 'minor', 'explanation': 'contradicted'}]})


def _evidence_outcome(success: bool, hallucination: float, *, failure: bool = False) -> EpisodeOutcome:
    return EpisodeOutcome("completed", success, success, 1000, 10, root_precision=1.0 if success else 0.5,
                          root_recall=1.0, model_failure=failure, mechanism_score=1.0 if success else 0.5,
                          evidence_judge_status="completed", path_score=1.0, hallucination_score=hallucination)


def test_success_hallucination_weight_penalizes_fabrication_on_successes_only():
    outcomes = [_evidence_outcome(True, 1.0), _evidence_outcome(True, 0.0), _evidence_outcome(False, 1.0)]
    v2 = trajectory_rewards(outcomes, token_budget=1000, turn_budget=10, efficiency_weight=0.2,
                            path_weight=0.05, hallucination_weight=0.1)
    v3 = trajectory_rewards(outcomes, token_budget=1000, turn_budget=10, efficiency_weight=0.2,
                            path_weight=0.05, hallucination_weight=0.1, success_hallucination_weight=0.3)
    assert v3[0].item() == pytest.approx(v2[0].item() - 0.2)   # fabricated success: -.3 instead of -.1
    assert v3[1].item() == pytest.approx(v2[1].item())         # clean success unchanged
    assert v3[2].item() == pytest.approx(v2[2].item())         # partial keeps -.1
    # success-first ordering: the worst success stays above the best partial, and a fabricated partial above -0.2
    assert v3[0].item() >= 0.5 > 0.35 >= v3[2].item() > -0.2


def test_success_hallucination_weight_is_bounded():
    with pytest.raises(ValueError):
        trajectory_rewards([_evidence_outcome(True, 0.5)], token_budget=1000, turn_budget=10,
                           path_weight=0.05, hallucination_weight=0.1, success_hallucination_weight=0.31)


def test_partial_weights_shift_partial_credit_to_the_mechanism():
    def outcome(mech, precision=1.0):
        return EpisodeOutcome("completed", precision == 1.0, mech == 1.0, 1000, 10, root_precision=precision, root_recall=1.0,
                              mechanism_score=mech, evidence_judge_status="completed", path_score=0.0, hallucination_score=0.0)
    outcomes = [outcome(1.0), outcome(0.5), outcome(0.0), outcome(1.0, precision=0.5)]
    kw = dict(token_budget=1000, turn_budget=10, efficiency_weight=0.2, path_weight=0.05, hallucination_weight=0.1,
              success_hallucination_weight=0.3)
    v3 = trajectory_rewards(outcomes, **kw)
    v4 = trajectory_rewards(outcomes, **kw, partial_root_weight=0.1, partial_mechanism_weight=0.2)
    assert v4[0].item() == pytest.approx(v3[0].item())                        # successes unchanged
    assert v3[1].item() == pytest.approx(0.25) and v4[1].item() == pytest.approx(0.2)
    assert v3[2].item() == pytest.approx(0.2) and v4[2].item() == pytest.approx(0.1)
    assert (v4[1] - v4[2]).item() == pytest.approx(2 * (v3[1] - v3[2]).item())  # mechanism step doubles
    assert v4[3].item() == pytest.approx(0.25 * 0.3)                          # right target + extra cause, mechanism right
    assert min(v4[0].item(), v3[0].item()) >= 0.5 > 0.3 >= max(v4[1:].tolist())


def test_partial_weights_are_bounded():
    with pytest.raises(ValueError):
        trajectory_rewards([_evidence_outcome(True, 0.0)], token_budget=1000, turn_budget=10,
                           partial_root_weight=0.15, partial_mechanism_weight=0.2)
    with pytest.raises(ValueError):
        trajectory_rewards([_evidence_outcome(True, 0.0)], token_budget=1000, turn_budget=10, partial_root_weight=-0.1)


def test_depth_weight_rewards_grounded_depth_on_successes_only():
    def outcome(success, depth):
        return EpisodeOutcome("completed", success, success, 1000, 10, root_precision=1.0, root_recall=1.0,
                              mechanism_score=1.0 if success else 0.5, evidence_judge_status="completed",
                              path_score=0.0, hallucination_score=0.0, causal_depth=depth)
    kw = dict(token_budget=1000, turn_budget=10, efficiency_weight=0.2, path_weight=0.05, hallucination_weight=0.1,
              success_hallucination_weight=0.3, partial_root_weight=0.1, partial_mechanism_weight=0.2)
    base = trajectory_rewards([outcome(True, 1.0), outcome(False, None)], **kw)
    deep = trajectory_rewards([outcome(True, 1.0), outcome(True, 1 / 3), outcome(False, None)], **kw, depth_weight=0.1)
    assert deep[0].item() == pytest.approx(base[0].item() + 0.1)
    assert deep[1].item() == pytest.approx(base[0].item() + 0.1 / 3)
    assert deep[2].item() == pytest.approx(base[1].item())                    # partial: no depth term
    with pytest.raises(ValueError):
        trajectory_rewards([outcome(True, None)], **kw, depth_weight=0.1)
    with pytest.raises(ValueError):
        trajectory_rewards([outcome(True, 1.0)], **kw, depth_weight=0.2)


def test_fabricated_success_cap_puts_clean_successes_first():
    def outcome(success, majors, hall=0.0, precision=1.0, mech=None):
        return EpisodeOutcome("completed", success, success, 1000, 10, root_precision=precision, root_recall=1.0,
                              mechanism_score=1.0 if success else (mech or 0.5), evidence_judge_status="completed",
                              path_score=1.0, hallucination_score=hall, causal_depth=1.0 if success else None,
                              major_claims=majors)
    kw = dict(token_budget=1000, turn_budget=10, efficiency_weight=0.2, path_weight=0.05, hallucination_weight=0.1,
              success_hallucination_weight=0.3, partial_root_weight=0.1, partial_mechanism_weight=0.2, depth_weight=0.1)
    r = trajectory_rewards([outcome(True, 0, 0.2), outcome(True, 1, 0.2), outcome(True, 2, 0.8), outcome(False, 0)],
                           **kw, fabricated_success_cap=0.35)
    clean, light, heavy, partial = r.tolist()
    # the cap applies before the evidence terms: .35 + .05 path - .3 * hallucination
    assert light == pytest.approx(0.35 + 0.05 - 0.3 * 0.2) and heavy == pytest.approx(0.35 + 0.05 - 0.3 * 0.8)
    assert clean >= 0.5 > light > partial > heavy > -0.2             # partial: .1 + .2 * .5 + .05 path = .25
    base = trajectory_rewards([outcome(True, 1, 0.8)], **kw)
    assert base[0].item() > 0.5                                   # without the cap the fabricated success stays >= .5
    with pytest.raises(ValueError):
        trajectory_rewards([outcome(True, None)], **kw, fabricated_success_cap=0.35)
    with pytest.raises(ValueError):
        trajectory_rewards([outcome(True, 0)], **kw, fabricated_success_cap=0.4)


def test_major_claim_penalty_ranks_clean_successes_first_and_is_bounded():
    def outcome(majors, hall=0.2):
        return EpisodeOutcome("completed", True, True, 1000, 10, root_precision=1.0, root_recall=1.0, mechanism_score=1.0,
                              evidence_judge_status="completed", path_score=1.0, hallucination_score=hall,
                              causal_depth=1.0, major_claims=majors)
    kw = dict(token_budget=1000, turn_budget=10, efficiency_weight=0.2, path_weight=0.05, hallucination_weight=0.1,
              success_hallucination_weight=0.3, partial_root_weight=0.1, partial_mechanism_weight=0.2, depth_weight=0.1)
    base = trajectory_rewards([outcome(0)], **kw)[0].item()
    r = trajectory_rewards([outcome(0), outcome(1), outcome(2), outcome(5)], **kw, major_claim_penalty=0.1).tolist()
    assert r[0] == pytest.approx(base)
    assert r[1] == pytest.approx(base - 0.1) and r[2] == pytest.approx(base - 0.2) and r[3] == pytest.approx(base - 0.3)
    with pytest.raises(ValueError):
        trajectory_rewards([outcome(None)], **kw, major_claim_penalty=0.1)
    with pytest.raises(ValueError):
        trajectory_rewards([outcome(0)], **kw, major_claim_penalty=0.2)
