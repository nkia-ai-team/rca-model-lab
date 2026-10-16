import importlib.util
import json
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


conv = _load("openrca2_convert_under_test", "scripts/openrca2_convert.py")
CASE = "case-openrca2-t-v3-00000000"
NS = "hs7"
START = 1_000_000


def _source(faults, *, name="hs7-profile-pod-failure-abc123", graph=None, injection=None):
    label = {"case": name, "system": "hs", "faults": faults, "process_applicable": True, "service_edges": [["profile", "frontend"]]}
    return conv.Source(name, Path("/nonexistent"), "hs", NS, START, START + 600, START + 300, label, graph or {}, injection or {})


def _row(t, metric, value, pod=None, ctr=None, dep=None, rs=None, sts=None, node="10.0.0.1", svc=None):
    # METRIC_COLUMNS order: time, metric, value, service_name, node, namespace, statefulset, deployment, replicaset, pod, container
    return (int(t * 1e9), metric, value, svc, node, NS, sts, dep, rs, pod, ctr)


def _entities(rows, trace_services=("profile", "frontend", "geo")):
    return conv.build_entities(_source([]), rows, set(trace_services), {}, CASE)


BASE_ROWS = [
    _row(START + 1, "container.cpu.usage", 0.25, pod="profile-5d8f7c9b6d-x2x4q", ctr="hotel-reserv-profile", dep="profile", svc="profile"),
    _row(START + 1, "container.cpu.usage", 0.1, pod="frontend-7cf848fd4c-w6rrm", ctr="hotel-reserv-frontend", dep="frontend", svc="frontend"),
    _row(START + 1, "container.cpu.usage", 0.1, pod="geo-5b47689f6c-g7w99", ctr="hotel-reserv-geo", dep="geo", svc="geo"),
    _row(START + 1, "container.cpu.usage", 0.01, pod="mongodb-geo-0", ctr="mongo", sts="mongodb-geo", svc="mongodb-geo"),
]


def test_entities_follow_k8s_labels_and_pod_naming():
    ws, node_ids, cluster, apps = _entities(BASE_ROWS)
    assert ws["profile"].kind == "deployment" and ws["profile"].replicasets == {"profile-5d8f7c9b6d"}
    assert ws["mongodb-geo"].kind == "statefulset" and not ws["mongodb-geo"].replicasets
    assert "mongodb-geo" not in apps  # datastore without spans: Kubernetes targets only (as rcaeval_convert types DBs)
    assert apps["profile"].workload == "profile" and apps["profile"].lang == "otel-go.otel_go"  # dataset card: hs is Go
    assert list(node_ids) == ["10.0.0.1"] and cluster == conv.uid(CASE, "cluster", NS)


def test_golden_maps_roles_to_roots_allowed_and_nothing():
    ws, _, _, apps = _entities(BASE_ROWS)
    faults = [
        {"service": "profile", "chaos_type": "PodFailure", "role": "required", "container": ["hotel-reserv-profile"]},
        {"service": "profile", "chaos_type": "CPUStress", "role": "required", "container": ["hotel-reserv-profile"]},  # same family -> one root
        {"service": "geo", "chaos_type": "MemoryStress", "role": "optional", "container": ["hotel-reserv-geo"]},
        {"service": "frontend", "chaos_type": "NetworkDelay", "role": "not_effective", "container": ["hotel-reserv-frontend"]},
    ]
    entry, why = conv.golden_entry(_source(faults), ws, apps)
    assert why == "" and entry["expected_status"] == "confirmed"
    assert len(entry["roots"]) == 1
    root = entry["roots"][0]
    p = ws["profile"]
    assert root["target_aliases"] == ["profile"]
    assert set(root["target_ids"]) == {apps["profile"].id, p.ids["deployment"], p.ids["replicaset:profile-5d8f7c9b6d"], p.ids["pod:profile-5d8f7c9b6d-x2x4q"],
                                       p.ids["container:profile-5d8f7c9b6d-x2x4q/hotel-reserv-profile"], p.ids["service"]}
    assert set(entry["allowed_target_ids"]) == set(conv.family_ids("geo", ws, apps))
    frontend = set(conv.family_ids("frontend", ws, apps))
    assert not frontend & (set(root["target_ids"]) | set(entry["allowed_target_ids"]))  # not_effective: naming it is wrong


