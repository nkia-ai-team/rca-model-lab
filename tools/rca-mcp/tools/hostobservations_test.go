package tools

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

const hostWindow = `"from":"2026-09-01T00:00:00Z","to":"2026-09-01T01:00:00Z"`

func TestHostObservationFiltersAndPagination(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		q := string(body)
		for _, s := range []string{"toString(remote_port)", "toString(local_port)", "positionCaseInsensitive(x,{query:String})", "local_port={local_port:UInt16}", "remote_port={remote_port:UInt16}", "direction={direction:String}", "LIMIT {limit:UInt32} OFFSET {offset:UInt32}", "direction, process, state"} {
			if !strings.Contains(q, s) {
				t.Errorf("missing predicate/order %s: %s", s, q)
			}
		}
		for k, v := range map[string]string{"query": "1521' OR 1=1 --", "local_port": "0", "remote_port": "1521", "direction": "out", "limit": "2", "offset": "1"} {
			if r.URL.Query().Get("param_"+k) != v {
				t.Errorf("parameter %s: %s", k, r.URL.RawQuery)
			}
		}
		if strings.Contains(q, "1521' OR") {
			t.Error("unbound user query")
		}
		io.WriteString(w, "{\"observed_at\":\"2026-09-01 00:01:00\",\"remote_port\":1521}\n{\"observed_at\":\"2026-09-01 00:01:00\",\"remote_port\":443}\n")
	}))
	defer srv.Close()
	out, err := NewHostObservationsTool(chForTest(srv.URL)).Call(context.Background(), json.RawMessage(`{"kind":"connections","query":"1521' OR 1=1 --","local_port":0,"remote_port":1521,"direction":"out","limit":1,"offset":1,`+hostWindow+`}`))
	if err != nil {
		t.Fatal(err)
	}
	env := out.(Envelope)
	page := env.Findings[1]
	if page["matching_count_lower_bound"] != 3 || page["next_offset"] != 2 || page["has_more"] != true || len(env.Scopes) != 0 || !env.QueryTruncated {
		t.Fatalf("incorrect pagination %#v", env)
	}
	if _, ok := page["total"]; ok {
		t.Fatal("invented exact total")
	}
}

func TestHostObservationRefsAndSyslogSeverity(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		if !strings.Contains(string(b), "severity <= {severity_max:UInt8}") || r.URL.Query().Get("param_severity_max") != "0" {
			t.Error("severity filter lost")
		}
		io.WriteString(w, "{\"observed_at\":\"2026-09-01 00:01:00\",\"message\":\"one\"}\n{\"observed_at\":\"2026-09-01 00:01:00\",\"message\":\"two\"}\n")
	}))
	defer srv.Close()
	var prior string
	for _, offset := range []string{"0", "1"} {
		out, err := NewHostObservationsTool(chForTest(srv.URL)).Call(context.Background(), json.RawMessage(`{"kind":"syslog","severity_max":0,"offset":`+offset+`,`+hostWindow+`}`))
		if err != nil {
			t.Fatal(err)
		}
		env := out.(Envelope)
		if env.Refs[0] == env.Refs[1] {
			t.Fatal("different rows collide")
		}
		if prior != "" && prior != env.Refs[0] {
			t.Fatal("ref depends on page")
		}
		prior = env.Refs[0]
	}
}

func TestHostObservationRejectInvalidArguments(t *testing.T) {
	for _, extra := range []string{`"kind":"connections","app":""`, `"kind":"connections","severity_max":0`, `"kind":"syslog","target":""`, `"kind":"syslog","remote_port":0`, `"kind":"syslog","direction":"in"`, `"kind":"connections","queryy":"x"`, `"kind":"connections","local_port":-1`, `"kind":"connections","remote_port":65536`, `"kind":"connections","direction":"sideways"`, `"kind":"syslog","severity_max":8`, `"kind":"syslog","severity_max":null`, `"kind":"syslog","limit":0`, `"kind":"syslog","limit":201`, `"kind":"syslog","offset":-1`, `"kind":"syslog","offset":1000001`} {
		if _, err := NewHostObservationsTool(nil).Call(context.Background(), json.RawMessage(`{`+extra+`,`+hostWindow+`}`)); err == nil {
			t.Errorf("accepted %s", extra)
		}
	}
}

func TestHostObservationEmptyOvershoot(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	defer srv.Close()
	out, err := NewHostObservationsTool(chForTest(srv.URL)).Call(context.Background(), json.RawMessage(`{"kind":"connections","offset":500,`+hostWindow+`}`))
	if err != nil {
		t.Fatal(err)
	}
	env := out.(Envelope)
	page := env.Findings[0]
	if page["matching_count_lower_bound"] != 0 || page["has_more"] != false || len(env.Scopes) != 0 {
		t.Fatalf("overshoot claims matched rows: %#v", env)
	}
}

func TestHostObservationBackendCapIsIncomplete(t *testing.T) {
	t.Setenv("RCA_CH_CAP_ROWS", "1")
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.WriteString(w, "{\"observed_at\":\"2026-09-01 00:01:00\",\"remote_port\":1521}\n{\"observed_at\":\"2026-09-01 00:01:00\",\"remote_port\":443}\n")
	}))
	defer srv.Close()
	out, err := NewHostObservationsTool(chForTest(srv.URL)).Call(context.Background(), json.RawMessage(`{"kind":"connections",`+hostWindow+`}`))
	if err != nil {
		t.Fatal(err)
	}
	env := out.(Envelope)
	page := env.Findings[1]
	if !env.QueryTruncated || page["has_more"] != true || page["next_offset"] != 1 || page["matching_count_lower_bound"] != 1 {
		t.Fatalf("backend cap claimed complete: %#v", env)
	}
}
