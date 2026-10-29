"""Mandatory Weights & Biases tracking contract for every fine-tuning run."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    return cleaned.strip("-") or "unknown"


def wandb_run_name(*, method: str, model_name: str, version: str, started_at: datetime | None = None) -> str:
    """Return ``date__method__model__version`` in UTC."""
    timestamp = (started_at or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%d-%H%M%S")
    model = Path(model_name.rstrip("/")).name
    return "__".join((_slug(timestamp), _slug(method), _slug(model), _slug(version)))


def require_wandb() -> Any:
    """Fail before training when an online W&B run cannot be created."""
    mode = os.environ.get("WANDB_MODE", "online").lower()
    if mode in {"disabled", "offline"}:
        raise RuntimeError("fine-tuning requires online W&B; WANDB_MODE cannot disable tracking")
    netrc = Path("~/.netrc").expanduser()
    has_netrc_login = netrc.is_file() and "api.wandb.ai" in netrc.read_text(encoding="utf-8", errors="ignore")
    if not os.environ.get("WANDB_API_KEY") and not has_netrc_login:
        raise RuntimeError("fine-tuning requires W&B credentials: set WANDB_API_KEY or run `wandb login`")
    try:
        import wandb
    except ImportError as error:
        raise RuntimeError("fine-tuning requires the `wandb` package") from error
    return wandb


def init_wandb_run(wandb: Any, *, entity: str, project: str, method: str, model_name: str, version: str, config: Mapping[str, Any]) -> Any:
    """Create the mandatory online run with the canonical audit-friendly name."""
    name = wandb_run_name(method=method, model_name=model_name, version=version)
    run = wandb.init(
        entity=entity,
        project=project,
        name=name,
        config={
            **dict(config),
            "tracking/run_name": name,
            "tracking/method": method,
            "tracking/model": Path(model_name.rstrip("/")).name,
            "tracking/version": version,
            "tracking/timezone": "UTC",
        },
    )
    if run is None:
        raise RuntimeError("wandb.init returned no run; refusing untracked fine-tuning")
    return run