def test_golden_also_accept_and_missing_root():
    ws, _, _, apps = _entities(BASE_ROWS)
    entry, _ = conv.golden_entry(_source([{"service": "profile", "chaos_type": "HTTPResponseAbort", "role": "required", "container": [], "also_accept": ["frontend"]}]), ws, apps)
    assert entry["roots"][0]["target_aliases"] == ["profile", "frontend"]
    assert set(conv.family_ids("frontend", ws, apps)) <= set(entry["roots"][0]["target_ids"])
    entry, why = conv.golden_entry(_source([{"service": "absent-svc", "chaos_type": "PodFailure", "role": "required", "container": []}]), ws, apps)
    assert entry is None and "absent-svc" in why


def test_unit_conversions_and_counter_rates():
    pod, ctr = "profile-5d8f7c9b6d-x2x4q", "hotel-reserv-profile"
    rows = BASE_ROWS + [
        _row(START + 2, "k8s.pod.cpu.usage", 0.3, pod=pod, dep="profile"),
        _row(START + 2, "k8s.pod.cpu.node.utilization", 0.004, pod=pod, dep="profile"),
        _row(START + 2, "container.memory.working_set", 2.0e8, pod=pod, ctr=ctr, dep="profile"),
        _row(START + 2, "k8s.container.cpu_limit", 0.5, pod=pod, ctr=ctr),
        _row(START + 2, "k8s.container.restarts", 1.0, pod=pod, ctr=ctr),
        _row(START + 2, "k8s.container.restarts", 2.0, pod=pod, ctr="sidecar"),
        _row(START + 3, "k8s.deployment.available", 1.0, dep="profile", node=None),
    ]
    sums = [  # rx and tx rows of a cumulative counter whose direction attribute was dropped
        _row(START + 0, "k8s.pod.network.io", 1000.0, pod=pod, dep="profile"), _row(START + 0, "k8s.pod.network.io", 500.0, pod=pod, dep="profile"),
        _row(START + 5, "k8s.pod.network.io", 2000.0, pod=pod, dep="profile"), _row(START + 5, "k8s.pod.network.io", 1000.0, pod=pod, dep="profile"),
        _row(START + 8, "k8s.pod.network.io", 10.0, pod=pod, dep="profile"), _row(START + 8, "k8s.pod.network.io", 5.0, pod=pod, dep="profile"),  # reset
    ]
    ws, node_ids, cluster, _ = _entities(rows + sums)
    series = conv.metric_series(_source([]), rows, sums, ws, node_ids, cluster, 0)
    got = {(s["metric"]["__name__"], s["metric"].get("pod")): s for s in series}
    assert got[("kcm.container.cpu_usage", pod)]["values"] == [250.0]  # 0.25 cores -> 250 mCPU
    assert got[("kcm.pod.cpu_usage", pod)]["values"] == [300.0]
    assert got[("kcm.pod.cpu_utilization_by_node_capacity", pod)]["values"] == [0.4]  # ratio -> percent
    assert got[("kcm.container.mem_usage", pod)]["values"] == [2.0e8]  # bytes unchanged
    assert got[("kcm.container.cpu_limit", pod)]["values"] == [500.0] and got[("kcm.pod.cpu_limit", pod)]["values"] == [500.0]
    assert got[("kcm.pod.container_restart_count", pod)]["values"] == [3.0]  # summed over the pod's containers
    assert got[("kcm.pod.network_rxtx_bps", pod)]["values"] == [300.0]  # (3000 - 1500) / 5 s; the reset is skipped
    labels = got[("kcm.container.cpu_usage", pod)]["metric"]
    assert labels["target_id"] == cluster and labels["namespace"] == NS and labels["container"] == ctr and labels["pod_id"] == ws["profile"].ids[f"pod:{pod}"]
    dep = next(s for s in series if s["metric"]["__name__"] == "kcm.deployment.available_replicas")
    assert dep["metric"]["deployment"] == "profile" and dep["metric"]["deployment_id"] == ws["profile"].ids["deployment"]
    assert got[("kcm.container.cpu_usage", pod)]["timestamps"] == [START * 1000]


