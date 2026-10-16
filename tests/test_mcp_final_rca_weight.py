"""Final RCA emphasis changes relative turn loss, not case exposure or target masks."""
from pathlib import Path

import pytest

from rca_lab.train.mcp_sft import build_examples, load_config

BASE = Path("configs/sft/gemma4-26b-a4b-polestar43-20261013.yaml")


def config(**overrides):
    base = load_config(BASE)
    return type(base).model_validate({**base.model_dump(), **overrides})


def rows():
    def message(name, **extra):
        return {"role": "assistant", "tool_calls": [{"function": {"name": name}}], **extra}
    return [
        {"case_id": "a", "run": 1, "tools": [], "messages": [message("lookup"), message("lookup"), message("submit_rca")]},
        {"case_id": "a", "run": 2, "tools": [], "messages": [message("lookup"), message("submit_rca")]},
        {"case_id": "b", "run": 1, "tools": [], "messages": [message("lookup"), message("submit_rca")]},
    ]


@pytest.fixture(autouse=True)
def tokenizer_stub(monkeypatch):
    def encode(tokenizer, messages, tools, **kwargs):
        return [
            {"input_ids": [1, 2, 3], "labels": [-100, 2, 3], "token_weight": [0, 1, 1], "original_message_index": i}
            for i, m in enumerate(messages) if m.get("supervise") is not False
        ]
    monkeypatch.setattr("rca_lab.train.mcp_sft.tokenize_turns", encode)


def test_default_exact_legacy_case_weights():
    examples = build_examples(config(), None, rows())
    assert [e["sample_weight"] for e in examples] == pytest.approx([0.7] * 5 + [1.75] * 2)


def test_final_emphasis_keeps_all_turns_case_mass_and_masks():
    baseline = build_examples(config(), None, rows())
    changed, manifest = build_examples(config(final_rca_loss_weight=3), None, rows(), return_manifest=True)
    assert len(changed) == len(baseline) == 7
    for old, new in zip(baseline, changed, strict=True):
        for field in ("input_ids", "labels", "token_weight", "original_message_index"):
            assert new[field] == old[field]
    for case in ("a", "b"):
        selected = [e for e in changed if e["case_id"] == case]
        assert sum(e["sample_weight"] for e in selected) == pytest.approx(3.5)
        final = next(e for e in selected if e["is_final_rca"])
        other = next(e for e in selected if not e["is_final_rca"])
        assert final["sample_weight"] / other["sample_weight"] == pytest.approx(3)
    audit = manifest["loss_weight_audit"]
    assert audit["sample_weight_sum"] == pytest.approx(7)
    assert audit["final_rca_turns"] == 3
    assert audit["final_rca_weight_fraction"] == pytest.approx((2/3 + 3/4)/2)


def test_unbalanced_normalizes_global_mass():
    examples = build_examples(config(case_balanced_loss=False, final_rca_loss_weight=3), None, rows())
    assert sum(e["sample_weight"] for e in examples) == pytest.approx(7)
    assert [e["sample_weight"] for e in examples] == pytest.approx([7/13, 7/13, 21/13, 7/13, 21/13, 7/13, 21/13])


def test_detection_uses_tool_schema_not_text_or_last_position():
    data = rows()[:1]
    data[0]["messages"] += [
        {"role": "assistant", "content": "submit_rca", "tool_calls": []},
        {"role": "assistant", "tool_calls": [{"function": {"name": "submit_rca"}}], "supervise": False},
    ]
    examples = build_examples(config(final_rca_loss_weight=3), None, data)
    assert [e["is_final_rca"] for e in examples] == [False, False, True, False]


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_invalid_multiplier_rejected(value):
    with pytest.raises(ValueError):
        config(final_rca_loss_weight=value)


@pytest.mark.parametrize("extra", [{"batch_size": 2}, {"sequence_mode": "episode", "chat_template_kwargs": {}}, {"method": "rl"}])
def test_unsupported_reductions_rejected(extra):
    with pytest.raises(ValueError, match="final_rca_loss_weight"):
        config(final_rca_loss_weight=3, **extra)
