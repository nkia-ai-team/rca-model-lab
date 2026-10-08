package tools

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestLoadAvailability(t *testing.T) {
	a, err := LoadAvailability("")
	if err != nil || len(a.NotCollected) != 0 {
		t.Fatalf("empty path must mean everything collected: %v %v", a, err)
	}
	dir := t.TempDir()
	good := filepath.Join(dir, "ok.json")
	os.WriteFile(good, []byte(`{"not_collected":["traces","database"],"note":"external dataset"}`), 0o644)
	a, err = LoadAvailability(good)
	if err != nil || !a.NotCollected["traces"] || !a.NotCollected["database"] || a.Note != "external dataset" {
		t.Fatalf("manifest not loaded: %+v %v", a, err)
	}
	bad := filepath.Join(dir, "bad.json")
	os.WriteFile(bad, []byte(`{"not_collected":["tracez"]}`), 0o644)
	if _, err := LoadAvailability(bad); err == nil {
		t.Fatal("unknown dataset must be rejected")
	}
}

func TestToolsetWithAvailability(t *testing.T) {
	first := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	plain := Toolset(Stores{}, first, first.Add(time.Hour))
	avail := Availability{NotCollected: map[string]bool{"traces": true, "database": true, "kubernetes": true}, Note: "n"}
	gated := ToolsetWith(Stores{}, first, first.Add(time.Hour), avail)
	if len(plain) != len(gated) {
		t.Fatalf("tool count changed: %d vs %d", len(plain), len(gated))
	}
	for i := range plain {
		if plain[i].Name != gated[i].Name || plain[i].Description != gated[i].Description || string(plain[i].Parameters) != string(gated[i].Parameters) {
			t.Fatalf("tool %d changed: %s vs %s", i, plain[i].Name, gated[i].Name)
		}
	}
	byName := map[string]int{}
	for i, tool := range gated {
		byName[tool.Name] = i
	}
	for _, name := range []string{"get_trace_spans", "breakdown_endpoints", "expand_topology", "db_blocking", "db_slow_queries", "get_k8s_state"} {
		out, err := gated[byName[name]].Call(context.Background(), json.RawMessage(`{}`))
		env, ok := out.(Envelope)
		if err != nil || !ok || env.Status != "no_data" || env.NoDataReason != NoDataNotCollected {
			t.Fatalf("%s: want no_data/not_collected, got %+v %v", name, out, err)
		}
	}
	if avail.Missing("scan_metrics") != nil || avail.Missing("sample_logs") != nil || avail.Missing("list_changes") != nil {
		t.Fatal("tools whose data is collected must stay available")
	}
	out, err := gated[byName["describe_data_sources"]].Call(context.Background(), json.RawMessage(`{}`))
	env, ok := out.(Envelope)
	if err != nil || !ok || len(env.Findings) == 0 || env.Findings[0]["domain"] != "capture_availability" {
		t.Fatalf("describe_data_sources must list the gaps first: %+v %v", out, err)
	}
	plainOut, _ := plain[byName["describe_data_sources"]].Call(context.Background(), json.RawMessage(`{}`))
	if pe := plainOut.(Envelope); len(pe.Findings) > 0 && pe.Findings[0]["domain"] == "capture_availability" {
		t.Fatal("without a manifest the catalog must be unchanged")
	}
}

func TestSampleDefaultBaselineClipsToCaptureStart(t *testing.T) {
	first := time.Date(2024, 1, 21, 8, 16, 0, 0, time.UTC)
	from, to := sampleDefaultBaseline(first, time.Time{})
	if !from.Equal(first.Add(-60*time.Minute)) || !to.Equal(first) {
		t.Fatalf("default baseline changed without capture start: %v %v", from, to)
	}
	cs := first.Add(-12 * time.Minute)
	from, _ = sampleDefaultBaseline(first, cs)
	if !from.Equal(cs) {
		t.Fatalf("baseline must start at capture start: %v", from)
	}
	from, _ = sampleDefaultBaseline(first, first.Add(-2*time.Hour))
	if !from.Equal(first.Add(-60 * time.Minute)) {
		t.Fatalf("an early capture start must not widen the baseline: %v", from)
	}
}

func TestUncapturedBaselineBuckets(t *testing.T) {
	defer func(old time.Time) { CaptureStart = old }(CaptureStart)
	base := time.Date(2026, 8, 20, 8, 52, 0, 0, time.UTC)
	CaptureStart = time.Time{}
	if k := uncapturedBaselineBuckets(base, 60, 16); k != 0 {
		t.Fatalf("no capture start must mean no uncaptured buckets, got %d", k)
	}
	CaptureStart = base.Add(8 * time.Minute)
	if k := uncapturedBaselineBuckets(base, 60, 16); k != 8 {
		t.Fatalf("want 8 uncaptured buckets, got %d", k)
	}
	CaptureStart = base.Add(-time.Hour)
	if k := uncapturedBaselineBuckets(base, 60, 16); k != 0 {
		t.Fatalf("capture starting before the baseline must not flag buckets, got %d", k)
	}
}
