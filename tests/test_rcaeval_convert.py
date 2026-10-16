import importlib.util
import json
import sys
from pathlib import Path


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


conv = _load("rcaeval_convert_under_test", "scripts/rcaeval_convert.py")


def test_log_severity_parses_formats_only():
    assert conv.log_severity("ts=2024-01-21T08:04:18Z caller=x.go:40 level=error msg=boom") == "ERROR"
    assert conv.log_severity("2024-11-22 02:51:59.404  WARN [carts,37dd,37dd,false] 7 --- [exec-17] o.s.web : nope") == "WARN"
    assert conv.log_severity("front-end POST /orders 500 72.9 ms - 70") == "ERROR"
    assert conv.log_severity("front-end GET /catalogue 404 1.2 ms - 9") == "WARN"
    assert conv.log_severity("method=Health result=1 took=6.874µs") == "INFO"


def test_shift_body_moves_embedded_timestamps():
    body = "ts=2024-01-21T08:04:18Z x 2024-11-22 02:51:59.404 y"
    out = conv.shift_body(body, 3600)
    assert "2024-01-21T09:04:18Z" in out and "2024-11-22 03:51:59.404" in out


def test_rcaeval_rate_columns_pass_through_and_cpu_percent_becomes_millicores():
    src = conv.Source(
        rel="RE2-SS/carts_cpu/1", service="carts", fault="cpu", inject=100.0, times=[0.0, 1.0, 2.0],
        columns={"carts_istio-request-total": [2.8, 2.7, 3.1], "carts_istio-error-total": [0.0, 0.3, 0.0],
                 "carts_container-cpu-usage-seconds-total": [50.0, 50.0, 50.0], "gke-node-a_node-cpu-seconds-total": [396.0, 396.1, 396.2]},
        logs=[], pod_node={"carts-795df7fd79-7x8z8": "gke-node-a"},
    )
    ws, node_ids, cluster = conv.workloads(src, "case-rcaeval-t-v3-00000000")
    got = {s["metric"]["__name__"]: s["values"][0] for s in conv.metric_series(src, ws, node_ids, cluster, 0.0)}
    assert abs(got["mesh.istio.rps"] - 2.866667) < 1e-5  # already req/s: no differencing
    assert got["kcm.container.cpu_usage"] == 500.0 and got["kcm.pod.cpu_usage"] == 500.0  # 50% of a core
    assert abs(got["mesh.istio.error_rate"] - 0.3 / 2.7 / 3) < 1e-6  # per-second ratio, then 10 s bucket mean
    assert "kcm.node.cpu_usage" not in got  # all-mode node CPU (incl. idle) carries no usage signal


def _series(values, name="mesh.istio.percentile95", start_ms=0):
    return {"metric": {"__name__": name, "target_id": "t"}, "values": values, "timestamps": [start_ms + i * 10_000 for i in range(len(values))]}


def test_blind_detector_opens_on_a_sustained_shift_after_its_own_baseline():
    flat = [1.0 + 0.01 * (i % 3) for i in range(48)]  # 480 s baseline
    series = _series(flat + [1.0] * 5 + [9.0] * 10 + [1.0] * 6)
    episodes = conv.detect([series], capture_start_ms=0, baseline_s=480)
    assert len(episodes) == 1
    ep = episodes[0]
    assert ep["open"] == series["timestamps"][53] and ep["close"] is not None and ep["peak"] > 5
    assert conv.detect([_series(flat + [1.0] * 20)], 0, 480) == []  # no shift, no alarm
    assert conv.detect([_series(flat + [9.0] * 10, name="kcm.container.mem_rss")], 0, 480) == []  # not an alarm metric
    assert conv.detect([_series(flat + [9.0] * 10, name="kcm.pod.cpu_usage")], 0, 480) == []  # resource alarms off by default
    assert len(conv.detect([_series(flat + [9.0] * 10, name="kcm.pod.cpu_usage")], 0, 480, metrics=conv.RESOURCE_METRICS)) == 1


def test_registry_rows_never_carry_the_fault_label():
    src = conv.Source(
        rel="RE2-SS/carts_cpu/1", service="carts", fault="cpu", inject=100.0, times=[0.0, 1.0],
        columns={"carts_container-cpu-usage-seconds-total": [1.0, 2.0], "carts-db_container-memory-rss": [1.0, 1.0], "gke-node-a_node-cpu-seconds-total": [1.0, 2.0]},
        logs=[], pod_node={"carts-795df7fd79-7x8z8": "gke-node-a", "carts-db-5d88c7dddc-chw52": "gke-node-a"},
    )
    case_id = "case-rcaeval-t-v3-00000000"
    ws, node_ids, cluster = conv.workloads(src, case_id)
    assert set(ws) == {"carts", "carts-db"} and ws["carts"].app and not ws["carts-db"].app
    rows = conv.pg_rows(case_id, ws, node_ids, cluster, "2026-07-01T00:00:00Z")
    blob = json.dumps(rows).lower()
    assert "cpu" not in blob.replace("kcm.", "") and "carts_cpu" not in blob
    kinds = {t["meta"].get("resource_kind") for t in rows["targets"]}
    assert {"cluster", "node", "deployment", "replica_set", "pod", "container", "service", "otel_java"} <= kinds
    app = next(t for t in rows["targets"] if t["type"] == "application")
    assert app["id"] == ws["carts"].ids["app"] and app["meta"]["resource_type"] == "otel-java.otel_java"


