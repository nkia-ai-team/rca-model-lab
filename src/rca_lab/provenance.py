"""Immutable artifact fingerprints shared by training and evaluation workflows."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

_MODEL_IDENTITY_FILES = (
    "config.json",
    "model.safetensors.index.json",
    "adapter_config.json",
    "training_manifest.json",
    "merge_manifest.json",
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def model_artifact_identity(value: str) -> str:
    path = Path(value)
    digest = hashlib.sha256()
    if path.is_file():
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
        return digest.hexdigest()
    if not path.is_dir():
        return ""
    selected = [path / name for name in _MODEL_IDENTITY_FILES if (path / name).is_file()]
    if not selected:
        return ""
    for item in selected:
        digest.update(item.name.encode())
        digest.update(item.read_bytes())
    return digest.hexdigest()


def resolve_model_identity(value: str, provided_sha256: str = "") -> str:
    """Resolve local artifacts directly or verify an explicit remote digest."""
    provided = provided_sha256.casefold()
    if provided and not _SHA256.fullmatch(provided):
        raise ValueError("model artifact sha256 must be 64 lowercase hexadecimal characters")
    local = model_artifact_identity(value)
    if local and provided and local != provided:
        raise ValueError(f"model artifact sha256 mismatch: local={local} provided={provided}")
    resolved = local or provided
    if not value or not resolved:
        raise ValueError("model artifact requires a readable path or explicit remote sha256")
    return resolved


def case_set_identity(case_root: Path, cases: list[str]) -> str:
    """Fingerprint case metadata plus the complete relative-path/size inventory."""
    digest = hashlib.sha256()
    for case in sorted(cases):
        case_dir = case_root / case
        meta = case_dir / "meta.json"
        if not meta.is_file():
            return ""
        digest.update(case.encode())
        digest.update(meta.read_bytes())
        for path in sorted(item for item in case_dir.rglob("*") if item.is_file()):
            relative = path.relative_to(case_dir)
            digest.update(str(relative).encode())
            digest.update(str(path.stat().st_size).encode())
    return digest.hexdigest()
