from rca_lab.mcp.numeric_grounding import extract_claims, ground

OBS_LATENCY = '{"findings":[{"metric":"http.server.duration","p50_ms":607,"p95_ms":2098,"refs":["vm:lat{target_id=a}:w1"]},' \
              '{"metric":"http.server.duration","p95_ms":6612,"refs":["vm:lat{target_id=a}:w2"]}]}'
OBS_OTHER = '{"findings":[{"ts":"2026-10-11T13:45:38.800Z","message":"java.lang.OutOfMemoryError","count":122}]}'


def _conclusion(text, refs=("vm:lat{target_id=a}:w1", "vm:lat{target_id=a}:w2")):
    return {"summary": "", "causes": [{"mechanism": text, "support_refs": list(refs), "target": "a"}], "external_causes": []}


def statuses(result):
    return {c.text: c.status for c in result.claims}


def test_observed_values_and_derived_ratio_are_grounded():
    r = ground(_conclusion("p95 rose from 2.1s to 6.6s (3.2x) after the rollout"), [OBS_LATENCY, OBS_OTHER])
    s = statuses(r)
    assert s["2.1s"] == "verified_cited" and s["6.6s"] == "verified_cited"   # 2098 ms / 6612 ms at the written precision
    assert s["3.2x"] == "derived"                                             # 6612 / 2098 = 3.15
    assert not r.unverified


def test_numbers_absent_from_every_observation_are_unverified():
    r = ground(_conclusion("p95 went from 4.4s to 9.9s"), [OBS_LATENCY, OBS_OTHER])
    assert {c.text for c in r.unverified} == {"4.4s", "9.9s"}


def test_value_seen_outside_the_cited_observations():
    r = ground(_conclusion("122 OutOfMemoryError lines", refs=("vm:lat{target_id=a}:w1",)), [OBS_LATENCY, OBS_OTHER])
    assert statuses(r)["122"] == "verified_anywhere"


def test_clock_times_match_utc_or_kst():
    r = ground(_conclusion("OOM at 13:45:38Z, i.e. 22:45:38 KST"), [OBS_LATENCY, OBS_OTHER])
    assert all(c.status != "unverified" for c in r.claims if c.unit == "clock_s")
    r = ground(_conclusion("OOM at 13:59:10Z"), [OBS_LATENCY, OBS_OTHER])
    assert [c.text for c in r.unverified] == ["13:59:10Z"]


def test_identifiers_and_small_counts_are_not_claims():
    text = ("testbed-account-677ff7cf9 on 192.168.200.137 ran release 1.4.0 with ORA-00001 on case f41-r, "
            "target c2efb1d9-00ec-4ca7-8fd9-80bd122c9d10, 2 pods restarted on 2026-10-11")
    assert extract_claims(text) == []


def test_sizes_compare_across_units():
    obs = '{"jvm.memory.used_after_last_gc_bytes": 90177536}'   # 86.0 MiB
    r = ground(_conclusion("live heap after GC reached 86MiB", refs=()), [obs])
    assert statuses(r)["86MiB"] == "verified_anywhere"


def test_korean_particles_after_units_and_thousands_separators():
    obs = '{"rollback_rate": 0.569, "server_latency_max_s": 488.1, "mem_mapped_file": 3620864}'
    r = ground(_conclusion("실패율 56.9%를 일으켰고 지연 최대 488.1초와 일치, mem_mapped_file 3,620,864로 급증", refs=()), [obs])
    assert not r.unverified, [c.text for c in r.unverified]


def test_labels_are_not_quantities():
    assert extract_claims("RTT on switch #109 and latency p50/99 limits") == []


def test_nanoseconds_bytes_and_per_minute_rates():
    obs = '{"span_ns": 204023000, "size_bytes": 1380761600, "rps": 0.38, "spans": 129689}'
    r = ground(_conclusion("CLIENT span 204,023,000ns, size 1,380,761,600Bytes, checkout 22.8/min, 129,689스팬", refs=()), [obs])
    assert not r.unverified, [c.text for c in r.unverified]
