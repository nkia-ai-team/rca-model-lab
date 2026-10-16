"""Convert OpenRCA 2.0 cases (HuggingFace anon-ops/ops-lite, Apache-2.0) into rca-mcp captures.

  uv run python scripts/openrca2_convert.py --src data/external/openrca2/ops-lite \
      --out data/external/openrca2/captures --tag o2a --limit 12 --system all

Each complete case directory (cases/<case>/, ten telemetry parquet files plus the four answer files) becomes one
capture directory in the layout scripts/rcaeval_convert.py produces, which tools/rca-mcp/replay/restore.sh loads:

  meta.json                         t1/t2, capture window, scenario_metadata, source (grader-only)
  data/victoriametrics.export       kcm.* series under the cluster target (namespace/pod/container labels) and
                                    trace-derived service SLIs (apm.trace_derived.*) under each application target
  data/clickhouse/otel_traces_local.parquet         spans with their real kind / status / HTTP attributes
  data/clickhouse/lucida_logs_local.parquet
  data/clickhouse/lucida_events_local.parquet       blind synthetic anomaly episodes on the trace-derived SLIs
  data/clickhouse/trace_error_chains_local.parquet  1-minute structural summaries (port of the lucida-next
  data/clickhouse/trace_path_signatures_local.parquet  trace-anomaly aggregation, see structural_summaries)
  data/postgres.dump                target registry (cluster, nodes, workloads, replicasets, pods, containers,
                                    k8s services, applications)
  data/sources.json                 datasets that were not collected

plus a golden contract (configs/eval/openrca2-<tag>.yaml) and a split (configs/teacher/openrca2-<tag>-split.yaml).

Blindness rules (as in rcaeval_convert):
- The answer files (label.json, causal_graph.json, injection.json, env.json's abnormal bounds) feed only meta.json and
  the golden contract. Case ids are hashes; the OpenRCA case name, chaos types and the injection record never reach the
  telemetry, the registry or sources.json (`blindness_violations` checks every converted capture).
- The alarm detector is label-blind: its baseline is the first `--detector-baseline` seconds of the capture, measured
  from the capture start, never from the abnormal-window start.
- t1/t2 are the abnormal window (the benchmark itself hands agents a normal/abnormal file split, so the onset is part of
  the benchmark input, not of the answer).
- Timestamps are shifted so the capture starts at `--anchor`; ISO date-times inside log bodies move with them.
"""

from __future__ import annotations

import argparse
import datetime as dt
import functools
import hashlib
import itertools
import json
import math
import re
import shutil
import sys
import tempfile
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rcaeval_convert as rc  # shared capture writers, detector, PG dump

ROOT = rc.ROOT
DATASET = "OpenRCA 2.0"
HF_REPO = "anon-ops/ops-lite"
HF_REVISION = "581eba39d0bfd2d397eded6cd7cbe3303255521a"
LICENSE = "Apache-2.0"
NS = uuid.uuid5(uuid.NAMESPACE_URL, "openrca2-convert/v1")
STEP = rc.STEP  # seconds per exported sample (source metrics arrive every 5-15 s, irregularly)
PARTS = ("traces", "metrics", "metrics_histogram", "metrics_sum", "logs")
TELEMETRY_FILES = tuple(f"{phase}_{part}.parquet" for phase in ("normal", "abnormal") for part in PARTS)
ANSWER_FILES = ("env.json", "injection.json", "label.json", "causal_graph.json")  # grader-only
SYSTEMS = ("ts", "hs", "otel-demo")
SYSTEM_NAMES = {"ts": "TrainTicket", "hs": "Hotel Reservation (DeathStarBench)", "otel-demo": "OpenTelemetry Demo"}
PUBLIC_COMPARE_FRACTION = 0.2
SOURCE_ID = "external"  # lucida_events_local.source_id: neutral, does not name the benchmark

# ------------------------------------------------------------------------------------------ metric mapping
# Source units are the OpenTelemetry kubeletstats / k8s_cluster receiver units: CPU in cores, memory and filesystem
# in bytes, *.utilization as a 0-1 ratio. Targets are the Polestar KCM names rcaeval_convert emits (KCM_CONTAINER,
# KCM_POD) and, where it has none, the Polestar KCM name for the same quantity (checked against the reference capture
# export: kcm.*.cpu_* in millicores, *_utilization_* in percent, memory in bytes).
CORES_TO_MILLICORES = 1000.0  # cores -> millicores (Polestar kcm.*.cpu_* unit)
RATIO_TO_PERCENT = 100.0  # 0-1 ratio -> percent (Polestar kcm.pod.*_utilization_* unit)
CONTAINER_GAUGES = {  # source metric -> (kcm name, scale); labels namespace/pod/container under the cluster target
    "container.cpu.usage": ("kcm.container.cpu_usage", CORES_TO_MILLICORES),  # cores -> mCPU
    "container.memory.working_set": ("kcm.container.mem_usage", 1.0),  # bytes; working set = rcaeval's mem_usage
    "container.memory.usage": ("kcm.container.mem_usage_total", 1.0),  # bytes
    "container.memory.rss": ("kcm.container.mem_rss", 1.0),  # bytes
    "k8s.container.cpu_limit": ("kcm.container.cpu_limit", CORES_TO_MILLICORES),  # cores -> mCPU
    "k8s.container.cpu_request": ("kcm.container.cpu_request", CORES_TO_MILLICORES),  # cores -> mCPU
    "k8s.container.memory_limit": ("kcm.container.mem_limit", 1.0),  # bytes
    "k8s.container.memory_request": ("kcm.container.mem_request", 1.0),  # bytes
}
POD_GAUGES = {  # labels namespace/pod under the cluster target
    "k8s.pod.cpu.usage": ("kcm.pod.cpu_usage", CORES_TO_MILLICORES),  # cores -> mCPU
    "k8s.pod.memory.working_set": ("kcm.pod.mem_working_set", 1.0),  # bytes
    "k8s.pod.memory.usage": ("kcm.pod.mem_usage", 1.0),  # bytes
    "k8s.pod.cpu.node.utilization": ("kcm.pod.cpu_utilization_by_node_capacity", RATIO_TO_PERCENT),  # ratio -> %
    "k8s.pod.memory.node.utilization": ("kcm.pod.mem_utilization_by_node_capacity", RATIO_TO_PERCENT),  # ratio -> %
    "k8s.pod.cpu_limit_utilization": ("kcm.pod.cpu_utilization_by_limit", RATIO_TO_PERCENT),  # ratio -> %
    "k8s.pod.memory_limit_utilization": ("kcm.pod.mem_utilization_by_limit", RATIO_TO_PERCENT),  # ratio -> %
    "k8s.pod.filesystem.usage": ("kcm.pod.ephemeral_storage_usage", 1.0),  # bytes (pod ephemeral storage)
}
POD_SUM_OF_CONTAINERS = {  # per-container gauges summed over the pod's containers at each sample time
    "k8s.container.restarts": ("kcm.pod.container_restart_count", 1.0),  # count (get_k8s_state restart probe)
    "k8s.container.ready": ("kcm.pod.container_ready", 1.0),  # ready containers (no Polestar equivalent)
    "k8s.container.cpu_limit": ("kcm.pod.cpu_limit", CORES_TO_MILLICORES),  # cores -> mCPU
    "k8s.container.memory_limit": ("kcm.pod.mem_limit", 1.0),  # bytes
}
# Cumulative counters (metrics_sum). The export dropped the `direction` attribute of k8s.pod.network.*: every pod
# carries two indistinguishable rows (receive and transmit) per sample, so only their sum (rx+tx) is reproducible.
# Rate = delta(sum) / delta(t) between consecutive samples; a negative delta is a counter reset and is skipped.
POD_COUNTER_RATES = {
    "k8s.pod.network.io": ("kcm.pod.network_rxtx_bps", 1.0),  # bytes -> bytes/s (rx+tx)
    "k8s.pod.network.errors": ("kcm.pod.network_rxtx_err_ps", 1.0),  # errors -> errors/s (rx+tx)
}
WORKLOAD_GAUGES = {  # (source, owner attribute) -> (kcm name, owner kind)
    "k8s.deployment.available": ("kcm.deployment.available_replicas", "deployment"),
    "k8s.deployment.desired": ("kcm.deployment.desired_replicas", "deployment"),
    "k8s.replicaset.available": ("kcm.replica_set.available_replicas", "replicaset"),
    "k8s.replicaset.desired": ("kcm.replica_set.desired_replicas", "replicaset"),
    "k8s.statefulset.ready_pods": ("kcm.stateful_set.ready_replicas", "statefulset"),
    "k8s.statefulset.desired_pods": ("kcm.stateful_set.desired_replicas", "statefulset"),
    "k8s.statefulset.updated_pods": ("kcm.stateful_set.updated_replicas", "statefulset"),
}
METRIC_INPUTS = set(CONTAINER_GAUGES) | set(POD_GAUGES) | set(POD_SUM_OF_CONTAINERS) | set(WORKLOAD_GAUGES)
SUM_INPUTS = set(POD_COUNTER_RATES)
# Runtime metric namespaces (OpenTelemetry semantic conventions) -> application resource type. A service is typed only
# when it emits metrics of exactly one runtime; the app-level runtime metrics themselves are not converted (the export
# dropped their dimension attributes: memory pool, GC name on most rows, route, status).
RUNTIME_PREFIXES = {
    "jvm.": "otel-java.otel_java", "go.": "otel-go.otel_go", "v8js.": "otel-node.otel_node", "nodejs.": "otel-node.otel_node",
    "cpython.": "otel-python.otel_python", "process.runtime.cpython.": "otel-python.otel_python", "dotnet.": rc.GENERIC_SERVICE,
}
SYSTEM_LANGUAGE = {"hs": ("otel-go.otel_go", "dataset_card_hs_go_grpc")}  # dataset card: Hotel Reservation is Go/gRPC

