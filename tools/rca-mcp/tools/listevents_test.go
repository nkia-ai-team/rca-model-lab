// list_events 접기 로직 단위 테스트 — 저장소 무관(hermetic).
// §8 결정 1 보강의 경계 사례: complete / open-only(미해소 — peak 폴백) /
// close-only(발단 역산) / unpaired 평면 / kcm 딱지·dedup 결과 조립.
package tools

import (
	"strings"
	"testing"
)

func TestAssembleListEventsBoundaries(t *testing.T) {
	epRows := []map[string]any{
		{ // complete — open/close 짝, close의 duration·peak 사용.
			"ep": "ep-1", "detector": "stream-anomaly", "reason": "threshold",
			"metric": "cpu.util", "n_open": float64(1), "n_close": float64(1),
			"open_at": "2026-07-21 06:40:00.000000000", "close_at": "2026-07-21 06:53:00.000000000",
			"dur_ms": float64(780000), "peak": "critical", "open_sev": "warning",
			"last_event_id": "e-close-1",
		},
		{ // open-only — duration 미상(0 금지), peak는 open 폴백 + 잠정 표식.
			"ep": "ep-2", "detector": "trace-anomaly", "reason": "latency",
			"metric": "", "n_open": float64(1), "n_close": float64(0),
			"open_at": "2026-07-21 06:50:00.000000000", "close_at": "1970-01-01 00:00:00.000000000",
			"dur_ms": float64(0), "peak": "", "open_sev": "caution",
			"last_event_id": "e-open-2",
		},
		{ // close-only — 발단 = close − duration 역산.
			"ep": "ep-3", "detector": "stream-anomaly", "reason": "threshold",
			"metric": "mem.used", "n_open": float64(0), "n_close": float64(1),
			"open_at": "1970-01-01 00:00:00.000000000", "close_at": "2026-07-21 06:45:00.000000000",
			"dur_ms": float64(600000), "peak": "warning", "open_sev": "",
			"last_event_id": "e-close-3",
		},
	}
	unRows := []map[string]any{
		{"eid": "e-al-1", "detector": "alarm-bridge", "severity": "critical",
			"reason": "", "event_kind": "external_alarm", "at": "2026-07-21 06:41:00", "status": "FIRING", "metric": ""},
	}
	kcmRows := []map[string]any{
		{"namespace": "rca-testbed-commerce", "object_kind": "Pod", "object_name": "testbed-x-1",
			"reason": "Killing", "event_type": "Normal", "max_count": float64(2),
			"first_at": "2026-07-21 06:42:00", "last_at": "2026-07-21 06:43:00", "sev": "INFO", "body": "Stopping container"},
		{"namespace": "kcm-monitoring", "object_kind": "Pod", "object_name": "kcm-agent-1",
			"reason": "Unhealthy", "event_type": "Warning", "max_count": float64(5),
			"first_at": "2026-07-21 06:40:00", "last_at": "2026-07-21 06:50:00", "sev": "WARN", "body": "probe failed"},
	}

	out, err := assembleListEvents("t-1", k8sIdentity{branch: "cluster", cluster: "c-1"}, epRows, unRows, kcmRows, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	env := out.(Envelope)
	if env.Status != "anomalous" {
		t.Fatalf("status: %s", env.Status)
	}

	byEp := map[string]Finding{}
	var meta Finding
	for _, f := range env.Findings {
		switch f["section"] {
		case "episode":
			byEp[f["episode_id"].(string)] = f
		case "summary_meta":
			meta = f
		}
	}
	// complete: close의 확정 peak·duration.
	if f := byEp["ep-1"]; f["boundary"] != "complete" || f["peak_severity"] != "critical" || f["duration_ms"] != 780000 {
		t.Fatalf("complete 접기 이상: %+v", f)
	}
	// open-only: duration "미상"(0 금지) + peak 폴백에 잠정 표식.
	if f := byEp["ep-2"]; f["boundary"] != "closed_after_window" || f["duration_ms"] != "미상" ||
		f["peak_severity"] != "caution" || f["peak_basis"] == nil {
		t.Fatalf("open-only 접기 이상: %+v", f)
	}
	// close-only: 발단 역산 = 06:45 − 10m = 06:35.
	if f := byEp["ep-3"]; f["boundary"] != "opened_before_window" ||
		!strings.Contains(f["derived_open_at"].(string), "06:35:00") {
		t.Fatalf("close-only 역산 이상: %+v", f)
	}
	// unpaired: 접기 제외 + FIRING 보완 + external 딱지.
	// kcm: fact 딱지 + 클러스터 조회의 namespace 구획 안내.
	var sawUnpaired, sawKcm bool
	for _, f := range env.Findings {
		if f["section"] == "unpaired" {
			sawUnpaired = true
			if f["label"] != "external" || f["status"] != "FIRING" {
				t.Fatalf("unpaired 이상: %+v", f)
			}
		}
		if f["section"] == "k8s" {
			sawKcm = true
			if f["label"] != "fact" || f["source"] != "kcm" {
				t.Fatalf("kcm 딱지 이상: %+v", f)
			}
		}
	}
	if !sawUnpaired || !sawKcm {
		t.Fatal("unpaired/kcm finding 누락")
	}
	if meta == nil || meta["namespace_note"] == nil {
		t.Fatalf("클러스터 다중 namespace 구획 안내 누락: %+v", meta)
	}
}

func TestListEventsLabelMapping(t *testing.T) {
	for det, want := range map[string]string{
		"stream-anomaly": "detection", "log-anomaly": "detection", "trace-anomaly": "detection",
		"forecast": "detection", "change-detect": "detection",
		"alarm-bridge": "external",
		"미지-detector":  "detection", // 폴백 — detector 원문은 finding에 남는다
	} {
		if got := listEvLabel(det); got != want {
			t.Fatalf("%s → %s (want %s)", det, got, want)
		}
	}
}
