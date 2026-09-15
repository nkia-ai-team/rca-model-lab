// 파이프라인 [1] Triage — seed를 결정론으로 선별·구조화한다 (설계 §2,
// 컨텍스트 예산 해소 2026-07-13). LLM 없음: 요약(손실 압축)이 아니라
// 접기(멤버 그룹핑)와 꼬리표(도메인 해석)만 한다. 원본 seed는 버려지지
// 않고 seed 조회 도구 뒤에 남는다(단계적 로딩).
//
// 도메인 해석의 정본은 get_target_meta(targets 조회)이고 topology 노드의
// Domain은 보조다. topology 박제는 best-effort에 잘릴 수 있으므로,
// topology에서 못 찾은 멤버 대상과 도메인 미해석 대상을 사실로 기록한다
// — 미조사 인접 도메인 안전장치와 같은 결.
package pipeline

import (
	"context"
	"fmt"
	"sort"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/seed"
)

// TargetMeta는 targets 1행의 메타다 — 도구 get_target_meta의 거울.
type TargetMeta struct {
	Name    string
	Domain  string // targets.type enum: server|network|database|application|kubernetes|virtualization
	Service string
}

// TargetMetaFunc는 target_id 목록의 메타 일괄 조회다(get_target_meta).
// 결과에 없는 id는 오류가 아니라 "도메인 미해석"이라는 사실로 기록된다.
type TargetMetaFunc func(ctx context.Context, ids []string) (map[string]TargetMeta, error)

// Symptom은 대표 현상이다 — seed Main* 필드의 전사(계산 없음).
type Symptom struct {
	TargetID  string
	Service   string
	Metric    string
	Observed  *float64
	Baseline  *float64
	Direction string
	Severity  string
	Title     string
}

// MemberGroup은 같은 (대상, 지표) 멤버들을 접은 묶음이다. 개별 멤버
// 원본은 seed 조회 도구로 되찾는다.
type MemberGroup struct {
	TargetID    string
	Metric      string
	Count       int
	FirstAt     time.Time
	LastAt      time.Time
	MaxSeverity string
	HasAnomaly  bool // class=anomaly 멤버 포함 여부
}

// 후보 대상의 발견 경로.
const (
	ViaTopology1Hop = "topology_1hop"
	ViaTopology2Hop = "topology_2hop"
	ViaHostKin      = "host_kin"
)

// CandidateTarget은 이웃 확장으로 찾은 조사 후보다 — 증상 대상이 아니
// 면서 원인이 숨어 있을 수 있는 대상(조용한 상류 포함).
type CandidateTarget struct {
	TargetID string
	Domain   string // 미해석이면 ""
	Via      string
}

// TriageResult는 [1]의 산출물이다 — [3] 1차 조사와 [4] 가설 생성의
// 책상에 올라가는 전부. 전부 seed에서 결정론으로 계산된다.
type TriageResult struct {
	Symptom Symptom
	From    time.Time // 이벤트 시간창 (FirstEventAt~LastEventAt)
	To      time.Time

	MemberGroups []MemberGroup // anomaly·severity 우선 정렬

	SymptomDomains   []string // 멤버 대상의 도메인 (정렬·중복 제거)
	CandidateDomains []string // 후보 대상의 도메인 (정렬·중복 제거)
	Candidates       []CandidateTarget
	TargetDomains    map[string]string // 해석된 대상 → 도메인
	TargetNames      map[string]string // 해석된 대상 → 이름(조사 브리핑 재료 — §9 결정 4 일괄 스크리닝 흡수)

	MissingFromTopology []string // topology 노드에 없는 멤버 대상 — 잘림 신호
	UnresolvedDomains   []string // 메타·topology 어느 쪽으로도 도메인 못 얻은 대상

	// UnprobeableNeighbors — 이웃 확장에 걸렸지만 target_id가 없어 조사
	// 불가한 topology 노드(실측: 'db:mysql' 같은 미등록 datastore 노드).
	// 후보로 흘리면 모든 도구가 실패하므로 제외하되, 미조사 인접과 같은
	// 결로 기록은 남긴다 — 과확신을 막는 쪽으로 흐르는 안전장치(§3).
	UnprobeableNeighbors []string
}