# ------------------------------------------------------------------------------------------ trace-derived SLIs
# Server spans per service per STEP bucket (span start time). Names follow the Polestar APM vocabulary
# (apm.agent.otel.java.{rps,percentile95,error_rate,...}) under an `apm.trace_derived.` prefix that says where they
# come from; units match that APM family (checked on the reference capture: percentile in ms, rps in req/s,
# error_rate in percent).
SLI = {
    "rps": "apm.trace_derived.rps",  # server spans / bucket seconds
    "span_count": "apm.trace_derived.span_count",  # server spans in the bucket
    "error_count": "apm.trace_derived.error_count",  # server spans with status ERROR or HTTP >= 500
    "error_rate": "apm.trace_derived.error_rate",  # 100 * error_count / span_count (absent when span_count == 0)
    "p50": "apm.trace_derived.percentile50", "p95": "apm.trace_derived.percentile95", "p99": "apm.trace_derived.percentile99",  # ms
}
SYMPTOM_METRICS = (SLI["p95"], SLI["p99"], SLI["error_rate"], SLI["rps"])
RESOURCE_METRICS = ("kcm.pod.cpu_usage", "kcm.pod.mem_working_set", "kcm.pod.network_rxtx_bps", "kcm.pod.container_restart_count")
# Detector floors / directions in the SLI units, same semantics as rcaeval_convert (latency 5 ms, error rate 1 point,
# request rate 0.05 req/s; latency and error rate alarm upward only). Registered into the shared detector's tables
# (additive: these names never collide with the rcaeval mesh.istio.* names).
ALARM_FLOORS = {SLI["p95"]: 5.0, SLI["p99"]: 5.0, SLI["error_rate"]: 1.0, SLI["rps"]: 0.05,
                "kcm.pod.cpu_usage": 10.0, "kcm.pod.mem_working_set": 8 * 2**20, "kcm.pod.network_rxtx_bps": 1024.0,
                "kcm.pod.container_restart_count": 1.0}
UPWARD_ONLY = (SLI["p95"], SLI["p99"], SLI["error_rate"], "kcm.pod.container_restart_count")
rc.ALARM_FLOORS.update(ALARM_FLOORS)
rc.UPWARD_ONLY = tuple(dict.fromkeys(tuple(rc.UPWARD_ONLY) + UPWARD_ONLY))

KIND = {"Server": "SERVER", "Client": "CLIENT", "Internal": "INTERNAL", "Producer": "PRODUCER", "Consumer": "CONSUMER"}
STATUS = {"Error": "ERROR", "Ok": "OK", "Unset": "UNSET"}
# Span-name grammars only (no semantics): the OpenTelemetry DB span name "<operation> <db>.<table>" and the HTTP client
# name "HTTP <METHOD> <url>". The SQL engine is not in the name, so db.system is the semconv value for an unknown SQL DB.
SQL_SPAN = re.compile(r"^(SELECT|INSERT|UPDATE|DELETE|REPLACE|MERGE|CALL|CREATE|DROP|ALTER|TRUNCATE)\s+([\w$]+)\.([\w$.]+)$")
HTTP_URL_SPAN = re.compile(r"^HTTP (GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS) (https?://\S+)$")
ZERO16 = b"\x00" * 16

# Chaos Mesh fault semantics (grader-side reference text for the mechanism judge only; never written to telemetry).
CHAOS_GLOSS = {
    "PodFailure": "pod made unavailable for the fault window (Chaos Mesh pod-failure)",
    "PodKill": "pod killed and recreated by its controller",
    "ContainerKill": "container killed and restarted",
    "CPUStress": "CPU stress workers run inside the container",
    "MemoryStress": "memory stress workers allocate memory inside the container",
    "NetworkDelay": "added network latency on the service's traffic",
    "NetworkLoss": "packet loss on the service's traffic",
    "NetworkCorrupt": "packet corruption on the service's traffic",
    "NetworkDuplicate": "packet duplication on the service's traffic",
    "NetworkPartition": "network partition (traffic blocked) between the service and its peer",
    "NetworkBandwidth": "bandwidth limit on the service's traffic",
    "DNSError": "DNS lookups of the named domain fail in the service",
    "DNSRandom": "DNS lookups of the named domain return random addresses in the service",
    "JVMLatency": "latency added to a JVM method of the service",
    "JVMRuntimeMutator": "runtime mutation of a JVM method's behaviour in the service",
    "JVMException": "a JVM method of the service throws an exception",
    "JVMReturn": "a JVM method of the service returns a substituted value",
    "JVMMemoryStress": "memory stress inside the service's JVM",
    "HTTPRequestAbort": "HTTP requests on an endpoint of the service are aborted (Chaos Mesh HTTPChaos)",
    "HTTPResponseAbort": "HTTP responses on an endpoint of the service are aborted (Chaos Mesh HTTPChaos)",
    "HTTPRequestDelay": "HTTP requests on an endpoint of the service are delayed (Chaos Mesh HTTPChaos)",
    "HTTPResponseDelay": "HTTP responses on an endpoint of the service are delayed (Chaos Mesh HTTPChaos)",
    "HTTPRequestReplaceMethod": "the HTTP method of requests on an endpoint of the service is replaced (Chaos Mesh HTTPChaos)",
    "HTTPRequestReplacePath": "the path of HTTP requests on an endpoint of the service is replaced (Chaos Mesh HTTPChaos)",
    "HTTPResponseReplaceBody": "the body of HTTP responses on an endpoint of the service is replaced (Chaos Mesh HTTPChaos)",
    "HTTPResponseReplaceCode": "the status code of HTTP responses on an endpoint of the service is replaced (Chaos Mesh HTTPChaos)",
}
PARAM_SKIP = {"app", "chaos_type", "namespace", "system", "system_type", "duration", "time_offset"}


def uid(case_id: str, kind: str, key: str) -> str:
    return str(uuid.uuid5(NS, f"{case_id}|{kind}|{key}"))


def case_id_of(name: str, tag: str) -> str:
    return f"case-openrca2-{tag}-v3-{hashlib.sha1(name.encode()).hexdigest()[:8]}"


def in_public_compare(name: str) -> bool:
    """Deterministic, membership-stable hold-out: a case's bucket depends only on its own name, so converting more
    cases later never moves an already held-out case into train (~20% of every system in expectation)."""
    h = int(hashlib.sha1(f"openrca2-public-compare/v1|{name}".encode()).hexdigest()[:8], 16)
    return h / 2**32 < PUBLIC_COMPARE_FRACTION


# ------------------------------------------------------------------------------------------ source

def case_complete(case_dir: Path) -> bool:
    if not all((case_dir / f).exists() for f in TELEMETRY_FILES + ANSWER_FILES):
        return False
    try:
        for f in TELEMETRY_FILES:
            pq.ParquetFile(case_dir / f)
        for f in ANSWER_FILES:
            json.loads((case_dir / f).read_text())
    except (OSError, ValueError, pa.ArrowException):  # partially downloaded file
        return False
    return True


def read_part(case_dir: Path, part: str, columns: list[str] | None = None, metrics: set[str] | None = None) -> pa.Table:
    """normal_<part> + abnormal_<part> concatenated, `time` as int64 ns."""
    tables = []
    for phase in ("normal", "abnormal"):
        path = case_dir / f"{phase}_{part}.parquet"
        names = pq.read_schema(path).names
        t = pq.read_table(path, columns=[c for c in columns if c in names] if columns else None)
        if metrics is not None and t.num_rows:
            t = t.filter(pc.is_in(pc.cast(t["metric"], pa.string()), value_set=pa.array(sorted(metrics), pa.string())))
        t = t.set_column(t.schema.get_field_index("time"), "time", pc.cast(t["time"], pa.int64()))
        tables.append(t)
    return pa.concat_tables(tables, promote_options="permissive")


def rows_of(t: pa.Table, columns: list[str]) -> list[tuple]:
    cols = [t[c].to_pylist() if c in t.column_names else [None] * t.num_rows for c in columns]
    return list(zip(*cols, strict=True))


@dataclass
class Source:
    name: str
    case_dir: Path
    system: str
    ns: str
    start: int  # NORMAL_START (unix s)
    end: int  # ABNORMAL_END
    abnormal_start: int
    label: dict[str, Any]
    graph: dict[str, Any]
    injection: dict[str, Any]


def read_source(case_dir: Path) -> Source:
    env = json.loads((case_dir / "env.json").read_text())
    label = json.loads((case_dir / "label.json").read_text())
    return Source(case_dir.name, case_dir, label["system"], env["NAMESPACE"], int(env["NORMAL_START"]), int(env["ABNORMAL_END"]),
                  int(env["ABNORMAL_START"]), label, json.loads((case_dir / "causal_graph.json").read_text()),
                  json.loads((case_dir / "injection.json").read_text()))


# ------------------------------------------------------------------------------------------ entities

@dataclass
class Pod:
    name: str
    node: str = ""
    containers: set[str] = field(default_factory=set)
    first: float = math.inf
    last: float = -math.inf
    rs: str = ""


