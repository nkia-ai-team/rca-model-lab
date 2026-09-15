package seed

import (
	"encoding/json"
	"testing"
)

// incidents.topology projection 을 그대로 역직렬화할 수 있어야 한다 —
// JSON 태그가 정본(signals.go)과 어긋나면 여기서 잡힌다.
func TestTopologyJSONContract(t *testing.T) {
	raw := `{
		"nodes": [{"id": "n1", "targetId": "t-db-1", "label": "주문 DB", "kind": "database", "domain": "database"}],
		"edges": [{"source": "t-app-1", "target": "t-db-1", "kind": "apm_db", "direction": "out",
		           "err_pct": 3.2, "p99_ms": 1200.5, "calls": 4400}]
	}`
	var g Graph
	if err := json.Unmarshal([]byte(raw), &g); err != nil {
		t.Fatalf("topology projection 역직렬화 실패: %v", err)
	}
	if g.Nodes[0].TargetID != "t-db-1" || g.Nodes[0].Domain != "database" {
		t.Errorf("node 필드 매핑 어긋남: %+v", g.Nodes[0])
	}
	e := g.Edges[0]
	if e.Source != "t-app-1" || e.ErrPct != 3.2 || e.P99Ms != 1200.5 || e.Calls != 4400 {
		t.Errorf("edge 필드 매핑 어긋남: %+v", e)
	}

	// 재직렬화 키도 정본과 동일해야 한다(omitempty 포함).
	out, err := json.Marshal(Graph{Nodes: []GraphNode{{ID: "n1", TargetID: "t1"}}, Edges: []Edge{{Source: "a", Target: "b"}}})
	if err != nil {
		t.Fatal(err)
	}
	want := `{"nodes":[{"id":"n1","targetId":"t1","label":"","kind":"","domain":""}],"edges":[{"source":"a","target":"b"}]}`
	if string(out) != want {
		t.Errorf("직렬화 키 불일치:\n got %s\nwant %s", out, want)
	}
}
