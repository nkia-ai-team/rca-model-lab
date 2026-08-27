"""Single source of truth for the safe student tool surface."""

from collections.abc import Iterable, Mapping

from rca_lab.harness.environment import EvidenceEnvironment
from rca_lab.harness.models import ActionName, CapabilityRegistry, ToolCapability

_REFRESHABLE = {
    ActionName.PROBE_METRIC,
    ActionName.METRIC_DESCRIBE,
    ActionName.METRIC_OVERVIEW,
    ActionName.METRIC_FETCH_RAW,
    ActionName.METRIC_FETCH_WINDOW,
    ActionName.METRIC_COMPARE,
    ActionName.PROBE_EVENTS,
    ActionName.PROBE_COHORT,
    ActionName.PROBE_DB_BLOCKING,
    ActionName.PROBE_TOPSQL,
    ActionName.PROBE_LOGS,
    ActionName.PROBE_TRACES,
    ActionName.PROBE_CHANGES,
    ActionName.PROBE_DB_QUERY_EVENTS,
    ActionName.PROBE_REPLICA_LAG,
    ActionName.PROBE_HOST_PEERS,
    ActionName.PROBE_SNMP_TRAPS,
    ActionName.DISCOVER_METRICS,
}


def build_registry(
    environment: EvidenceEnvironment,
    candidates: tuple[str, ...],
    *,
    metric_discovery: bool = False,
    metric_inventory: Mapping[str, Iterable[str]] | None = None,
    sub_summarize: bool = False,
    query_result_limit: int = 12,
) -> CapabilityRegistry:
    disabled = set()
    if not metric_discovery:
        disabled.add(ActionName.DISCOVER_METRICS)
    if not sub_summarize:
        disabled.add(ActionName.SUB_SUMMARIZE)
    tools = tuple(
        ToolCapability(name=action, read_only=action not in {ActionName.NOTE, ActionName.ANSWER}, refreshable=action in _REFRESHABLE)
        for action in ActionName
        if action not in disabled
    )
    metrics, sources, kinds = environment.dynamic_dimensions()
    if metric_discovery and metric_inventory:
        allowed_targets = set(candidates)
        merged = {target: set(values) for target, values in metrics.items()}
        for target, values in metric_inventory.items():
            if target not in allowed_targets:
                continue
            merged.setdefault(target, set()).update(metric for metric in values if metric)
        metrics = {target: tuple(sorted(values)) for target, values in merged.items()}
    registry = CapabilityRegistry(
        tools=tools,
        candidates=tuple(sorted(set(candidates))),
        metrics=metrics,
        sources=sources,
        kinds=kinds,
        query_result_limit=query_result_limit,
    )
    environment.validate_registry(registry)
    return registry
