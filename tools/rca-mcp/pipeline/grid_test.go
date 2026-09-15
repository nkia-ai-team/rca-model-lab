package pipeline

import (
	"encoding/json"
	"testing"
)

// BuildGrid — 배정표대로 채움·빈 칸 결정론(정렬), 격자 밖 도구·범위 밖
// 대상 무시, mock 세계 인자 변형(service 등)·read_timeseries 복수
// targets 흡수.
func TestBuildGrid(t *testing.T) {
	args := func(k, v string) json.RawMessage {
		b, _ := json.Marshal(map[string]string{k: v})
		return b
	}
	multi, _ := json.Marshal(map[string]any{"targets": []string{"db-b", "밖-대상"}})
	calls := []ToolCallRecord{
		{Name: "scan_metrics", Args: args("target", "svc-a")},
		{Name: "list_events", Args: args("target", "svc-a")},
		{Name: "sample_logs", Args: args("target", "svc-a")},
		{Name: "get_trace_breakdown", Args: args("service", "svc-a")}, // 격자 밖 도구
		{Name: "read_timeseries", Args: multi},                        // 복수 대상 + 범위 밖 혼합
		{Name: "list_events", Args: args("target", "밖-대상")},            // 범위 밖
	}
	g := BuildGrid([]string{"svc-a", "db-b", "host-c"}, calls)

	if len(g.Targets) != 3 {
		t.Fatalf("대상 3 기대, got %v", g.Targets)
	}
	if len(g.Filled["svc-a"]) != 3 {
		t.Errorf("svc-a 전 관점 채움 기대, got %v", g.Filled["svc-a"])
	}
	if len(g.Filled["db-b"]) != 1 || g.Filled["db-b"][0] != ViewMetrics {
		t.Errorf("db-b metrics만 기대, got %v", g.Filled["db-b"])
	}
	// 빈 칸: db-b 2 + host-c 3 = 5, 대상·관점 순.
	if len(g.Empty) != 5 {
		t.Fatalf("빈 칸 5 기대, got %d: %v", len(g.Empty), g.Empty)
	}
	if g.Empty[0].TargetID != "db-b" || g.Empty[0].Viewpoint != ViewEvents {
		t.Errorf("첫 빈 칸 db-b/events 기대, got %v", g.Empty[0])
	}
}
