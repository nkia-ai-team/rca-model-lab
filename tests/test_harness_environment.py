from datetime import UTC, datetime

import pytest

from rca_lab.harness.environment import EvidenceEnvironment
from rca_lab.harness.models import Evidence, EvidenceQuery, TimeWindow
from rca_lab.harness.registry import build_registry

NOW = datetime.now(UTC)


def evidence(index: int, *, target: str = "payment", metric: str = "cpu") -> Evidence:
    return Evidence(
        id=f"ev-{index:03d}",
        target=target,
        source="metric",
        kind="timeseries",
        metric=metric,
        window=TimeWindow(start=NOW, end=NOW),
        anomaly_score=float(index),
        observation=f"row {index}",
    )


def test_prompt_cap_never_truncates_backing_evidence() -> None:
    environment = EvidenceEnvironment(evidence(index) for index in range(500))

    view = environment.prompt_view(300)

    assert len(view) == 300
    assert len(environment) == 500
    assert environment.get("ev-000") is not None


def test_structured_query_intersects_dimensions_and_only_caps_result() -> None:
    environment = EvidenceEnvironment(
        [evidence(1), evidence(2, target="order"), evidence(3, metric="memory")]
    )

    result = environment.query(EvidenceQuery(target="payment", source="metric", limit=1))

    assert [row.id for row in result] == ["ev-003"]
    assert len(environment) == 3


def test_registry_is_derived_from_actual_evidence() -> None:
    environment = EvidenceEnvironment(
        [evidence(1), evidence(2, target="order", metric="latency")]
    )

    registry = build_registry(environment, ("payment", "order"))

    assert registry.metrics == {"payment": ("cpu",), "order": ("latency",)}
    assert registry.sources == ("metric",)
    assert registry.kinds == ("timeseries",)


def test_duplicate_evidence_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="unique"):
        EvidenceEnvironment([evidence(1), evidence(1)])
