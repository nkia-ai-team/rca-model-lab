import math

import pytest
import torch

from rca_lab.train.grpo import grpo_loss
from rca_lab.train.grpo_correction import corrected_grpo_loss


def inputs():
    current = torch.tensor([[-1., -2., -3.], [-2., -1., -3.]], requires_grad=True)
    proximal = current.detach().clone().requires_grad_()
    behavior = current.detach().clone().requires_grad_()
    reference = (current.detach() - .1).requires_grad_()
    mask = torch.tensor([[True, True, False], [True, True, True]])
    adv = torch.tensor([.5, -.5], requires_grad=True)
    return current, proximal, behavior, reference, mask, adv


def test_identical_reduces_to_core_and_detaches_every_other_input():
    c, p, b, ref, mask, adv = inputs()
    actual = corrected_grpo_loss(c, p, b, ref, mask, adv)
    expected = grpo_loss(c, p, ref, mask, adv)
    torch.testing.assert_close(actual.loss, expected.loss)
    g1 = torch.autograd.grad(actual.loss, c, retain_graph=True)[0]
    g2 = torch.autograd.grad(expected.loss, c, retain_graph=True)[0]
    torch.testing.assert_close(g1, g2)
    actual.loss.backward()
    assert c.grad is not None
    assert all(x.grad is None for x in (p, b, ref, adv))
    assert all(not x.requires_grad for x in actual.diagnostics.values())


@pytest.mark.parametrize('adv,ratio,expected', [(1., 1.5, -1.2), (-1., .5, .8),
                                               (1., .5, -.5), (-1., 1.5, 1.5)])
def test_signed_clipping(adv, ratio, expected):
    p = torch.tensor([[-2.]])
    c = p + math.log(ratio)
    out = corrected_grpo_loss(c, p, p, p, torch.ones_like(p, dtype=torch.bool),
                              torch.tensor([adv]), kl_beta=0)
    assert out.loss.item() == pytest.approx(expected)


def test_initial_ratio_is_one_with_nonunit_correction_and_exact_cap_diagnostics():
    p = torch.tensor([[-3., -3., -3.]])
    raw = torch.tensor([[.5, 1., 4.]])
    b = p - raw.log()
    out = corrected_grpo_loss(p, p, b, p, torch.ones_like(p, dtype=torch.bool),
                              torch.tensor([1.]), kl_beta=0)
    assert out.loss.item() == pytest.approx(-3.5 / 3)
    d = out.diagnostics
    assert d['ppo_ratio_mean'].item() == 1
    assert d['ppo_clip_fraction'].item() == 0
    assert d['cap_fraction'].item() == pytest.approx(1 / 3)
    assert d['truncated_weight_mass'].item() == pytest.approx(2.)
    assert d['truncated_weight_mass_fraction'].item() == pytest.approx(2 / 5.5)
    assert d['raw_ess'].item() == pytest.approx(5.5**2 / 17.25)
    assert d['capped_ess'].item() == pytest.approx(3.5**2 / 5.25)


def test_masked_nan_allowed_but_valid_nan_rejected():
    args = list(inputs())
    for i in range(4):
        args[i] = args[i].detach().clone()
        args[i][0, 2] = float('nan')
    assert torch.isfinite(corrected_grpo_loss(*args).loss)
    for i in range(4):
        bad = list(args)
        bad[i] = bad[i].clone()
        bad[i][0, 0] = float('nan')
        with pytest.raises(ValueError):
            corrected_grpo_loss(*bad)


def test_turn_microbatch_weighted_gradients_equal_complete_episode():
    c = torch.tensor([[-2., -2.2, -1.8, -2.1, -2.3]], requires_grad=True)
    p = torch.full_like(c, -2.)
    b = p - torch.tensor([[.1, -.1, .2, -.2, 0.]])
    ref = torch.full_like(c, -2.1)
    mask = torch.ones_like(c, dtype=torch.bool)
    adv = torch.tensor([.4])
    whole = corrected_grpo_loss(c, p, b, ref, mask, adv).loss
    whole_grad = torch.autograd.grad(whole, c)[0]
    partial = 0
    for sl in (slice(0, 2), slice(2, 5)):
        n = c[:, sl].numel()
        partial = partial + corrected_grpo_loss(c[:, sl], p[:, sl], b[:, sl],
                                                ref[:, sl], mask[:, sl], adv).loss * n / 5
    part_grad = torch.autograd.grad(partial, c)[0]
    torch.testing.assert_close(whole, partial)
    torch.testing.assert_close(whole_grad, part_grad)


@pytest.mark.parametrize('cap', [0, -1, float('inf'), float('nan')])
def test_invalid_cap(cap):
    with pytest.raises(ValueError):
        corrected_grpo_loss(*inputs(), importance_cap=cap)


def test_overflow_and_shape_fail_closed():
    args = list(inputs())
    args[2] = torch.full_like(args[2], -1000.)
    with pytest.raises(ValueError, match='overflow'):
        corrected_grpo_loss(*args)
    args[2] = torch.zeros(1)
    with pytest.raises(ValueError, match='shape'):
        corrected_grpo_loss(*args)


def test_proximal_snapshot_unchanged_across_updates():
    c, p, b, ref, mask, adv = inputs()
    initial = p.detach().clone()
    opt = torch.optim.SGD([c], lr=.01)
    for _ in range(2):
        opt.zero_grad()
        corrected_grpo_loss(c, p, b, ref, mask, adv).loss.backward()
        opt.step()
    torch.testing.assert_close(p, initial)
    assert not torch.equal(c.detach(), initial)
