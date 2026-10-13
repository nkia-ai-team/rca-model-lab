package tools

import (
	"context"
	"database/sql"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestETHostingReverseAndForwardClosure(t *testing.T) {
	inv := &etInventory{byID: map[string]etTargetMeta{
		"host1": {Name: "node1", Type: "server", ResourceKind: "node", ResourceKey: "node1"},
		"host2": {Name: "node2", Type: "server", ResourceKind: "node", ResourceKey: "node2"},
		"a":     {Name: "display a", Type: "application"}, "b": {Name: "display b", Type: "application"},
		"c": {Name: "display c", Type: "application"},
	}}
	ids := map[string]etSvcID{"svc-a": {TargetID: "a", Status: "resolved"}, "svc-b": {TargetID: "b", Status: "resolved"}, "svc-c": {TargetID: "c", Status: "resolved"}}
	rows := []map[string]any{{"svc": "svc-a", "pod": "a1"}, {"svc": "svc-a", "pod": "a2"}, {"svc": "svc-b", "pod": "b1"}, {"svc": "svc-c", "pod": "c1"}, {"svc": "missing", "pod": "absent"}}
	rels := etJoinHosting(inv, ids, rows, map[string]string{"a1": "node1", "a2": "node1", "b1": "node1", "c1": "node2"})
	for _, seedID := range []string{"host1", "a"} {
		res := newETResult()
		seed := res.addNode(etTargetNode(inv, seedID, 0))
		etHostingClosure(res, rels, []*etNode{seed}, 0)
		if len(res.nodes) != 3 || len(res.edges) != 2 {
			t.Fatalf("seed %s: nodes=%d edges=%d", seedID, len(res.nodes), len(res.edges))
		}
		if res.nodes["target:host2"] != nil || res.nodes["target:c"] != nil {
			t.Fatal("unrelated host leaked")
		}
		for _, e := range res.edges {
			if e.Target != "target:host1" || e.Source == "target:host1" || e.Hop != 0 || e.WindowBasis == "" {
				t.Fatalf("bad relation: %+v", e)
			}
		}
		for _, n := range res.nodes {
			if n.Hop != 0 {
				t.Fatal("hosting consumed hop")
			}
		}
	}
}

func TestETHostingAmbiguousAndMissingIdentity(t *testing.T) {
	inv := &etInventory{byID: map[string]etTargetMeta{
		"h1": {ResourceKind: "node", ResourceKey: "duplicate", Type: "server"},
		"h2": {ResourceKind: "node", ResourceKey: "duplicate", Type: "server"},
	}}
	rows := []map[string]any{{"svc": "ambiguous", "pod": "p"}, {"svc": "missing", "pod": "q"}}
	ids := map[string]etSvcID{"ambiguous": {Status: "ambiguous", Candidates: []string{"a", "b"}}}
	if got := etJoinHosting(inv, ids, rows, map[string]string{"p": "duplicate"}); len(got) != 0 {
		t.Fatal("ambiguous host must not resolve")
	}
	got := etJoinHosting(inv, ids, rows, map[string]string{"p": "unregistered"})
	if len(got) != 1 || got[0].App.ResolutionStatus != "ambiguous" || got[0].Host.ResolutionStatus != "unresolved" {
		t.Fatalf("missing identity discarded/invented: %+v", got)
	}
}

func TestETPodNodesRejectDuplicateNames(t *testing.T) {
	p := newDgFakePG(t)
	p.script("FROM kcm_resources", []string{"name", "node"}, [][]string{{"same", "node1"}, {"same", "node2"}, {"same", "node3"}, {"unique", "node1"}, {"empty", ""}})
	db, err := sql.Open("pgx", p.dsn())
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	got, err := etPodNodes(context.Background(), db, []string{"same", "unique", "empty"})
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 || got["unique"] != "node1" {
		t.Fatalf("ambiguous names selected: %v", got)
	}
}

func TestETHostingBoundsAndOutputLimit(t *testing.T) {
	host := &etNode{ID: "target:h", Type: "server"}
	var rels []etHostingRelation
	for i := 0; i < etMaxNodes+5; i++ {
		rels = append(rels, etHostingRelation{App: &etNode{ID: fmt.Sprintf("service:%03d", i), Type: "application"}, Host: host, Pod: fmt.Sprint(i)})
	}
	res := newETResult()
	res.addNode(host)
	etHostingClosure(res, rels, []*etNode{host}, 0)
	if len(res.nodes) > etMaxNodes || res.truncAt < 0 || len(res.boundary) == 0 {
		t.Fatal("bounds not exposed")
	}
	env := etEnvelope(res, host, time.Now().Add(-time.Hour), time.Now(), "test", nil, 2, etCoverage{}, false)
	if !env.Truncated {
		t.Fatal("truncation missing")
	}
	edges := 0
	for _, f := range env.Findings {
		if f["kind"] == "edge" {
			edges++
		}
	}
	if edges != 2 {
		t.Fatalf("limit: %d", edges)
	}
}

func TestETHostingDirectInventoryRequiresTypedHost(t *testing.T) {
	inv := &etInventory{byID: map[string]etTargetMeta{
		"h": {Type: "server"}, "cluster": {Type: "k8s"},
		"good": {Type: "application", HostTargetID: "h"},
		"bad":  {Type: "application", HostTargetID: "cluster"},
		"self": {Type: "application", HostTargetID: "self"},
		"pod":  {Type: "k8s", HostTargetID: "h"},
	}}
	rels := etJoinHosting(inv, nil, nil, nil)
	if len(rels) != 1 || rels[0].App.TargetID != "good" {
		t.Fatalf("invalid ownership inferred: %+v", rels)
	}
	res := newETResult()
	host := res.addNode(etTargetNode(inv, "h", 0))
	etHostingClosure(res, rels, []*etNode{host}, 0)
	for _, e := range res.edges {
		if e.WindowBasis != "current_inventory; not_historical" {
			t.Fatalf("historical certainty: %+v", e)
		}
	}
}

func TestETExpandFromServerLoadsHostedServices(t *testing.T) {
	const host = "aaaaaaaa-0000-4000-8000-000000000001"
	const app = "aaaaaaaa-0000-4000-8000-000000000002"
	p := newDgFakePG(t)
	p.script("FROM targets", []string{"id", "name", "type", "rk", "rkey", "rtype", "parent", "host"}, [][]string{
		{host, "node1", "kubernetes_resource", "node", "node1", "", "", ""},
		{app, "different display name", "application", "", "", "", "", ""},
	})
	p.script("FROM kcm_resources", []string{"name", "node"}, [][]string{{"pod1", "node1"}})
	db, err := sql.Open("pgx", p.dsn())
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	ch := dgCH(t, []dgCHScript{
		{"groupUniqArray(10)", []map[string]any{{"svc": "svc-a", "spans": 1, "with_id": 1, "ids": []string{app}}}},
		{"GROUP BY svc, pod", []map[string]any{{"svc": "svc-a", "pod": "pod1"}}},
	})
	out, err := etExpand(context.Background(), db, ch, host, 1, time.Now().Add(-time.Hour), time.Now(), "test", []string{etKindHosted}, 400)
	if err != nil {
		t.Fatal(err)
	}
	env := out.(Envelope)
	if env.Status != "normal" {
		t.Fatalf("host lookup failed: %+v", env)
	}
	found := false
	for _, f := range env.Findings {
		if e, ok := f["edge"].(*etEdge); ok && e.Source == "target:"+app && e.Target == "target:"+host && e.Hop == 0 {
			found = true
		}
	}
	if !found {
		t.Fatal("reverse relation missing at zero cost")
	}
}

func TestETHostingPreservesInventoryOnBackendFailure(t *testing.T) {
	inv := &etInventory{byID: map[string]etTargetMeta{
		"h": {Type: "server"}, "a": {Type: "application", HostTargetID: "h"},
	}}
	failure := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusServiceUnavailable) }))
	defer failure.Close()
	p := newDgFakePG(t)
	db, err := sql.Open("pgx", p.dsn())
	if err != nil {
		t.Fatal(err)
	}
	db.Close() // deterministic pod lookup failure
	for _, tc := range []struct {
		name string
		ch   *CH
	}{
		{"span failure", &CH{BaseURL: failure.URL}},
		{"pod inventory failure", dgCH(t, []dgCHScript{{"GROUP BY svc, pod", []map[string]any{{"svc": "s", "pod": "p"}}}})},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var acc truncAcc
			rels, err := etHostingRelations(context.Background(), db, tc.ch, inv, nil, time.Now().Add(-time.Hour), time.Now(), &acc)
			if err == nil {
				t.Fatal("backend failure hidden")
			}
			if len(rels) != 1 || rels[0].App.TargetID != "a" || rels[0].Host.TargetID != "h" || rels[0].Pod != "" {
				t.Fatalf("independent inventory relation lost: %+v", rels)
			}
			res := newETResult()
			host := res.addNode(etTargetNode(inv, "h", 0))
			etHostingClosure(res, rels, []*etNode{host}, 0)
			if len(res.edges) != 1 {
				t.Fatal("partial result cannot be traversed")
			}
		})
	}
}
