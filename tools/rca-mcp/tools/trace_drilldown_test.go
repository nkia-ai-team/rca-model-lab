package tools

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestTraceActivityGapAndDisappearance(t *testing.T) {
	from := time.Date(2000, 1, 1, 0, 4, 0, 0, time.UTC)
	to := from.Add(4 * time.Minute)
	rows := []map[string]any{}
	add := func(name, phase string, bucket, count int) {
		rows = append(rows, map[string]any{"service_name": "svc", "span_kind": "CONSUMER", "span_name": name, "topic": "orders", "consumer_group": "shipping", "phase": phase, "bucket": float64(bucket), "unique_count": float64(count), "raw_count": float64(count), "exemplar_trace_id": "abc"})
	}
	for i := 0; i < 4; i++ {
		add("recover", "baseline", i, 10)
		add("stopped", "baseline", i, 10)
	}
	add("recover", "current", 0, 10)
	add("recover", "current", 3, 30)
	env := traceActivityEnvelope(rows, nil, traceActivityInput{}, from, to, from.Add(-4*time.Minute), 60, 20)
	if len(env.Findings) != 4 {
		t.Fatal(env)
	}
	for _, f := range env.Findings {
		if f["span_name"] == "recover" {
			got := f["current_counts"].([]int64)
			if got[1] != 0 || got[2] != 0 || got[3] != 30 {
				t.Fatal(got)
			}
			if f["current_total"] != f["baseline_total"] {
				t.Fatal("fixture requires equal totals but a gap")
			}
		}
		if f["span_name"] == "stopped" && f["activity_state"] != "not_seen_in_current" {
			t.Fatal(f)
		}
	}
	partial := traceActivityEnvelope(rows, &CHTrunc{}, traceActivityInput{}, from, to, from.Add(-4*time.Minute), 60, 20)
	if partial.Findings[0]["activity_state"] != "incomplete_query" {
		t.Fatal("truncated input appeared complete")
	}
}

func TestTracePageIdentityAndBounds(t *testing.T) {
	rows := []map[string]any{{"signature": "a", "window_start": "t"}, {"signature": "b", "window_start": "t"}, {"signature": "c", "window_start": "t"}}
	env := tracePage("summaries", rows, nil, 2, 0)
	if env.Refs[0] == env.Refs[1] {
		t.Fatal("distinct signatures share ref")
	}
	if len(env.Scopes) != 0 {
		t.Fatal("uncomputed exact totals exposed")
	}
	page := env.Findings[2]
	if page["next_offset"] != 2 || page["matching_count_lower_bound"] != 3 {
		t.Fatal(page)
	}
	empty := tracePage("summaries", nil, nil, 2, 500)
	if empty.Findings[0]["matching_count_lower_bound"] != 0 {
		t.Fatal("empty overshoot fabricated count")
	}
}

func TestRawTraceAndSummaryQueries(t *testing.T) {
	var queries []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		queries = append(queries, string(b))
		io.WriteString(w, `{"trace_id":"abc","span_id":"s1","duration_ns":"18446744073709551615","span_attributes":{"messaging.destination.name":"orders"}}`+"\n")
	}))
	defer srv.Close()
	out, err := NewTraceSpansTool(chForTest(srv.URL)).Call(context.Background(), json.RawMessage(`{"trace_id":"abc","from":"2000-01-01T00:00:00Z","to":"2000-01-01T01:00:00Z"}`))
	if err != nil {
		t.Fatal(err)
	}
	if out.(Envelope).Findings[0]["duration_ns"] != "18446744073709551615" {
		t.Fatal(out)
	}
	if !strings.Contains(queries[0], "resource_attributes") || !strings.Contains(queries[0], "events_attributes") {
		t.Fatal(queries[0])
	}
	_, err = NewTraceSummaryTool(chForTest(srv.URL)).Call(context.Background(), json.RawMessage(`{"kind":"path","target":"svc","from":"2000-01-01T00:00:00Z","to":"2000-01-01T01:00:00Z","signature":"x' OR 1=1"}`))
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(queries[1], "chain_depth") || strings.Contains(queries[1], "OR 1=1") {
		t.Fatal(queries[1])
	}
	_, err = NewTraceSpansTool(nil).Call(context.Background(), json.RawMessage(`{"trace_id":"abc","from":"2000-01-01T00:00:00Z","to":"2000-01-01T01:00:00Z","ignored":1}`))
	if err == nil {
		t.Fatal("unknown parameter accepted")
	}
}
