import json

import pytest

from rca_lab.train.checkpoint import (
    TrainingCheckpointContract,
    file_sha256,
    tokenizer_artifact_identity,
    verify_training_checkpoint,
)


def contract(tmp_path, block_size):
    (tmp_path / "config.json").write_text(json.dumps({"quantization_config": {
        "quant_method": "fp8", "fmt": "e4m3", "weight_block_size": block_size}}))
    (tmp_path / "model.safetensors.index.json").write_text("{}")
    for name in ["tokenizer.json", "tokenizer_config.json", "chat_template.jinja"]:
        (tmp_path / name).write_text("fixture")
    return TrainingCheckpointContract(
        model_name=str(tmp_path), tokenizer_name=str(tmp_path),
        checkpoint_format="finegrained_fp8_block", model_revision="a" * 40,
        model_config_sha256=file_sha256(tmp_path / "config.json"),
        model_index_sha256=file_sha256(tmp_path / "model.safetensors.index.json"),
        tokenizer_sha256=tokenizer_artifact_identity(tmp_path), dequantize_for_training=True,
    )


def test_finegrained_fp8_identity_and_format(tmp_path):
    assert verify_training_checkpoint(contract(tmp_path, [128, 128]))["dequantize_for_training"]


def test_declared_finegrained_block_size_must_match(tmp_path):
    with pytest.raises(ValueError, match="finegrained"):
        verify_training_checkpoint(contract(tmp_path, [64, 128]))
