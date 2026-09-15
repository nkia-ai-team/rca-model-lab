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

const processArgs = `"target":"11111111-1111-1111-1111-111111111111","from":"2026-09-01T00:00:00Z","to":"2026-09-01T01:00:00Z"`

func TestProcessSnapshotPreservesIdentityAndAsOfMetadata(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		sql := string(b)
		for _, want := range []string{"toString(s.proc_key)", "toString(s.identity.5)", "m.seen_at <= s.observed_at", "GROUP BY m.target_id, m.proc_key", "ORDER BY cpu_pct DESC, s.proc_key ASC", "LIMIT 3 OFFSET 0", "max(cpu_pct) AS cpu_peak"} {
			if !strings.Contains(sql, want) {
				t.Errorf("SQL missing %s", want)
			}
		}
		io.WriteString(w, `{"proc_key":"18446744073709551615","pid":123,"ppid":1,"create_time_unix":"1788220800","cpu_pct":99,"mem_rss":1000,"threads":5,"observed_at":"2026-09-01 00:30:00","metadata_rows":"2","user":"operator","cmdline":"worker --queue"}`+"\n"+`{"proc_key":"18446744073709551614","pid":124,"create_time_unix":"0","metadata_rows":"0"}`+"\n")
	}))
	defer srv.Close()
	out, err := NewProcessSnapshotTool(chForTest(srv.URL)).Call(context.Background(), json.RawMessage(`{`+processArgs+`,"top_n":2}`))
	if err != nil {
		t.Fatal(err)
	}
	env := out.(Envelope)
	a, b := env.Findings[0], env.Findings[1]
	if a["proc_key"] != "18446744073709551615" || a["create_time"] != "2026-09-01T00:00:00Z" {
		t.Fatalf("lost identity/time: %+v", a)
	}
	if env.Refs[0] == env.Refs[1] {
		t.Fatal("ref collision")
	}
	if a["metadata_available"] != true || b["metadata_available"] != false {
		t.Fatalf("metadata availability: %+v", env.Findings)
	}
	if _, exists := b["user"]; exists {
		t.Fatal("fabricated missing metadata")
	}
}

func TestProcessSnapshotPaginationAndFilters(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		sql := string(b)
		for _, want := range []string{"proc_key={proc_key:UInt64}", "pid={pid:UInt32}", "LIMIT 2 OFFSET 2"} {
			if !strings.Contains(sql, want) {
				t.Errorf("missing %s", want)
			}
		}
		if r.URL.Query().Get("param_proc_key") != "18446744073709551615" {
			t.Error("key parameter lost")
		}
		io.WriteString(w, `{"proc_key":"1"}`+"\n"+`{"proc_key":"2"}`+"\n")
	}))
	defer srv.Close()
	out, err := NewProcessSnapshotTool(chForTest(srv.URL)).Call(context.Background(), json.RawMessage(`{`+processArgs+`,"top_n":1,"offset":2,"pid":123,"proc_key":"18446744073709551615"}`))
	if err != nil {
		t.Fatal(err)
	}
	env := out.(Envelope)
	p := env.Findings[len(env.Findings)-1]
	if !env.Truncated || len(env.Scopes) != 0 || p["total_exact"] != false || p["next_offset"] != 3 || p["total_lower_bound"] != 4 {
		t.Fatalf("untruthful page: %+v", env)
	}
}

func TestProcessSnapshotInvalidOptions(t *testing.T) {
	for _, option := range []string{`"offset":-1`, `"offset":1000001`, `"offset":"2"`, `"pid":0`, `"pid":4294967296`, `"proc_key":"18446744073709551616"`, `"top_n":-1`, `"sort_by":"unknown"`} {
		t.Run(option, func(t *testing.T) {
			_, err := NewProcessSnapshotTool(nil).Call(context.Background(), json.RawMessage(`{`+processArgs+`,`+option+`}`))
			if err == nil {
				t.Fatal("accepted invalid option")
			}
		})
	}
}

func TestProcessSnapshotEmptyOffsetDoesNotInventTotal(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	defer srv.Close()
	out, err := NewProcessSnapshotTool(chForTest(srv.URL)).Call(context.Background(), json.RawMessage(`{`+processArgs+`,"offset":100}`))
	if err != nil {
		t.Fatal(err)
	}
	env := out.(Envelope)
	p := env.Findings[0]
	if len(env.Scopes) != 0 || p["total_exact"] != false || p["total_lower_bound"] != 0 {
		t.Fatalf("invented total %+v", env)
	}
}

func TestProcessSnapshotRejectsUnknownFilter(t *testing.T) {
	_, err := NewProcessSnapshotTool(nil).Call(context.Background(), json.RawMessage(`{`+processArgs+`,"query":"worker"}`))
	if err == nil {
		t.Fatal("silently ignored filter")
	}
}
