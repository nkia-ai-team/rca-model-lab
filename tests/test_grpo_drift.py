from copy import deepcopy
import math

import pytest

from rca_lab.train.grpo_drift import build_drift_report


def episodes():
    return [dict(episode_id=str(i), turns=[dict(old_logprobs=[-2., -3.],
                  proximal_logprobs=[-2., -3.], prompt_tokens=10)]) for i in range(4)]


def test_equal_probabilities_pass_budgets_but_never_autoapprove():
    out = build_drift_report(episodes())
    assert out['all_experimental_budgets_pass']
    assert out['status'] == 'pending_review'
    assert out['optimizer_approved'] is False
    assert out['trajectory_manifest_coverage'] == 'requires_upstream_verification'
    assert out['context_bins']['0-8k']['tokens'] == 8
    assert out['episodes'][0]['summary']['raw_normalized_ess'] == 1


def test_extreme_max_alone_is_not_gate_and_exact_mass_budget():
    eps = episodes()
    # Constant ratio gives ESS1, but ratio4 truncation loses half the mass.
    for ep in eps:
        ep['turns'][0]['proximal_logprobs'] = [-2+math.log(4), -3+math.log(4)]
    out = build_drift_report(eps)
    s = out['episodes'][0]['summary']
    assert s['raw_normalized_ess'] == pytest.approx(1.)
    assert s['truncated_mass_fraction'] == pytest.approx(.5)
    assert not out['all_experimental_budgets_pass']
    for ep in eps:
        ep['turns'][0]['proximal_logprobs'] = [-2.2, -3.2]
    out = build_drift_report(eps)
    assert out['all_experimental_budgets_pass']  # still pending signed-drift review
    assert out['systematic_and_context_growth_review'] == 'pending_review'


def test_each_episode_not_just_group_is_checked():
    eps = episodes()
    eps[0]['turns'][0]['proximal_logprobs'] = [-.7, -10.]
    out = build_drift_report(eps)
    assert not out['episodes'][0]['experimental_budgets_pass']
    assert out['episodes'][1]['experimental_budgets_pass']
    assert not out['all_experimental_budgets_pass']


def test_bins_and_multiple_turns_account_every_token():
    eps = episodes()
    for i, ep in enumerate(eps):
        ep['turns'][0]['prompt_tokens'] = [10, 8192, 32768, 65536][i]
        ep['turns'].append(deepcopy(ep['turns'][0]))
    out = build_drift_report(eps)
    assert len(out['context_bins']) == 4
    assert sum(v['tokens'] for v in out['context_bins'].values()) == 16
    assert all(ep['summary']['tokens'] == 4 for ep in out['episodes'])


def control():
    return dict(episode_id='0', turn_index=0, generation_logprobs=[-2., -3.],
                prefill_logprobs=[-2.216, -3.], prompt_tokens=10)


def test_serving_control_comparison_and_token_identity():
    eps = episodes()
    out = build_drift_report(eps, [control()])
    c = out['episodes'][0]['turns'][0]['serving_control']
    assert c['serving_self']['absolute_quantiles']['1.0'] == pytest.approx(.216)
    assert c['comparison_status'] == 'pending_review'
    assert not c['token_identity_verified']
    eps[0]['turns'][0]['completion_token_ids'] = [7, 8]
    ctrl = control() | {'completion_token_ids': [7, 8]}
    out = build_drift_report(eps, [ctrl])
    assert out['episodes'][0]['turns'][0]['serving_control']['token_identity_verified']
    ctrl['completion_token_ids'] = [7, 9]
    with pytest.raises(ValueError, match='identity'):
        build_drift_report(eps, [ctrl])


@pytest.mark.parametrize('mutation', ['nan', 'missing', 'shape', 'duplicate', 'empty'])
def test_invalid_group_fails_closed(mutation):
    eps = episodes()
    if mutation == 'nan':
        eps[0]['turns'][0]['proximal_logprobs'][0] = float('nan')
    elif mutation == 'missing':
        eps.pop()
    elif mutation == 'shape':
        eps[0]['turns'][0]['old_logprobs'].pop()
    elif mutation == 'duplicate':
        eps[0]['episode_id'] = eps[1]['episode_id']
    else:
        eps[0]['turns'] = []
    with pytest.raises(ValueError):
        build_drift_report(eps)


@pytest.mark.parametrize('mutation', ['generation', 'prefill', 'unknown', 'duplicate', 'context'])
def test_invalid_controls_rejected(mutation):
    ctrl = control()
    controls = [ctrl]
    if mutation == 'generation':
        ctrl['generation_logprobs'][0] = -2.1
    elif mutation == 'prefill':
        ctrl['prefill_logprobs'].pop()
    elif mutation == 'unknown':
        ctrl['turn_index'] = 99
    elif mutation == 'context':
        ctrl['prompt_tokens'] = 99
    else:
        controls.append(deepcopy(ctrl))
    with pytest.raises(ValueError):
        build_drift_report(episodes(), controls)


def test_inputs_immutable_and_json_serializable():
    import json
    eps, controls = episodes(), [control()]
    before = deepcopy((eps, controls))
    json.dumps(build_drift_report(eps, controls), allow_nan=False)
    assert (eps, controls) == before