def test_sli_from_server_spans():
    _, _, _, apps = _entities(BASE_ROWS)
    durations = [float(i) for i in range(1, 21)]  # 20 spans in the first bucket, 1..20 ms
    spans = [(START + 0.1 * i, d, i in (3, 7)) for i, d in enumerate(durations)]  # two errors (status ERROR or HTTP 5xx)
    series = conv.sli_series({"profile": spans}, apps, START, START + 30, 0)
    got = {s["metric"]["__name__"]: s for s in series}
    assert got["apm.trace_derived.rps"]["values"] == [2.0, 0.0, 0.0]  # 20 spans / 10 s, empty buckets are 0 req/s
    assert got["apm.trace_derived.span_count"]["values"][0] == 20.0 and got["apm.trace_derived.error_count"]["values"][0] == 2.0
    assert got["apm.trace_derived.error_rate"]["values"] == [10.0]  # percent; absent when there is no request
    assert got["apm.trace_derived.percentile95"]["values"] == [19.0] and got["apm.trace_derived.percentile99"]["values"] == [20.0]
    assert got["apm.trace_derived.percentile50"]["values"] == [10.0]  # nearest rank, ms
    assert got["apm.trace_derived.rps"]["metric"] == {"__name__": "apm.trace_derived.rps", "target_id": apps["profile"].id, "service_name": "profile"}


def test_detector_uses_sli_floors_and_directions():
    def series(values, name):
        return {"metric": {"__name__": name, "target_id": "t"}, "values": values, "timestamps": [i * 10_000 for i in range(len(values))]}

    def detect(values, name):
        return conv.rc.detect([series(values, name)], 0, 180, metrics=conv.SYMPTOM_METRICS)

    flat = [20.0 + 0.1 * (i % 3) for i in range(18)]  # 180 s baseline
    assert detect(flat + [22.0] * 10, conv.SLI["p95"]) == []  # +2 ms < 5 ms floor
    assert len(detect(flat + [80.0] * 10, conv.SLI["p95"])) == 1
    assert detect(flat + [1.0] * 10, conv.SLI["p95"]) == []  # latency drop is not a symptom
    busy = [5.0 + 0.1 * (i % 3) for i in range(18)]
    assert len(detect(busy + [0.0] * 10, conv.SLI["rps"])) == 1  # traffic loss alarms
    assert detect(flat + [80.0] * 10, "kcm.pod.cpu_usage") == []  # resource series do not alarm by default


def test_span_attributes_keep_real_kinds_and_parse_name_grammars():
    attrs = conv.span_attributes("SERVER", "HTTP /hotels", "GET", 500, None, 12)
    assert attrs == {"http.request.method": "GET", "http.response.status_code": "500", "http.response.body.size": "12"}
    db = conv.span_attributes("CLIENT", "SELECT ts.orders", None, None, None, None)
    assert db["db.system"] == "other_sql" and db["db.name"] == "ts" and db["db.sql.table"] == "orders" and db["db.operation"] == "SELECT"
    url = conv.span_attributes("CLIENT", "HTTP POST http://ts-ui-dashboard:8080/api/v1/users/login", None, None, None, None)
    assert url["url.full"] == "http://ts-ui-dashboard:8080/api/v1/users/login" and url["http.request.method"] == "POST"
    assert "db.system" not in conv.span_attributes("INTERNAL", "SELECT ts.orders", None, None, None, None)


