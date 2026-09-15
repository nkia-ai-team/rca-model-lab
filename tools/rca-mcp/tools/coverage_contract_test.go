package tools

import (
	"context"
	"database/sql"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestK8sObservationContract(t *testing.T) {
	for _, tc := range []struct {
		name, status            string
		running, zero, positive bool
	}{
		{"missing", "no_data", false, false, false},
		{"running_only", "no_data", true, false, false},
		{"measured_zero", "normal", true, true, false},
		{"positive_without_running", "anomalous", false, false, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				q := r.URL.Query().Get("query")
				if strings.Contains(q, " > 0") {
					t.Error("zero-filtering query")
				}
				result := "[]"
				if (strings.Contains(q, "container_state_running") && tc.running) || (strings.Contains(q, "container_oom_killed") && tc.positive) {
					result = `[{"metric":{"namespace":"a","pod":"p"},"value":[1,"1"]}]`
				} else if tc.zero && !strings.Contains(q, "container_state_running") {
					result = `[{"metric":{"namespace":"a","pod":"p"},"value":[1,"0"]}]`
				}
				fmt.Fprintf(w, `{"status":"success","data":{"resultType":"vector","result":%s}}`, result)
			}))
			defer srv.Close()
			from := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
			out, err := queryK8sState(context.Background(), &VM{BaseURL: srv.URL}, nil, "target", from, from.Add(time.Hour))
			if err != nil {
				t.Fatal(err)
			}
			env := out.(Envelope)
			if env.Status != tc.status {
				t.Fatalf("status %s", env.Status)
			}
			if strings.Contains(env.AssessmentBasis, "전부 0") {
				t.Fatal("blanket exclusion")
			}
			if !tc.zero && env.DegradedSources["vm"] != "partial_signal_coverage" {
				t.Fatal("missing machine-readable partial coverage")
			}
			if !tc.zero && !tc.positive && len(env.Scopes) != 0 {
				t.Fatal("absent signals emitted observed-zero scopes")
			}
			if tc.positive && len(env.Scopes) != 1 {
				t.Fatal("only observed OOM signal should have scope")
			}
			for _, f := range env.Findings {
				if f["signal"] == "oom_killed" && f["observation"] != nil {
					want := "absent"
					if tc.zero {
						want = "observed_zero"
					}
					if tc.positive {
						want = "positive"
					}
					if f["observation"] != want {
						t.Fatalf("observation %v want %s", f, want)
					}
				}
			}
		})
	}
}

func TestCollectorWindowRelation(t *testing.T) {
	from := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	to := from.Add(time.Hour)
	for _, tc := range []struct {
		at   sql.NullTime
		want string
	}{
		{sql.NullTime{}, "unknown"},
		{sql.NullTime{Time: from.Add(-time.Second), Valid: true}, "before_window"},
		{sql.NullTime{Time: from, Valid: true}, "within_window_snapshot_only"},
		{sql.NullTime{Time: to, Valid: true}, "after_window"},
	} {
		if got := collectorWindowRelation(tc.at, from, to); got != tc.want {
			t.Fatalf("got %s want %s", got, tc.want)
		}
	}
}

func TestK8sPodNamespaceAndExpansion(t *testing.T) {
	for _, kind := range []string{"pod", "deployment"} {
		t.Run(kind, func(t *testing.T) {
			p := newDgFakePG(t)
			p.script("FROM kcm_resource_targets", []string{"cluster_target_id", "resource_kind", "resource_key"}, [][]string{{dgUUID, kind, "payments/worker"}})
			db, err := sql.Open("pgx", p.dsn())
			if err != nil {
				t.Fatal(err)
			}
			defer db.Close()
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				q := r.URL.Query().Get("query")
				if kind == "pod" && (!strings.Contains(q, `namespace="payments"`) || !strings.Contains(q, `pod="worker"`)) {
					t.Errorf("lost namespace/pod: %s", q)
				}
				fmt.Fprint(w, `{"status":"success","data":{"resultType":"vector","result":[]}}`)
			}))
			defer srv.Close()
			from := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
			out, err := queryK8sState(context.Background(), &VM{BaseURL: srv.URL}, db, dgPeerUUID, from, from.Add(time.Hour))
			if err != nil {
				t.Fatal(err)
			}
			if kind == "deployment" {
				found := false
				for _, f := range out.(Envelope).Findings {
					if f["queried_cluster"] == dgUUID {
						found = true
					}
				}
				if !found {
					t.Fatal("cluster expansion undisclosed")
				}
			}
		})
	}
}
