from __future__ import annotations

import json
from pathlib import Path

import pytest

from rca_lab.provenance import file_sha256
from rca_lab.train.checkpoint import (
    TrainingCheckpointContract,
    save_training_tokenizer_artifacts,
    tokenizer_artifact_identity,
    verify_training_checkpoint,
)


def _fp8_contract(tmp_path: Path) -> TrainingCheckpointContract:
    model = tmp_path / "model"
    tokenizer = tmp_path / "tokenizer"
    model.mkdir()
    tokenizer.mkdir()
    (model / "config.json").write_text(
        json.dumps(
            {
                "quantization_config": {
                    "quant_method": "compressed-tensors",
                    "quantization_status": "compressed",
                    "config_groups": {
                        "FP8_BLOCK": {
                            "weights": {
                                "type": "float",
                                "num_bits": 8,
                                "strategy": "block",
                                "block_structure": [128, 128],
                            }
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    (model / "model.safetensors.index.json").write_text('{"weight_map": {}}\n', encoding="utf-8")
    (tokenizer / "tokenizer.json").write_text('{"version":"1.0"}\n', encoding="utf-8")
    (tokenizer / "tokenizer_config.json").write_text("{}\n", encoding="utf-8")
    (tokenizer / "chat_template.jinja").write_text("{{ messages }}\n", encoding="utf-8")
    return TrainingCheckpointContract(
        model_name=str(model),
        tokenizer_name=str(tokenizer),
        checkpoint_format="compressed_tensors_fp8_block",
        model_revision="8" * 40,
        model_config_sha256=file_sha256(model / "config.json"),
        model_index_sha256=file_sha256(model / "model.safetensors.index.json"),
        tokenizer_sha256=tokenizer_artifact_identity(tokenizer),
        dequantize_for_training=True,
    )


def test_fp8_training_contract_verifies_checkpoint_and_tokenizer(tmp_path: Path) -> None:
    contract = _fp8_contract(tmp_path)

    evidence = verify_training_checkpoint(contract)

    assert evidence["checkpoint_format"] == "compressed_tensors_fp8_block"
    assert evidence["source_weight_dtype"] == "float8_e4m3fn"
    assert evidence["training_compute_dtype"] == "bfloat16"
    assert evidence["dequantize_for_training"] is True
    assert evidence["model_revision"] == "8" * 40


def test_fp8_training_requires_explicit_dequantization() -> None:
    with pytest.raises(ValueError, match="dequantize_for_training"):
        TrainingCheckpointContract(
            model_name="RedHatAI/Muse-Glimmer-30B-FP8-block",
            tokenizer_name="tokenizer",
            checkpoint_format="compressed_tensors_fp8_block",
            model_revision="8" * 40,
            model_config_sha256="a" * 64,
            model_index_sha256="b" * 64,
            tokenizer_sha256="c" * 64,
        )


def test_checkpoint_verification_rejects_changed_model_files(tmp_path: Path) -> None:
    contract = _fp8_contract(tmp_path)
    (Path(contract.model_name) / "config.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="config SHA-256"):
        verify_training_checkpoint(contract)


def test_checkpoint_verification_rejects_changed_chat_template(tmp_path: Path) -> None:
    contract = _fp8_contract(tmp_path)
    (Path(contract.tokenizer_name) / "chat_template.jinja").write_text(
        "changed\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="tokenizer SHA-256"):
        verify_training_checkpoint(contract)


def test_saved_tokenizer_preserves_identity_critical_source_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = _fp8_contract(tmp_path)
    output = tmp_path / "output"

    class FakeTokenizer:
        def save_pretrained(self, destination: Path) -> None:
            destination.mkdir()
            (destination / "tokenizer.json").write_text("normalized\n", encoding="utf-8")
            (destination / "tokenizer_config.json").write_text(
                '{"is_local": true}\n', encoding="utf-8"
            )
            (destination / "chat_template.jinja").write_text("normalized\n", encoding="utf-8")

    monkeypatch.setattr(
        "rca_lab.train.checkpoint.load_training_tokenizer", lambda _contract: FakeTokenizer()
    )

    identity = save_training_tokenizer_artifacts(contract, output)

    assert identity == contract.tokenizer_sha256
    for name in ("tokenizer.json", "tokenizer_config.json", "chat_template.jinja"):
        assert (output / name).read_bytes() == (Path(contract.tokenizer_name) / name).read_bytes()
