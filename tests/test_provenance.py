from pathlib import Path

import pytest

from rca_lab.provenance import model_artifact_identity, resolve_model_identity


def test_remote_model_identity_accepts_explicit_digest() -> None:
    digest = "a" * 64
    assert resolve_model_identity("/remote/model", digest) == digest


def test_local_model_identity_cross_checks_explicit_digest(tmp_path: Path) -> None:
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text('{"model_type":"test"}\n')
    actual = model_artifact_identity(str(model))
    assert resolve_model_identity(str(model), actual) == actual
    with pytest.raises(ValueError, match="mismatch"):
        resolve_model_identity(str(model), "a" * 64)


@pytest.mark.parametrize("digest", ["", "xyz", "a" * 63])
def test_unreadable_model_rejects_missing_or_malformed_digest(digest: str) -> None:
    with pytest.raises(ValueError):
        resolve_model_identity("/remote/model", digest)