// SymptomTargetsOf는 §10 증상 앵커의 후보 대상 목록이다 — 주 증상 먼저,
// 이하 멤버 그룹 정렬 순(anomaly·severity 우선), 중복 제거. "동률이면
// Triage 정렬 1위"(§10 피연산자 B)의 정렬이 이 순서다.
func SymptomTargetsOf(t TriageResult) []string {
	seen := map[string]bool{}
	var out []string
	add := func(id string) {
		if id != "" && !seen[id] {
			seen[id] = true
			out = append(out, id)
		}
	}
	add(t.Symptom.TargetID)
	for _, g := range t.MemberGroups {
		add(g.TargetID)
	}
	return out
}

// severityRank — 정렬용. 계약 enum: critical|warning|caution|info|cleared.
var severityRank = map[string]int{
	"critical": 4, "warning": 3, "caution": 2, "info": 1, "cleared": 0,
}

// Triage는 seed를 받아 결정론 선별·구조화한다. meta는 필수다 — 도메인
// 해석 없는 Triage는 후보 도메인 안전장치(§3)를 무력화한다.
func Triage(ctx context.Context, s seed.IncidentSeed, meta TargetMetaFunc) (TriageResult, error) {
	if meta == nil {
		return TriageResult{}, fmt.Errorf("triage: TargetMetaFunc 필수")
	}

	r := TriageResult{
		Symptom: Symptom{
			TargetID: s.MainTargetID, Service: s.MainService, Metric: s.MainMetric,
			Observed: s.MainObserved, Baseline: s.MainBaseline,
			Direction: s.MainDirection, Severity: s.Severity, Title: s.Title,
		},
		From: s.FirstEventAt, To: s.LastEventAt,
	}

	symptomTargets := r.foldMembers(s)
	nodeDomain, adj := indexTopology(s.Topology)
	r.findMissing(symptomTargets, nodeDomain)
	r.expandCandidates(s, symptomTargets, adj, nodeDomain)
	if err := r.resolveDomains(ctx, symptomTargets, nodeDomain, meta); err != nil {
		return TriageResult{}, err
	}
	return r, nil
}

// foldMembers — ② 멤버 접기. 같은 (대상, 지표)를 한 묶음으로. 증상
// 대상 집합(MainTargetID 포함)을 돌려준다.
func (r *TriageResult) foldMembers(s seed.IncidentSeed) map[string]bool {
	symptom := map[string]bool{}
	if s.MainTargetID != "" {
		symptom[s.MainTargetID] = true
	}

	type key struct{ target, metric string }
	groups := map[key]*MemberGroup{}
	var order []key
	for _, m := range s.Members {
		symptom[m.TargetID] = true
		k := key{m.TargetID, m.Metric}
		g, ok := groups[k]
		if !ok {
			g = &MemberGroup{TargetID: m.TargetID, Metric: m.Metric,
				FirstAt: m.OccurredAt, LastAt: m.OccurredAt, MaxSeverity: m.Severity}
			groups[k] = g
			order = append(order, k)
		}
		g.Count++
		if m.OccurredAt.Before(g.FirstAt) {
			g.FirstAt = m.OccurredAt
		}
		if m.OccurredAt.After(g.LastAt) {
			g.LastAt = m.OccurredAt
		}
		if severityRank[m.Severity] > severityRank[g.MaxSeverity] {
			g.MaxSeverity = m.Severity
		}
		if m.Class == "anomaly" {
			g.HasAnomaly = true
		}
	}
	for _, k := range order {
		r.MemberGroups = append(r.MemberGroups, *groups[k])
	}
	// anomaly 우선 → severity 높은 순 → 이른 순 → id 순 (결정론).
	sort.SliceStable(r.MemberGroups, func(i, j int) bool {
		a, b := r.MemberGroups[i], r.MemberGroups[j]
		if a.HasAnomaly != b.HasAnomaly {
			return a.HasAnomaly
		}
		if severityRank[a.MaxSeverity] != severityRank[b.MaxSeverity] {
			return severityRank[a.MaxSeverity] > severityRank[b.MaxSeverity]
		}
		if !a.FirstAt.Equal(b.FirstAt) {
			return a.FirstAt.Before(b.FirstAt)
		}
		if a.TargetID != b.TargetID {
			return a.TargetID < b.TargetID
		}
		return a.Metric < b.Metric
	})
	return symptom
}

