package tools

import "testing"

func TestETDBGroupsStayCallerOnlyAfterTargetResolution(t *testing.T) {
	r := newETResult()
	inv := &etInventory{
		byID:  map[string]etTargetMeta{"db-host": {Name: "shared DB host", Type: "database"}},
		dbRes: map[string]string{"postgresql|orders": "db-host", "postgresql|inventory": "db-host"},
	}
	rows := []etDBRow{
		{Svc: "app", System: "postgresql", DBName: "orders", Addr: "db", RawRows: 12, Spans: 10, Errs: 2, P95: 900},
		{Svc: "app", System: "postgresql", DBName: "inventory", Addr: "db", RawRows: 20, Spans: 20, Errs: 0, P95: 30},
	}
	for _, hop := range []int{1, 2} {
		for _, row := range rows {
			etAddDB(r, inv, nil, row, hop)
		}
	}
	if len(r.edges) != 2 {
		t.Fatalf("resolved DB resources collapsed: %d edges", len(r.edges))
	}
	for _, e := range r.edges {
		if e.Observation != "caller_only" || e.CalleeObserved != nil || e.MissingReason == "" || e.Hop != 1 {
			t.Fatalf("DB observation semantics changed: %+v", e)
		}
		for _, row := range rows {
			if e.AggregationDimensions["db_name"] == row.DBName &&
				(e.RawRows != row.RawRows || e.UniqueSpans != row.Spans || e.CallerObserved.Errors != row.Errs || e.CallerObserved.Latency["p95"] != row.P95) {
				t.Fatalf("DB stats mixed: %+v", e)
			}
		}
	}
}

func TestETPairedServicesRemainDistinctAfterResolution(t *testing.T) {
	r := newETResult()
	inv := &etInventory{byID: map[string]etTargetMeta{"shared": {Name: "shared"}}}
	svcs := map[string]etSvcID{"a": {TargetID: "shared"}, "b": {TargetID: "shared"}}
	for _, svc := range []string{"a", "b"} {
		etAddPaired(r, inv, svcs, etPair{CallerSvc: svc, CalleeSvc: "consumer", PK: "CLIENT", CK: "SERVER", Pairs: 1}, 1)
	}
	if len(r.edges) != 2 {
		t.Fatalf("distinct source service groups collapsed: %d", len(r.edges))
	}
}

func TestETRepeatedPairKeepsOneAggregate(t *testing.T) {
	r := newETResult()
	inv := &etInventory{byID: map[string]etTargetMeta{}}
	p := etPair{CallerSvc: "publisher", CalleeSvc: "consumer", PK: "PRODUCER", CK: "CONSUMER",
		Topic: "orders", ConsumerGroup: "billing", RawRows: 12, Pairs: 10, CallerErr: 2, CalleeErr: 1,
		CallerP95: 30, CalleeP95: 50}
	for _, hop := range []int{2, 1, 3} {
		etAddPaired(r, inv, nil, p, hop)
	}
	if len(r.edges) != 1 {
		t.Fatalf("repeat observation produced %d edges", len(r.edges))
	}
	for _, e := range r.edges {
		if e.RawRows != 12 || e.PairedSpanPairs != 10 || e.Hop != 1 || e.CallerObserved.Errors != 2 || e.CalleeObserved.Latency["p95"] != 50 {
			t.Fatalf("repeat BFS observation corrupted aggregate: %+v", e)
		}
	}
}

func TestETPairedDimensionsPreserveIndependentStatistics(t *testing.T) {
	r := newETResult()
	inv := &etInventory{byID: map[string]etTargetMeta{}}
	pairs := []etPair{
		{CallerSvc: "a", CalleeSvc: "b", PK: "PRODUCER", CK: "CONSUMER", Topic: "orders", ConsumerGroup: "billing", Pairs: 10, CalleeErr: 3, CalleeP95: 700},
		{CallerSvc: "a", CalleeSvc: "b", PK: "PRODUCER", CK: "CONSUMER", Topic: "orders", ConsumerGroup: "shipping", Pairs: 20, CalleeErr: 0, CalleeP95: 40},
		{CallerSvc: "a", CalleeSvc: "b", PK: "PRODUCER", CK: "CONSUMER", Topic: "returns", ConsumerGroup: "billing", Pairs: 30, CalleeErr: 1, CalleeP95: 90},
		{CallerSvc: "a", CalleeSvc: "b", PK: "INTERNAL", CK: "CONSUMER", Pairs: 40},
		{CallerSvc: "a", CalleeSvc: "b", PK: "CLIENT", CK: "CONSUMER", Pairs: 50},
	}
	for _, p := range pairs {
		etAddPaired(r, inv, nil, p, 1)
	}
	if len(r.edges) != len(pairs) {
		t.Fatalf("lost aggregation dimensions: got %d edges, want %d", len(r.edges), len(pairs))
	}
	for _, p := range pairs {
		found := false
		for _, e := range r.edges {
			if e.Topic == p.Topic && e.ConsumerGroup == p.ConsumerGroup && e.SpanKinds == p.PK+"→"+p.CK {
				found = true
				if e.PairedSpanPairs != p.Pairs || e.CalleeObserved.Errors != p.CalleeErr || e.CalleeObserved.Latency["p95"] != p.CalleeP95 {
					t.Fatalf("mixed statistics for %s/%s: %+v", p.Topic, p.ConsumerGroup, e)
				}
			}
		}
		if !found {
			t.Fatalf("missing pair %+v", p)
		}
	}
}
