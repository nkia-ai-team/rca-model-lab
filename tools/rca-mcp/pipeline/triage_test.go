package pipeline

import (
	"context"
	"reflect"
	"testing"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/seed"
)

var tr0 = time.Date(2026, 7, 13, 9, 0, 0, 0, time.UTC)

func trTs(min int) time.Time { return tr0.Add(time.Duration(min) * time.Minute) }

func fp(v float64) *float64 { return &v }

// 예시 사건: 주문 API 지연. app-1(멤버 3건)·db-1(멤버 2건)이 증상,
// topology는 app-1→db-1→stor-1 사슬 + app-1→net-1. cache-1은 topology에
// 없는 멤버 대상(잘림 신호). host-9는 db-1의 HostKin.
func sampleSeed() seed.IncidentSeed {
	return seed.IncidentSeed{
		IncidentID: "inc-1", Title: "주문 API 응답 지연",
		Severity: "critical", MainTargetID: "app-1", MainService: "order",
		MainMetric: "http_latency_p99", MainObserved: fp(12.0), MainBaseline: fp(0.2),
		MainDirection: "up", FirstEventAt: trTs(0), LastEventAt: trTs(10),
		Members: []seed.SeedMember{
			{TargetID: "app-1", Metric: "http_latency_p99", Class: "anomaly", Severity: "critical", OccurredAt: trTs(2)},
			{TargetID: "app-1", Metric: "http_latency_p99", Class: "anomaly", Severity: "warning", OccurredAt: trTs(1)},
			{TargetID: "app-1", Metric: "error_rate", Class: "context", Severity: "caution", OccurredAt: trTs(3)},
			{TargetID: "db-1", Metric: "cpu_usage", Class: "anomaly", Severity: "warning", OccurredAt: trTs(0)},
			{TargetID: "cache-1", Metric: "hit_ratio", Class: "context", Severity: "info", OccurredAt: trTs(5)},
		},
		Topology: seed.Graph{
			Nodes: []seed.GraphNode{
				{ID: "n1", TargetID: "app-1", Domain: "application"},
				{ID: "n2", TargetID: "db-1", Domain: "database"},
				{ID: "n3", TargetID: "stor-1", Domain: ""}, // 도메인 빈 노드 — 메타가 정본
				{ID: "n4", TargetID: "net-1", Domain: "network"},
			},
			Edges: []seed.Edge{
				{Source: "app-1", Target: "db-1", Kind: "apm_db"},
				{Source: "db-1", Target: "stor-1", Kind: "host_socket"},
				{Source: "app-1", Target: "net-1", Kind: "network_link"},
			},
		},
		HostKin: []seed.KinTarget{
			{TargetID: "host-9", Relation: "host"},
			{TargetID: "db-1", Relation: "sibling"}, // 이미 증상 대상 — 후보로 안 오름
		},
	}
}

func sampleMeta(ctx context.Context, ids []string) (map[string]TargetMeta, error) {
	all := map[string]TargetMeta{
		"app-1":  {Name: "주문API", Domain: "application", Service: "order"},
		"db-1":   {Name: "주문DB", Domain: "database"},
		"stor-1": {Name: "스토리지", Domain: "server"},
		"net-1":  {Name: "스위치", Domain: "network"},
		"host-9": {Name: "호스트9", Domain: "server"},
		// cache-1은 메타에 없음 — topology에도 없어 미해석으로 남는다.
	}
	out := map[string]TargetMeta{}
	for _, id := range ids {
		if m, ok := all[id]; ok {
			out[id] = m
		}
	}
	return out, nil
}

func TestTriage(t *testing.T) {
	r, err := Triage(context.Background(), sampleSeed(), sampleMeta)
	if err != nil {
		t.Fatalf("Triage 실패: %v", err)
	}

	// ① 대표 현상 = Main* 전사.
	if r.Symptom.TargetID != "app-1" || r.Symptom.Metric != "http_latency_p99" ||
		*r.Symptom.Observed != 12.0 || r.From != trTs(0) || r.To != trTs(10) {
		t.Errorf("대표 현상 전사 어긋남: %+v", r.Symptom)
	}

	// ② 멤버 접기: 5건 → 4묶음, app-1 지연 2건이 한 묶음.
	if len(r.MemberGroups) != 4 {
		t.Fatalf("MemberGroups = %d, want 4: %+v", len(r.MemberGroups), r.MemberGroups)
	}
	g0 := r.MemberGroups[0] // anomaly + critical + 이른 순 → app-1 지연 묶음
	if g0.TargetID != "app-1" || g0.Metric != "http_latency_p99" ||
		g0.Count != 2 || !g0.FirstAt.Equal(trTs(1)) || g0.MaxSeverity != "critical" || !g0.HasAnomaly {
		t.Errorf("첫 묶음 어긋남: %+v", g0)
	}
	if last := r.MemberGroups[3]; last.HasAnomaly { // context/info 묶음이 맨 뒤
		t.Errorf("정렬 어긋남 — 맨 뒤가 anomaly: %+v", last)
	}

	// ③ 도메인 해석: 증상 = 멤버 대상들(app/db + cache 미해석).
	if want := []string{"application", "database"}; !reflect.DeepEqual(r.SymptomDomains, want) {
		t.Errorf("SymptomDomains = %v, want %v", r.SymptomDomains, want)
	}
	// stor-1은 topology Domain이 비었지만 메타(정본)가 server로 해석.
	if r.TargetDomains["stor-1"] != "server" {
		t.Errorf("stor-1 도메인 = %q, want server (메타 정본)", r.TargetDomains["stor-1"])
	}
	if want := []string{"cache-1"}; !reflect.DeepEqual(r.UnresolvedDomains, want) {
		t.Errorf("UnresolvedDomains = %v, want %v", r.UnresolvedDomains, want)
	}

	// ④ 이웃 확장: net-1·stor-1(1홉 — db-1 증상이므로), host-9(HostKin).
	wantCand := []CandidateTarget{
		{TargetID: "host-9", Domain: "server", Via: ViaHostKin},
		{TargetID: "net-1", Domain: "network", Via: ViaTopology1Hop},
		{TargetID: "stor-1", Domain: "server", Via: ViaTopology1Hop},
	}
	if !reflect.DeepEqual(r.Candidates, wantCand) {
		t.Errorf("Candidates = %+v, want %+v", r.Candidates, wantCand)
	}
	if want := []string{"network", "server"}; !reflect.DeepEqual(r.CandidateDomains, want) {
		t.Errorf("CandidateDomains = %v, want %v", r.CandidateDomains, want)
	}

	// 잘림 신호: cache-1은 topology에 없다.
	if want := []string{"cache-1"}; !reflect.DeepEqual(r.MissingFromTopology, want) {
		t.Errorf("MissingFromTopology = %v, want %v", r.MissingFromTopology, want)
	}
}