// indexTopology — 노드 도메인 색인과 무방향 인접 목록. target_id 없는
// 노드(미등록 datastore 등)는 색인에서 뺀다 — 인접 목록에는 남아
// expandCandidates가 조사 불가 이웃으로 분류한다.
func indexTopology(g seed.Graph) (nodeDomain map[string]string, adj map[string][]string) {
	nodeDomain = map[string]string{}
	for _, n := range g.Nodes {
		if n.TargetID != "" {
			nodeDomain[n.TargetID] = n.Domain
		}
	}
	adj = map[string][]string{}
	for _, e := range g.Edges {
		adj[e.Source] = append(adj[e.Source], e.Target)
		adj[e.Target] = append(adj[e.Target], e.Source)
	}
	return nodeDomain, adj
}

// findMissing — topology에서 못 찾은 증상 대상. 박제 잘림(truncated)의
// 신호이며, 이웃 확장이 이 대상들에서는 시작하지 못한다는 사실 기록.
func (r *TriageResult) findMissing(symptom map[string]bool, nodeDomain map[string]string) {
	for t := range symptom {
		if _, ok := nodeDomain[t]; !ok {
			r.MissingFromTopology = append(r.MissingFromTopology, t)
		}
	}
	sort.Strings(r.MissingFromTopology)
}

// expandCandidates — ④ 이웃 확장. topology 1~2홉 + HostKin. 증상 대상
// 자신은 제외하고, 같은 대상이 여러 경로로 발견되면 가까운 쪽을 남긴다.
func (r *TriageResult) expandCandidates(s seed.IncidentSeed, symptom map[string]bool, adj map[string][]string, nodeDomain map[string]string) {
	via := map[string]string{}
	hop := map[string]int{}
	for t := range symptom {
		for _, n1 := range adj[t] {
			if !symptom[n1] && (hop[n1] == 0 || hop[n1] > 1) {
				hop[n1], via[n1] = 1, ViaTopology1Hop
			}
			for _, n2 := range adj[n1] {
				if !symptom[n2] && hop[n2] == 0 {
					hop[n2], via[n2] = 2, ViaTopology2Hop
				}
			}
		}
	}
	for _, k := range s.HostKin {
		if !symptom[k.TargetID] && via[k.TargetID] == "" {
			via[k.TargetID] = ViaHostKin
		}
	}
	ids := make([]string, 0, len(via))
	for t := range via {
		ids = append(ids, t)
	}
	sort.Strings(ids)
	for _, t := range ids {
		// topology 경유 이웃인데 target_id 색인에 없으면(미등록 노드)
		// 조사 불가 — 후보 대신 기록으로 남긴다. HostKin은 topology
		// 노드가 아니어도 실 target_id이므로 그대로 후보.
		if _, ok := nodeDomain[t]; !ok && via[t] != ViaHostKin {
			r.UnprobeableNeighbors = append(r.UnprobeableNeighbors, t)
			continue
		}
		r.Candidates = append(r.Candidates, CandidateTarget{TargetID: t, Via: via[t]})
	}
}

// resolveDomains — ③ 도메인 해석. 정본 = get_target_meta, 보조 =
// topology 노드 Domain. 둘 다 없으면 미해석으로 기록.
func (r *TriageResult) resolveDomains(ctx context.Context, symptom map[string]bool, nodeDomain map[string]string, meta TargetMetaFunc) error {
	ids := make([]string, 0, len(symptom)+len(r.Candidates))
	for t := range symptom {
		ids = append(ids, t)
	}
	for _, c := range r.Candidates {
		ids = append(ids, c.TargetID)
	}
	sort.Strings(ids)

	metas, err := meta(ctx, ids)
	if err != nil {
		return fmt.Errorf("triage: get_target_meta: %w", err)
	}
	r.TargetDomains = map[string]string{}
	r.TargetNames = map[string]string{}
	for _, t := range ids {
		if n := metas[t].Name; n != "" {
			r.TargetNames[t] = n
		}
		d := metas[t].Domain
		if d == "" {
			d = nodeDomain[t]
		}
		if d == "" {
			r.UnresolvedDomains = append(r.UnresolvedDomains, t)
			continue
		}
		r.TargetDomains[t] = d
	}
	sort.Strings(r.UnresolvedDomains)

	symDom, candDom := map[string]bool{}, map[string]bool{}
	for t := range symptom {
		if d := r.TargetDomains[t]; d != "" {
			symDom[d] = true
		}
	}
	for i, c := range r.Candidates {
		d := r.TargetDomains[c.TargetID]
		r.Candidates[i].Domain = d
		if d != "" {
			candDom[d] = true
		}
	}
	r.SymptomDomains = sortedKeys(symDom)
	r.CandidateDomains = sortedKeys(candDom)
	return nil
}

func sortedKeys(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
