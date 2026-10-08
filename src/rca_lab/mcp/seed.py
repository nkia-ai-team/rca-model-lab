"""Observed-alarm seed: the only case input a blind investigator receives.

Contract (tools/rca-mcp/docs/spec-blind-rca-input.md): read *only* the time bounds from
meta.json, extract real alarms from lucida_events_local in [t1, t2), never copy the scenario
id, cause, description or injection text. The seed is a starting point, not the answer.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

MAX_ALARMS_DEFAULT = 50
# Alarm-storm suppression (2026-09-22). The seed used to be the first `max_alarms` open alarms in
# time order, so one host firing the same metric alarms every few seconds at onset filled every
# slot (F05-H: 321 eligible alarms on 26 targets, seed = 50 alarms on tb-w1 within 30 s) and the
# investigator never saw the other targets. Identical alarms collapse to their first occurrence
# (with repeat_count / last_at, the storm size stays visible) and each target keeps at most
# PER_TARGET_MAX_DEFAULT alarms. Category-level rule, no case knowledge.
PER_TARGET_MAX_DEFAULT = 8
_EVENT_COLUMNS = [
    "event_id",
    "occurred_at",
    "detector",
    "event_kind",
    "severity",
    "target_id",
    "service_name",
    "reason",
    "transition",
    "class",
    "evidence",
    "raw",
]


def _parse_utc(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value).astimezone(dt.UTC)


def time_bounds(case_dir: Path) -> tuple[str, str]:
    """Return (t1, t2) RFC3339 strings. Nothing else is read from meta.json here."""
    meta = json.loads((case_dir / "meta.json").read_text(encoding="utf-8"))
    return str(meta["t1"]), str(meta["t2"])


def _events_table(case_dir: Path) -> pa.Table:
    table = pq.read_table(case_dir / "data/clickhouse/lucida_events_local.parquet", columns=_EVENT_COLUMNS)
    options = pc.CastOptions(target_type=pa.timestamp("us", tz="UTC"), allow_time_truncate=True)
    index = table.schema.get_field_index("occurred_at")
    return table.set_column(index, "occurred_at", pc.cast(table["occurred_at"], options=options))


def _number(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _alarm_view(row: dict[str, Any]) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    raw: dict[str, Any] = {}
    for key, sink in (("evidence", evidence), ("raw", raw)):
        try:
            parsed = json.loads(row.get(key) or "{}")
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            sink.update(parsed)
    exemplars = evidence.get("exemplars") if isinstance(evidence.get("exemplars"), dict) else {}
    view = {
        "event_id": row["event_id"],
        "occurred_at": row["occurred_at"].strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "detector": row["detector"],
        "kind": row["event_kind"],
        "class": row["class"],
        "severity": row["severity"],
        "target_id": row["target_id"],
        "service": row.get("service_name") or "",
        "reason": row.get("reason") or "",
        "transition": row.get("transition") or "",
    }
    metric = exemplars.get("metric") or raw.get("metric")
    if metric:
        view["metric"] = metric
    baseline = _number(evidence.get("expected"))
    observed = _number(raw.get("value", evidence.get("observed")))
    if baseline is not None:
        view["baseline"] = baseline
    if observed is not None:
        view["observed"] = observed
    # The detector's own `direction` field is a band-side label that reads as contradictory next
    # to the numbers ("down" with observed > baseline) and cost the student a paragraph of
    # confusion every turn; state the comparison plainly instead.
    if baseline is not None and observed is not None and baseline != observed:
        view["change"] = "above_baseline" if observed > baseline else "below_baseline"
    return view


def build_observed_seed(case_dir: Path, *, max_alarms: int = MAX_ALARMS_DEFAULT, per_target_max: int = PER_TARGET_MAX_DEFAULT) -> dict[str, Any]:
    """Public seed for one captured case. Raises when no alarm exists in the window."""
    t1, t2 = time_bounds(case_dir)
    start, end = _parse_utc(t1), _parse_utc(t2)
    table = _events_table(case_dir)
    rows = table.to_pylist()
    in_window = [row for row in rows if start <= row["occurred_at"] < end]
    eligible = [row for row in in_window if row.get("transition") == "open" and row.get("severity") != "cleared"]
    eligible.sort(key=lambda row: (row["occurred_at"], row["event_id"]))
    views = [_alarm_view(row) for row in eligible]
    collapsed, repeats = _collapse_repeats(views)
    capped, capped_out = _cap_per_target(collapsed, per_target_max)
    alarms = capped[:max_alarms]
    if not alarms:
        raise ValueError(f"no open alarm inside [{t1}, {t2}) for {case_dir.name}")
    selected = alarms
    targets = sorted({alarm["target_id"] for alarm in alarms if alarm["target_id"]})
    services = sorted({alarm["service"] for alarm in alarms if alarm["service"]})
    return {
        "seed_kind": "observed_alarms",
        "time_window": {"first_event": t1, "last_event": t2, "basis": "UTC"},
        "alarms": alarms,
        "affected_targets": targets,
        "affected_services": services,
        "selection": {
            "source_total": len(rows),
            "in_window": len(in_window),
            "eligible": len(eligible),
            "collapsed_repeats": repeats,
            "per_target_max": per_target_max,
            "dropped_by_target_cap": capped_out,
            "selected": len(selected),
            "truncated": len(capped) > len(selected),
        },
    }


def _repeat_key(view: dict[str, Any]) -> tuple[Any, ...]:
    return (view.get("target_id"), view.get("kind"), view.get("class"), view.get("metric"), view.get("reason"), view.get("change"))


def _collapse_repeats(views: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Keep the first occurrence of each identical alarm; annotate it with repeat_count / last_at."""
    first: dict[tuple[Any, ...], dict[str, Any]] = {}
    out: list[dict[str, Any]] = []
    repeats = 0
    for view in views:
        key = _repeat_key(view)
        kept = first.get(key)
        if kept is None:
            kept = dict(view)
            first[key] = kept
            out.append(kept)
            continue
        kept["repeat_count"] = int(kept.get("repeat_count", 1)) + 1
        kept["last_at"] = view["occurred_at"]
        repeats += 1
    return out, repeats


def _cap_per_target(views: list[dict[str, Any]], per_target_max: int) -> tuple[list[dict[str, Any]], int]:
    """At most `per_target_max` alarms per target (earliest first); order stays chronological."""
    if per_target_max <= 0:
        return list(views), 0
    seen: dict[Any, int] = {}
    out: list[dict[str, Any]] = []
    dropped = 0
    for view in views:
        target = view.get("target_id")
        if seen.get(target, 0) >= per_target_max:
            dropped += 1
            continue
        seen[target] = seen.get(target, 0) + 1
        out.append(view)
    return out, dropped