def test_structural_summaries_port():
    assert conv.stable_path_name("GET /api/v1/users/4d2a46c7-71cb-4cf1-b5bb-b68406d9da6f/orders") == "GET /api/v1/users/<*>/orders"
    assert conv.stable_path_name("geo.Geo/Nearby") == "geo.Geo/Nearby"  # short tokens kept
    assert conv.stable_path_name("memcached_get_profile") == "memcached_get_profile"
    spans = [("t1", 1, "a", "", "HTTP /hotels", True), ("t1", 2, "b", "a", "search.Nearby", True), ("t1", 3, "c", "b", "geo.Nearby", True),
             ("t1", 4, "d", "a", "profile.Get", False)]
    assert conv.maximal_error_chains(spans, "frontend") == [("frontend:HTTP /hotels>frontend:search.Nearby>frontend:geo.Nearby", 3, "t1")]
    _, _, _, apps = _entities(BASE_ROWS)
    chains, paths = conv.structural_summaries({("frontend", 0): spans}, apps, START, START + 600, 0)
    sigs = {r["chain_signature"]: r for r in chains}
    assert len(sigs) == 6  # every contiguous segment of the depth-3 chain
    assert sigs["frontend:search.Nearby"]["route"] == "search.Nearby" and sigs["frontend:search.Nearby"]["chain_count"] == 1
    assert {r["path_signature"] for r in paths} == {"frontend:HTTP /hotels->frontend:search.Nearby", "frontend:search.Nearby->frontend:geo.Nearby",
                                                     "frontend:HTTP /hotels->frontend:profile.Get"}
    assert all(r["window_start"] == START * 10**9 and r["window_end"] == (START + 60) * 10**9 for r in chains + paths)


def test_reference_text_renders_required_chain_only():
    graph = {
        "nodes": [{"component": "service|profile", "state": ["unavailable"]}, {"component": "span|frontend::HTTP /hotels", "state": ["erroring"]}],
        "edges": [{"source": "service|profile", "target": "span|frontend::HTTP /hotels"}],
        "root_causes": [{"component": "service|profile", "state": ["unavailable"]}, {"component": "container|geo", "state": ["unknown"]}],
        "alarm_nodes": [{"component": "span|frontend::HTTP /hotels", "state": ["erroring"]}],
        "component_to_service": {"service|profile": "profile", "container|geo": "geo", "span|frontend::HTTP /hotels": "frontend"},
    }
    faults = [{"service": "profile", "chaos_type": "PodFailure", "role": "required", "container": []},
              {"service": "geo", "chaos_type": "CPUStress", "role": "optional", "container": []}]
    meta = conv.scenario_metadata(_source(faults, graph=graph))
    assert meta["title"] == "profile PodFailure"
    assert "service|profile [unavailable] -> span|frontend::HTTP /hotels [erroring]" in meta["cause"]
    assert "geo" not in meta["cause"] and "geo: CPUStress" in meta["description"]  # optional faults stay out of the judged reference


def test_blindness_check_finds_planted_answer_tokens(tmp_path):
    src = _source([{"service": "profile", "chaos_type": "PodFailure", "role": "required", "container": []}], injection={"task_id": "1876f7f5-fa7a-45d9-99c3"})
    tokens = conv.forbidden_tokens(src)
    assert {"hs7-profile-pod-failure-abc123", "PodFailure", "pod-failure", "1876f7f5-fa7a-45d9-99c3", "chaos"} <= set(tokens)
    data = tmp_path / "data"
    (data / "clickhouse").mkdir(parents=True)
    (data / "victoriametrics.export").write_text(json.dumps({"metric": {"__name__": "kcm.pod.cpu_usage", "pod": "profile-1"}, "values": [1], "timestamps": [0]}) + "\n")
    (data / "sources.json").write_text(json.dumps({"not_collected": ["snmp"], "note": "clean"}))
    pq.write_table(pa.table({"body": ["GET /hotels 200", "ok"]}), data / "clickhouse" / "lucida_logs_local.parquet")
    (tmp_path / "meta.json").write_text(json.dumps({"scenario_metadata": {"title": "profile PodFailure"}}))  # grader-only, not scanned
    assert conv.blindness_violations(tmp_path, tokens, {"targets": [{"name": "profile"}]}) == {}
    pq.write_table(pa.table({"body": ["chaos-daemon: podfailure on profile"]}), data / "clickhouse" / "lucida_logs_local.parquet")
    hits = conv.blindness_violations(tmp_path, tokens, {"targets": [{"name": "hs7-profile-pod-failure-abc123"}]})
    assert hits["lucida_logs_local.parquet:body:chaos"] == 1 and hits["lucida_logs_local.parquet:body:podfailure"] == 1
    assert hits["postgres:hs7-profile-pod-failure-abc123"] == 1


