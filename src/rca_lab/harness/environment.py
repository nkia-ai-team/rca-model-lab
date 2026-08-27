"""Lossless evidence store with bounded actor-facing views."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable

from rca_lab.harness.models import CapabilityRegistry, Evidence, EvidenceQuery


class EvidenceEnvironment:
    """Keeps every evidence row; limits only query/prompt views."""

    def __init__(self, evidence: Iterable[Evidence]):
        rows = tuple(evidence)
        ids = [row.id for row in rows]
        if len(ids) != len(set(ids)):
            raise ValueError("evidence ids must be unique")
        self._rows = rows
        self._by_id = {row.id: row for row in rows}

    def __len__(self) -> int:
        return len(self._rows)

    def get(self, evidence_id: str) -> Evidence | None:
        return self._by_id.get(evidence_id)

    @property
    def ids(self) -> frozenset[str]:
        return frozenset(self._by_id)

    def slice(self, evidence_ids: Iterable[str]) -> tuple[Evidence, ...]:
        return tuple(self._by_id[item] for item in evidence_ids if item in self._by_id)

    def query(self, query: EvidenceQuery) -> tuple[Evidence, ...]:
        rows = [row for row in self._rows if self._matches(row, query)]
        rows.sort(key=lambda row: (-row.anomaly_score, row.id))
        return tuple(rows[: query.limit] if query.limit else rows)

    def top(self, limit: int) -> tuple[Evidence, ...]:
        if limit < 0:
            raise ValueError("limit must be >= 0")
        rows = sorted(self._rows, key=lambda row: (-row.anomaly_score, row.id))
        return tuple(rows[:limit])

    def grep(self, text: str, limit: int = 0) -> tuple[Evidence, ...]:
        wanted = text.casefold()
        rows = (
            row
            for row in self._rows
            if wanted in row.observation.casefold() or wanted in row.metric.casefold()
        )
        result = tuple(rows)
        return result[:limit] if limit else result

    def prompt_view(self, limit: int) -> tuple[Evidence, ...]:
        """Bound prompt size without mutating or truncating the environment."""
        return self.top(limit)

    def fingerprint(self) -> str:
        payload = [row.model_dump(mode="json") for row in sorted(self._rows, key=lambda x: x.id)]
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(raw).hexdigest()

    def dynamic_dimensions(self) -> tuple[dict[str, tuple[str, ...]], tuple[str, ...], tuple[str, ...]]:
        metrics: dict[str, set[str]] = defaultdict(set)
        sources: set[str] = set()
        kinds: set[str] = set()
        for row in self._rows:
            if row.target and row.metric:
                metrics[row.target].add(row.metric)
            if row.source:
                sources.add(row.source)
            if row.kind:
                kinds.add(row.kind)
        return (
            {target: tuple(sorted(values)) for target, values in metrics.items()},
            tuple(sorted(sources)),
            tuple(sorted(kinds)),
        )

    def validate_registry(self, registry: CapabilityRegistry) -> None:
        metrics, sources, kinds = self.dynamic_dimensions()
        if registry.metrics != metrics:
            raise ValueError("registry metrics must come from retained evidence")
        if registry.sources != sources or registry.kinds != kinds:
            raise ValueError("registry source/kind enums must come from retained evidence")

    @staticmethod
    def _matches(row: Evidence, query: EvidenceQuery) -> bool:
        if query.target and row.target != query.target:
            return False
        if query.source and row.source != query.source:
            return False
        if query.kind and row.kind != query.kind:
            return False
        if query.start and row.window and row.window.end < query.start:
            return False
        return not (query.end and row.window and row.window.start > query.end)
