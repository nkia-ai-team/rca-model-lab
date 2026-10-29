"""Evidence-source masking for RL rollouts (2026-10-19, user: "있는 버전·없는 버전 둘 다 학습 → 다양한 추론 경로").

The capture data is never modified. A masked variant of a case only changes what the tools return:
  * whole datasets (traces, database, host, process, kubernetes, changes) are reported as not collected through the
    rca-mcp availability manifest (RCA_CAPTURE_SOURCES, tools/availability.go) — the same mechanism external captures use;
  * "changes" additionally hides the explicit rollout traces the manifest cannot reach: change-detect alarms in the seed
    and in list_events, the Deployment/ReplicaSet rollout events (ScalingReplicaSet, ReplicaSet SuccessfulCreate/Delete),
    and the limit-change history in describe_target. Indirect evidence (new pod names, versions in logs/spans, pod
    lifecycle events) is kept on purpose: the agent has to infer a release from symptoms.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

MASKABLE = ("changes", "database", "traces", "host", "process", "kubernetes")   # names rca-mcp's manifest accepts
# Collector coverage variants (2026-10-21, user): customer environments differ in which Polestar collectors are installed.
# A variant removes everything one collector would have produced — its datasets (rca-mcp manifest), its metric families,
# its tools, and the incident alarms its data would have raised — so the policy learns other paths to the same answer.
COVERAGE = {
    "apm": {"datasets": ("traces",), "metric_prefixes": ("jvm.",), "detectors": ("trace-anomaly",)},
    "dpm": {"datasets": ("database",), "metric_prefixes": ("dpm.", "db.")},
    "kcm": {"datasets": ("kubernetes",), "metric_prefixes": ("kcm.",), "k8s_events": True},
    "sms": {"datasets": ("host", "process"), "metric_prefixes": ("sms.", "system.", "process.")},
    "logs": {"block_tools": ("sample_logs",), "detectors": ("log-anomaly",)},
}
COVERAGE_NOTE = {"apm": "APM(분산 트레이스·JVM 지표)", "dpm": "DB 모니터링(DPM 세션·top-SQL·DB 지표)", "kcm": "쿠버네티스 에이전트(KCM 상태·이벤트·지표)",
                 "sms": "호스트 에이전트(syslog·프로세스·호스트 지표)", "logs": "애플리케이션 로그"}
ROLLOUT_REASONS = {"ScalingReplicaSet"}
REPLICASET_ROLLOUT_REASONS = {"SuccessfulCreate", "SuccessfulDelete"}
CHANGE_DETECTOR = "change-detect"
# listevents.go summary tails (anomalous / normal), swapped when the masked episodes were the only anomaly
LISTEV_ANOMALOUS_TAIL = " 감지(detection)는 2차 신호다 — 단서로 쓰고 원본 확인은 scan_metrics·sample_logs로."
LISTEV_NORMAL_TAIL = (" 시간창 내 사건 0건 — 단, 이상감지는 감시 목록(화이트리스트) 안만 본다."
                      " 무혐의 뜻이 아니며 원본 확인은 scan_metrics·sample_logs 몫이다.")


def parse_masks(raw: str | None) -> tuple[str, ...]:
    masks = tuple(sorted({m.strip() for m in (raw or "").split(",") if m.strip()}))
    unknown = [m for m in masks if m not in MASKABLE and m not in COVERAGE]
    if unknown:
        raise ValueError(f"unknown evidence mask {unknown}; allowed {MASKABLE + tuple(COVERAGE)}")
    return masks


def manifest_datasets(masks: tuple[str, ...]) -> tuple[str, ...]:
    """Datasets rca-mcp must report as not collected for these masks (dataset names and collector variants)."""
    out = {m for m in masks if m in MASKABLE}
    for m in masks:
        out.update(COVERAGE.get(m, {}).get("datasets", ()))
    return tuple(sorted(out))


def _metric_prefixes(masks: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(p for m in masks for p in COVERAGE.get(m, {}).get("metric_prefixes", ()))


def _blocked_tools(masks: tuple[str, ...]) -> set[str]:
    return {t for m in masks for t in COVERAGE.get(m, {}).get("block_tools", ())}


def _hidden_detectors(masks: tuple[str, ...]) -> set[str]:
    return {d for m in masks for d in COVERAGE.get(m, {}).get("detectors", ())}


def write_manifest(path: Path, masks: tuple[str, ...], base: Path | None = None) -> Path:
    """Availability manifest for the masked datasets, merged with an existing capture manifest (external captures)."""
    not_collected, note = set(manifest_datasets(masks)), "이 캡처에는 일부 데이터 원천이 수집되지 않았다."
    if base is not None and base.exists():
        old = json.loads(base.read_text(encoding="utf-8"))
        not_collected |= set(old.get("not_collected", []))
        note = old.get("note") or note
    path.write_text(json.dumps({"not_collected": sorted(not_collected), "note": note}, ensure_ascii=False), encoding="utf-8")
    return path


def _alarm_hidden(alarm: dict[str, Any], masks: tuple[str, ...]) -> bool:
    if "changes" in masks and alarm.get("detector") == CHANGE_DETECTOR:
        return True
    if alarm.get("detector") in _hidden_detectors(masks):
        return True
    prefixes = _metric_prefixes(masks)
    return bool(prefixes) and str(alarm.get("metric") or "").startswith(prefixes)


def filter_seed(seed: dict[str, Any], masks: tuple[str, ...]) -> dict[str, Any]:
    if not any(_alarm_hidden(a, masks) for a in seed.get("alarms", [])):
        return seed
    alarms = [a for a in seed.get("alarms", []) if not _alarm_hidden(a, masks)]
    if not alarms:
        raise ValueError(f"every seed alarm comes from a masked source {masks}; no incident is left to investigate")
    out = dict(seed, alarms=alarms)
    out["affected_targets"] = sorted({a["target_id"] for a in alarms if a.get("target_id")})
    out["affected_services"] = sorted({a["service"] for a in alarms if a.get("service")})
    if isinstance(seed.get("selection"), dict):
        sel, gone = seed["selection"], len(seed.get("alarms", [])) - len(alarms)
        out["selection"] = dict(sel, selected=len(alarms), eligible=max(0, int(sel.get("eligible", 0)) - gone))
    return out


def _is_rollout_event(f: dict[str, Any]) -> bool:
    reason, kind = str(f.get("reason", "")), str(f.get("kind", ""))
    return reason in ROLLOUT_REASONS or (reason in REPLICASET_ROLLOUT_REASONS and kind == "ReplicaSet")


def _rewrite_reason_tail(summary: str, removed_objects: dict[tuple[str, str], int]) -> str:
    """Summary tail "사유별 객체 수 — Warning[A 3] Normal[B 1, C 2]": subtract the masked objects, drop empty tokens."""
    def bracket(m: re.Match) -> str:
        et, parts = m.group(1), []
        for part in m.group(2).split(", "):
            reason, _, n = part.rpartition(" ")
            if not n.isdigit():
                parts.append(part)
                continue
            left = 0 if reason in ROLLOUT_REASONS else int(n) - removed_objects.get((et, reason), 0)
            if left > 0:
                parts.append(f"{reason} {left}")
        return f" {et}[{', '.join(parts)}]" if parts else ""
    return re.sub(r" (Warning|Normal)\[([^\]]*)\]", bracket, summary)


def _count(summary: str, pattern: str, removed: int) -> str:
    return re.sub(pattern, lambda m: m.group(0).replace(m.group(1), str(max(0, int(m.group(1)) - removed)), 1), summary, count=1)


def filter_list_events(payload: dict[str, Any]) -> dict[str, Any]:
    findings = payload.get("findings") or []
    keep, dropped_refs, removed = [], set(), {"episode": 0, "unpaired": 0, "k8s": 0}
    for f in findings:
        sec = f.get("section")
        if (sec in ("episode", "unpaired") and f.get("detector") == CHANGE_DETECTOR) or (sec == "k8s" and _is_rollout_event(f)):
            removed[sec] += 1
            dropped_refs.update(f.get("refs") or [])
            continue
        keep.append(f)
    removed_objects: dict[tuple[str, str], int] = {}
    for f in keep:
        if f.get("section") != "summary_meta":
            continue
        counts = f.get("counts") or {}
        for sec, keys in (("episode", ("episodes_total", "episodes_returned")),
                          ("unpaired", ("unpaired_total", "unpaired_returned")),
                          ("k8s", ("k8s_groups_total", "k8s_groups_returned"))):
            for key in keys:
                if isinstance(counts.get(key), int):
                    counts[key] = max(0, counts[key] - removed[sec])
        rows = f.get("k8s_reason_summary")
        if isinstance(rows, list):
            for r in rows:
                if _is_rollout_event(r):
                    k = (str(r.get("event_type")), str(r.get("reason")))
                    removed_objects[k] = removed_objects.get(k, 0) + int(r.get("objects") or 0)
            f["k8s_reason_summary"] = [r for r in rows if not _is_rollout_event(r)]
            if isinstance(f.get("k8s_reason_summary_total"), int):
                f["k8s_reason_summary_total"] -= len(rows) - len(f["k8s_reason_summary"])
    if not any(removed.values()) and not removed_objects:
        return payload
    out = dict(payload, findings=keep)
    if isinstance(payload.get("refs"), list):
        out["refs"] = [r for r in payload["refs"] if r not in dropped_refs]
    summary = str(payload.get("summary", ""))
    summary = _count(summary, r"감지 에피소드 (\d+)건", removed["episode"])
    summary = _count(summary, r"\(반환 (\d+)", removed["episode"])
    summary = _count(summary, r"단건 (\d+)건", removed["unpaired"])
    summary = _count(summary, r"K8s 기록 (\d+)그룹", removed["k8s"])
    summary = _rewrite_reason_tail(summary, removed_objects)
    # status flips to normal when the change-detect episodes were the only anomaly (listevents.go status rule)
    episodes_left = any(f.get("section") == "episode" for f in keep)
    active_unpaired = any(f.get("section") == "unpaired" and f.get("severity") not in ("cleared", "info") for f in keep)
    warn = re.search(r"\(Warning (\d+)\)", summary)
    if payload.get("status") == "anomalous" and not (episodes_left or active_unpaired or (warn and int(warn.group(1)) > 0)):
        out["status"] = "normal"
        summary = summary.replace(LISTEV_ANOMALOUS_TAIL, LISTEV_NORMAL_TAIL)
    out["summary"] = summary
    return out


def _strip_limit_changes(node: Any) -> Any:
    if isinstance(node, dict):
        if node.get("stability") == "changed" and "changes" in node:
            node = {k: v for k, v in node.items() if k not in ("changes", "stability")}
        return {k: _strip_limit_changes(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_strip_limit_changes(v) for v in node]
    return node


def not_collected_response(tool: str, masks: tuple[str, ...]) -> str:
    what = ", ".join(COVERAGE_NOTE[m] for m in masks if m in COVERAGE and tool in COVERAGE[m].get("block_tools", ()))
    return json.dumps({"status": "no_data", "no_data_reason": "not_collected",
                       "summary": f"이 캡처에는 {what} 데이터가 수집되지 않았다 — 관측 결손이며 원인 배제 근거가 아니다. 다른 원천으로 조사하라.",
                       "refs": [f"tool:{tool}:not_collected"]}, ensure_ascii=False, separators=(",", ":"))


def _drop_metric_families(node: Any, prefixes: tuple[str, ...]) -> Any:
    """Remove every finding / series / ref of the masked metric families (recursively)."""
    if isinstance(node, dict):
        return {k: _drop_metric_families(v, prefixes) for k, v in node.items()}
    if isinstance(node, list):
        out = []
        for v in node:
            if isinstance(v, dict) and str(v.get("metric") or "").startswith(prefixes):
                continue
            if isinstance(v, str) and (v.startswith(prefixes) or any(f"vm:{p}" in v for p in prefixes)):
                continue
            out.append(_drop_metric_families(v, prefixes))
        return out
    return node


def _drop_k8s_events(payload: dict[str, Any]) -> dict[str, Any]:
    findings = payload.get("findings") or []
    gone = [f for f in findings if f.get("section") == "k8s"]
    if not gone:
        return payload
    dropped = {r for f in gone for r in (f.get("refs") or [])}
    keep = [f for f in findings if f.get("section") != "k8s"]
    for f in keep:
        if f.get("section") == "summary_meta":
            for key in ("k8s_groups_total", "k8s_groups_returned"):
                if isinstance((f.get("counts") or {}).get(key), int):
                    f["counts"][key] = 0
            for key in ("k8s_reason_summary", "k8s_reason_summary_total", "k8s_reason_summary_truncated", "k8s_reason_summary_note"):
                f.pop(key, None)
    out = dict(payload, findings=keep)
    if isinstance(payload.get("refs"), list):
        out["refs"] = [r for r in payload["refs"] if r not in dropped]
    summary = re.sub(r"K8s 기록 \d+그룹\(Warning \d+\)", "K8s 기록 0그룹(Warning 0)", str(payload.get("summary", "")))
    out["summary"] = re.sub(r" 사유별 객체 수 —.*?\(summary_meta\.k8s_reason_summary에 namespace·kind별\)\.", "", summary)
    return out


def filter_response(tool: str, text: str, masks: tuple[str, ...]) -> str:
    """Post-filter one tool response for the masked variant (dataset masks are enforced by rca-mcp itself)."""
    prefixes = _metric_prefixes(masks)
    k8s = any(COVERAGE.get(m, {}).get("k8s_events") for m in masks)
    if not ("changes" in masks and tool in ("list_events", "describe_target")) and not prefixes and not (k8s and tool == "list_events"):
        return text
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text
    if not isinstance(payload, dict):
        return text
    if "changes" in masks:
        payload = filter_list_events(payload) if tool == "list_events" else (_strip_limit_changes(payload) if tool == "describe_target" else payload)
    if k8s and tool == "list_events":
        payload = _drop_k8s_events(payload)
    if prefixes:
        had = bool(payload.get("findings"))
        payload = _drop_metric_families(payload, prefixes)
        if had and not payload.get("findings"):
            payload.update(status="no_data", no_data_reason="not_collected",
                           summary="이 캡처에는 요청한 지표 계열이 수집되지 않았다 — 관측 결손이며 원인 배제 근거가 아니다. 다른 원천으로 조사하라.")
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class MaskedMCP:
    """Wraps an MCPClient: same interface, responses filtered for the masked variant."""

    def __init__(self, inner: Any, masks: tuple[str, ...]):
        self.inner, self.masks = inner, masks

    def call(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        if name in _blocked_tools(self.masks):   # the collector behind this tool is absent in the variant
            return not_collected_response(name, self.masks), False
        text, is_error = self.inner.call(name, arguments)
        return (text if is_error else filter_response(name, text, self.masks)), is_error

    def __getattr__(self, item: str) -> Any:
        return getattr(self.inner, item)
