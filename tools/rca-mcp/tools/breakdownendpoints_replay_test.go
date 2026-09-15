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

// Exercise the actual tool through HTTP, including widening and a missing
// baseline. Source data (not today's date) must determine the verdict.
func TestBreakdownHistoricalBaseline(t *testing.T) {
	for _, mode := range []string{"present", "empty", "widened", "widen_error"} {
		t.Run(mode, func(t *testing.T) {
			from := time.Date(2000, 1, 1, 10, 0, 0, 0, time.UTC)
			baseQueries := 0
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				body, _ := io.ReadAll(r.Body)
				if strings.Contains(string(body), "child_ns") {
					return // self-time optional detail
				}
				if strings.Contains(string(body), "AS by_name") {
					io.WriteString(w, `{"service_name":"svc","by_name":1}`+"\n")
					return
				}
				p50 := 500.0
				if r.URL.Query().Get("param_from") != chTime(from) {
					baseQueries++
					if mode == "widen_error" && baseQueries == 2 {
						http.Error(w, "bad query", http.StatusBadRequest)
						return
					}
					if mode == "empty" || ((mode == "widened" || mode == "widen_error") && baseQueries == 1) {
						return
					}
					p50 = 10
				}
				json.NewEncoder(w).Encode(map[string]any{"section": "entry", "kind": "SERVER", "label": "GET /x", "n": 100, "uniq_n": 100, "p50_ms": p50, "p95_ms": p50, "total_ms": p50 * 100})
			}))
			defer srv.Close()
			args := json.RawMessage(`{"target":"svc","from":"2000-01-01T10:00:00Z","to":"2000-01-01T10:30:00Z","sections":["entry"]}`)
			out, err := NewBreakdownEndpointsTool(chForTest(srv.URL), from, from.Add(30*time.Minute)).Call(context.Background(), args)
			if mode == "widen_error" {
				if err == nil {
					t.Fatal("baseline query failure was hidden")
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			env := out.(Envelope)
			if baseQueries == 0 {
				t.Fatal("historical baseline never queried")
			}
			if mode == "empty" {
				if !strings.Contains(env.AssessmentBasis, "관측이 없어") {
					t.Fatal(env.AssessmentBasis)
				}
				if env.Status != "normal" {
					t.Fatal("cannot infer a change without baseline")
				}
			} else if !strings.Contains(env.AssessmentBasis, "2000-01-01") {
				t.Fatal(env.AssessmentBasis)
			}
			if mode != "empty" && env.Status != "anomalous" {
				t.Fatalf("historical 50x latency change lost: %+v", env)
			}
			if mode == "widened" && (baseQueries != 2 || !strings.Contains(env.AssessmentBasis, "widened")) {
				t.Fatal(env.AssessmentBasis)
			}
			if strings.Contains(env.AssessmentBasis, "보존(3일) 밖") {
				t.Fatal("wall-clock TTL leaked into replay")
			}
		})
	}
}
