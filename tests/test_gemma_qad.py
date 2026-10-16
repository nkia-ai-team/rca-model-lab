import pytest

from rca_lab.train.gemma_qad import masked_kl, quant_config, selected_hidden, validate_train_rows


def test_qad_case_identity_excludes_heldout_and_f40():
    cfg = {
        "train_case_ids": ["case-f40-a"],
        "forbidden_case_ids": ["sealed-a"],
        "forbidden_scenario_ids": ["F40"],
    }
    with pytest.raises(ValueError, match="excluded"):
        validate_train_rows([{"case_id": "case-f40-a"}], cfg)
    cfg["train_case_ids"] = ["train-a"]
    with pytest.raises(ValueError, match="exactly"):
        validate_train_rows([{"case_id": "sealed-a"}], cfg)
    validate_train_rows([{"case_id": "train-a"}], cfg)


def test_quant_config_covers_fused_experts_and_exact_exclusions():
    recipe = {"ignore": ["lm_head", "model.layers.0.router.proj"]}
    cfg = quant_config(recipe)
    assert any(x["quantizer_name"] == "*weight_quantizers.*" for x in cfg["quant_cfg"])
    assert cfg["quant_cfg"][-1] == {
        "quantizer_name": "model.layers.0.router.proj.*",
        "enable": False,
    }
    assert not any("kv" in x["quantizer_name"] for x in cfg["quant_cfg"])


def test_chunk_kl_hidden_gradient_matches_full_and_masks_causal_shift():
    torch = pytest.importorskip("torch")
    from types import SimpleNamespace

    torch.manual_seed(12)
    source = torch.randn(1, 7, 4, requires_grad=True)

    class Backbone:
        def __call__(self, **kwargs):
            return SimpleNamespace(last_hidden_state=source)

    model = SimpleNamespace(model=Backbone())
    example = {"input_ids": [0] * 7, "labels": [-100, -100, 2, -100, 4, 5, -100]}
    hidden = selected_hidden(model, example, "cpu")
    assert torch.equal(hidden, source[0, [1, 3, 4]])
    projection = torch.randn(4, 11)
    target = torch.randn(3, 11).log_softmax(-1)
    full = masked_kl(hidden @ projection, target).mean()
    expected = torch.autograd.grad(full, source, retain_graph=True)[0]
    leaf = hidden.detach().requires_grad_(True)
    for i in range(len(leaf)):
        (masked_kl(leaf[i : i + 1] @ projection, target[i : i + 1]).sum() / len(leaf)).backward()
    hidden.backward(leaf.grad)
    torch.testing.assert_close(source.grad, expected)
    assert source.grad[0, [0, 2, 5, 6]].count_nonzero() == 0


def test_kl_identity_and_teacher_gradient_frozen():
    torch = pytest.importorskip("torch")
    logits = torch.tensor([[0.2, 1.3, -0.8]], requires_grad=True)
    target = logits.detach().log_softmax(-1)
    value = masked_kl(logits, target)
    assert abs(value.item()) < 1e-6
    value.sum().backward()
    assert target.grad is None
    with pytest.raises(ValueError, match="temperature"):
        masked_kl(logits, target, temperature=2)


def test_export_recipe_resolves_wildcard_exclusions_and_rejects_other_precision():
    from rca_lab.train.gemma_qad import validate_export_recipe

    quant = {
        "quant_method": "modelopt",
        "quant_algo": "FP8_PER_CHANNEL_PER_TOKEN",
        "ignore": ["lm_head", "model.layers.0.router*"],
    }
    recipe = {"ignore": ["lm_head", "model.layers.0.router.proj"]}
    names = ["lm_head", "model.layers.0.router.proj", "model.layers.0.self_attn.q_proj"]
    validate_export_recipe(quant, recipe, names)
    with pytest.raises(ValueError, match="exclusion drift"):
        validate_export_recipe({**quant, "ignore": ["*"]}, recipe, names)
    with pytest.raises(ValueError, match="algorithm"):
        validate_export_recipe({**quant, "quant_algo": "FP8"}, recipe, names)
