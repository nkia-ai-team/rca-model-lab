"""Immutable artifact fingerprints shared by training and evaluation workflows."""

from __future__ import annotations

import hashlib
from pathlib import Path

_MODEL_IDENTITY_FILES = (
    "config.json",
    "model.safetensors.index.json",
    "adapter_config.json",
    "merge_manifest.json",
)


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

