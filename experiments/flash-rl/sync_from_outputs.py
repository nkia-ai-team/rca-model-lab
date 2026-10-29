"""Copy the Flash RL harness source out of outputs/ (git-ignored) into this tracked snapshot.

The running loop reads its code from outputs/; this directory is a versioned copy, refreshed by
running this script and committing the result. Only source and small specs are copied: run state,
logs, rollouts, evaluations and checkpoints stay in outputs/ (and on the KT NFS).

    uv run python experiments/flash-rl/sync_from_outputs.py
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEST = Path(__file__).resolve().parent
OUTPUTS = REPO / "outputs"

# snapshot dir -> source dir under outputs/
SOURCES = {
    "loop": "flash-rl-loop-20261015",
    "rollout": "flash-next-rl-pilot-20261014/rollout",
    # runtime in use by the loop (config data.runtime) and the latest one (optimizer sweep)
    "runtime/general-update-fast18-20261026": "flash-runtime-reconstructed-20261027/general-update-fast18-20261026",
    "runtime/general-update-fast20-20261027": "flash-runtime-reconstructed-20261027/general-update-fast20-20261027",
    "runtime/lora-export-20261015": "flash-runtime-reconstructed-20261027/lora-export-20261015",
    "runtime/_reconstruction_tools": "flash-runtime-reconstructed-20261027/_reconstruction_tools",
}
SINGLE_FILES = {"runtime/RECONSTRUCTION.md": "flash-runtime-reconstructed-20261027/RECONSTRUCTION.md"}

SKIP_DIRS = re.compile(r"^(u\d+|eval|wandb|decisive-items|rollouts|episodes|groups|__pycache__|\.venv|cache.*|ckpt.*)$")
SOURCE_EXT = (".py", ".sh", ".md", ".yaml", ".yml", ".toml")
SPEC_FILES = re.compile(r"^(config\.json|devpanel-.*\.json)$")
MAX_BYTES = 300_000


def wanted(name: str) -> bool:
    if ".bak" in name or "original" in name or ".pre-" in name:
        return False
    return name.endswith(SOURCE_EXT) or bool(SPEC_FILES.match(name))


def copy_tree(src: Path, dst: Path) -> int:
    count = 0
    for root, dirs, files in os.walk(src):
        dirs[:] = sorted(d for d in dirs if not SKIP_DIRS.match(d))
        for name in sorted(files):
            path = Path(root) / name
            if not wanted(name) or path.stat().st_size > MAX_BYTES:
                continue
            target = dst / path.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            count += 1
    return count


def main() -> None:
    for sub in list(SOURCES) + list(SINGLE_FILES):
        target = DEST / sub
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    for sub, src in SOURCES.items():
        print(f"{sub}: {copy_tree(OUTPUTS / src, DEST / sub)} files")
    for sub, src in SINGLE_FILES.items():
        (DEST / sub).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(OUTPUTS / src, DEST / sub)
        print(f"{sub}: 1 file")


if __name__ == "__main__":
    main()
