from __future__ import annotations

import runpy
from pathlib import Path

from rca_lab.provenance import file_sha256
from rca_lab.train.checkpoint import tokenizer_artifact_identity

module = runpy.run_path(
    Path(__file__).parents[1] / "scripts" / "materialize_prime_training_cache.py"
)
build_cache_manifest = module["build_cache_manifest"]


def test_cache_manifest_binds_dense_cache_to_fp8_source(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("checkpoint_format: compressed_tensors_fp8_block\n", encoding="utf-8")
    output = tmp_path / "cache"
    output.mkdir()
    (output / "config.json").write_text("{}\n", encoding="utf-8")
    (output / "model.safetensors.index.json").write_text('{"weight_map": {}}\n', encoding="utf-8")
    (output / "tokenizer.json").write_text("{}\n", encoding="utf-8")
    (output / "tokenizer_config.json").write_text("{}\n", encoding="utf-8")
    (output / "chat_template.jinja").write_text("{{ messages }}\n", encoding="utf-8")
    source = {"model_revision": "8" * 40, "source_weight_dtype": "float8_e4m3fn"}

    manifest = build_cache_manifest(
        config_path=config,
        output=output,
        source=source,
        removed_weight_scale_tensors=416,
    )

    assert manifest["source"] == source
    assert manifest["source_config_sha256"] == file_sha256(config)
    assert manifest["cache_tokenizer_sha256"] == tokenizer_artifact_identity(output)
    assert manifest["cache_weight_dtype"] == "bfloat16"
    assert manifest["removed_weight_scale_tensors"] == 416
    assert manifest["disposable"] is True