@dataclass
class Workload:
    name: str
    kind: str  # deployment | statefulset
    ns: str
    pods: dict[str, Pod] = field(default_factory=dict)
    replicasets: set[str] = field(default_factory=set)
    ids: dict[str, str] = field(default_factory=dict)
    desired: float | None = None
    available: float | None = None


@dataclass
class App:
    name: str
    workload: str | None
    lang: str
    lang_basis: str
    id: str = ""


def owner_from_pod_name(pod: str) -> tuple[str, str, str]:
    """(workload, kind, replicaset) from the Kubernetes pod naming convention (the same rule rca-mcp's
    podOwnerCandidates uses): <sts>-<ordinal> or <deployment>-<rs hash>-<pod id>."""
    segs = pod.split("-")
    if len(segs) >= 2 and segs[-1].isdigit():
        return "-".join(segs[:-1]), "statefulset", ""
    if len(segs) >= 3:
        return "-".join(segs[:-2]), "deployment", "-".join(segs[:-1])
    return segs[0], "deployment", ""


METRIC_COLUMNS = ["time", "metric", "value", "service_name", "attr.k8s.node.name", "attr.k8s.namespace.name", "attr.k8s.statefulset.name",
                  "attr.k8s.deployment.name", "attr.k8s.replicaset.name", "attr.k8s.pod.name", "attr.k8s.container.name"]


def build_entities(src: Source, metric_rows: list[tuple], trace_services: set[str], runtime_of: dict[str, set[str]], case_id: str
                   ) -> tuple[dict[str, Workload], dict[str, str], str, dict[str, App]]:
    """Workloads (deployments / statefulsets), their pods / replicasets / containers, nodes and applications."""
    pods: dict[str, Pod] = {}
    owner: dict[str, tuple[str, str]] = {}
    for t, _metric, _v, svc, node, ns, sts, dep, rs, pod, ctr in metric_rows:
        if not pod or (ns and ns != src.ns):
            continue
        p = pods.setdefault(pod, Pod(pod))
        ts = t / 1e9
        p.first, p.last = min(p.first, ts), max(p.last, ts)
        if node:
            p.node = node
        if ctr:
            p.containers.add(ctr)
        if rs:
            p.rs = rs
        if dep:
            owner[pod] = (dep, "deployment")
        elif sts:
            owner[pod] = (sts, "statefulset")
    workloads: dict[str, Workload] = {}
    for pod, p in sorted(pods.items()):
        name, kind, rs = owner_from_pod_name(pod)
        if pod in owner:
            name, kind = owner[pod]
            rs = rs if kind == "deployment" and rs.startswith(name + "-") else ""
        if kind == "deployment":
            p.rs = p.rs or rs
        w = workloads.setdefault(name, Workload(name, kind, src.ns))
        w.pods[pod] = p
        if p.rs:
            w.replicasets.add(p.rs)
    for _t, metric, value, _svc, _node, ns, sts, dep, rs, pod, _ctr in metric_rows:  # replicasets known only from k8s.replicaset.*
        if metric in ("k8s.replicaset.available", "k8s.replicaset.desired") and rs and not pod:
            name = rs.rsplit("-", 1)[0]
            if name in workloads and workloads[name].kind == "deployment":
                workloads[name].replicasets.add(rs)
        if metric in ("k8s.deployment.desired", "k8s.statefulset.desired_pods") and (dep or sts) in workloads:
            workloads[dep or sts].desired = value
        if metric in ("k8s.deployment.available", "k8s.statefulset.ready_pods") and (dep or sts) in workloads:
            workloads[dep or sts].available = value
    for w in workloads.values():
        w.ids = {w.kind: uid(case_id, w.kind, w.name), "service": uid(case_id, "service", w.name)}
        for rs in w.replicasets:
            w.ids[f"replicaset:{rs}"] = uid(case_id, "replicaset", rs)
        for pod, p in w.pods.items():
            w.ids[f"pod:{pod}"] = uid(case_id, "pod", pod)
            for ctr in p.containers:
                w.ids[f"container:{pod}/{ctr}"] = uid(case_id, "container", f"{pod}/{ctr}")
    nodes = sorted({p.node for p in pods.values() if p.node})
    node_ids = {n: uid(case_id, "node", n) for n in nodes}
    cluster = uid(case_id, "cluster", src.ns)
    apps = {}
    for svc in sorted(trace_services):
        runtimes = runtime_of.get(svc, set())
        if len(runtimes) == 1:
            lang, basis = next(iter(runtimes)), "runtime_metrics"
        elif src.system in SYSTEM_LANGUAGE and not runtimes:
            lang, basis = SYSTEM_LANGUAGE[src.system]
        else:
            lang, basis = rc.GENERIC_SERVICE, "no_language_evidence" if not runtimes else "mixed_runtime_metrics"
        apps[svc] = App(svc, svc if svc in workloads else None, lang, basis, uid(case_id, "app", svc))
    return workloads, node_ids, cluster, apps


def family_ids(name: str, workloads: dict[str, Workload], apps: dict[str, App]) -> list[str]:
    """All targets that name a service: application, workload, replicasets, pods, containers, k8s service."""
    ids = [apps[name].id] if name in apps else []
    w = workloads.get(name)
    if w:
        ids.append(w.ids[w.kind])
        ids += [w.ids[k] for k in sorted(w.ids) if k.startswith("replicaset:")]
        ids += [w.ids[k] for k in sorted(w.ids) if k.startswith("pod:")]
        ids += [w.ids[k] for k in sorted(w.ids) if k.startswith("container:")]
        ids.append(w.ids["service"])
    return ids


def pod_at(w: Workload | None, t_s: float) -> Pod | None:
    """The workload's pod at time t: its only pod, or the only pod whose observed lifetime (+-STEP) covers t.
    Concurrent replicas are ambiguous and return None (spans/logs carry no pod name in the source)."""
    if w is None or not w.pods:
        return None
    if len(w.pods) == 1:
        return next(iter(w.pods.values()))
    live = [p for p in w.pods.values() if p.first - STEP <= t_s <= p.last + STEP]
    return live[0] if len(live) == 1 else None


# ------------------------------------------------------------------------------------------ metrics

def bucketize(points: list[tuple[float, float]], start: int, shift: int, end: int) -> tuple[list[int], list[float]]:
    """STEP-bucket means of the points inside the capture window [start, end), shifted, in ms."""
    points = sorted(p for p in points if start <= p[0] < end)
    times, values = zip(*points, strict=True) if points else ((), ())
    ts, vals = rc.resample(list(times), list(values), start)
    return [int((t + shift) * 1000) for t in ts], [round(v, 6) for v in vals]


def counter_rates(samples: dict[float, list[float]]) -> list[tuple[float, float]]:
    """Per-second rate of a cumulative counter whose rows at one instant are summed (the rx+tx pair). Instants with a
    different row count than the usual one are dropped (a missing half would read as a drop)."""
    usual = Counter(len(v) for v in samples.values()).most_common(1)[0][0] if samples else 0
    pts = sorted((t, sum(v)) for t, v in samples.items() if len(v) == usual)
    out = []
    for (t0, v0), (t1, v1) in itertools.pairwise(pts):
        if t1 > t0 and v1 >= v0:
            out.append((t1, (v1 - v0) / (t1 - t0)))
    return out


def metric_series(src: Source, metric_rows: list[tuple], sum_rows: list[tuple], workloads: dict[str, Workload], node_ids: dict[str, str],
                  cluster: str, shift: int) -> list[dict[str, Any]]:
    pod_owner = {pod: w for w in workloads.values() for pod in w.pods}
    rs_owner = {rs: w for w in workloads.values() for rs in w.replicasets}
    cluster_label = f"cluster-{cluster}"
    points: dict[tuple[str, tuple], list[tuple[float, float]]] = defaultdict(list)
    summed: dict[tuple[str, tuple], dict[float, float]] = defaultdict(lambda: defaultdict(float))

    def pod_labels(w: Workload, pod: str) -> dict[str, str]:
        node = w.pods[pod].node
        return {"target_id": cluster, "namespace": w.ns, "pod": pod, "node": node, "pod_id": w.ids[f"pod:{pod}"],
                "node_id": node_ids.get(node, ""), "cluster_id": cluster_label}

    for t, metric, value, _svc, _node, ns, sts, dep, rs, pod, ctr in metric_rows:
        if value is None or (isinstance(value, float) and not math.isfinite(value)) or (ns and ns != src.ns):
            continue
        ts = t / 1e9
        w = pod_owner.get(pod) if pod else None
        if w is not None:
            if metric in CONTAINER_GAUGES and ctr:
                name, scale = CONTAINER_GAUGES[metric]
                labels = {**pod_labels(w, pod), "container": ctr, "container_id": f"{pod}/{ctr}"}
                points[(name, tuple(sorted(labels.items())))].append((ts, value * scale))
            if metric in POD_GAUGES:
                name, scale = POD_GAUGES[metric]
                points[(name, tuple(sorted(pod_labels(w, pod).items())))].append((ts, value * scale))
            if metric in POD_SUM_OF_CONTAINERS and ctr:
                name, scale = POD_SUM_OF_CONTAINERS[metric]
                summed[(name, tuple(sorted(pod_labels(w, pod).items())))][ts] += value * scale
        elif metric in WORKLOAD_GAUGES:
            name, kind = WORKLOAD_GAUGES[metric]
            key = {"deployment": dep, "statefulset": sts, "replicaset": rs}[kind]
            owner = workloads.get(key) if kind != "replicaset" else rs_owner.get(key)
            if not key or owner is None or (kind != "replicaset" and owner.kind != kind):
                continue
            ident = owner.ids[f"replicaset:{key}"] if kind == "replicaset" else owner.ids[kind]
            labels = {"target_id": cluster, "namespace": owner.ns, kind: key, f"{kind}_id": ident, "cluster_id": cluster_label}
            points[(name, tuple(sorted(labels.items())))].append((ts, value))
    counters: dict[tuple[str, tuple], dict[float, list[float]]] = defaultdict(lambda: defaultdict(list))
    for t, metric, value, _svc, _node, ns, _sts, _dep, _rs, pod, _ctr in sum_rows:
        w = pod_owner.get(pod) if pod else None
        if w is None or value is None or metric not in POD_COUNTER_RATES or (ns and ns != src.ns):
            continue
        name, _scale = POD_COUNTER_RATES[metric]
        counters[(name, tuple(sorted(pod_labels(w, pod).items())))][t / 1e9].append(value)
    for key, by_t in summed.items():
        points[key].extend(by_t.items())
    for (name, labels), by_t in counters.items():
        scale = next(s for n, s in POD_COUNTER_RATES.values() if n == name)
        points[(name, labels)].extend((t, v * scale) for t, v in counter_rates(by_t))
    series = []
    for (name, labels), pts in sorted(points.items()):
        ts, vals = bucketize(pts, src.start, shift, src.end)
        if vals:
            series.append({"metric": {"__name__": name, **dict(labels)}, "values": vals, "timestamps": ts})
    return series


