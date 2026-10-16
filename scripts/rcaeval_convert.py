"""Convert RCAEval cases (Pham et al., WWW'25; Zenodo 14590730, CC BY 4.0) into rca-mcp captures.

  uv run python scripts/rcaeval_convert.py --src data/external/rcaeval/RE2-SS --out data/external/rcaeval/captures \
      --limit 10 --pg-container rcaeval-pgtool

Each RCAEval run directory (<service>_<fault>/<rep>/) becomes one capture directory that
`tools/rca-mcp/replay/restore.sh` can load and the rca-mcp tools can serve:

  meta.json                       t1/t2, capture window, scenario_metadata (grader-only), source/license
  data/victoriametrics.export     kcm.* series under the cluster target (namespace/pod/container labels,
                                  as in Polestar captures), mesh SLIs under each application target
  data/clickhouse/lucida_logs_local.parquet
  data/clickhouse/lucida_events_local.parquet   blind synthetic anomaly episodes (stream-anomaly shape)
  data/postgres.dump              target registry (cluster, nodes, workloads, pods, containers, services, apps)
  data/sources.json               datasets that were not collected (tools answer not_collected, availability.go)

plus a golden entry (configs/eval/<name>.yaml) and a split file (configs/teacher/<name>-split.yaml).

Design rules:
- Nothing the student can see carries the fault label: case ids are hashes, the RCAEval directory name
  (e.g. carts_cpu) appears only in meta.json (grader-only).
- The alarm detector is blind to the label: its baseline is the first `--detector-baseline` seconds of the
  capture, never the injection time.
- Timestamps are shifted so the capture starts at `--anchor` (Polestar-like dates); ISO timestamps inside
  log bodies are shifted by the same amount.
- Only telemetry that exists is emitted; everything else is declared not collected.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import math
import re
import statistics
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import yaml

ROOT = Path(__file__).resolve().parents[1]
REF_CAPTURE = Path("/data/eval-cases/case-f01-h-v3-48ec8e62")  # parquet schemas only (no meta.json read)
NS = uuid.uuid5(uuid.NAMESPACE_URL, "rcaeval-convert/v1")
STEP = 10  # seconds per exported sample (source is 1 s)

# Sock Shop application services (RCAEval RE2/RE3 SS) and their implementation (microservices-demo manifests).
APP_LANG = {
    "front-end": "otel-node.otel_node", "catalogue": "otel-go.otel_go", "payment": "otel-go.otel_go", "user": "otel-go.otel_go",
    "carts": "otel-java.otel_java", "orders": "otel-java.otel_java", "shipping": "otel-java.otel_java", "queue-master": "otel-java.otel_java",
}
# Per-system facts. Applications = workloads with Istio sidecar metrics (plus the verified list for Sock Shop).
# Language: Sock Shop from its manifests; otherwise the observed log layout (Spring Boot => Java) or, with no
# language evidence, the generic service code (e.g. the Train Ticket UI/gateway). Entry = user-facing service.
SYSTEMS = {
    "SS": {"namespace": "sock-shop", "entry": "front-end", "alias": {"front": "front-end"}, "lang": APP_LANG},
    "TT": {"namespace": "train-ticket", "entry": "ts-ui-dashboard", "alias": {}, "lang": {}},
    "OB": {"namespace": "online-boutique", "entry": "frontend", "alias": {}, "lang": {}},
}
SPRING_LOG = re.compile(r"^\S+ \S+\s+(TRACE|DEBUG|INFO|WARN|ERROR)\s|\s(INFO|WARN|ERROR)\s+\d+\s+---\s+\[")
GENERIC_SERVICE = "apm.service"
ENTITY_ALIAS = SYSTEMS["SS"]["alias"]  # RCAEval exports front-end pod network series under "front"


def system_of(src_root: Path) -> str:
    """RE2-TT -> TT."""
    return src_root.name.split("-")[-1].upper()

FAULT_TEXT = {  # RCAEval paper §3 (fault injection) — grader-only reference for the mechanism judge and hints
    "cpu": ("CPU hog", "stress-ng CPU hog injected into the {svc} container", "{svc} container CPU usage rises sharply at onset while co-located containers and upstream latency follow"),
    "mem": ("memory leak", "stress-ng memory leak injected into the {svc} container", "{svc} container memory usage (working set) grows abnormally from onset"),
    "disk": ("disk stress", "stress-ng disk I/O stress injected into the {svc} container", "{svc} container disk I/O (fs reads/writes) surges from onset"),
    "socket": ("socket stress", "stress-ng socket stress injected into the {svc} container", "{svc} container socket count / network activity surges from onset"),
    "delay": ("network delay", "tc network delay injected on the {svc} container's traffic", "latency of requests served by {svc} rises from onset while its resource usage stays normal"),
    # RE2-TT raw data (2026-10-02 check, 15 runs): p99 jumps while p50, istio errors, CPU and sockets stay flat — loss surfaces as
    # retransmission tail latency, not as error responses.
    "loss": ("packet loss", "tc packet drop injected on the {svc} container's traffic", "requests involving {svc} show intermittent tail-latency spikes (p99 up, p50 flat) from onset while its CPU/memory/socket usage stays normal; error responses may stay flat"),
    "f1": ("incorrect parameter values", "code fault: incorrect parameter values in {svc}", "{svc} logs errors / stack traces from the faulty code path after onset"),
    "f2": ("missing parameters", "code fault: missing parameters in {svc}", "{svc} logs errors / stack traces from the faulty code path after onset"),
    "f3": ("missing function call", "code fault: missing function call in {svc}", "{svc} logs errors / stack traces from the faulty code path after onset"),
    "f4": ("incorrect return values", "code fault: incorrect return values in {svc}", "{svc} logs errors / stack traces from the faulty code path after onset"),
    "f5": ("missing exception handlers", "code fault: missing exception handlers in {svc}", "{svc} logs errors / stack traces from the faulty code path after onset"),
}
NOT_COLLECTED = ["traces", "database", "kubernetes", "changes", "process", "host", "snmp", "cluster_projection"]


def uid(case_id: str, kind: str, key: str) -> str:
    return str(uuid.uuid5(NS, f"{case_id}|{kind}|{key}"))


def iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------------------------------ source parsing

@dataclass
class Source:
    rel: str
    service: str
    fault: str
    inject: float
    times: list[float]
    columns: dict[str, list[float | None]]
    logs: list[dict[str, str]]
    pod_node: dict[str, str]
    run_dir: Path | None = None
    system: str = "SS"


def _num(value: str) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) or math.isinf(x) else x


class SourceDefect(ValueError):
    """The source run is unusable as published (logged and skipped, never repaired)."""


def read_source(run_dir: Path, src_root: Path) -> Source:
    service, fault = run_dir.parent.name.rsplit("_", 1)
    with (run_dir / "metrics.csv").open() as handle:
        reader = csv.reader(handle)
        header = next(reader)
        raw_rows = [row for row in reader if row]
    rows = [row for row in raw_rows if _num(row[0]) is not None]
    if len(raw_rows) - len(rows) > 0.05 * max(len(raw_rows), 1):
        # RE2-OB checkoutservice_{cpu,mem}/2: ~20% of metric rows carry no timestamp — the run cannot be placed in time
        raise SourceDefect(f"{len(raw_rows) - len(rows)}/{len(raw_rows)} metric rows without a valid timestamp")
    times = [float(row[0]) for row in rows]
    columns = {name: [_num(row[i]) if i < len(row) else None for row in rows] for i, name in enumerate(header) if i > 0}
    logs = []
    if (run_dir / "logs.csv").exists():
        with (run_dir / "logs.csv").open() as handle:
            logs = list(csv.DictReader(handle))
    pod_node: dict[str, str] = {}
    for f in sorted(run_dir.glob("pod-node-*.csv")):
        with f.open() as handle:
            for row in csv.DictReader(handle):
                pod_node[row["POD"]] = row["NODE_NAME"]
    return Source(str(run_dir.relative_to(src_root.parent)), service, fault.lower(), float((run_dir / "inject_time.txt").read_text().strip()), times, columns, logs, pod_node,
                  run_dir, system_of(src_root))


# ------------------------------------------------------------------------------------------ entities

@dataclass
class Workload:
    name: str
    pod: str
    node: str
    app: bool
    ids: dict[str, str] = field(default_factory=dict)
    ns: str = "sock-shop"
    lang: str = ""
    lang_basis: str = ""


def _language(name: str, system: dict[str, Any], logs: list[dict[str, str]]) -> tuple[str, str]:
    if name in system["lang"]:
        return system["lang"][name], "manifest"
    lines = [r.get("message") or "" for r in logs if r.get("container_name") == name][:200]
    if lines and sum(bool(SPRING_LOG.search(m)) for m in lines) >= 0.5 * len(lines):
        return "otel-java.otel_java", "spring_boot_log_layout"
    return GENERIC_SERVICE, "no_language_evidence"


def workloads(src: Source, case_id: str) -> tuple[dict[str, Workload], dict[str, str], str]:
    system = SYSTEMS.get(src.system, SYSTEMS["SS"])
    alias = system["alias"]
    kinds: dict[str, set[str]] = {}
    for c in src.columns:
        entity, kind = c.split("_", 1)
        kinds.setdefault(alias.get(entity, entity), set()).add(kind)
    entities = set(kinds)
    nodes = sorted(e for e in entities if e.startswith("gke-")) or sorted(set(src.pod_node.values()))
    names = sorted(e for e in entities if not e.startswith("gke-") and not e.endswith("-exporter") and e != "ts")
    pods = {}
    for pod, node in src.pod_node.items():
        base = re.sub(r"-[a-z0-9]{8,10}-[a-z0-9]{5}$", "", pod)
        pods[base] = (pod, node)
    cluster = uid(case_id, "cluster", system["namespace"])
    out = {}
    for name in names:
        pod, node = pods.get(name, (f"{name}-0", nodes[0] if nodes else "node-0"))
        is_app = name in system["lang"] or any(k.startswith("istio-") for k in kinds[name])
        w = Workload(name, pod, node, is_app, ns=system["namespace"])
        if is_app:
            w.lang, w.lang_basis = _language(name, system, src.logs)
        rs = pod.rsplit("-", 1)[0] if pod.count("-") >= 2 else f"{name}-0"
        w.ids = {k: uid(case_id, k, key) for k, key in (("deployment", name), ("replicaset", rs), ("pod", pod), ("container", f"{pod}/{name}"), ("service", name))}
        if w.app:
            w.ids["app"] = uid(case_id, "app", name)
        w.rs = rs
        out[name] = w
    node_ids = {n: uid(case_id, "node", n) for n in nodes}
    return out, node_ids, cluster


# ------------------------------------------------------------------------------------------ metrics

# RCAEval exports every Prometheus counter already as a per-second rate (checked on RE2-SS/RE2-TT/RE3-SS: "*-total"
# columns rise and fall about equally often; istio-request-total ~3 req/s). CPU columns are percent of one core: a CPU
# hog pins ts-auth-service at exactly 70.0 under a 70000us/100ms quota and carts at 50.0 under 50000. So values are
# taken as-is (no differencing) and CPU is scaled percent -> millicores.
CPU_PCT_TO_MILLICORES = 10.0
KCM_CONTAINER = {
    "container-cpu-usage-seconds-total": ("kcm.container.cpu_usage", CPU_PCT_TO_MILLICORES),
    "container-cpu-system-seconds-total": ("kcm.container.cpu_system", CPU_PCT_TO_MILLICORES),
    "container-cpu-user-seconds-total": ("kcm.container.cpu_user", CPU_PCT_TO_MILLICORES),
    "container-memory-working-set-bytes": ("kcm.container.mem_usage", 1.0),
    "container-memory-usage-bytes": ("kcm.container.mem_usage_total", 1.0),
    "container-memory-rss": ("kcm.container.mem_rss", 1.0),
    "container-memory-cache": ("kcm.container.mem_cache", 1.0),
    "container-memory-swap": ("kcm.container.mem_swap", 1.0),
    "container-memory-mapped-file": ("kcm.container.mem_mapped_file", 1.0),
    "container-memory-max-usage-bytes": ("kcm.container.mem_max_usage", 1.0),
    "container-memory-failures-total": ("kcm.container.mem_failures", 1.0),
    "container-sockets": ("kcm.container.sockets", 1.0),
    "container-blkio-device-usage-total": ("kcm.container.blkio_usage", 1.0),
}
KCM_POD = {
    "container-cpu-usage-seconds-total": ("kcm.pod.cpu_usage", CPU_PCT_TO_MILLICORES),
    "container-memory-working-set-bytes": ("kcm.pod.mem_working_set", 1.0),
    "container-memory-usage-bytes": ("kcm.pod.mem_usage", 1.0),
    "container-network-receive-bytes-total": ("kcm.pod.network_rx_bps", 1.0),
    "container-network-transmit-bytes-total": ("kcm.pod.network_tx_bps", 1.0),
    "container-network-receive-packets-total": ("kcm.pod.network_rx_iops", 1.0),
    "container-network-transmit-packets-total": ("kcm.pod.network_tx_iops", 1.0),
    "container-network-receive-errors-total": ("kcm.pod.network_rx_err_ps", 1.0),
    "container-network-transmit-errors-total": ("kcm.pod.network_tx_err_ps", 1.0),
    "container-network-receive-packets-dropped-total": ("kcm.pod.network_rx_drop_ps", 1.0),
    "container-network-transmit-packets-dropped-total": ("kcm.pod.network_tx_drop_ps", 1.0),
    "container-fs-reads-bytes-total": ("kcm.pod.blkio_rx_bps", 1.0),
    "container-fs-writes-bytes-total": ("kcm.pod.blkio_wx_bps", 1.0),
    "container-fs-reads-total": ("kcm.pod.blkio_rx_iops", 1.0),
    "container-fs-writes-total": ("kcm.pod.blkio_wx_iops", 1.0),
}
LIMITS = {"container-spec-cpu-quota": (("kcm.container.cpu_limit", "kcm.pod.cpu_limit"), 0.01),  # quota µs/100ms -> millicores
          "container-spec-memory-limit-bytes": (("kcm.container.mem_limit", "kcm.pod.mem_limit"), 1.0)}
MESH = {  # Istio sidecar metrics per service -> application target (a mesh view, not an APM agent)
    "istio-request-total": "mesh.istio.rps", "istio-error-total": "mesh.istio.error_count",
    "istio-latency-50": "mesh.istio.percentile50", "istio-latency-90": "mesh.istio.percentile90",
    "istio-latency-95": "mesh.istio.percentile95", "istio-latency-99": "mesh.istio.percentile99",
    "istio-bytes-50": "mesh.istio.response_bytes_p50", "istio-bytes-90": "mesh.istio.response_bytes_p90",
    "istio-bytes-95": "mesh.istio.response_bytes_p95", "istio-bytes-99": "mesh.istio.response_bytes_p99",
}
# node-cpu-seconds-total is summed over all modes including idle (~= cores x 100: 787-793 on 8-core TT nodes, ~396 on
# 4-core SS nodes) -- it carries no usage signal, so it is not emitted as kcm.node.cpu_usage.
NODE = {
    "node-memory-active-bytes": ("kcm.node.mem_active", 1.0),
    "node-memory-inactive-bytes": ("kcm.node.mem_inactive", 1.0), "node-disk-read-bytes-total": ("kcm.node.blkio_rx_bps", 1.0),
    "node-disk-written-bytes-total": ("kcm.node.blkio_wx_bps", 1.0), "node-disk-reads-completed-total": ("kcm.node.blkio_rx_iops", 1.0),
    "node-disk-writes-completed-total": ("kcm.node.blkio_wx_iops", 1.0), "node-network-receive-packets-total": ("kcm.node.network_rx_iops", 1.0),
    "node-network-transmit-packets-total": ("kcm.node.network_tx_iops", 1.0), "node-network-receive-errs-total": ("kcm.node.network_rx_error", 1.0),
    "node-network-transmit-errs-total": ("kcm.node.network_tx_error", 1.0), "node-network-receive-drop-total": ("kcm.node.network_rx_drop", 1.0),
    "node-network-transmit-drop-total": ("kcm.node.network_tx_drop", 1.0),
}


def resample(times: list[float], values: list[float | None], start: float) -> tuple[list[int], list[float | None]]:
    buckets: dict[int, list[float]] = {}
    for t, v in zip(times, values, strict=True):
        if v is not None:
            buckets.setdefault(int((t - start) // STEP), []).append(v)
    idx = sorted(buckets)
    return [int(start + i * STEP) for i in idx], [statistics.fmean(buckets[i]) for i in idx]


def metric_series(src: Source, ws: dict[str, Workload], node_ids: dict[str, str], cluster: str, shift: float) -> list[dict[str, Any]]:
    start = src.times[0]
    series = []

    def emit(name: str, labels: dict[str, str], values: list[float | None], scale: float) -> None:
        ts, vals = resample(src.times, [None if v is None else v * scale for v in values], start)
        if vals:
            series.append({"metric": {"__name__": name, **labels}, "values": [round(v, 6) for v in vals], "timestamps": [int((t + shift) * 1000) for t in ts]})

    alias = SYSTEMS.get(src.system, SYSTEMS["SS"])["alias"]
    by_entity: dict[str, dict[str, list[float | None]]] = {}
    for col, values in src.columns.items():
        entity, kind = col.split("_", 1)
        by_entity.setdefault(alias.get(entity, entity), {})[kind] = values
    for entity, kinds in by_entity.items():
        if entity.startswith("gke-"):
            for kind, values in kinds.items():
                if kind in NODE:
                    name, scale = NODE[kind]
                    emit(name, {"target_id": cluster, "node": entity, "node_id": node_ids.get(entity, ""), "cluster_id": f"cluster-{cluster}"}, values, scale)
            continue
        w = ws.get(entity)
        if w is None:
            continue
        pod_labels = {"target_id": cluster, "namespace": w.ns, "pod": w.pod, "node": w.node, "pod_id": w.ids["pod"], "node_id": node_ids.get(w.node, ""), "cluster_id": f"cluster-{cluster}"}
        ctr_labels = {**pod_labels, "container": w.name, "container_id": f"{w.pod}/{w.name}"}
        for kind, values in kinds.items():
            if kind in KCM_CONTAINER:
                emit(KCM_CONTAINER[kind][0], ctr_labels, values, KCM_CONTAINER[kind][1])
            if kind in KCM_POD:
                emit(KCM_POD[kind][0], pod_labels, values, KCM_POD[kind][1])
            if kind in LIMITS:
                (cname, pname), scale = LIMITS[kind]
                limited = [v if v is not None and v > 0 else None for v in values]  # 0/-1 = no limit
                emit(cname, ctr_labels, limited, scale)
                emit(pname, pod_labels, limited, scale)
            if kind in MESH and w.app:
                emit(MESH[kind], {"target_id": w.ids["app"], "service_name": w.name}, values, 1.0)
        if w.app and "istio-request-total" in kinds and "istio-error-total" in kinds:
            req, err = kinds["istio-request-total"], kinds["istio-error-total"]
            ratio = [None if r is None or e is None else (e / r if r > 0 else 0.0) for r, e in zip(req, err, strict=True)]
            emit("mesh.istio.error_rate", {"target_id": w.ids["app"], "service_name": w.name}, ratio, 1.0)
    return series


# ------------------------------------------------------------------------------------------ alarms (blind detector)

SYMPTOM_METRICS = ("mesh.istio.percentile95", "mesh.istio.percentile99", "mesh.istio.error_rate", "mesh.istio.rps")
RESOURCE_METRICS = ("kcm.pod.cpu_usage", "kcm.pod.mem_working_set", "kcm.pod.network_rx_bps", "kcm.pod.network_tx_bps",
                    "kcm.pod.blkio_rx_bps", "kcm.pod.blkio_wx_bps", "kcm.container.sockets")
# Pilot 2026-09-30: with resource alarms the root container's own anomaly was in the seed in 11/12 cases (the answer
# is read off the alarms). Default = service-level symptoms only; resource series stay queryable through the tools.
ALARM_METRICS = SYMPTOM_METRICS
# Detector unit floors and directions (TT pilot 2026-09-30: without them a low-traffic endpoint going 0 -> 0.0067 rps
# scored z=40000, and latency *drops* after start-up warm-up spikes filled the seed with below-baseline alarms).
# Floors are the smallest change that is operationally meaningful in the metric's own unit; latency and error rate
# only alarm upward (the Polestar stream-anomaly direction band), request rate alarms both ways (traffic loss is a symptom).
ALARM_FLOORS = {"mesh.istio.percentile95": 0.005, "mesh.istio.percentile99": 0.005, "mesh.istio.error_rate": 0.01, "mesh.istio.rps": 0.05}
UPWARD_ONLY = ("mesh.istio.percentile95", "mesh.istio.percentile99", "mesh.istio.error_rate")


def detect(series: list[dict[str, Any]], capture_start_ms: int, baseline_s: int, z_open: float = 5.0, z_close: float = 2.0, hold: int = 4,
           metrics: tuple[str, ...] = ALARM_METRICS) -> list[dict[str, Any]]:
    """Robust z vs the first `baseline_s` seconds of each series (median / MAD, floored by ALARM_FLOORS): an episode
    opens after `hold` consecutive points above z_open and closes after `hold` below z_close; UPWARD_ONLY metrics
    count only rises. Label-blind."""
    episodes = []
    for s in series:
        name = s["metric"]["__name__"]
        if name not in metrics:
            continue
        ts, vals = s["timestamps"], s["values"]
        base = [v for t, v in zip(ts, vals, strict=True) if t < capture_start_ms + baseline_s * 1000]
        if len(base) < 6:
            continue
        med = statistics.median(base)
        mad = statistics.median([abs(v - med) for v in base]) * 1.4826
        scale = max(mad, abs(med) * 0.05, ALARM_FLOORS.get(name, 1e-6))
        upward = name in UPWARD_ONLY
        above = below = 0
        open_at = peak = None
        start_i = None
        for i, (t, v) in enumerate(zip(ts, vals, strict=True)):
            if t < capture_start_ms + baseline_s * 1000:
                continue
            z = max(v - med, 0.0) / scale if upward else abs(v - med) / scale
            if open_at is None:
                above = above + 1 if z > z_open else 0
                if above >= hold:
                    start_i = i - hold + 1
                    open_at, peak, below = ts[start_i], max((max(x - med, 0.0) if upward else abs(x - med)) / scale for x in vals[start_i:i + 1]), 0
            else:
                peak = max(peak, z)
                below = below + 1 if z < z_close else 0
                if below >= hold:
                    episodes.append({"series": s, "open": open_at, "close": ts[i], "peak": peak, "value": vals[start_i], "expected": med, "std": scale})
                    open_at, above = None, 0
        if open_at is not None:
            episodes.append({"series": s, "open": open_at, "close": None, "peak": peak, "value": vals[start_i], "expected": med, "std": scale})
    return episodes


def severity(z: float) -> str:
    return "critical" if z >= 8 else "warning" if z >= 5 else "caution"


def event_rows(episodes: list[dict[str, Any]], names: dict[str, str], case_id: str) -> list[dict[str, Any]]:
    rows = []
    for ep in episodes:
        labels = ep["series"]["metric"]
        metric = labels["__name__"]
        target = labels.get("pod_id") or labels["target_id"] if metric.startswith("kcm.") else labels["target_id"]
        service = labels.get("service_name") or labels.get("container") or names.get(target, "")
        episode_id = uid(case_id, "episode", f"{metric}|{target}|{ep['open']}")
        sev = severity(ep["peak"])
        direction = "up" if ep["value"] >= ep["expected"] else "down"
        attrs = {"method": "robust_z", "metric": metric, "value": str(ep["value"]), "baseline": str(ep["expected"]), "std": str(ep["std"]),
                 "z_score": str(round(ep["peak"], 3)), "threshold": "5", "reason": "level_shift"}
        raw = {"method": "robust_z", "metric": metric, "target_id": target, "ts": iso(ep["open"] / 1000), "value": ep["value"], "z_score": ep["peak"]}
        evidence = {"archetype": "level", "baseline_source": "capture_head_median", "deviation": ep["peak"], "direction": direction,
                    "exemplars": {"method": "robust_z", "metric": metric}, "expected": ep["expected"], "method": "robust_z", "metric": metric,
                    "observed": ep["value"], "reason_detail": "level_shift", "std": ep["std"], "threshold": 5}
        base = {"detector": "stream-anomaly", "event_kind": "metric_anomaly", "target_id": target, "service_name": service, "domain": "-",
                "source_id": "rcaeval", "cluster_id": "-", "schema_version": "v1", "class": "anomaly", "reason": "metric_anomaly",
                "episode_id": episode_id, "signal_key": "metric-series:" + hashlib.md5(f"{metric}|{target}".encode()).hexdigest()[:26],
                "flags": [], "model_version": "-", "occurrence_count": 1}
        rows.append({**base, "event_id": uid(case_id, "event", episode_id + "|open"), "occurred_at": ep["open"] * 1_000_000, "emitted_at": ep["open"] * 1_000_000,
                     "severity": sev, "attributes": attrs, "raw": json.dumps(raw), "evidence": json.dumps(evidence), "transition": "open", "duration_ms": 0, "peak_severity": sev})
        if ep["close"] is not None:
            rows.append({**base, "event_id": uid(case_id, "event", episode_id + "|close"), "occurred_at": ep["close"] * 1_000_000, "emitted_at": ep["close"] * 1_000_000,
                         "severity": "cleared", "attributes": attrs, "raw": json.dumps(raw), "evidence": json.dumps(evidence), "transition": "close",
                         "duration_ms": int(ep["close"] - ep["open"]), "peak_severity": sev})
    return rows


# ------------------------------------------------------------------------------------------ logs

LEVEL_KV = re.compile(r"\blevel=(\w+)", re.IGNORECASE)
LEVEL_TOKEN = re.compile(r"^\S+\s+\S+\s+(TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL)\b")
HTTP_STATUS = re.compile(r"\s(?:GET|POST|PUT|DELETE|PATCH)\s\S+\s(\d{3})\s")
SEV_NUM = {"TRACE": 1, "DEBUG": 5, "INFO": 9, "WARN": 13, "ERROR": 17, "FATAL": 21}
ISO_IN_BODY = re.compile(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(\.\d+)?(Z)?")


def log_severity(message: str) -> str:
    """Format parsing only (Go kit level=, Spring level column, HTTP access-log status)."""
    m = LEVEL_KV.search(message)
    if m:
        level = m.group(1).upper()
        return "WARN" if level == "WARNING" else level if level in SEV_NUM else "INFO"
    m = LEVEL_TOKEN.search(message)
    if m:
        return "WARN" if m.group(1) == "WARNING" else m.group(1)
    m = HTTP_STATUS.search(message)
    if m:
        return "ERROR" if m.group(1).startswith("5") else "WARN" if m.group(1).startswith("4") else "INFO"
    return "INFO"


def shift_body(body: str, shift: float) -> str:
    def repl(m: re.Match[str]) -> str:
        try:
            t = dt.datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}").replace(tzinfo=dt.UTC) + dt.timedelta(seconds=shift)
        except ValueError:
            return m.group(0)
        sep = "T" if "T" in m.group(0) else " "
        return t.strftime(f"%Y-%m-%d{sep}%H:%M:%S") + (m.group(3) or "") + (m.group(4) or "")
    return ISO_IN_BODY.sub(repl, body)


def log_rows(src: Source, ws: dict[str, Workload], node_ids: dict[str, str], shift: float) -> list[dict[str, Any]]:
    rows = []
    for rec in src.logs:
        w = ws.get(rec.get("container_name", ""))
        if w is None:
            continue
        ts_ns = int(rec["timestamp"]) + int(shift * 1e9)
        message = rec.get("message") or ""
        sev = log_severity(message)
        target = w.ids.get("app") or w.ids["pod"]
        rows.append({
            "timestamp": ts_ns, "observed_timestamp": ts_ns, "target_id": target, "host_target_id": node_ids.get(w.node, ""),
            "collector_kind": "polestar_kcm", "event_source": "container_log", "event_level": "-",
            "trace_id": b"\x00" * 32, "span_id": b"\x00" * 16, "trace_flags": 0, "severity_text": sev, "severity_number": SEV_NUM[sev],
            "body": shift_body(message, shift), "service_name": w.name, "service_version": "-", "service_namespace": w.ns, "deployment_env": "-",
            "host_name": w.pod, "host_ip": "-", "k8s_namespace": w.ns, "k8s_pod_name": w.pod, "k8s_node_name": w.node, "k8s_container_name": w.name,
            "log_attributes": {}, "resource_attributes": {"service.name": w.name, "lucida.target_id": target, "k8s.pod.name": w.pod},
        })
    return rows


def write_parquet(rows: list[dict[str, Any]], table: str, out: Path) -> None:
    schema = pq.read_schema(REF_CAPTURE / "data" / "clickhouse" / f"{table}.parquet")
    columns = {}
    for f in schema:
        vals = [r.get(f.name) for r in rows]
        if pa.types.is_map(f.type):
            vals = [list((v or {}).items()) for v in vals]
        columns[f.name] = pa.array(vals, type=f.type)
    pq.write_table(pa.table(columns, schema=schema), out)


# ------------------------------------------------------------------------------------------ traces

MONGO_OP = re.compile(r"^(find|insert|update|delete|getMore|aggregate|count|findAndModify|distinct|killCursors|createIndexes|drop)\s+([\w.-]+)$")
HTTP_OP = re.compile(r"^(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+(\S+)$")
ZERO16 = b"\x00" * 16


def write_traces(src: Source, ws: dict[str, Workload], shift: float, out: Path, batch: int = 200_000) -> int:
    """RCAEval traces.csv (traceID, spanID, parentSpanID, serviceName, operationName, startTime µs, duration µs,
    statusCode) -> otel_traces_local. span_kind is not in the source, so it is derived from structure only:
    no parent or a parent in another service -> SERVER; a child in another service -> CLIENT; a MongoDB driver
    command span ("<command> <db>.<collection>", the driver's naming format) -> CLIENT with db.*; else INTERNAL.
    HTTP spans ("<METHOD> <path>") get http.request.method / url.path. Empty source status -> UNSET."""
    path = (src.run_dir / "traces.csv") if src.run_dir else None
    if path is None or not path.exists():
        return 0
    fields = ("traceID", "spanID", "parentSpanID", "serviceName", "operationName", "startTime", "duration")
    service_of: dict[tuple[str, str], str] = {}
    with path.open() as handle:
        for row in csv.DictReader(handle):
            if all(row.get(k) is not None for k in fields):
                service_of[(row["traceID"], row["spanID"])] = row["serviceName"]
    if not service_of:  # header-only file (e.g. RCAEval Sock Shop): traces not collected
        return 0
    calls_out: set[tuple[str, str]] = set()
    with path.open() as handle:
        for row in csv.DictReader(handle):
            if any(row.get(k) is None for k in fields):
                continue
            parent = (row["traceID"], row["parentSpanID"])
            if row["parentSpanID"] and service_of.get(parent) not in (None, row["serviceName"]):
                calls_out.add(parent)
    schema = pq.read_schema(REF_CAPTURE / "data" / "clickhouse" / "otel_traces_local.parquet")
    writer = pq.ParquetWriter(out, schema)
    shift_ns = int(shift * 1e9)
    rows: list[dict[str, Any]] = []
    total = 0

    def flush() -> None:
        columns = {}
        for f in schema:
            vals = [r.get(f.name) for r in rows]
            if pa.types.is_map(f.type):
                vals = [list((v or {}).items()) for v in vals]
            columns[f.name] = pa.array(vals, type=f.type)
        writer.write_table(pa.table(columns, schema=schema))
        rows.clear()

    with path.open() as handle:
        for row in csv.DictReader(handle):
            if any(row.get(k) is None for k in fields):
                continue
            svc, op = row["serviceName"], row["operationName"]
            key, parent_key = (row["traceID"], row["spanID"]), (row["traceID"], row["parentSpanID"])
            parent_svc = service_of.get(parent_key) if row["parentSpanID"] else None
            attrs: dict[str, str] = {}
            mongo, http = MONGO_OP.match(op), HTTP_OP.match(op)
            if not row["parentSpanID"] or (parent_svc is not None and parent_svc != svc):
                kind = "SERVER"
            elif key in calls_out:
                kind = "CLIENT"
            elif mongo:
                kind = "CLIENT"
                target = mongo.group(2)
                db = target.split(".", 1)[0]
                attrs.update({"db.system": "mongodb", "db.operation.name": mongo.group(1), "db.name": db, "server.address": db,
                              "db.collection.name": target.split(".", 1)[1] if "." in target else ""})
            else:
                kind = "INTERNAL"
            if http:
                attrs.update({"http.request.method": http.group(1), "url.path": http.group(2)})
            try:
                start_ns = int(row["startTime"]) * 1000 + shift_ns
                dur_ns = max(0, int(float(row["duration"])) * 1000)
            except ValueError:
                continue
            w = ws.get(svc)
            target = (w.ids.get("app") or w.ids["pod"]) if w else ""
            rows.append({
                "timestamp": start_ns, "trace_id": row["traceID"].encode()[:32].ljust(32, b"0"), "span_id": row["spanID"].encode()[:16].ljust(16, b"0"),
                "parent_span_id": row["parentSpanID"].encode()[:16].ljust(16, b"0") if row["parentSpanID"] else ZERO16,
                "trace_state": "-", "span_name": op, "span_kind": kind, "status_code": "ERROR" if row.get("statusCode", "").startswith("5") else "UNSET",
                "status_message": "-", "duration_ns": dur_ns, "end_timestamp": start_ns + dur_ns, "service_name": svc, "service_version": "-",
                "service_namespace": w.ns if w else "-", "deployment_env": "-", "host_name": w.pod if w else "-",
                "k8s_namespace": w.ns if w else "-", "k8s_pod_name": w.pod if w else "-", "k8s_node_name": w.node if w else "-",
                "span_attributes": attrs, "resource_attributes": {"service.name": svc, "lucida.target_id": target} if target else {"service.name": svc},
                "events_name": [], "events_timestamp": [], "events_attributes": [], "links_trace_id": [], "links_span_id": [],
                "otel_schema_url": "-", "scope_name": "rcaeval", "scope_version": "-",
            })
            if len(rows) >= batch:
                total += len(rows)
                flush()
    if rows:
        total += len(rows)
        flush()
    writer.close()
    return total


# ------------------------------------------------------------------------------------------ PostgreSQL registry

def pg_rows(case_id: str, ws: dict[str, Workload], node_ids: dict[str, str], cluster: str, created: str) -> dict[str, list[dict[str, Any]]]:
    targets, krt, kres, members, collectors = [], [], [], [], []

    def k8s(kind: str, key: str, target: str, parent: str, resource_kind: str, resource_type: str, extra: dict | None = None, label_kind: str | None = None) -> None:
        name = f"{cluster}:{label_kind or kind}:{key}"
        meta = {"id_path": [cluster] + ([parent] if parent != cluster else []), "namespace": key.split("/")[0] if "/" in key else "",
                "resource_key": key, "resource_kind": resource_kind, "resource_type": resource_type, "host_target_id": cluster,
                "parent_target_id": parent, "cluster_target_id": cluster, **(extra or {})}
        targets.append({"id": target, "type": "kubernetes_resource", "name": name, "display_name": key.split("/")[-1], "meta": meta})
        krt.append({"target_id": target, "cluster_target_id": cluster, "parent_target_id": parent, "resource_kind": label_kind or kind, "resource_key": key, "meta": {"name": key.split("/")[-1]}})

    ns = next(iter(ws.values())).ns if ws else "sock-shop"
    group = "s_" + re.sub(r"[^a-z0-9]", "", ns)
    targets.append({"id": cluster, "type": "kubernetes", "name": f"cluster-{cluster}", "display_name": f"{ns}-cluster",
                    "meta": {"icon": "k8s:cluster", "id_path": [], "cluster_id": f"cluster-{cluster}", "resource_key": cluster, "resource_kind": "cluster",
                             "resource_type": "kcm.cluster", "host_target_id": cluster, "collector": "polestar-agent"}})
    members.append({"scope": "unified", "kind": "system", "group_id": "default", "asset_ref": cluster, "target_id": cluster})
    collectors.append({"target_id": cluster, "kind": "polestar_kcm", "created_at": created})
    for node, nid in node_ids.items():
        k8s("node", node, nid, cluster, "node", "kcm.node", {"icon": "k8s:node", "series_labels": {"node": node}})
    for w in ws.values():
        ns_name = f"{w.ns}/{w.name}"
        k8s("deployment", ns_name, w.ids["deployment"], cluster, "deployment", "kcm.deployment", {"icon": "k8s:deployment", "series_labels": {"namespace": w.ns, "deployment": w.name}})
        k8s("replicaset", f"{w.ns}/{w.rs}", w.ids["replicaset"], cluster, "replica_set", "kcm.replica_set", {"icon": "k8s:replicaset"})
        k8s("pod", f"{w.ns}/{w.pod}", w.ids["pod"], cluster, "pod", "kcm.pod")
        k8s("container", f"{w.ns}/{w.pod}/{w.name}", w.ids["container"], w.ids["pod"], "container", "kcm.container")
        k8s("service", ns_name, w.ids["service"], cluster, "service", "kcm.service", {"icon": "k8s:service", "series_labels": {"service": w.name, "namespace": w.ns}})
        kres.append({"target_id": cluster, "kind": "pod", "uid": w.ids["pod"], "namespace": w.ns, "name": w.pod,
                     "data": {"podName": w.pod, "nodeName": w.node, "namespaceName": w.ns, "containers": [w.name], "status": "Running"}})
        kres.append({"target_id": cluster, "kind": "deployment", "uid": w.ids["deployment"], "namespace": w.ns, "name": w.name,
                     "data": {"deploymentName": w.name, "namespaceName": w.ns, "desiredPods": 1, "availablePods": 1}})
        kres.append({"target_id": cluster, "kind": "service", "uid": w.ids["service"], "namespace": w.ns, "name": w.name,
                     "data": {"serviceName": w.name, "namespaceName": w.ns, "type": "ClusterIP"}})
        for key in ("deployment", "replicaset", "service"):
            members.append({"scope": "unified", "kind": "system", "group_id": "default", "asset_ref": w.ids[key], "target_id": w.ids[key]})
        if w.app:
            app = w.ids["app"]
            rtype = w.lang or GENERIC_SERVICE
            targets.append({"id": app, "type": "application", "name": w.name, "display_name": w.name,
                            "meta": {"icon": "app:service", "group": ns, "id_path": [], "resource_key": app, "resource_kind": rtype.split(".")[-1],
                                     "resource_type": rtype, "service_name": w.name, "host_target_id": app, "collector": "istio-mesh",
                                     "language_basis": w.lang_basis}})
            members.append({"scope": "unified", "kind": "system", "group_id": "default", "asset_ref": app, "target_id": app})
            members.append({"scope": "unified", "kind": "service", "group_id": group, "asset_ref": app, "target_id": app, "label": w.name})
            collectors.append({"target_id": app, "kind": "otel_recv", "created_at": created})
    return {"targets": targets, "kcm_resource_targets": krt, "kcm_resources": kres, "asset_tree_members": members, "collectors": collectors,
            "groups": [{"id": group, "name": ns}]}


def pg_dump(rows: dict[str, list[dict[str, Any]]], case_id: str, container: str, template: str, out: Path) -> None:
    db = "c_" + hashlib.md5(case_id.encode()).hexdigest()[:12]

    def psql(sql: str, dbname: str = "postgres") -> None:
        subprocess.run(["docker", "exec", "-i", container, "psql", "-v", "ON_ERROR_STOP=1", "-q", "-U", "lucida", "-d", dbname], input=sql, text=True, check=True, capture_output=True)

    def lit(v: Any) -> str:
        if v is None:
            return "NULL"
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)
        return "'" + str(v).replace("'", "''") + "'"

    psql(f"DROP DATABASE IF EXISTS {db}; CREATE DATABASE {db} TEMPLATE {template};")
    stmts = ["SET search_path = public;",
             *[f"INSERT INTO asset_tree_groups(id, scope, kind, name) VALUES ({lit(g['id'])},'unified','service',{lit(g['name'])}) ON CONFLICT DO NOTHING;" for g in rows.get("groups", [])]]
    for t in rows["targets"]:
        stmts.append(f"INSERT INTO targets(id,type,name,display_name,status,meta) VALUES ({lit(t['id'])},{lit(t['type'])},{lit(t['name'])},{lit(t['display_name'])},'active',{lit(t['meta'])}::jsonb);")
    for k in rows["kcm_resource_targets"]:
        stmts.append(f"INSERT INTO kcm_resource_targets(target_id,cluster_target_id,parent_target_id,resource_kind,resource_key,meta) VALUES ({lit(k['target_id'])},{lit(k['cluster_target_id'])},{lit(k['parent_target_id'])},{lit(k['resource_kind'])},{lit(k['resource_key'])},{lit(k['meta'])}::jsonb);")
    for r in rows["kcm_resources"]:
        stmts.append(f"INSERT INTO kcm_resources(target_id,kind,uid,namespace,name,data) VALUES ({lit(r['target_id'])},{lit(r['kind'])},{lit(r['uid'])},{lit(r['namespace'])},{lit(r['name'])},{lit(r['data'])}::jsonb);")
    for m in rows["asset_tree_members"]:
        stmts.append(f"INSERT INTO asset_tree_members(scope,kind,group_id,asset_ref,target_id,label) VALUES ({lit(m['scope'])},{lit(m['kind'])},{lit(m['group_id'])},{lit(m['asset_ref'])},{lit(m['target_id'])},{lit(m.get('label'))}) ON CONFLICT DO NOTHING;")
    for c in rows["collectors"]:
        stmts.append(f"INSERT INTO collectors(target_id,kind,enabled,last_collect_status,created_at,updated_at) VALUES ({lit(c['target_id'])},{lit(c['kind'])},true,'success',{lit(c['created_at'])},{lit(c['created_at'])});")
    psql("\n".join(stmts), db)
    dump = subprocess.run(["docker", "exec", container, "pg_dump", "-U", "lucida", "-d", db, "-Fc", "--no-owner"], capture_output=True, check=True)
    out.write_bytes(dump.stdout)
    psql(f"DROP DATABASE {db};")


# ------------------------------------------------------------------------------------------ driver

def convert(run_dir: Path, src_root: Path, out_root: Path, args: argparse.Namespace) -> dict[str, Any] | None:
    src = read_source(run_dir, src_root)
    case_id = f"case-rcaeval-{args.tag}-v3-{hashlib.sha1(src.rel.encode()).hexdigest()[:8]}"
    shift = args.anchor.timestamp() - src.times[0]
    ws, node_ids, cluster = workloads(src, case_id)
    if src.service not in ws or not ws[src.service].app:
        print(f"  skip {src.rel}: root service {src.service} not an application workload", flush=True)
        return None
    series = metric_series(src, ws, node_ids, cluster, shift)
    names = {w.ids["pod"]: w.name for w in ws.values()} | {w.ids["app"]: w.name for w in ws.values() if w.app}
    capture_start_ms = int((src.times[0] + shift) * 1000)
    scope = SYMPTOM_METRICS + RESOURCE_METRICS if args.alarm_scope == "all" else SYMPTOM_METRICS
    episodes = detect(series, capture_start_ms, args.detector_baseline, metrics=scope)
    entry = args.entry_service or SYSTEMS.get(src.system, SYSTEMS["SS"])["entry"]
    if args.alarm_scope == "entry":
        # only the user-facing entry service alarms (an architectural fact of the app, not the label): the
        # investigation has to walk from the user-visible symptom to the origin
        episodes = [ep for ep in episodes if ep["series"]["metric"].get("service_name") == entry]
    events = event_rows(episodes, names, case_id)
    opens = sorted(e["occurred_at"] for e in events if e["transition"] == "open")
    if not opens:
        print(f"  skip {src.rel}: blind detector found no anomaly", flush=True)
        return None
    t1 = opens[0] / 1e9
    t2 = src.times[-1] + shift
    case_dir = out_root / case_id
    (case_dir / "data" / "clickhouse").mkdir(parents=True, exist_ok=True)
    with (case_dir / "data" / "victoriametrics.export").open("w") as handle:
        for s in series:
            handle.write(json.dumps(s) + "\n")
    write_parquet(log_rows(src, ws, node_ids, shift), "lucida_logs_local", case_dir / "data" / "clickhouse" / "lucida_logs_local.parquet")
    write_parquet(events, "lucida_events_local", case_dir / "data" / "clickhouse" / "lucida_events_local.parquet")
    spans = write_traces(src, ws, shift, case_dir / "data" / "clickhouse" / "otel_traces_local.parquet")
    not_collected = [d for d in NOT_COLLECTED if not (d == "traces" and spans > 0)]
    created = iso(src.times[0] + shift - 30 * 86400)
    pg_dump(pg_rows(case_id, ws, node_ids, cluster, created), case_id, args.pg_container, args.pg_template, case_dir / "data" / "postgres.dump")
    have = "컨테이너·노드 지표, 서비스 메시(istio) 지표, 컨테이너 로그, 합성 이상 알람" + (", 분산 트레이스(span 종류는 구조 추론·오류 상태 없음)" if spans else "")
    (case_dir / "data" / "sources.json").write_text(json.dumps({"not_collected": not_collected, "note": f"외부 공개 데이터셋(RCAEval) 변환 캡처 — {have}만 있다."}, ensure_ascii=False, indent=1))
    title, cause, evidence = (x.format(svc=src.service) for x in FAULT_TEXT[src.fault])
    meta = {
        "case_id": case_id, "t1": iso(t1), "t2": iso(t2), "capture_start": iso(src.times[0] + shift), "capture_end": iso(src.times[-1] + shift),
        "scenario_metadata": {"title": f"{src.service} {title}", "description": f"RCAEval {args.tag.upper()} {src.system} case: {cause}.", "cause": cause,
                              "injection_summary": f"{cause} at {iso(src.inject + shift)}", "user_impact": "front-end request latency and/or errors rise",
                              "distinguishing_evidence": evidence},
        "source": {"dataset": "RCAEval", "record": "zenodo:14590730", "license": "CC BY 4.0", "citation": "Pham et al., RCAEval, WWW'25 companion",
                   "path": src.rel, "root_service": src.service, "fault": src.fault, "inject_time": iso(src.inject + shift), "time_shift_s": shift},
    }
    (case_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    w = ws[src.service]
    # rule A: confirmed needs the root anomaly and the impact linked in time AND path; without traces the call path
    # is not observable, so traceless captures are provisional (the pilot teacher answered provisional in 8/12).
    status = "provisional" if "traces" in not_collected else "confirmed"
    golden = {"expected_status": status, "roots": [{"target_ids": [w.ids["app"], w.ids["deployment"], w.ids["replicaset"], w.ids["pod"], w.ids["container"], w.ids["service"]], "target_aliases": [w.name]}]}
    print(f"  {case_id}: {src.rel} series={len(series)} logs={len(src.logs)} spans={spans} alarms={len(opens)} t1={meta['t1']} status={status}", flush=True)
    return {"case_id": case_id, "golden": golden}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--src", type=Path, required=True, help="RCAEval system directory, e.g. data/external/rcaeval/RE2-SS")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--tag", default="ss2", help="short dataset tag in case ids / config names")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--anchor", type=dt.datetime.fromisoformat, default=dt.datetime(2026, 8, 20, 9, 0, tzinfo=dt.UTC))
    parser.add_argument("--detector-baseline", type=int, default=480, help="seconds from capture start used as the detector baseline")
    parser.add_argument("--alarm-scope", choices=("symptom", "entry", "all"), default="symptom",
                        help="symptom: service SLI alarms of every service (default); entry: only the entry service's SLI alarms; all: also container resource alarms")
    parser.add_argument("--entry-service", default=None, help="user-facing entry service for --alarm-scope entry (default: per system)")
    parser.add_argument("--pg-container", default="rcaeval-pgtool")
    parser.add_argument("--pg-template", default="rcae_tmpl")
    parser.add_argument("--golden", type=Path, default=None)
    parser.add_argument("--split", type=Path, default=None)
    args = parser.parse_args()
    runs = sorted(p for p in args.src.glob("*/*") if (p / "metrics.csv").exists())
    if args.limit:
        # first repetition of every <service>_<fault>, interleaved by fault so a small pilot covers every fault type
        firsts = sorted({p.parent: p for p in sorted(runs, key=lambda p: p.name, reverse=True)}.values())
        by_fault: dict[str, list[Path]] = {}
        for p in firsts:
            by_fault.setdefault(p.parent.name.rsplit("_", 1)[1], []).append(p)
        order = [lst[i] for i in range(max(map(len, by_fault.values()))) for lst in by_fault.values() if i < len(lst)]
        runs = order[: args.limit]
    args.out.mkdir(parents=True, exist_ok=True)
    golden, cases = {}, []
    for run in runs:
        try:
            res = convert(run, args.src, args.out, args)
        except SourceDefect as exc:
            print(f"  skip {run.relative_to(args.src.parent)}: {exc}", flush=True)
            continue
        if res:
            golden[res["case_id"]] = res["golden"]
            cases.append(res["case_id"])
    golden_path = args.golden or ROOT / "configs" / "eval" / f"rcaeval-{args.tag}.yaml"
    split_path = args.split or ROOT / "configs" / "teacher" / f"rcaeval-{args.tag}-split.yaml"
    header = f"# RCAEval {args.tag} converted captures (scripts/rcaeval_convert.py). Source: Pham et al., RCAEval, WWW'25 companion; Zenodo 14590730, CC BY 4.0.\n"
    golden_path.write_text(header + yaml.safe_dump({"version": 1, "cases": golden}, sort_keys=False))
    split_path.write_text(header + yaml.safe_dump({"train": cases, "validation": [], "sealed_eval": []}, sort_keys=False))
    print(f"converted {len(cases)}/{len(runs)} -> {args.out}; golden {golden_path}; split {split_path}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