// 2홉 발견과 가까운 경로 우선을 확인한다: app-1 → mid-1 → far-1 사슬에서
// far-1은 2홉. 같은 대상이 1홉으로도 닿으면 1홉이 남는다.
func TestTriageTwoHop(t *testing.T) {
	s := seed.IncidentSeed{
		MainTargetID: "app-1",
		Members: []seed.SeedMember{
			{TargetID: "app-1", Metric: "m", Class: "anomaly", Severity: "warning", OccurredAt: trTs(0)},
		},
		Topology: seed.Graph{
			Nodes: []seed.GraphNode{
				{ID: "a", TargetID: "app-1", Domain: "application"},
				{ID: "b", TargetID: "mid-1", Domain: "server"},
				{ID: "c", TargetID: "far-1", Domain: "database"},
			},
			Edges: []seed.Edge{
				{Source: "app-1", Target: "mid-1"},
				{Source: "mid-1", Target: "far-1"},
			},
		},
	}
	meta := func(ctx context.Context, ids []string) (map[string]TargetMeta, error) {
		out := map[string]TargetMeta{}
		for _, id := range ids {
			out[id] = TargetMeta{Domain: map[string]string{
				"app-1": "application", "mid-1": "server", "far-1": "database"}[id]}
		}
		return out, nil
	}
	r, err := Triage(context.Background(), s, meta)
	if err != nil {
		t.Fatal(err)
	}
	want := []CandidateTarget{
		{TargetID: "far-1", Domain: "database", Via: ViaTopology2Hop},
		{TargetID: "mid-1", Domain: "server", Via: ViaTopology1Hop},
	}
	if !reflect.DeepEqual(r.Candidates, want) {
		t.Errorf("Candidates = %+v, want %+v", r.Candidates, want)
	}
}

// topology가 통째로 비어도(극단값) Triage는 성립한다 — 후보는 HostKin
// 에서만 나오고, 멤버 대상 전원이 잘림 신호로 남는다.
func TestTriageEmptyTopology(t *testing.T) {
	s := seed.IncidentSeed{
		MainTargetID: "app-1",
		Members: []seed.SeedMember{
			{TargetID: "app-1", Metric: "m", Class: "anomaly", Severity: "critical", OccurredAt: trTs(0)},
		},
		HostKin: []seed.KinTarget{{TargetID: "host-9", Relation: "host"}},
	}
	meta := func(ctx context.Context, ids []string) (map[string]TargetMeta, error) {
		out := map[string]TargetMeta{}
		for _, id := range ids {
			if id == "app-1" {
				out[id] = TargetMeta{Domain: "application"}
			}
			if id == "host-9" {
				out[id] = TargetMeta{Domain: "server"}
			}
		}
		return out, nil
	}
	r, err := Triage(context.Background(), s, meta)
	if err != nil {
		t.Fatal(err)
	}
	if want := []CandidateTarget{{TargetID: "host-9", Domain: "server", Via: ViaHostKin}}; !reflect.DeepEqual(r.Candidates, want) {
		t.Errorf("Candidates = %+v, want %+v", r.Candidates, want)
	}
	if want := []string{"app-1"}; !reflect.DeepEqual(r.MissingFromTopology, want) {
		t.Errorf("MissingFromTopology = %v, want %v", r.MissingFromTopology, want)
	}
}

// meta 없이는 Triage가 성립하지 않는다 — 도메인 해석은 안전장치의 입력.
func TestTriageRequiresMeta(t *testing.T) {
	if _, err := Triage(context.Background(), sampleSeed(), nil); err == nil {
		t.Fatal("meta nil인데 Triage가 수락함")
	}
}