# ------------------------------------------------------------------------------------------ traces

TRACE_COLUMNS = ["time", "trace_id", "span_id", "parent_span_id", "span_name", "attr.span_kind", "service_name", "duration",
                 "attr.status_code", "attr.k8s.pod.name", "attr.http.request.method", "attr.http.response.status_code",
                 "attr.http.request.content_length", "attr.http.response.content_length"]


def percentile(sorted_values: list[float], q: float) -> float:
    """Nearest-rank percentile of an ascending list."""
    return sorted_values[max(0, math.ceil(q * len(sorted_values)) - 1)]


def sli_series(server: dict[str, list[tuple[float, float, bool]]], apps: dict[str, App], start: int, end: int, shift: int) -> list[dict[str, Any]]:
    """server[svc] = [(start_s, duration_ms, is_error)] of SERVER spans -> per-bucket SLI series under the app target."""
    n_buckets = math.ceil((end - start) / STEP)
    series = []
    for svc, spans in sorted(server.items()):
        buckets: list[list[tuple[float, bool]]] = [[] for _ in range(n_buckets)]
        for t, dur, err in spans:
            i = int((t - start) // STEP)
            if 0 <= i < n_buckets:
                buckets[i].append((dur, err))
        out: dict[str, tuple[list[int], list[float]]] = {k: ([], []) for k in SLI}
        for i, b in enumerate(buckets):
            ts = int((start + i * STEP + shift) * 1000)
            seconds = min(STEP, end - (start + i * STEP))
            errors = sum(1 for _, e in b if e)
            values = {"rps": len(b) / seconds, "span_count": float(len(b)), "error_count": float(errors)}
            if b:
                durs = sorted(d for d, _ in b)
                values.update({"error_rate": 100.0 * errors / len(b), "p50": percentile(durs, 0.50), "p95": percentile(durs, 0.95), "p99": percentile(durs, 0.99)})
            for k, v in values.items():
                out[k][0].append(ts)
                out[k][1].append(round(v, 6))
        for k, (ts, vals) in out.items():
            if vals:
                series.append({"metric": {"__name__": SLI[k], "target_id": apps[svc].id, "service_name": svc}, "values": vals, "timestamps": ts})
    return series


def span_attributes(kind: str, name: str, method: Any, http_status: Any, req_len: Any, resp_len: Any) -> dict[str, str]:
    attrs: dict[str, str] = {}
    if method:
        attrs["http.request.method"] = str(method)
    if http_status is not None:
        attrs["http.response.status_code"] = str(int(http_status))
    if req_len is not None:
        attrs["http.request.body.size"] = str(int(req_len))
    if resp_len is not None:
        attrs["http.response.body.size"] = str(int(resp_len))
    if kind == "CLIENT":
        sql, url = SQL_SPAN.match(name), HTTP_URL_SPAN.match(name)
        if sql:
            attrs.update({"db.system": "other_sql", "db.operation": sql.group(1), "db.operation.name": sql.group(1), "db.name": sql.group(2),
                          "db.sql.table": sql.group(3)})
        elif url:
            attrs.update({"http.request.method": url.group(1), "url.full": url.group(2)})
    return attrs


@dataclass
class TraceResult:
    spans: int = 0
    server: dict[str, list[tuple[float, float, bool]]] = field(default_factory=lambda: defaultdict(list))
    structural: dict[tuple[str, int], list[tuple]] = field(default_factory=lambda: defaultdict(list))


def write_traces(src: Source, trace_rows: list[tuple], workloads: dict[str, Workload], apps: dict[str, App], shift: int, out: Path,
                 batch: int = 200_000) -> TraceResult:
    schema = pq.read_schema(rc.REF_CAPTURE / "data" / "clickhouse" / "otel_traces_local.parquet")
    writer = pq.ParquetWriter(out, schema)
    shift_ns = shift * 1_000_000_000
    rows: list[dict[str, Any]] = []
    res = TraceResult()

    def flush() -> None:
        columns = {}
        for f in schema:
            vals = [r.get(f.name) for r in rows]
            if pa.types.is_map(f.type):
                vals = [list((v or {}).items()) for v in vals]
            columns[f.name] = pa.array(vals, type=f.type)
        writer.write_table(pa.table(columns, schema=schema))
        rows.clear()

    for t, trace_id, span_id, parent, name, kind_raw, svc, dur, status_raw, pod_attr, method, http_status, req_len, resp_len in trace_rows:
        if not trace_id or not span_id or t is None:
            continue
        kind = KIND.get(kind_raw or "", "INTERNAL")
        status = STATUS.get(status_raw or "", "UNSET")
        svc = svc or ""
        dur_ns = int(dur or 0)
        t_s = t / 1e9
        app = apps.get(svc)
        w = workloads.get(app.workload) if app and app.workload else None
        pod = (w.pods.get(pod_attr) if (w and pod_attr) else None) or pod_at(w, t_s)
        error = status == "ERROR" or (http_status is not None and int(http_status) >= 500)
        if kind == "SERVER" and app:
            res.server[svc].append((t_s, dur_ns / 1e6, error))
        if app:
            res.structural[(svc, int((t_s - src.start) // 60))].append((trace_id, t, span_id, parent or "", name or "", status == "ERROR"))
        resource = {"service.name": svc, "k8s.namespace.name": src.ns}
        if app:
            resource["lucida.target_id"] = app.id
        if pod:
            resource["k8s.pod.name"] = pod.name
        start_ns = t + shift_ns
        rows.append({
            "timestamp": start_ns, "trace_id": trace_id.encode()[:32].ljust(32, b"0"), "span_id": span_id.encode()[:16].ljust(16, b"0"),
            "parent_span_id": parent.encode()[:16].ljust(16, b"0") if parent else ZERO16, "trace_state": "-", "span_name": name or "",
            "span_kind": kind, "status_code": status, "status_message": "-", "duration_ns": dur_ns, "end_timestamp": start_ns + dur_ns,
            "service_name": svc, "service_version": "-", "service_namespace": src.ns, "deployment_env": "-", "host_name": pod.name if pod else "-",
            "k8s_namespace": src.ns, "k8s_pod_name": pod.name if pod else "-", "k8s_node_name": pod.node if pod and pod.node else "-",
            "span_attributes": span_attributes(kind, name or "", method, http_status, req_len, resp_len), "resource_attributes": resource,
            "events_name": [], "events_timestamp": [], "events_attributes": [], "links_trace_id": [], "links_span_id": [],
            "otel_schema_url": "-", "scope_name": "-", "scope_version": "-",
        })
        res.spans += 1
        if len(rows) >= batch:
            flush()
    if rows:
        flush()
    writer.close()
    return res


# ------------------------------------------------------------------------------------------ structural trace summaries
# Port of lucida-next backend/services/ai/features/observer/traceanomaly (service/chain.go, structural.go,
# templater.go, repository/clickhouse.go Insert*Aggregates): per service and window, the service's own spans grouped by
# trace (trace_id order); error chains = maximal runs of ERROR spans parent->child (min depth 1), stored as every
# contiguous segment; paths = parent->child pairs keyed by structurally templated span names. The production worker
# runs this every cycle; here the cycle is 1 minute aligned to the capture start.
TEMPLATE_WILDCARD = "<*>"
RE_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
RE_IPV4 = re.compile(r"^[0-9]{1,3}(\.[0-9]{1,3}){3}$")
DETECTOR_NAME = "trace-anomaly"
MAX_EXEMPLAR_TRACE_IDS = 20


def is_opaque(s: str, min_len: int = 6) -> bool:
    if s == TEMPLATE_WILDCARD:
        return True
    n = len(s)
    if n < min_len:
        return False
    has_digit = any(c.isascii() and c.isdigit() for c in s)
    has_alpha = any(c.isascii() and c.isalpha() for c in s)
    has_upper = any("A" <= c <= "Z" for c in s)
    all_hex = all(c in "0123456789abcdefABCDEF" for c in s)
    return (all_hex and n >= 8) or (has_digit and has_alpha and n >= 8) or (not has_alpha and has_digit and n >= 5) or (n >= 16 and (has_digit or has_upper))


@functools.lru_cache(maxsize=100_000)
def stable_path_name(name: str) -> str:
    fields = name.split()
    path = next((f for f in reversed(fields) if "/" in f), None)
    if path is None:
        return name
    method = " ".join(f for f in fields if "/" not in f)
    segs = [TEMPLATE_WILDCARD if RE_UUID.match(s) or RE_IPV4.match(s) else s for s in path.split("/")]
    segs = [TEMPLATE_WILDCARD if is_opaque(s) else s for s in segs]
    return f"{method} {'/'.join(segs)}" if method else "/".join(segs)


def merge_exemplars(dst: list[str], src: list[str], cap: int) -> list[str]:
    out = list(dst)
    for x in src:
        if len(out) >= cap:
            break
        if x and x not in out:
            out.append(x)
    return out


def maximal_error_chains(spans: list[tuple], svc: str, min_depth: int = 1) -> list[tuple[str, int, str]]:
    """spans: (trace_id, t, span_id, parent, name, is_error) of one service in one trace."""
    by_id = {s[2]: s for s in spans}
    children: dict[str, list[str]] = defaultdict(list)
    for s in spans:
        children[s[3]].append(s[2])
    out: list[tuple[str, int, str]] = []

    def visit(span_id: str, prefix: list[str], seen: frozenset[str]) -> None:
        s = by_id.get(span_id)
        if span_id in seen or s is None or not s[5]:
            return
        chain = prefix + [f"{svc}:{s[4]}"]
        err_children = [c for c in sorted(children.get(span_id, [])) if by_id[c][5]]
        if not err_children:
            if len(chain) >= min_depth:
                out.append((">".join(chain), len(chain), s[0]))
            return
        for c in err_children:
            visit(c, chain, seen | {span_id})

    starts = sorted(s[2] for s in spans if s[5] and (not s[3] or s[3] not in by_id or not by_id[s[3]][5]))
    for start in starts:
        visit(start, [], frozenset())
    return sorted(out, key=lambda x: x[0])


def chain_route(key: str) -> str:
    first = key.split(">", 1)[0]
    return first.split(":", 1)[1] if ":" in first else first


def chain_segments(key: str) -> list[str]:
    parts = key.split(">")
    return [">".join(parts[i:j]) for i in range(len(parts)) for j in range(i + 1, len(parts) + 1)]


def structural_summaries(structural: dict[tuple[str, int], list[tuple]], apps: dict[str, App], start: int, end: int, shift: int
                         ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    chain_rows, path_rows = [], []
    for (svc, w), spans in sorted(structural.items()):
        w_start, w_end = start + 60 * w, min(start + 60 * (w + 1), end)
        if w < 0 or w_start >= end:
            continue
        target = apps[svc].id
        by_trace: dict[str, list[tuple]] = defaultdict(list)
        for s in spans:
            by_trace[s[0]].append(s)
        chains: dict[str, dict[str, Any]] = {}
        paths: dict[str, dict[str, Any]] = {}
        for trace_id in sorted(by_trace):
            trace = sorted(by_trace[trace_id], key=lambda s: (s[1], s[2]))
            for key, depth, tid in maximal_error_chains(trace, svc):
                a = chains.setdefault(key, {"count": 0, "depth": depth, "exemplars": []})
                a["count"] += 1
                a["depth"] = max(a["depth"], depth)
                a["exemplars"] = merge_exemplars(a["exemplars"], [tid], 3)
            by_id = {s[2]: s for s in trace}
            for s in trace:
                parent = by_id.get(s[3]) if s[3] else None
                if parent is None:
                    continue
                sig = f"{svc}:{stable_path_name(parent[4])}->{svc}:{stable_path_name(s[4])}"
                p = paths.setdefault(sig, {"count": 0, "trace_ids": []})
                p["count"] += 1
                p["trace_ids"] = merge_exemplars(p["trace_ids"], [s[0]], MAX_EXEMPLAR_TRACE_IDS)
        segments: dict[str, dict[str, Any]] = {}
        for key in sorted(chains):
            ch = chains[key]
            for seg in chain_segments(key):
                cur = segments.setdefault(seg, {"count": 0, "depth": seg.count(">") + 1, "exemplars": list(ch["exemplars"])})
                cur["count"] += ch["count"]
                cur["depth"] = max(cur["depth"], seg.count(">") + 1)
                cur["exemplars"] = merge_exemplars(cur["exemplars"], ch["exemplars"], 3)
        ws_ns, we_ns = (w_start + shift) * 10**9, (w_end + shift) * 10**9
        computed = we_ns + 2 * 10**9
        for sig in sorted(segments):
            seg = segments[sig]
            chain_rows.append({"window_start": ws_ns, "window_end": we_ns, "target_id": target, "service_name": svc, "route": chain_route(sig),
                               "chain_hash": hashlib.sha1(f"{DETECTOR_NAME}|{target}|{svc}|error_chain|{sig}".encode()).hexdigest(),
                               "chain_signature": sig, "chain_depth": seg["depth"], "chain_count": seg["count"], "exemplar_trace_ids": seg["exemplars"],
                               "computed_at": computed})
        for sig in sorted(paths):
            p = paths[sig]
            path_rows.append({"window_start": ws_ns, "window_end": we_ns, "target_id": target, "service_name": svc,
                              "path_hash": hashlib.sha1(f"{DETECTOR_NAME}|{target}|{svc}|novel_path|{sig}".encode()).hexdigest(),
                              "path_signature": sig, "path_count": p["count"], "exemplar_trace_ids": p["trace_ids"], "computed_at": computed})
    return chain_rows, path_rows


# ------------------------------------------------------------------------------------------ logs

LOG_COLUMNS = ["time", "trace_id", "span_id", "level", "service_name", "message", "attr.k8s.pod.name"]
# Unix epoch seconds (optionally fractional) or milliseconds inside log bodies (e.g. JSON access logs' "ts"). A number is
# taken as a timestamp only when it falls within a day of the capture window, so ids and counters are left alone.
EPOCH_IN_BODY = re.compile(r"(?<![\w.])(1\d{9}|1\d{12})(\.\d+)?(?![\w])")


def shift_epochs(body: str, lo: int, hi: int, shift: int) -> str:
    def repl(m: re.Match[str]) -> str:
        value = int(m.group(1))
        ms = len(m.group(1)) == 13
        seconds = value / 1000 if ms else value
        if not lo <= seconds <= hi:
            return m.group(0)
        return str(value + (shift * 1000 if ms else shift)) + (m.group(2) or "")
    return EPOCH_IN_BODY.sub(repl, body)


def log_level(level: Any) -> str:
    text = str(level or "").strip().upper()
    text = {"WARNING": "WARN", "CRITICAL": "FATAL", "ERR": "ERROR"}.get(text, text)
    return text if text in rc.SEV_NUM else "INFO"


def log_rows(src: Source, rows_in: list[tuple], workloads: dict[str, Workload], apps: dict[str, App], node_ids: dict[str, str], shift: int
             ) -> tuple[list[dict[str, Any]], int]:
    rows, skipped = [], 0
    for t, trace_id, span_id, level, svc, message, pod_attr in rows_in:
        svc = svc or ""
        app = apps.get(svc)
        w = workloads.get(app.workload if app and app.workload else svc)
        pod = (w.pods.get(pod_attr) if (w and pod_attr) else None) or pod_at(w, t / 1e9)
        target = app.id if app else (w.ids[f"pod:{pod.name}"] if (w and pod) else "")
        if not target:
            skipped += 1
            continue
        ts_ns = t + shift * 1_000_000_000
        sev = log_level(level)
        node = pod.node if pod else ""
        resource = {"service.name": svc, "lucida.target_id": target}
        if pod:
            resource["k8s.pod.name"] = pod.name
        rows.append({
            "timestamp": ts_ns, "observed_timestamp": ts_ns, "target_id": target, "host_target_id": node_ids.get(node, ""),
            "collector_kind": "otel_recv", "event_source": "container_log", "event_level": "-",
            "trace_id": trace_id.encode()[:32].ljust(32, b"0") if trace_id else b"\x00" * 32,
            "span_id": span_id.encode()[:16].ljust(16, b"0") if span_id else ZERO16, "trace_flags": 0,
            "severity_text": sev, "severity_number": rc.SEV_NUM[sev], "service_name": svc,
            "body": shift_epochs(rc.shift_body(message or "", shift), src.start - 86400, src.end + 86400, shift),
            "service_version": "-", "service_namespace": src.ns, "deployment_env": "-", "host_name": pod.name if pod else "-", "host_ip": "-",
            "k8s_namespace": src.ns, "k8s_pod_name": pod.name if pod else "-", "k8s_node_name": node or "-",
            "k8s_container_name": next(iter(sorted(pod.containers)), "-") if pod and len(pod.containers) == 1 else "-",
            "log_attributes": {}, "resource_attributes": resource,
        })
    return rows, skipped


# ------------------------------------------------------------------------------------------ PostgreSQL registry

def pg_rows(src: Source, workloads: dict[str, Workload], node_ids: dict[str, str], cluster: str, apps: dict[str, App], created: str
            ) -> dict[str, list[dict[str, Any]]]:
    """Same tables and semantics as rcaeval_convert.pg_rows (kcm_resource_targets: cluster_target_id, resource_kind,
    resource_key = namespace/name), extended to statefulsets, several pods / replicasets per workload, containers named
    by the source and applications without a Kubernetes workload."""
    targets, krt, kres, members, collectors = [], [], [], [], []
    ns = src.ns
    group = "s_" + re.sub(r"[^a-z0-9]", "", ns)
    capture_end = src.end

    def k8s(kind: str, key: str, target: str, parent: str, resource_kind: str, resource_type: str, extra: dict | None = None) -> None:
        meta = {"id_path": [cluster] + ([parent] if parent != cluster else []), "namespace": key.split("/")[0] if "/" in key else "",
                "resource_key": key, "resource_kind": resource_kind, "resource_type": resource_type, "host_target_id": cluster,
                "parent_target_id": parent, "cluster_target_id": cluster, **(extra or {})}
        targets.append({"id": target, "type": "kubernetes_resource", "name": f"{cluster}:{kind}:{key}", "display_name": key.split("/")[-1], "meta": meta})
        krt.append({"target_id": target, "cluster_target_id": cluster, "parent_target_id": parent, "resource_kind": kind, "resource_key": key,
                    "meta": {"name": key.split("/")[-1]}})

    targets.append({"id": cluster, "type": "kubernetes", "name": f"cluster-{cluster}", "display_name": f"{ns}-cluster",
                    "meta": {"icon": "k8s:cluster", "id_path": [], "cluster_id": f"cluster-{cluster}", "resource_key": cluster, "resource_kind": "cluster",
                             "resource_type": "kcm.cluster", "host_target_id": cluster, "collector": "polestar-agent"}})
    members.append({"scope": "unified", "kind": "system", "group_id": "default", "asset_ref": cluster, "target_id": cluster})
    collectors.append({"target_id": cluster, "kind": "polestar_kcm", "created_at": created})
    for node, nid in node_ids.items():
        k8s("node", node, nid, cluster, "node", "kcm.node", {"icon": "k8s:node", "series_labels": {"node": node}})
    for w in workloads.values():
        key = f"{ns}/{w.name}"
        wid = w.ids[w.kind]
        if w.kind == "statefulset":
            k8s("statefulset", key, wid, cluster, "stateful_set", "kcm.stateful_set", {"icon": "k8s:statefulset", "series_labels": {"namespace": ns, "statefulset": w.name}})
        else:
            k8s("deployment", key, wid, cluster, "deployment", "kcm.deployment", {"icon": "k8s:deployment", "series_labels": {"namespace": ns, "deployment": w.name}})
        for rs in sorted(w.replicasets):
            k8s("replicaset", f"{ns}/{rs}", w.ids[f"replicaset:{rs}"], cluster, "replica_set", "kcm.replica_set", {"icon": "k8s:replicaset"})
        for pod, p in sorted(w.pods.items()):
            pid = w.ids[f"pod:{pod}"]
            k8s("pod", f"{ns}/{pod}", pid, cluster, "pod", "kcm.pod")
            for ctr in sorted(p.containers):
                k8s("container", f"{ns}/{pod}/{ctr}", w.ids[f"container:{pod}/{ctr}"], pid, "container", "kcm.container")
            status = "Running" if p.last >= capture_end - 60 else "Unknown"  # not observed in the last minute of the capture
            kres.append({"target_id": cluster, "kind": "pod", "uid": pid, "namespace": ns, "name": pod,
                         "data": {"podName": pod, "nodeName": p.node, "namespaceName": ns, "containers": sorted(p.containers), "status": status}})
        k8s("service", key, w.ids["service"], cluster, "service", "kcm.service", {"icon": "k8s:service", "series_labels": {"service": w.name, "namespace": ns}})
        desired = int(w.desired) if w.desired is not None else len(w.pods)
        available = int(w.available) if w.available is not None else desired
        kres.append({"target_id": cluster, "kind": w.kind, "uid": wid, "namespace": ns, "name": w.name,
                     "data": {f"{w.kind}Name": w.name, "namespaceName": ns, "desiredPods": desired, "availablePods": available}})
        kres.append({"target_id": cluster, "kind": "service", "uid": w.ids["service"], "namespace": ns, "name": w.name,
                     "data": {"serviceName": w.name, "namespaceName": ns, "type": "ClusterIP"}})
        for ref in [wid, w.ids["service"]] + [w.ids[f"replicaset:{rs}"] for rs in sorted(w.replicasets)]:
            members.append({"scope": "unified", "kind": "system", "group_id": "default", "asset_ref": ref, "target_id": ref})
    for app in apps.values():
        targets.append({"id": app.id, "type": "application", "name": app.name, "display_name": app.name,
                        "meta": {"icon": "app:service", "group": ns, "id_path": [], "resource_key": app.id, "resource_kind": app.lang.split(".")[-1],
                                 "resource_type": app.lang, "service_name": app.name, "host_target_id": app.id, "collector": "otel-traces",
                                 "language_basis": app.lang_basis}})
        members.append({"scope": "unified", "kind": "system", "group_id": "default", "asset_ref": app.id, "target_id": app.id})
        members.append({"scope": "unified", "kind": "service", "group_id": group, "asset_ref": app.id, "target_id": app.id, "label": app.name})
        collectors.append({"target_id": app.id, "kind": "otel_recv", "created_at": created})
    return {"targets": targets, "kcm_resource_targets": krt, "kcm_resources": kres, "asset_tree_members": members, "collectors": collectors,
            "groups": [{"id": group, "name": ns}]}


# ------------------------------------------------------------------------------------------ answer key -> meta / golden

def fault_params(src: Source, fault: dict[str, Any]) -> str:
    """Injection parameters of one fault (grader-only): engine_config[index] in the newer record format, the
    human-readable display_config in the older one (only when it can be tied to this fault)."""
    configs = src.injection.get("engine_config")
    if not isinstance(configs, list):
        disp = src.injection.get("display_config")
        try:
            disp = json.loads(disp) if isinstance(disp, str) else disp
        except ValueError:
            return ""
        if not isinstance(disp, dict):
            return ""
        point = disp.get("injection_point") if isinstance(disp.get("injection_point"), dict) else {}
        if point.get("app_name", fault["service"]) != fault["service"] and len(src.label["faults"]) > 1:
            return ""
        items = [f"{k}={disp[k]}" for k in sorted(disp) if k not in PARAM_SKIP | {"injection_point"}]
        items += [f"{k}={point[k]}" for k in sorted(point) if k != "app_name"]
        return ", ".join(items)
    idx = fault.get("index")
    conf = configs[idx] if isinstance(idx, int) and 0 <= idx < len(configs) and isinstance(configs[idx], dict) else None
    if conf is None or conf.get("app") != fault["service"]:
        return ""
    return ", ".join(f"{k}={conf[k]}" for k in sorted(conf) if k not in PARAM_SKIP)


def fault_text(src: Source, fault: dict[str, Any]) -> str:
    gloss = CHAOS_GLOSS.get(fault["chaos_type"])
    params = fault_params(src, fault)
    text = f"{fault['service']}: {fault['chaos_type']}" + (f" — {gloss}" if gloss else "")
    return text + (f" ({params})" if params else "")


def propagation_paths(graph: dict[str, Any], root_services: set[str], max_per_root: int = 4) -> list[str]:
    """Shortest verified path from each root-cause component of a required fault to each alarm component, rendered
    with node states (roots of optional / not-effective faults are left out: they are not part of the answer)."""
    states = {n["component"]: "/".join(n.get("state") or []) for n in graph.get("nodes", [])}
    states |= {r["component"]: "/".join(r.get("state") or []) for r in graph.get("root_causes", []) if r["component"] not in states}
    owner = graph.get("component_to_service") or {}
    roots = sorted({r["component"] for r in graph.get("root_causes", [])})
    roots = [r for r in roots if owner.get(r, r.split("|", 1)[-1].split("::", 1)[0]) in root_services] or roots
    adj: dict[str, list[str]] = defaultdict(list)
    for e in graph.get("edges", []):
        adj[e["source"]].append(e["target"])
    alarms = sorted({a["component"] for a in graph.get("alarm_nodes", [])})

    def node(c: str) -> str:
        return f"{c} [{states[c]}]" if states.get(c) else c

    def service_of(c: str) -> str:
        return owner.get(c) or c.split("|", 1)[-1].split("::", 1)[0]

    def bfs(sources: list[str]) -> dict[str, str | None]:
        prev: dict[str, str | None] = dict.fromkeys(sources)
        queue = list(sources)
        while queue:
            nxt = []
            for c in queue:
                for d in sorted(adj.get(c, [])):
                    if d not in prev:
                        prev[d] = c
                        nxt.append(d)
            queue = nxt
        return prev

    lines = []
    nodes = sorted(set(states) | set(adj) | {d for ds in adj.values() for d in ds})
    for root in roots:
        prev, lead = bfs([root]), ""
        if not any(a in prev and a != root for a in alarms):
            # the root component itself is often not a graph node (state_resolution_reason root_component_not_in_causal_graph):
            # the verified chain then starts at the root service's own components
            own = [c for c in nodes if c != root and service_of(c) == service_of(root)]
            prev, lead = bfs(own), f"{node(root)} => "
        paths = []
        for a in alarms:
            if a in prev and a != root:
                path, cur = [], a
                while cur is not None:
                    path.append(cur)
                    cur = prev[cur]
                paths.append(path[::-1])
        paths.sort(key=lambda p: (-len(p), p))
        if not paths:
            lines.append(f"{node(root)} (no downstream path recorded)")
        for p in paths[:max_per_root]:
            lines.append(lead + " -> ".join(node(c) for c in p))
        if len(paths) > max_per_root:
            lines.append(f"(+{len(paths) - max_per_root} more paths from {root})")
    return lines


def scenario_metadata(src: Source) -> dict[str, str]:
    faults = src.label["faults"]
    required = [f for f in faults if f["role"] == "required"]
    others = [f for f in faults if f["role"] != "required"]
    edges = [f"{a} -> {b}" for a, b in src.label.get("service_edges", [])]
    paths = propagation_paths(src.graph, {s for f in required for s in [f["service"], *f.get("also_accept", [])]})
    title = " + ".join(f"{f['service']} {f['chaos_type']}" for f in required)
    cause = ("Root cause (faults that took effect beyond their own service): " + "; ".join(fault_text(src, f) for f in required) + ".\n"
             "Verified propagation (component -> component [state]):\n- " + "\n- ".join(paths)
             + (f"\nService-level propagation: {', '.join(edges)}." if edges else ""))
    alarm_desc = ", ".join(f"{a['component']} [{'/'.join(a.get('state') or [])}]" for a in src.graph.get("alarm_nodes", []))
    description = f"{DATASET} {src.system} ({SYSTEM_NAMES.get(src.system, src.system)}) case in namespace {src.ns}."
    if others:
        description += " Other co-injected faults (not part of the root-cause set): " + "; ".join(f"{fault_text(src, f)} [{f['role']}]" for f in others) + "."
    return {"title": title, "description": description, "cause": cause,
            "injection_summary": "; ".join(fault_text(src, f) for f in faults) + " during the abnormal window",
            "user_impact": f"effect observed at: {alarm_desc}" if alarm_desc else "",
            "distinguishing_evidence": f"Effect observed at the end of the verified paths: {alarm_desc}."
                                       + (f" Service-level propagation: {', '.join(edges)}." if edges else "")}


def golden_entry(src: Source, workloads: dict[str, Workload], apps: dict[str, App]) -> tuple[dict[str, Any] | None, str]:
    roots, seen, missing = [], set(), []
    for f in src.label["faults"]:
        if f["role"] != "required":
            continue
        names = [f["service"]] + [s for s in f.get("also_accept", []) if s != f["service"]]
        ids = [i for n in names for i in family_ids(n, workloads, apps)]
        if not family_ids(f["service"], workloads, apps):
            missing.append(f["service"])
        key = tuple(sorted(set(ids)))
        if ids and key not in seen:
            seen.add(key)
            roots.append({"target_ids": list(dict.fromkeys(ids)), "target_aliases": names})
    if missing or not roots:
        return None, f"required root service(s) absent from the telemetry: {missing}"
    root_ids = {i for r in roots for i in r["target_ids"]}
    allowed = list(dict.fromkeys(i for f in src.label["faults"] if f["role"] == "optional" for i in family_ids(f["service"], workloads, apps) if i not in root_ids))
    entry: dict[str, Any] = {"expected_status": "confirmed", "roots": roots}
    if allowed:
        entry["allowed_target_ids"] = allowed
        entry["allowed_note"] = "optional faults: effect confined to their own resources or masked by a co-injected fault (naming them is not penalised)"
    return entry, ""


# ------------------------------------------------------------------------------------------ blindness

def forbidden_tokens(src: Source) -> list[str]:
    """Strings that would leak the answer or the case identity into model-visible data."""
    tokens = {src.name, "chaos", "openrca", "ops-lite", "anon-ops"} | {f["chaos_type"] for f in src.label["faults"]} | set(CHAOS_GLOSS)
    for key in ("task_id", "trace_id", "name", "injection_name"):
        if isinstance(src.injection.get(key), str) and len(src.injection[key]) >= 8:
            tokens.add(src.injection[key])
    # descriptive case names (<namespace>-<service>-<fault slug>-<suffix>): the multi-word fault slug
    slug = src.name
    for f in src.label["faults"]:
        slug = slug.replace(f["service"], " ")
    words = [w for w in re.split(r"[\s]+", slug) if w.strip("-")]
    for w in words:
        part = w.strip("-")
        parts = part.split("-")
        if len(parts) >= 2 and re.fullmatch(r"[a-z0-9]{6}", parts[-1]):
            part = "-".join(parts[:-1])
        if part.count("-") >= 1 and not part.startswith(src.ns):
            tokens.add(part)
    return sorted(tokens)


def blindness_violations(case_dir: Path, tokens: list[str], pg: dict[str, Any] | None = None) -> dict[str, int]:
    """Case-insensitive occurrences of each token in every model-visible artefact (meta.json and the golden are
    grader-only; the PG rows are checked before the compressed dump)."""
    lowered = [t.lower() for t in tokens]
    hits: Counter[str] = Counter()

    def scan(text: str, where: str) -> None:
        low = text.lower()
        for tok in lowered:
            n = low.count(tok)
            if n:
                hits[f"{where}:{tok}"] += n

    data = case_dir / "data"
    scan((data / "victoriametrics.export").read_text(), "victoriametrics.export")
    scan((data / "sources.json").read_text(), "sources.json")
    for f in sorted((data / "clickhouse").glob("*.parquet")):
        t = pq.read_table(f)
        for col in t.column_names:
            typ = t.schema.field(col).type
            if pa.types.is_string(typ) or pa.types.is_large_string(typ) or pa.types.is_map(typ) or pa.types.is_list(typ):
                scan(json.dumps(t[col].to_pylist(), default=str), f"{f.name}:{col}")
    if pg is not None:
        scan(json.dumps(pg, ensure_ascii=False), "postgres")
    return dict(hits)


# ------------------------------------------------------------------------------------------ driver

def coverage_gaps(kind: str, times_ns: list[int], start: int, end: int, slack: int = 60) -> str:
    """Factual collection-coverage sentence for a telemetry kind that is absent or covers only part of the capture
    (a gap is a collection property of the source dataset, not a symptom)."""
    if not times_ns:
        return f" 이 캡처에는 {kind}가 없다."
    first, last = min(times_ns) / 1e9 - start, max(times_ns) / 1e9 - start
    if first <= slack and last >= (end - start) - slack:
        return ""
    return f" {kind}는 캡처 시작 후 약 {max(0.0, first) / 60:.0f}분~{min(end - start, last) / 60:.0f}분 구간에만 있다(그 밖은 수집 공백이며 장애 증거가 아니다)."


def sources_note(system: str, coverage: str = "") -> str:
    base = ("외부 공개 데이터셋 변환 캡처 — 컨테이너·pod 지표(kubelet/k8s 수신기), 레플리카·재시작·ready 컨테이너 수, 분산 트레이스(span 종류·상태·HTTP 코드 포함), "
            "트레이스 구조 요약(1분 창), 서비스 로그, 트레이스 파생 서비스 SLI(apm.trace_derived.*: server span 기준 rps(req/s)·span_count·error_count·"
            "error_rate(%, 상태 ERROR 또는 HTTP 5xx)·percentile50/95/99(ms)), 합성 이상 알람만 있다. 노드 지표, Kubernetes 이벤트(OOMKilled·CrashLoopBackOff·"
            "waiting 사유), DB 세션·쿼리, 변경 이력, 프로세스·호스트·SNMP 데이터는 없다. pod 네트워크 지표는 송수신 합(rxtx)만 있다.")
    extra = {"ts": " 애플리케이션 런타임 지표(JVM 등)는 변환하지 않았다.",
             "hs": " 애플리케이션 런타임 지표는 원천에 없다.",
             "otel-demo": " 애플리케이션 런타임·미들웨어 지표(JVM·Kafka·Redis·PostgreSQL 등)는 변환하지 않았다."}
    return base + extra.get(system, "") + coverage


NOT_COLLECTED = ["database", "changes", "process", "host", "snmp", "cluster_projection"]


def convert(case_dir: Path, out_root: Path, args: argparse.Namespace) -> dict[str, Any] | None:
    t_begin = time.time()
    src = read_source(case_dir)
    case_id = case_id_of(src.name, args.tag)
    shift = int(args.anchor.timestamp()) - src.start
    metric_rows = rows_of(read_part(case_dir, "metrics", METRIC_COLUMNS, METRIC_INPUTS), METRIC_COLUMNS)
    sum_rows = rows_of(read_part(case_dir, "metrics_sum", METRIC_COLUMNS, SUM_INPUTS), METRIC_COLUMNS)
    traces = read_part(case_dir, "traces", TRACE_COLUMNS)
    trace_rows = rows_of(traces, TRACE_COLUMNS)
    trace_services = {s for s in set(traces["service_name"].to_pylist()) if s}
    runtime_of: dict[str, set[str]] = defaultdict(set)
    for part in ("metrics", "metrics_sum", "metrics_histogram"):
        t = read_part(case_dir, part, ["time", "metric", "service_name"]).select(["service_name", "metric"])
        pairs = t.group_by(["service_name", "metric"]).aggregate([]) if t.num_rows else t
        for svc, metric in zip(pairs["service_name"].to_pylist(), pairs["metric"].to_pylist(), strict=True):
            for prefix, lang in RUNTIME_PREFIXES.items():
                if svc and metric and metric.startswith(prefix):
                    runtime_of[svc].add(lang)
    workloads, node_ids, cluster, apps = build_entities(src, metric_rows + sum_rows, trace_services, runtime_of, case_id)
    golden, why = golden_entry(src, workloads, apps)
    if golden is None:
        print(f"  skip {src.name}: {why}", flush=True)
        return None
    case_dir_out = out_root / case_id
    ch = case_dir_out / "data" / "clickhouse"
    # spans go to a scratch file first: a case the detector skips must leave nothing behind in the output tree
    tmp = Path(tempfile.mkdtemp(prefix="openrca2-convert-"))
    try:
        tr = write_traces(src, trace_rows, workloads, apps, shift, tmp / "otel_traces_local.parquet")
        span_times = [r[0] for r in trace_rows if r[0] is not None]
        return _finish(src, args, case_id, case_dir_out, ch, tmp, tr, metric_rows, sum_rows, workloads, node_ids, cluster, apps, golden, shift, t_begin,
                       span_times)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _finish(src: Source, args: argparse.Namespace, case_id: str, case_dir_out: Path, ch: Path, tmp: Path, tr: TraceResult, metric_rows: list[tuple],
            sum_rows: list[tuple], workloads: dict[str, Workload], node_ids: dict[str, str], cluster: str, apps: dict[str, App], golden: dict[str, Any],
            shift: int, t_begin: float, span_times: list[int]) -> dict[str, Any] | None:
    series = metric_series(src, metric_rows, sum_rows, workloads, node_ids, cluster, shift) + sli_series(tr.server, apps, src.start, src.end, shift)
    capture_start_ms = (src.start + shift) * 1000
    scope = SYMPTOM_METRICS + RESOURCE_METRICS if args.alarm_scope == "all" else SYMPTOM_METRICS
    episodes = rc.detect(series, capture_start_ms, args.detector_baseline, metrics=scope)
    names = {a.id: a.name for a in apps.values()} | {w.ids[f"pod:{p}"]: w.name for w in workloads.values() for p in w.pods}
    events = [{**e, "source_id": SOURCE_ID} for e in rc.event_rows(episodes, names, case_id)]
    t1, t2 = src.abnormal_start + shift, src.end + shift
    in_window = [e for e in events if e["transition"] == "open" and t1 * 10**9 <= e["occurred_at"] < t2 * 10**9]
    if not in_window:
        print(f"  skip {src.name}: blind detector found no alarm inside the abnormal window", flush=True)
        return None
    ch.mkdir(parents=True, exist_ok=True)
    shutil.move(tmp / "otel_traces_local.parquet", ch / "otel_traces_local.parquet")
    with (case_dir_out / "data" / "victoriametrics.export").open("w") as handle:
        for s in series:
            handle.write(json.dumps(s) + "\n")
    log_in = rows_of(read_part(src.case_dir, "logs", LOG_COLUMNS), LOG_COLUMNS)
    logs, logs_skipped = log_rows(src, log_in, workloads, apps, node_ids, shift)
    coverage = coverage_gaps("서비스 로그", [r[0] for r in log_in if r[0] is not None], src.start, src.end) + coverage_gaps("트레이스", span_times, src.start, src.end)
    rc.write_parquet(logs, "lucida_logs_local", ch / "lucida_logs_local.parquet")
    rc.write_parquet(events, "lucida_events_local", ch / "lucida_events_local.parquet")
    chain_rows, path_rows = structural_summaries(tr.structural, apps, src.start, src.end, shift)
    rc.write_parquet(chain_rows, "trace_error_chains_local", ch / "trace_error_chains_local.parquet")
    rc.write_parquet(path_rows, "trace_path_signatures_local", ch / "trace_path_signatures_local.parquet")
    (case_dir_out / "data" / "sources.json").write_text(json.dumps({"not_collected": NOT_COLLECTED, "note": sources_note(src.system, coverage)}, ensure_ascii=False, indent=1))
    created = rc.iso(src.start + shift - 30 * 86400)
    pg = pg_rows(src, workloads, node_ids, cluster, apps, created)
    hits = blindness_violations(case_dir_out, forbidden_tokens(src), pg)
    if hits:
        raise RuntimeError(f"{src.name}: answer tokens in model-visible data: {hits}")
    rc.pg_dump(pg, case_id, args.pg_container, args.pg_template, case_dir_out / "data" / "postgres.dump")
    faults = [{k: f[k] for k in ("service", "chaos_type", "role", "container", "also_accept") if k in f} for f in src.label["faults"]]
    meta = {
        "case_id": case_id, "t1": rc.iso(t1), "t2": rc.iso(t2), "capture_start": rc.iso(src.start + shift), "capture_end": rc.iso(src.end + shift),
        "scenario_metadata": scenario_metadata(src),
        "source": {"dataset": DATASET, "record": f"huggingface:{HF_REPO}@{HF_REVISION[:8]}", "revision": HF_REVISION, "license": LICENSE,
                   "path": f"cases/{src.name}", "system": src.system, "namespace": src.ns, "faults": faults,
                   "process_applicable": src.label.get("process_applicable"), "service_edges": src.label.get("service_edges", []),
                   "abnormal_start": rc.iso(t1), "abnormal_end": rc.iso(t2), "time_shift_s": shift,
                   "public_compare": in_public_compare(src.name)},
    }
    (case_dir_out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    seconds = round(time.time() - t_begin, 1)
    print(f"  {case_id}: {src.name} [{src.system}] series={len(series)} spans={tr.spans} logs={len(logs)} (skipped {logs_skipped}) "
          f"chains={len(chain_rows)} paths={len(path_rows)} alarms={sum(e['transition'] == 'open' for e in events)} in_window={len(in_window)} "
          f"roots={len(golden['roots'])} allowed={len(golden.get('allowed_target_ids', []))} {seconds}s", flush=True)
    return {"case_id": case_id, "golden": golden, "system": src.system, "name": src.name, "public_compare": in_public_compare(src.name), "seconds": seconds}


def select_cases(src_root: Path, system: str, limit: int, names: list[str] | None) -> list[Path]:
    cases = [d for d in sorted((src_root / "cases").iterdir()) if d.is_dir() and (not names or d.name in names)]
    complete = [d for d in cases if case_complete(d)]
    by_system: dict[str, list[Path]] = defaultdict(list)
    for d in complete:
        s = json.loads((d / "label.json").read_text())["system"]
        if system in ("all", s):
            by_system[s].append(d)
    if not limit:
        return [d for s in SYSTEMS for d in by_system.get(s, [])]
    # fault-type interleaved within a system (primary = first required fault's chaos type, hash order inside a type),
    # then round-robin across systems so a small pilot covers every system and fault type
    per_system = {}
    for s, dirs in by_system.items():
        by_type: dict[str, list[Path]] = defaultdict(list)
        for d in sorted(dirs, key=lambda d: hashlib.sha1(d.name.encode()).hexdigest()):
            faults = json.loads((d / "label.json").read_text())["faults"]
            primary = next((f["chaos_type"] for f in faults if f["role"] == "required"), "none")
            by_type[primary].append(d)
        types = sorted(by_type)
        per_system[s] = [by_type[t][i] for i in range(max(map(len, by_type.values()))) for t in types if i < len(by_type[t])]
    order = [per_system[s][i] for i in range(max(map(len, per_system.values()), default=0)) for s in SYSTEMS if s in per_system and i < len(per_system[s])]
    return order[:limit]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--src", type=Path, required=True, help="OpenRCA 2.0 download root (contains cases/)")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--tag", default="o2a", help="short tag in case ids / config names")
    parser.add_argument("--limit", type=int, default=0, help="fault-type-interleaved, system-stratified subset of this size")
    parser.add_argument("--system", choices=("all",) + SYSTEMS, default="all")
    parser.add_argument("--cases", nargs="*", default=None, help="explicit OpenRCA case names (optional)")
    parser.add_argument("--anchor", type=dt.datetime.fromisoformat, default=dt.datetime(2026, 8, 24, 9, 0, tzinfo=dt.UTC))
    parser.add_argument("--detector-baseline", type=int, default=180,
                        help="seconds from capture start used as the detector baseline (the normal half is 240 or 300 s)")
    parser.add_argument("--alarm-scope", choices=("symptom", "all"), default="symptom",
                        help="symptom: trace-derived SLI alarms only (default); all: also pod resource / restart alarms")
    parser.add_argument("--pg-container", default="rcaeval-pgtool")
    parser.add_argument("--pg-template", default="rcae_tmpl")
    parser.add_argument("--golden", type=Path, default=None)
    parser.add_argument("--split", type=Path, default=None)
    args = parser.parse_args()
    runs = select_cases(args.src, args.system, args.limit, args.cases)
    args.out.mkdir(parents=True, exist_ok=True)
    golden, train, public, timings = {}, [], [], []
    for case_dir in runs:
        res = convert(case_dir, args.out, args)
        if res:
            golden[res["case_id"]] = res["golden"]
            (public if res["public_compare"] else train).append(res["case_id"])
            timings.append(res["seconds"])
    golden_path = args.golden or ROOT / "configs" / "eval" / f"openrca2-{args.tag}.yaml"
    split_path = args.split or ROOT / "configs" / "teacher" / f"openrca2-{args.tag}-split.yaml"
    header = (f"# OpenRCA 2.0 {args.tag} converted captures (scripts/openrca2_convert.py). Source: huggingface {HF_REPO}@{HF_REVISION[:8]}, {LICENSE}.\n"
              "# public_compare: deterministic ~20% hold-out per system (hash of the case name) for comparison with published OpenRCA 2.0 results; never train on it.\n")
    golden_path.write_text(header + yaml.safe_dump({"version": 1, "cases": golden}, sort_keys=False))
    split_path.write_text(header + yaml.safe_dump({"train": train, "validation": [], "sealed_eval": [], "public_compare": public}, sort_keys=False))
    mean = f"{sum(timings) / len(timings):.1f}s/case" if timings else "-"
    print(f"converted {len(golden)}/{len(runs)} -> {args.out} ({mean}); train {len(train)} public_compare {len(public)}; golden {golden_path}; split {split_path}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
