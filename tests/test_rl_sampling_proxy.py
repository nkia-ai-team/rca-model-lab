from __future__ import annotations

import pytest

from rca_lab.openai_proxy import prepare_chat_payload


def test_prepare_chat_payload_pins_sampling_and_sft_template_contract() -> None:
    payload = prepare_chat_payload(
        {"model": "rca-actor", "chat_template_kwargs": {"custom": True}},
        temperature=0.7,
        seed=101,
        reasoning_strength="low",
    )

    assert payload["temperature"] == 0.7
    assert payload["seed"] == 101
    assert payload["chat_template_kwargs"] == {
        "custom": True,
        "reasoning_strength": "low",
    }


def test_prepare_chat_payload_rejects_non_object_template_kwargs() -> None:
    with pytest.raises(TypeError, match="chat_template_kwargs must be an object"):
        prepare_chat_payload(
            {"chat_template_kwargs": "low"},
            temperature=1.0,
            seed=1,
            reasoning_strength="low",
        )
