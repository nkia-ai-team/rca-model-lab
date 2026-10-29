import json

import pytest

from rca_lab.mcp.masking import (
    LISTEV_ANOMALOUS_TAIL,
    LISTEV_NORMAL_TAIL,
    MaskedMCP,
    filter_response,
    filter_seed,
    parse_masks,
    write_manifest,
)


def _list_events(extra_findings=(), warn=5, episodes=None):
    episodes = episodes if episodes is not None else [
        {"section": "episode", "detector": "change-detect", "reason": "config", "episode_id": "T1", "refs": ["ch:ev:T1"]},
        {"section": "episode", "detector": "anomaly", "reason": "latency", "episode_id": "T2", "refs": ["ch:ev:T2"]},
    ]
    k8s = [
        {"section": "k8s", "kind": "Deployment", "name": "api", "reason": "ScalingReplicaSet", "event_type": "Normal", "refs": ["k:1"]},
        {"section": "k8s", "kind": "ReplicaSet", "name": "api-5f7", "reason": "SuccessfulCreate", "event_type": "Normal", "refs": ["k:2"]},
        {"section": "k8s", "kind": "Job", "name": "backfill", "reason": "SuccessfulCreate", "event_type": "Normal", "refs": ["k:3"]},
        {"section": "k8s", "kind": "Pod", "name": "api-5f7-x", "reason": "Killing", "event_type": "Normal", "refs": ["k:4"]},
    ]
    meta = {"section": "summary_meta",
            "counts": {"episodes_total": len(episodes), "episodes_returned": len(episodes), "unpaired_total": 0,
                       "unpaired_returned": 0, "k8s_groups_total": 4, "k8s_groups_returned": 4},
            "k8s_reason_summary": [
                {"namespace": "ns", "kind": "Deployment", "reason": "ScalingReplicaSet", "event_type": "Normal", "objects": 1},
                {"namespace": "ns", "kind": "ReplicaSet", "reason": "SuccessfulCreate", "event_type": "Normal", "objects": 1},
                {"namespace": "ns", "kind": "Job", "reason": "SuccessfulCreate", "event_type": "Normal", "objects": 1},
                {"namespace": "ns", "kind": "Pod", "reason": "Killing", "event_type": "Normal", "objects": 1}],
            "k8s_reason_summary_total": 4}
    findings = [*episodes, *extra_findings, *k8s, meta]
    summary = (f"사건 — 감지 에피소드 {len(episodes)}건(반환 {len(episodes)}, 창 안 미해소 0), 단건 0건, "
               f"K8s 기록 4그룹(Warning {warn}).{LISTEV_ANOMALOUS_TAIL} 사유별 객체 수 — "
               "Normal[Killing 1, ScalingReplicaSet 1, SuccessfulCreate 2] (summary_meta.k8s_reason_summary에 namespace·kind별).")
    refs = [r for f in findings for r in f.get("refs", [])]
    return {"status": "anomalous", "summary": summary, "findings": findings, "refs": refs}


def test_changes_mask_hides_rollout_traces_and_keeps_the_rest():
    out = json.loads(filter_response("list_events", json.dumps(_list_events()), ("changes",)))
    kept = {(f["section"], f.get("reason")) for f in out["findings"]}
    assert ("episode", "config") not in kept and ("episode", "latency") in kept
    names = {f.get("name") for f in out["findings"] if f["section"] == "k8s"}
    assert names == {"backfill", "api-5f7-x"}                       # Job create and pod lifecycle stay (indirect evidence)
    meta = next(f for f in out["findings"] if f["section"] == "summary_meta")
    assert meta["counts"]["episodes_total"] == 1 and meta["counts"]["k8s_groups_total"] == 2
    assert {r["kind"] for r in meta["k8s_reason_summary"]} == {"Job", "Pod"} and meta["k8s_reason_summary_total"] == 2
    assert "감지 에피소드 1건(반환 1" in out["summary"] and "K8s 기록 2그룹" in out["summary"]
    assert "Normal[Killing 1, SuccessfulCreate 1]" in out["summary"] and "ScalingReplicaSet" not in out["summary"]
    assert set(out["refs"]) == {"ch:ev:T2", "k:3", "k:4"}
    assert out["status"] == "anomalous"


def test_status_turns_normal_when_the_change_episode_was_the_only_anomaly():
    payload = _list_events(warn=0, episodes=[
        {"section": "episode", "detector": "change-detect", "reason": "config", "refs": ["ch:ev:T1"]}])
    out = json.loads(filter_response("list_events", json.dumps(payload), ("changes",)))
    assert out["status"] == "normal"
    assert LISTEV_NORMAL_TAIL in out["summary"] and LISTEV_ANOMALOUS_TAIL not in out["summary"]
    assert "감지 에피소드 0건(반환 0" in out["summary"]


def test_other_masks_and_tools_pass_through_unchanged():
    raw = json.dumps(_list_events())
    assert filter_response("list_events", raw, ("traces",)) == raw
    assert filter_response("sample_logs", raw, ("changes",)) == raw
    assert filter_response("list_events", "not json", ("changes",)) == "not json"