def test_log_bodies_shift_iso_and_epoch_timestamps_only():
    body = '{"ts":1000000123.0125716,"msg":"handled","at":"1970-01-12T13:48:43Z","order":1234567890123456,"id":1999999999,"ms":1000000200500}'
    start = 1_000_000_000  # original capture start (unix s)
    out = conv.shift_epochs(conv.rc.shift_body(body, 3600), start - 86400, start + 600 + 86400, 3600)
    assert '"ts":1000003723.0125716' in out and '"ms":1000003800500' in out  # epoch s / ms near the capture move
    assert '"at":"1970-01-12T14:48:43Z"' in out  # ISO date-times move (rcaeval_convert.shift_body)
    assert '"id":1999999999' in out and '"order":1234567890123456' in out  # numbers outside the window are not timestamps


def test_coverage_gaps_are_stated_not_hidden():
    full = [int((START + x) * 1e9) for x in (5, 300, 595)]
    assert conv.coverage_gaps("로그", full, START, START + 600) == ""
    assert "없다" in conv.coverage_gaps("로그", [], START, START + 600)
    head = [int((START + x) * 1e9) for x in (2, 150, 299)]
    assert "0분~5분" in conv.coverage_gaps("로그", head, START, START + 600)


def test_case_ids_and_public_compare_are_deterministic():
    names = [f"case-{i}" for i in range(2000)]
    held = [n for n in names if conv.in_public_compare(n)]
    assert 0.17 < len(held) / len(names) < 0.23
    assert held == [n for n in names if conv.in_public_compare(n)]
    assert conv.case_id_of("batch-01KQHDBB5Y8K69Z13G7VSWG9FS", "o2a").startswith("case-openrca2-o2a-v3-")
    assert "batch" not in conv.case_id_of("batch-01KQHDBB5Y8K69Z13G7VSWG9FS", "o2a").removeprefix("case-openrca2-o2a-v3-")


def test_limit_selection_is_system_stratified_and_fault_interleaved(tmp_path, monkeypatch):
    cases = {"a1": ("ts", "PodFailure"), "a2": ("ts", "PodFailure"), "a3": ("ts", "NetworkDelay"), "b1": ("hs", "CPUStress"), "b2": ("hs", "PodFailure"),
             "c1": ("otel-demo", "PodKill")}
    for name, (system, chaos) in cases.items():
        (tmp_path / "cases" / name).mkdir(parents=True)
        (tmp_path / "cases" / name / "label.json").write_text(json.dumps({"system": system, "faults": [{"service": "x", "chaos_type": chaos, "role": "required"}]}))
    monkeypatch.setattr(conv, "case_complete", lambda d: True)
    picked = [p.name for p in conv.select_cases(tmp_path, "all", 3, None)]
    assert [cases[n][0] for n in picked] == ["ts", "hs", "otel-demo"]
    ts_only = [p.name for p in conv.select_cases(tmp_path, "ts", 2, None)]
    assert {cases[n][1] for n in ts_only} == {"PodFailure", "NetworkDelay"}