def test_external_capture_env_only_for_converted_captures(tmp_path):
    runner = _load("mcp_case_run_env_under_test", "scripts/mcp_case_run.py")
    (tmp_path / "data").mkdir()
    (tmp_path / "meta.json").write_text(json.dumps({"capture_start": "2026-08-20T09:00:00Z"}))
    assert runner.external_capture_env(tmp_path) == {}
    (tmp_path / "data" / "sources.json").write_text(json.dumps({"not_collected": ["traces"]}))
    env = runner.external_capture_env(tmp_path)
    assert env["RCA_CAPTURE_START"] == "2026-08-20T09:00:00Z" and env["RCA_CAPTURE_SOURCES"].endswith("data/sources.json")
    assert Path(env["RCA_CAPTURE_SOURCES"]).is_absolute()


def test_trace_span_kinds_are_derived_from_structure(tmp_path):
    import pyarrow.parquet as pq

    run = tmp_path / "run"
    run.mkdir()
    header = "time,traceID,spanID,serviceName,methodName,operationName,startTimeMillis,startTime,duration,statusCode,parentSpanID\n"
    rows = [
        "01:00,t1,a0,ts-ui,,GET /api/order,1,1000000,500,,",  # root -> SERVER
        "01:00,t1,a1,ts-ui,,POST /api/v1/order,1,1000100,400,,a0",  # has a child in another service -> CLIENT
        "01:00,t1,b0,ts-order,,POST /api/v1/order,1,1000200,300,,a1",  # parent in another service -> SERVER
        "01:00,t1,b1,ts-order,,find ts-order-mongo.orders,1,1000300,50,,b0",  # mongo driver span -> CLIENT + db.*
        "01:00,t1,b2,ts-order,,OrderRepository.findById,1,1000350,40,,b0",  # same-service leaf -> INTERNAL
    ]
    (run / "traces.csv").write_text(header + "\n".join(rows) + "\n")
    src = conv.Source(rel="RE2-TT/ts-order_cpu/1", service="ts-order", fault="cpu", inject=0.0, times=[0.0], columns={}, logs=[], pod_node={}, run_dir=run, system="TT")
    ws = {n: conv.Workload(n, f"{n}-pod", "node-a", True, ids={"app": f"id-{n}", "pod": f"pod-{n}"}, ns="train-ticket") for n in ("ts-ui", "ts-order")}
    out = tmp_path / "t.parquet"
    assert conv.write_traces(src, ws, 0.0, out) == 5
    t = pq.read_table(out, columns=["span_id", "span_kind", "span_attributes", "service_name"]).to_pylist()
    assert [r["span_kind"] for r in t] == ["SERVER", "CLIENT", "SERVER", "CLIENT", "INTERNAL"]
    db = dict(t[3]["span_attributes"])
    assert db["db.system"] == "mongodb" and db["server.address"] == "ts-order-mongo" and db["db.collection.name"] == "orders"


def test_header_only_traces_mean_not_collected(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "traces.csv").write_text("time,traceID,spanID,serviceName,methodName,operationName,startTimeMillis,startTime,duration,statusCode,parentSpanID\n")
    src = conv.Source(rel="RE3-SS/x_f1/1", service="x", fault="f1", inject=0.0, times=[0.0], columns={}, logs=[], pod_node={}, run_dir=run)
    assert conv.write_traces(src, {}, 0.0, tmp_path / "t.parquet") == 0 and not (tmp_path / "t.parquet").exists()


def test_detector_floors_sparse_traffic_and_ignores_latency_drops():
    flat = [0.0] * 48
    assert conv.detect([_series(flat + [0.0067] * 10, name="mesh.istio.rps")], 0, 480) == []  # 0 -> 1 req / 150 s is noise
    assert len(conv.detect([_series(flat + [2.0] * 10, name="mesh.istio.rps")], 0, 480)) == 1  # real traffic appears
    busy = [2.0 + 0.01 * (i % 3) for i in range(48)]
    assert len(conv.detect([_series(busy + [0.1] * 10, name="mesh.istio.rps")], 0, 480)) == 1  # traffic loss alarms
    slow = [1.0 + 0.01 * (i % 3) for i in range(48)]
    assert conv.detect([_series(slow + [0.2] * 10)], 0, 480) == []  # latency drop after warm-up is not a symptom
    assert len(conv.detect([_series(slow + [3.0] * 10)], 0, 480)) == 1