def test_describe_target_drops_limit_change_history_only():
    payload = {"findings": [{"limits": [
        {"matched_on": "cpu", "stability": "changed", "changes": [{"before": 1, "after": 2}], "last_value": 2},
        {"matched_on": "mem", "stability": "observed_constant", "last_value": 4}]}]}
    out = json.loads(filter_response("describe_target", json.dumps(payload), ("changes",)))
    rows = out["findings"][0]["limits"]
    assert rows[0] == {"matched_on": "cpu", "last_value": 2}
    assert rows[1]["stability"] == "observed_constant"


def test_seed_filter_drops_change_alarms_and_refuses_an_empty_incident():
    seed = {"alarms": [{"detector": "change-detect", "target_id": "a", "service": "s1"},
                       {"detector": "anomaly", "target_id": "b", "service": "s2"}],
            "affected_targets": ["a", "b"], "affected_services": ["s1", "s2"],
            "selection": {"eligible": 2, "selected": 2}}
    out = filter_seed(seed, ("changes",))
    assert [a["target_id"] for a in out["alarms"]] == ["b"]
    assert out["affected_targets"] == ["b"] and out["selection"] == {"eligible": 1, "selected": 1}
    assert filter_seed(seed, ("database",)) is seed
    with pytest.raises(ValueError):
        filter_seed({"alarms": [seed["alarms"][0]]}, ("changes",))


def test_manifest_merges_with_an_existing_capture_manifest(tmp_path):
    base = tmp_path / "base.json"
    base.write_text(json.dumps({"not_collected": ["snmp"], "note": "외부 캡처"}))
    out = json.loads(write_manifest(tmp_path / "m.json", ("traces",), base).read_text())
    assert out == {"not_collected": ["snmp", "traces"], "note": "외부 캡처"}
    assert parse_masks("traces, changes") == ("changes", "traces")
    with pytest.raises(ValueError):
        parse_masks("metrics")


def test_masked_client_filters_successful_calls_only():
    class Fake:
        def __init__(self, text, err):
            self.text, self.err, self.closed = text, err, False

        def call(self, name, arguments):
            return self.text, self.err

    raw = json.dumps(_list_events())
    ok = MaskedMCP(Fake(raw, False), ("changes",))
    assert "ScalingReplicaSet" not in ok.call("list_events", {})[0]
    err = MaskedMCP(Fake(raw, True), ("changes",))
    assert err.call("list_events", {})[0] == raw
    assert err.closed is False                                      # attribute passthrough


def test_collector_variant_hides_its_tools_metrics_alarms_and_datasets(tmp_path):
    from rca_lab.mcp.masking import manifest_datasets
    assert manifest_datasets(("dpm", "sms")) == ("database", "host", "process")
    # metric families of the absent collector disappear from metric tools
    scan = {"status": "anomalous", "findings": [{"metric": "dpm.mysql.row_count", "refs": ["vm:dpm.mysql.row_count{target_id=T1}:w"]},
                                                 {"metric": "jvm.memory.used", "refs": ["vm:jvm.memory.used{target_id=T1}:w"]}],
            "refs": ["vm:dpm.mysql.row_count{target_id=T1}:w", "vm:jvm.memory.used{target_id=T1}:w"]}
    out = json.loads(filter_response("scan_metrics", json.dumps(scan), ("dpm",)))
    assert [f["metric"] for f in out["findings"]] == ["jvm.memory.used"] and out["refs"] == ["vm:jvm.memory.used{target_id=T1}:w"]
    only = json.loads(filter_response("read_timeseries", json.dumps({"status": "anomalous", "findings": [scan["findings"][0]]}), ("dpm",)))
    assert only["status"] == "no_data" and only["no_data_reason"] == "not_collected"
    # incident alarms the collector would have raised are not part of the incident
    seed = {"alarms": [{"detector": "stream-anomaly", "metric": "dpm.mysql.row_count", "target_id": "a"},
                       {"detector": "log-anomaly", "target_id": "b"},
                       {"detector": "stream-anomaly", "metric": "sms.cpu", "target_id": "c"}], "selection": {"eligible": 3}}
    assert [a["target_id"] for a in filter_seed(seed, ("dpm",))["alarms"]] == ["b", "c"]
    assert [a["target_id"] for a in filter_seed(seed, ("logs",))["alarms"]] == ["a", "c"]
    with pytest.raises(ValueError):
        filter_seed({"alarms": [seed["alarms"][1]]}, ("logs",))


def test_log_collector_variant_blocks_log_tool_without_calling_the_server():
    class Fake:
        calls = 0

        def call(self, name, arguments):
            Fake.calls += 1
            return "{}", False
    text, err = MaskedMCP(Fake(), ("logs",)).call("sample_logs", {"target": "T1"})
    assert not err and Fake.calls == 0 and json.loads(text)["no_data_reason"] == "not_collected"


def test_kubernetes_variant_drops_k8s_event_rows():
    out = json.loads(filter_response("list_events", json.dumps(_list_events()), ("kcm",)))
    assert not [f for f in out["findings"] if f["section"] == "k8s"]
    assert "K8s 기록 0그룹(Warning 0)" in out["summary"] and "사유별 객체 수" not in out["summary"]
