// expand_topology — "이 대상, 누구와 연결돼 있나"를 원천에서 즉석 조립한다
// (재설계 §12). 박제(incidents.topology)를 읽던 get_topology의 자리를
// 대신하며, 조사 도중 새로 쥔 대상 주변으로의 확장이 존재 이유다
// (박제는 격상 시점·anchor 1-hop 고정이라 그 자리가 비어 있다).
//
// 설계에서 실측이 뒤집은 것들(§12.1):
//   - 앱↔앱 간선은 span resource_attributes의 lucida.target_id로 양끝
//     UUID가 나온다. 단 계약이 아니라 배포 규약이라 미주입 서비스는
//     service_name 폴백 + 표식으로 떨어뜨린다(가짜 UUID 금지).
//   - 상대가 span을 안 내보내면 INNER JOIN이 간선을 통째로 지운다.
//     계측 없는 외부 의존·타임아웃이 정확히 그 경우라, 자식 없는 CLIENT
//     span에서 목적지를 만든다(apm_client_peer). 단 db.system 보유 span은
//     정의상 전부 자식이 없으므로 apm_db가 먼저다(§12.3 우선순위).
//   - 앱↔물리 다리는 hosted_on을 hop_cost=0 간선으로 놓아 잇는다. 원천
//     경로는 구현 중 재실측으로 다시 바뀌었다(아래 etHostedOn 주석).
package tools

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/llm"
)

// 잘림 상한(§12.8). 실측 도출이 아니라 현 규모(서비스 21·간선쌍 24·
// 최대 차수 7)의 약 10배 여유폭이다 — 이 환경에서는 발동하지 않으므로
// 잘림 표기 3필드는 검증 경로가 없다(스펙에 명시).
const (
	etMaxNodes       = 200
	etMaxEdges       = 400
	etMaxFanout      = 20 // 노드×간선종류×홉당
	etDefaultHops    = 2
	etMaxHops        = 3
	etParentLookback = 5 * time.Minute // §12.6 — parent가 창 밖이라 간선이 사라지는 것 방지
	etPeerPathSample = 3               // peer 간선에 보존할 경로 표본 수
)

// 간선 종류(§12.8 enum).
const (
	etKindSync    = "sync_call"
	etKindAsync   = "async_candidate"
	etKindUnknown = "unknown_parent_child"
	etKindDB      = "apm_db"
	etKindPeer    = "apm_client_peer"
	etKindHosted  = "hosted_on"
	etKindFDB     = "network_fdb"
	etKindLLDP    = "network_lldp"
)

// etNode는 지도의 노드 하나다. provenance는 한 값이 아니라 4축이다(§12.2).
type etNode struct {
	ID       string `json:"node_id"` // target:<uuid> | peer:<scheme>://<authority>:<port> | service:<name>
	TargetID string `json:"target_id,omitempty"`
	Name     string `json:"name"`
	Type     string `json:"type,omitempty"`
	Hop      int    `json:"hop"`

	ResolutionStatus string   `json:"resolution_status"`
	MatchBasis       string   `json:"match_basis"`
	Confidence       string   `json:"confidence"`
	Scope            string   `json:"scope"`
	CandidateCount   int      `json:"candidate_count,omitempty"`
	Candidates       []string `json:"candidates,omitempty"`

	// SelfObserved는 이 노드 자기 span의 kind별 에러 수다(§12.6-2).
	// 간선에 실으면 "피호출자가 자기 기록엔 정상"인 경우 결정적 사실이
	// 사라진다 — INTERNAL 에러는 node-local 증거이지 간선 속성이 아니다.
	// 인과 귀속은 하지 않는다.
	SelfObserved map[string]int64 `json:"self_observed,omitempty"`
}

// etObs는 한쪽 관측이다 — 호출자와 피호출자를 병치한다(§12.6).
type etObs struct {
	Errors  int64            `json:"errors"`
	Latency map[string]int64 `json:"latency_ms,omitempty"` // p50/p95/max
	Basis   string           `json:"latency_basis,omitempty"`
}

// etEdge는 간선 하나다. hop_cost=0인 hosted_on은 홉 거리를 올리지 않는다.
type etEdge struct {
	Source string `json:"source"`
	Target string `json:"target"`
	Kind   string `json:"kind"`
	Hop    int    `json:"hop"`

	Observation string `json:"observation"` // both | caller_only | topology_record
	SpanKinds   string `json:"span_kinds,omitempty"`

	RawRows         int64 `json:"raw_rows,omitempty"`
	UniqueSpans     int64 `json:"unique_spans,omitempty"`
	PairedSpanPairs int64 `json:"paired_span_pairs,omitempty"`
	// RetriesCollapsed는 항상 false다 — retry를 묶을 근거가 원천에 없다
	// (OTel에 retry 표식 필드 없음, span link 실측 0건). 호출 수는 논리
	// 요청 수가 아니라는 사실을 숨기지 않는다(§12.6-4).
	RetriesCollapsed bool `json:"retries_collapsed"`

	CallerObserved *etObs `json:"caller_observed,omitempty"`
	CalleeObserved *etObs `json:"callee_observed,omitempty"`
	// ObservationMismatch — 호출자는 실패로 보는데 피호출자 기록은 정상.
	// 판정이 아니라 사실 병치다(실측: 83% 실패 간선의 callee_err가 0).
	ObservationMismatch bool `json:"observation_mismatch,omitempty"`

	WindowBasis   string   `json:"window_basis,omitempty"`
	Topic         string   `json:"topic,omitempty"`
	ConsumerGroup string   `json:"consumer_group,omitempty"`
	MissingReason string   `json:"missing_reason,omitempty"`
	Paths         []string `json:"path_samples,omitempty"`
	Note          string   `json:"note,omitempty"`
	// Preserve source grouping even when distinct services or DB resources
	// resolve to the same inventory target. Percentiles cannot be added.
	AggregationDimensions map[string]string `json:"aggregation_dimensions,omitempty"`
}

// etResult는 조립 결과다.
type etResult struct {
	nodes    map[string]*etNode
	edges    map[string]*etEdge
	order    []string // 노드 삽입 순서(결정론 정렬 보조)
	sources  map[string]string
	notes    []string
	producer []map[string]any // 끝점 없는 PRODUCER 고아(토픽 건수만)
	truncAt  int
	boundary []string
}

func newETResult() *etResult {
	return &etResult{
		nodes:   map[string]*etNode{},
		edges:   map[string]*etEdge{},
		sources: map[string]string{},
		truncAt: -1,
	}
}

func (r *etResult) addNode(n *etNode) *etNode {
	if ex, ok := r.nodes[n.ID]; ok {
		if n.Hop < ex.Hop {
			ex.Hop = n.Hop
		}
		return ex
	}
	r.nodes[n.ID] = n
	r.order = append(r.order, n.ID)
	return n
}

func etEdgeKey(e *etEdge) string {
	// Keep every source aggregation dimension. JSON encoding both escapes
	// delimiters in values and sorts map keys for deterministic identity.
	b, _ := json.Marshal([]any{e.Source, e.Target, e.Kind, e.Observation,
		e.SpanKinds, e.Topic, e.ConsumerGroup, e.AggregationDimensions})
	return string(b)
}

func (r *etResult) addEdge(e *etEdge) {
	k := etEdgeKey(e)
	if ex, ok := r.edges[k]; ok {
		// BFS can retrieve the same full-window aggregate at both endpoints.
		// Keep the first observation intact rather than counting it again or
		// mixing its errors/percentiles with counts from a subsequent query.
		if e.Hop < ex.Hop {
			ex.Hop = e.Hop
		}
		return
	}
	r.edges[k] = e
}

// ---------------------------------------------------------------- 도구 표면

// NewExpandTopologyTool은 expand_topology 도구를 만든다. firstEvent·
// lastEvent는 기본 창의 재료다(§12.8) — 박제를 안 읽으므로 incidentID
// 바인딩은 없다.
func NewExpandTopologyTool(pg *sql.DB, ch *CH, firstEvent, lastEvent time.Time, nowFn func() time.Time) llm.Tool {
	if nowFn == nil {
		nowFn = time.Now
	}
	params, _ := json.Marshal(map[string]any{
		"type": "object",
		"properties": map[string]any{
			"target": map[string]string{"type": "string",
				"description": "확장 기준 대상 target_id(UUID). 필수"},
			"hops": map[string]any{"type": "integer",
				"description": fmt.Sprintf("확장 깊이(기본 %d, 최대 %d). hosted_on은 홉을 소모하지 않는다", etDefaultHops, etMaxHops)},
			"from": map[string]string{"type": "string",
				"description": "선택. UTC RFC3339 또는 now-1h. 기본 = 인시던트 발단"},
			"to": map[string]string{"type": "string",
				"description": "선택. 기본 = 인시던트 창 끝(없으면 now)"},
			"edge_kinds": map[string]any{"type": "array", "items": map[string]string{"type": "string"},
				"description": "선택. 출력 필터일 뿐 탐색은 항상 전 종류로 한다(막으면 그 종류를 거쳐야 닿는 노드가 사라진다). " +
					"sync_call·async_candidate·unknown_parent_child·apm_db·apm_client_peer·hosted_on·network_fdb·network_lldp"},
			"limit": map[string]any{"type": "integer",
				"description": fmt.Sprintf("선택. 간선 수 상한(기본 %d) — 노드는 간선에서 파생된다", etMaxEdges)},
		},
		"required": []string{"target"},
	})
	return llm.Tool{
		Name: "expand_topology",
		Description: "대상 주변의 연결을 원천에서 즉석 조립한다 — 앱 호출(트레이스)·앱→DB·계측 밖 목적지·" +
			"올라탄 호스트·네트워크 장비. 간선마다 호출자/피호출자 양쪽 관측을 병치하며(어긋남 자체가 단서), " +
			"끝점의 대상 UUID를 못 풀어도 간선은 살린다(버리면 의존이 없는 것처럼 보인다). " +
			"트레이스 0건은 의존 없음이 아니라 계측 없음일 수 있다.",
		Parameters: params,
		Call: func(ctx context.Context, args json.RawMessage) (any, error) {
			var in struct {
				Target    string   `json:"target"`
				Hops      int      `json:"hops"`
				From      string   `json:"from"`
				To        string   `json:"to"`
				EdgeKinds []string `json:"edge_kinds"`
				Limit     int      `json:"limit"`
			}
			if err := json.Unmarshal(args, &in); err != nil || in.Target == "" {
				return nil, fmt.Errorf(`인자 오류: {"target": "<uuid>"} 필수. hops(기본 %d)·from·to·edge_kinds·limit은 선택`, etDefaultHops)
			}
			if !uuidRe.MatchString(in.Target) {
				return nil, fmt.Errorf("%q는 target_id가 아님 — 순수 UUID여야 한다(접두 없음). "+
					"db:postgresql 같은 문자열은 구 topology의 가짜 노드 ID이고 이 도구는 그런 ID를 만들지 않는다", in.Target)
			}
			hops := in.Hops
			if hops <= 0 {
				hops = etDefaultHops
			}
			if hops > etMaxHops {
				hops = etMaxHops
			}
			limit := in.Limit
			if limit <= 0 || limit > etMaxEdges {
				limit = etMaxEdges
			}
			now := nowFn().UTC()
			from, to, wbasis, err := etWindow(in.From, in.To, firstEvent, lastEvent, now)
			if err != nil {
				return nil, err
			}
			return etExpand(ctx, pg, ch, in.Target, hops, from, to, wbasis, in.EdgeKinds, limit)
		},
	}
}

// etWindow는 기본 창을 정한다(§12.8): [firstEvent, lastEvent]. 한쪽만
// 있으면 있는 쪽 ±30분, 둘 다 없으면 now-1h. 어느 경우인지 밝힌다.
func etWindow(inFrom, inTo string, firstEvent, lastEvent, now time.Time) (time.Time, time.Time, string, error) {
	var from, to time.Time
	basis := []string{}

	if inFrom != "" {
		t, err := parseFlexTime(inFrom, now)
		if err != nil {
			return from, to, "", fmt.Errorf("from 파싱: %w", err)
		}
		from, basis = t.UTC(), append(basis, "from=인자")
	}
	if inTo != "" {
		t, err := parseFlexTime(inTo, now)
		if err != nil {
			return from, to, "", fmt.Errorf("to 파싱: %w", err)
		}
		to, basis = t.UTC(), append(basis, "to=인자")
	}

	switch {
	case from.IsZero() && to.IsZero():
		switch {
		case !firstEvent.IsZero() && !lastEvent.IsZero():
			from, to = firstEvent.UTC(), lastEvent.UTC()
			basis = append(basis, "인시던트 창 [발단, 끝]")
		case !firstEvent.IsZero():
			from, to = firstEvent.UTC().Add(-30*time.Minute), firstEvent.UTC().Add(30*time.Minute)
			basis = append(basis, "발단 ±30분(창 끝 미상)")
		case !lastEvent.IsZero():
			from, to = lastEvent.UTC().Add(-30*time.Minute), lastEvent.UTC().Add(30*time.Minute)
			basis = append(basis, "창 끝 ±30분(발단 미상)")
		default:
			from, to = now.Add(-time.Hour), now
			basis = append(basis, "now-1h(인시던트 창 미바인딩 — 라이브 탐색)")
		}
	case from.IsZero():
		from = to.Add(-time.Hour)
		basis = append(basis, "from=to-1h")
	case to.IsZero():
		to = from.Add(time.Hour)
		basis = append(basis, "to=from+1h")
	}
	if !from.Before(to) {
		return from, to, "", fmt.Errorf("시간창 오류: from(%s) < to(%s) 필요", from.Format(time.RFC3339), to.Format(time.RFC3339))
	}
	return from, to, strings.Join(basis, ", "), nil
}

// ---------------------------------------------------------------- 조립

func etExpand(ctx context.Context, pg *sql.DB, ch *CH, seed string, hops int, from, to time.Time,
	wbasis string, kindFilter []string, limit int) (any, error) {

	res := newETResult()
	var acc truncAcc
	inv, err := etLoadTargets(ctx, pg)
	if err != nil {
		return nil, err
	}

	seedNode := etTargetNode(inv, seed, 0)
	if seedNode == nil {
		return nil, fmt.Errorf("target %s를 대상 명부에서 찾지 못함 — 삭제됐거나 미등록이다. "+
			"describe_target으로 정체를 먼저 확인하라", seed)
	}
	res.addNode(seedNode)

	// 서비스 이름 ↔ 대상 UUID 매핑(창 안 span 기준). 미주입 서비스는
	// service_name 폴백 노드가 되고 표식이 붙는다(§12.2).
	//
	// ch=required(deps.go, 2b): 진입점의 CH 실패는 삼키지 않고 전파한다 —
	// 종전엔 sources에 "error:"로 접혀 도구가 성공을 가장했고(호출 관측
	// 전무가 '연결 없음'처럼 보인다), 원문 오류 문자열까지 새었다(§15.3-2).
	// withBackendGuard가 no_data(backend_error) 봉투로 사상한다. 홉 중간의
	// CH 실패(아래 etApmHop)는 이미 얻은 관측이 있으므로 기존 부분 강등을
	// 유지하되 토큰만 싣는다(판단 지점).
	svcOf, cov, err := etServiceIdentity(ctx, ch, from, to, &acc)
	if err != nil {
		return nil, err
	}
	res.sources["apm"] = "checked"

	frontier := []*etNode{seedNode}
	for hop := 1; hop <= hops && len(frontier) > 0; hop++ {
		var next []*etNode

		// (1) apm 계열 — 앱 노드에서만 의미가 있다.
		apps := etFilterKind(frontier, "application")
		if len(apps) > 0 && res.sources["apm"] == "checked" {
			grown, err := etApmHop(ctx, ch, res, inv, svcOf, apps, hop, from, to, &acc)
			if err != nil {
				// 분류 토큰만(§15.3-2) — 원문은 error 사슬(trace) 몫.
				res.sources["apm"] = "error: " + beDetail(err)
				res.notes = append(res.notes, "apm 조회 실패("+beDetail(err)+") — 이 응답의 0건을 '연결 없음'으로 읽으면 안 된다")
			}
			next = append(next, grown...)
		}

		// (2) zero-cost closure — hosted_on은 홉을 소모하지 않으므로
		// 같은 층에서 즉시 편입되고, 그 호스트가 frontier가 된다(§12.5).
		hosted, err := etHostedOn(ctx, pg, ch, res, inv, append(apps, etFilterKind(next, "application")...), hop, from, to, &acc)
		if err != nil {
			res.sources["hosted_on"] = "error: " + beDetail(err)
		} else if len(apps) > 0 {
			res.sources["hosted_on"] = "checked"
		}
		next = append(next, hosted...)

		// (3) 네트워크 — frontier 노드마다 유형으로 판정한다(seed 한 번이
		// 아니다. hosted_on으로 들어온 노드가 여기 걸린다).
		netFrontier := append(etFilterKind(frontier, "server", "network"), etFilterKind(next, "server", "network")...)
		if len(netFrontier) > 0 {
			grown, err := etNetworkHop(ctx, pg, res, inv, netFrontier, hop)
			if err != nil {
				res.sources["network"] = "error: " + beDetail(err)
			} else {
				res.sources["network"] = "checked"
			}
			next = append(next, grown...)
		} else if _, seen := res.sources["network"]; !seen {
			res.sources["network"] = "not_applicable(앱 대상 — 스위치 간선이 앱 지도에 섞이는 것을 막는다)"
		}

		if len(res.nodes) >= etMaxNodes || len(res.edges) >= limit {
			res.truncAt = hop
			for _, n := range next {
				res.boundary = append(res.boundary, n.ID)
			}
			break
		}
		frontier = next
	}

	if _, seen := res.sources["hosted_on"]; !seen {
		res.sources["hosted_on"] = "not_applicable"
	}
	res.sources["socket"] = "checked_unusable_for_app_ownership(앱 프로세스 0건 — " +
		"이 원천의 0건은 무의존을 뜻하지 않는다. §2.9-2 청구)"

	return etEnvelope(res, seedNode, from, to, wbasis, kindFilter, limit, cov, acc), nil
}

func etFilterKind(ns []*etNode, kinds ...string) []*etNode {
	var out []*etNode
	for _, n := range ns {
		for _, k := range kinds {
			if n.Type == k {
				out = append(out, n)
				break
			}
		}
	}
	return out
}

// ---------------------------------------------------------------- PG 명부

type etTargetMeta struct {
	Name, Type, ResourceKind, ResourceKey string
}

type etInventory struct {
	byID  map[string]etTargetMeta
	nodes map[string]string // k8s node resource_key -> target_id
	dbRes map[string]string // "engine|db.name" -> parent target_id (§12.1 정정 2)
	dbAmb map[string][]string
	// k8sSvc는 k8s Service 단축명 -> target_id다. 고아 CLIENT의 목적지
	// authority(예: testbed-product:8081)가 내부 서비스인 경우 정체를
	// 알 수 있는데도 peer 그림자 노드를 만들면 같은 의존이 두 정체로
	// 중복된다(구현 스모크 실측) — §12.2 "가짜 노드 금지"의 귀결.
	k8sSvc map[string]string
	k8sAmb map[string][]string
}

func etLoadTargets(ctx context.Context, pg *sql.DB) (*etInventory, error) {
	inv := &etInventory{
		byID:   map[string]etTargetMeta{},
		nodes:  map[string]string{},
		dbRes:  map[string]string{},
		dbAmb:  map[string][]string{},
		k8sSvc: map[string]string{},
		k8sAmb: map[string][]string{},
	}
	rows, err := pg.QueryContext(ctx, `
		SELECT id::text, coalesce(nullif(display_name,''), name), type,
		       coalesce(meta->>'resource_kind',''), coalesce(meta->>'resource_key',''),
		       coalesce(meta->>'resource_type',''), coalesce(meta->>'parent_target_id','')
		FROM targets`)
	if err != nil {
		return nil, fmt.Errorf("expand_topology 대상 명부: %w", pgErr(err))
	}
	defer rows.Close()
	for rows.Next() {
		var id, name, typ, rk, rkey, rtype, parent string
		if err := rows.Scan(&id, &name, &typ, &rk, &rkey, &rtype, &parent); err != nil {
			return nil, fmt.Errorf("expand_topology 명부 행: %w", pgErr(err))
		}
		inv.byID[id] = etTargetMeta{Name: name, Type: etNormType(typ, rk), ResourceKind: rk, ResourceKey: rkey}
		if rk == "node" && rkey != "" {
			inv.nodes[rkey] = id
		}
		if rk == "service" && rkey != "" {
			short := rkey
			if i := strings.LastIndex(rkey, "/"); i >= 0 {
				short = rkey[i+1:]
			}
			if prev, dup := inv.k8sSvc[short]; dup && prev != id {
				inv.k8sAmb[short] = append(inv.k8sAmb[short], id)
			} else {
				inv.k8sSvc[short] = id
			}
		}
		// db_resource typed meta 경로 — 이름 문자열 파싱이 아니다(§12.1
		// 정정 2). 등급은 environment_observation: 생산자 정본이 이
		// 도메인을 "미승격·탐색 전용"으로 규정하므로 계약이 아니다.
		if rk == "database" && rkey != "" && parent != "" {
			eng := etEngineOf(rtype)
			key := eng + "|" + rkey
			if prev, dup := inv.dbRes[key]; dup && prev != parent {
				inv.dbAmb[key] = append(inv.dbAmb[key], parent)
			} else {
				inv.dbRes[key] = parent
			}
		}
	}
	return inv, pgErr(rows.Err())
}

// etNormType은 조회 분기용 유형이다. targets.type은 정본이 아니므로
// (§8 결정 3 — pod·container·node가 전부 kubernetes_resource)
// resource_kind를 우선한다.
func etNormType(typ, rk string) string {
	switch rk {
	case "node":
		return "server"
	case "pod", "container":
		return "k8s"
	case "database":
		return "database"
	}
	switch typ {
	case "application":
		return "application"
	case "server", "server_resource":
		return "server"
	case "network", "network_resource":
		return "network"
	case "database", "db_resource":
		return "database"
	}
	return typ
}

// etEngineOf는 resource_type("dpm.postgresql.database")에서 엔진을 뽑는다.
func etEngineOf(rtype string) string {
	parts := strings.Split(rtype, ".")
	if len(parts) >= 2 {
		return parts[1]
	}
	return ""
}

func etTargetNode(inv *etInventory, id string, hop int) *etNode {
	m, ok := inv.byID[id]
	if !ok {
		return nil
	}
	return &etNode{
		ID: "target:" + id, TargetID: id, Name: m.Name, Type: m.Type, Hop: hop,
		ResolutionStatus: "resolved", MatchBasis: "resource_attr_uuid",
		Confidence: "producer_contract", Scope: "managed_target",
	}
}

// ---------------------------------------------------------------- CH: 정체

// etServiceIdentity는 창 안 span에서 service_name → target_id를 뽑는다.
// distinct UUID가 정확히 1개일 때만 resolved다 — 생산자는 max()로
// 혼재를 숨기지만 우리는 숨기지 않는다(§12.2).
type etSvcID struct {
	TargetID   string
	Status     string
	Candidates []string
}

type etCoverage struct {
	Spans, WithID int64
	Missing       []string
	Multi         []string
}

func etServiceIdentity(ctx context.Context, ch *CH, from, to time.Time, acc *truncAcc) (map[string]etSvcID, etCoverage, error) {
	var cov etCoverage
	out := map[string]etSvcID{}
	rows, tr, err := ch.Query(ctx, `
		SELECT service_name AS svc,
		       count() AS spans,
		       countIf(resource_attributes['lucida.target_id'] != '') AS with_id,
		       groupUniqArray(10)(resource_attributes['lucida.target_id']) AS ids
		FROM otel_traces_local
		WHERE timestamp >= {from:DateTime64(9)} AND timestamp < {to:DateTime64(9)}
		  AND service_name != ''
		GROUP BY svc`,
		map[string]string{"from": chTime(from), "to": chTime(to)})
	if err != nil {
		return nil, cov, fmt.Errorf("expand_topology 서비스 정체 조회: %w", err)
	}
	acc.note(tr)
	for _, r := range rows {
		svc := chStr(r["svc"])
		spans, withID := chInt(r["spans"]), chInt(r["with_id"])
		cov.Spans += spans
		cov.WithID += withID
		var ids []string
		if arr, ok := r["ids"].([]any); ok {
			for _, v := range arr {
				if s := chStr(v); s != "" {
					ids = append(ids, s)
				}
			}
		}
		sort.Strings(ids)
		switch len(ids) {
		case 0:
			out[svc] = etSvcID{Status: "unresolved"}
			cov.Missing = append(cov.Missing, svc)
		case 1:
			out[svc] = etSvcID{TargetID: ids[0], Status: "resolved"}
		default:
			out[svc] = etSvcID{Status: "ambiguous", Candidates: ids}
			cov.Multi = append(cov.Multi, svc)
		}
	}
	sort.Strings(cov.Missing)
	sort.Strings(cov.Multi)
	return out, cov, nil
}

// etSvcNode는 서비스 이름을 노드로 만든다 — UUID가 있으면 대상 노드,
// 없으면 service_name 폴백(가짜 UUID 금지, §12.2).
func etSvcNode(inv *etInventory, svcOf map[string]etSvcID, svc string, hop int) *etNode {
	id, ok := svcOf[svc]
	if ok && id.Status == "resolved" {
		if n := etTargetNode(inv, id.TargetID, hop); n != nil {
			n.Confidence = "deployment_convention" // env 주입 의존(§12.1 정정 1)
			return n
		}
		// UUID는 있으나 명부에 없다 — 미등록.
		return &etNode{ID: "service:" + svc, Name: svc, Type: "application", Hop: hop,
			ResolutionStatus: "unresolved", MatchBasis: "service_name",
			Confidence: "deployment_convention", Scope: "managed_target",
			Candidates: []string{id.TargetID}, CandidateCount: 1}
	}
	n := &etNode{ID: "service:" + svc, Name: svc, Type: "application", Hop: hop,
		ResolutionStatus: "unresolved", MatchBasis: "service_name",
		Confidence: "deployment_convention", Scope: "managed_target"}
	if ok && id.Status == "ambiguous" {
		n.ResolutionStatus = "ambiguous"
		n.Candidates, n.CandidateCount = id.Candidates, len(id.Candidates)
	}
	return n
}

// ---------------------------------------------------------------- CH: apm 홉

func etApmHop(ctx context.Context, ch *CH, res *etResult, inv *etInventory, svcOf map[string]etSvcID,
	frontier []*etNode, hop int, from, to time.Time, acc *truncAcc) ([]*etNode, error) {

	svcs := etFrontierServices(frontier, inv, svcOf)
	if len(svcs) == 0 {
		return nil, nil
	}
	var grown []*etNode

	paired, err := etPairedEdges(ctx, ch, svcs, from, to, acc)
	if err != nil {
		return nil, err
	}
	for _, p := range paired {
		grown = append(grown, etAddPaired(res, inv, svcOf, p, hop)...)
	}

	dbe, err := etDBEdges(ctx, ch, svcs, from, to, acc)
	if err != nil {
		return grown, err
	}
	for _, d := range dbe {
		grown = append(grown, etAddDB(res, inv, svcOf, d, hop)...)
	}

	peers, prods, err := etOrphanEdges(ctx, ch, svcs, from, to, acc)
	if err != nil {
		return grown, err
	}
	for _, p := range peers {
		grown = append(grown, etAddPeer(res, inv, svcOf, p, hop)...)
	}
	res.producer = append(res.producer, prods...)

	if err := etSelfObserved(ctx, ch, res, svcs, from, to, acc); err != nil {
		return grown, err
	}
	return grown, nil
}

func etFrontierServices(frontier []*etNode, inv *etInventory, svcOf map[string]etSvcID) []string {
	want := map[string]bool{}
	for _, n := range frontier {
		if n.TargetID != "" {
			for svc, id := range svcOf {
				if id.TargetID == n.TargetID {
					want[svc] = true
				}
			}
		}
		if strings.HasPrefix(n.ID, "service:") {
			want[strings.TrimPrefix(n.ID, "service:")] = true
		}
		// 대상 이름이 곧 서비스 이름인 경우(실측: application 대상 name =
		// service_name)도 받는다.
		if n.TargetID != "" {
			if m, ok := inv.byID[n.TargetID]; ok && m.Name != "" {
				if _, isSvc := svcOf[m.Name]; isSvc {
					want[m.Name] = true
				}
			}
		}
	}
	out := make([]string, 0, len(want))
	for s := range want {
		out = append(out, s)
	}
	sort.Strings(out)
	return out
}

type etPair struct {
	CallerSvc, CalleeSvc            string
	PK, CK                          string
	Topic, ConsumerGroup            string
	RawRows                         int64
	Pairs                           int64
	CallerErr, CalleeErr            int64
	CallerP50, CallerP95, CallerMax int64
	CalleeP50, CalleeP95, CalleeMax int64
}

// etPairedEdges — 양쪽 관측이 다 있는 간선. 창 소속은 child 기준이고
// parent는 lookback만큼 소급 조회한다(§12.6 — 좁은 창에서 parent가 밖에
// 있다고 간선이 사라지면 이 규칙이 막으려던 실패가 그대로 난다).
// 집계 축은 계산 가능한 것만: raw_rows / paired_span_pairs. retry는
// 묶을 근거가 원천에 없으므로 logical_calls는 만들지 않는다.
func etPairedEdges(ctx context.Context, ch *CH, svcs []string, from, to time.Time, acc *truncAcc) ([]etPair, error) {
	rows, tr, err := ch.Query(ctx, `
		SELECT p.service_name AS caller, c.service_name AS callee,
		       p.span_kind AS pk, c.span_kind AS ck,
		       c.span_attributes['messaging.destination.name'] AS topic,
		       c.span_attributes['messaging.kafka.consumer.group'] AS consumer_group,
		       count() AS raw_rows,
		       uniqExact((c.trace_id, p.span_id, c.span_id)) AS pairs,
		       uniqExactIf((c.trace_id, p.span_id, c.span_id), p.status_code = 'ERROR') AS caller_err,
		       uniqExactIf((c.trace_id, p.span_id, c.span_id), c.status_code = 'ERROR') AS callee_err,
		       toInt64(quantile(0.5)(p.duration_ns)/1000000) AS cp50,
		       toInt64(quantile(0.95)(p.duration_ns)/1000000) AS cp95,
		       toInt64(max(p.duration_ns)/1000000) AS cpmax,
		       toInt64(quantile(0.5)(c.duration_ns)/1000000) AS ep50,
		       toInt64(quantile(0.95)(c.duration_ns)/1000000) AS ep95,
		       toInt64(max(c.duration_ns)/1000000) AS epmax
		FROM otel_traces_local AS c
		INNER JOIN otel_traces_local AS p
		  ON c.trace_id = p.trace_id AND c.parent_span_id = p.span_id
		WHERE c.timestamp >= {from:DateTime64(9)} AND c.timestamp < {to:DateTime64(9)}
		  AND p.timestamp >= {plook:DateTime64(9)} AND p.timestamp < {to:DateTime64(9)}
		  AND c.service_name != p.service_name
		  AND (has({svcs:Array(String)}, c.service_name) OR has({svcs:Array(String)}, p.service_name))
		GROUP BY caller, callee, pk, ck, topic, consumer_group`,
		map[string]string{
			"from": chTime(from), "to": chTime(to),
			"plook": chTime(from.Add(-etParentLookback)),
			"svcs":  chArray(svcs),
		})
	if err != nil {
		return nil, fmt.Errorf("expand_topology 호출 간선 조회: %w", err)
	}
	acc.note(tr)
	out := make([]etPair, 0, len(rows))
	for _, r := range rows {
		out = append(out, etPair{
			CallerSvc: chStr(r["caller"]), CalleeSvc: chStr(r["callee"]),
			PK: chStr(r["pk"]), CK: chStr(r["ck"]),
			Topic: chStr(r["topic"]), ConsumerGroup: chStr(r["consumer_group"]),
			RawRows: chInt(r["raw_rows"]), Pairs: chInt(r["pairs"]),
			CallerErr: chInt(r["caller_err"]), CalleeErr: chInt(r["callee_err"]),
			CallerP50: chInt(r["cp50"]), CallerP95: chInt(r["cp95"]), CallerMax: chInt(r["cpmax"]),
			CalleeP50: chInt(r["ep50"]), CalleeP95: chInt(r["ep95"]), CalleeMax: chInt(r["epmax"]),
		})
	}
	return out, nil
}

// etSpanKindEdge는 span_kind 조합을 간선 종류로 고정한다(§12.6-1).
// 무제한 일반화하지 않는다 — PRODUCER→CONSUMER는 parent-child만으로
// 메시지 계보를 보장하지 못하므로 async_candidate로 낮춘다(실측:
// span link가 전 kind 0건이라 링크 경로도 없다).
func etSpanKindEdge(pk, ck string) (string, string) {
	switch {
	case pk == "CLIENT" && ck == "SERVER":
		return etKindSync, "CLIENT→SERVER"
	case pk == "PRODUCER" && ck == "CONSUMER":
		return etKindAsync, "PRODUCER→CONSUMER"
	default:
		return etKindUnknown, pk + "→" + ck
	}
}

func etAddPaired(res *etResult, inv *etInventory, svcOf map[string]etSvcID, p etPair, hop int) []*etNode {
	src := res.addNode(etSvcNode(inv, svcOf, p.CallerSvc, hop))
	dst := res.addNode(etSvcNode(inv, svcOf, p.CalleeSvc, hop))
	kind, kinds := etSpanKindEdge(p.PK, p.CK)

	callerBasis, calleeBasis := "caller_client_duration", "callee_server_duration"
	semantics := ""
	if kind == etKindAsync {
		callerBasis, calleeBasis = "producer_duration", "consumer_duration"
		semantics = "consume_duration_not_edge_delay — consumer duration은 소비 처리시간이지 발행↔소비 지연이 아니다"
	}

	e := &etEdge{
		Source: src.ID, Target: dst.ID, Kind: kind, Hop: hop,
		Observation: "both", SpanKinds: kinds,
		RawRows: p.RawRows, PairedSpanPairs: p.Pairs, RetriesCollapsed: false,
		CallerObserved: &etObs{Errors: p.CallerErr, Basis: callerBasis,
			Latency: map[string]int64{"p50": p.CallerP50, "p95": p.CallerP95, "max": p.CallerMax}},
		CalleeObserved: &etObs{Errors: p.CalleeErr, Basis: calleeBasis,
			Latency: map[string]int64{"p50": p.CalleeP50, "p95": p.CalleeP95, "max": p.CalleeMax}},
		WindowBasis: "child_timestamp",
		Topic:       p.Topic, ConsumerGroup: p.ConsumerGroup,
		AggregationDimensions: map[string]string{"caller_service": p.CallerSvc, "callee_service": p.CalleeSvc},
		Note:                  semantics,
	}
	// 어긋남 자체가 단서다 — 호출자는 실패로 보는데 피호출자 기록은 정상.
	if p.CallerErr > 0 && p.CalleeErr == 0 {
		e.ObservationMismatch = true
		if e.Note != "" {
			e.Note += " / "
		}
		e.Note += "호출자만 실패로 관측 — 피호출자가 실패를 자기 기록에 남기지 않고 있다(에러 삼킴·타임아웃·클라이언트측 실패). 판정 아님"
	}
	res.addEdge(e)
	return []*etNode{src, dst}
}

// ---------------------------------------------------------------- CH: apm_db

type etDBRow struct {
	Svc, System, DBName, Addr string
	RawRows, Spans, Errs      int64
	P50, P95, Max             int64
}

func etDBEdges(ctx context.Context, ch *CH, svcs []string, from, to time.Time, acc *truncAcc) ([]etDBRow, error) {
	rows, tr, err := ch.Query(ctx, `
		SELECT service_name AS svc,
		       span_attributes['db.system'] AS sys,
		       span_attributes['db.name'] AS dbname,
		       span_attributes['server.address'] AS addr,
		       count() AS raw_rows,
		       uniqExact((trace_id, span_id)) AS spans,
		       uniqExactIf((trace_id, span_id), status_code = 'ERROR') AS errs,
		       toInt64(quantile(0.5)(duration_ns)/1000000) AS p50,
		       toInt64(quantile(0.95)(duration_ns)/1000000) AS p95,
		       toInt64(max(duration_ns)/1000000) AS pmax
		FROM otel_traces_local
		WHERE timestamp >= {from:DateTime64(9)} AND timestamp < {to:DateTime64(9)}
		  AND span_attributes['db.system'] != ''
		  AND has({svcs:Array(String)}, service_name)
		GROUP BY svc, sys, dbname, addr`,
		map[string]string{"from": chTime(from), "to": chTime(to), "svcs": chArray(svcs)})
	if err != nil {
		return nil, fmt.Errorf("expand_topology DB 간선 조회: %w", err)
	}
	acc.note(tr)
	out := make([]etDBRow, 0, len(rows))
	for _, r := range rows {
		out = append(out, etDBRow{
			Svc: chStr(r["svc"]), System: chStr(r["sys"]), DBName: chStr(r["dbname"]), Addr: chStr(r["addr"]),
			RawRows: chInt(r["raw_rows"]), Spans: chInt(r["spans"]), Errs: chInt(r["errs"]),
			P50: chInt(r["p50"]), P95: chInt(r["p95"]), Max: chInt(r["pmax"]),
		})
	}
	return out, nil
}

func etAddDB(res *etResult, inv *etInventory, svcOf map[string]etSvcID, d etDBRow, hop int) []*etNode {
	src := res.addNode(etSvcNode(inv, svcOf, d.Svc, hop))

	var dst *etNode
	key := d.System + "|" + d.DBName
	if amb, isAmb := inv.dbAmb[key]; isAmb {
		dst = &etNode{ID: "peer:db/" + key, Name: d.DBName + " (" + d.System + ")", Type: "database", Hop: hop,
			ResolutionStatus: "ambiguous", MatchBasis: "db_resource_meta",
			Confidence: "environment_observation", Scope: "peer_observed",
			Candidates: amb, CandidateCount: len(amb)}
	} else if parent, ok := inv.dbRes[key]; ok && d.DBName != "" {
		if n := etTargetNode(inv, parent, hop); n != nil {
			n.MatchBasis = "db_resource_meta"
			// 생산자 정본이 이 도메인을 "미승격·탐색 전용"으로 규정하므로
			// 계약이 아니다(§12.1 정정 2). 등급을 올리지 않는다.
			n.Confidence = "environment_observation"
			dst = n
		}
	}
	if dst == nil {
		// Oracle PDB·Redis는 생산자가 의도적으로 db_resource 행을 만들지
		// 않는다 — 단서(engine·주소·논리DB명)를 그대로 노출하고 가짜
		// 노드 ID는 만들지 않는다.
		label := d.System
		if d.DBName != "" {
			label += "/" + d.DBName
		}
		if d.Addr != "" {
			label += " @" + d.Addr
		}
		dst = &etNode{ID: "peer:db/" + d.System + "/" + d.Addr + "/" + d.DBName,
			Name: label, Type: "database", Hop: hop,
			ResolutionStatus: "unresolved", MatchBasis: "db_resource_meta",
			Confidence: "environment_observation", Scope: "peer_observed"}
	}
	dst = res.addNode(dst)

	res.addEdge(&etEdge{
		Source: src.ID, Target: dst.ID, Kind: etKindDB, Hop: hop,
		Observation: "caller_only", SpanKinds: "CLIENT(db)",
		RawRows: d.RawRows, UniqueSpans: d.Spans, RetriesCollapsed: false,
		CallerObserved: &etObs{Errors: d.Errs, Basis: "caller_client_duration",
			Latency: map[string]int64{"p50": d.P50, "p95": d.P95, "max": d.Max}},
		MissingReason:         "unknown — DB는 피호출자 계측이 없다(구조적)",
		WindowBasis:           "caller_timestamp",
		AggregationDimensions: map[string]string{"service": d.Svc, "db_system": d.System, "db_name": d.DBName, "server_address": d.Addr},
	})
	return []*etNode{src, dst}
}

// ---------------------------------------------------------------- CH: 고아

type etPeerRow struct {
	Svc, Scheme, Authority, Path string
	RawRows, Spans, Errs         int64
	P50, P95, Max                int64
}

// etOrphanEdges — 자식 없는 CLIENT/PRODUCER. §12.3 우선순위: db.system
// 보유는 이미 apm_db가 가져갔으므로 여기서 제외한다(이 배제가 빠지면
// DB CLIENT span 전량 — 실측 2.8만 건 — 이 peer로 흘러든다).
// PRODUCER 고아는 목적지 속성이 전부 비어 있고 토픽만 있어 끝점을
// 만들지 않는다(토픽은 대상이 아니라 채널이다).
func etOrphanEdges(ctx context.Context, ch *CH, svcs []string, from, to time.Time, acc *truncAcc) ([]etPeerRow, []map[string]any, error) {
	rows, tr, err := ch.Query(ctx, `
		SELECT c.service_name AS svc, c.span_kind AS kind,
		       c.span_attributes['server.address'] AS addr,
		       c.span_attributes['url.full'] AS url,
		       c.span_attributes['messaging.destination.name'] AS topic,
		       count() AS raw_rows,
		       uniqExact((c.trace_id, c.span_id)) AS spans,
		       uniqExactIf((c.trace_id, c.span_id), c.status_code = 'ERROR') AS errs,
		       toInt64(quantile(0.5)(c.duration_ns)/1000000) AS p50,
		       toInt64(quantile(0.95)(c.duration_ns)/1000000) AS p95,
		       toInt64(max(c.duration_ns)/1000000) AS pmax
		FROM otel_traces_local AS c
		LEFT JOIN (
			SELECT DISTINCT trace_id, parent_span_id
			FROM otel_traces_local
			WHERE timestamp >= {from:DateTime64(9)} AND timestamp < {tolook:DateTime64(9)}
			  AND parent_span_id != ''
		) AS ch2 ON ch2.trace_id = c.trace_id AND ch2.parent_span_id = c.span_id
		WHERE c.timestamp >= {from:DateTime64(9)} AND c.timestamp < {to:DateTime64(9)}
		  AND c.span_kind IN ('CLIENT', 'PRODUCER')
		  AND ch2.parent_span_id = ''
		  AND c.span_attributes['db.system'] = ''
		  AND has({svcs:Array(String)}, c.service_name)
		GROUP BY svc, kind, addr, url, topic`,
		map[string]string{
			"from": chTime(from), "to": chTime(to),
			"tolook": chTime(to.Add(etParentLookback)),
			"svcs":   chArray(svcs),
		})
	if err != nil {
		return nil, nil, fmt.Errorf("expand_topology 고아 CLIENT 조회: %w", err)
	}
	acc.note(tr)

	peers := map[string]*etPeerRow{}
	prodByTopic := map[string]int64{}
	for _, r := range rows {
		svc, kind := chStr(r["svc"]), chStr(r["kind"])
		addr, url, topic := chStr(r["addr"]), chStr(r["url"]), chStr(r["topic"])
		if kind == "PRODUCER" || (addr == "" && url == "") {
			t := topic
			if t == "" {
				t = "(토픽 미상)"
			}
			prodByTopic[svc+"\x00"+t] += chInt(r["spans"])
			continue
		}
		scheme, authority, path := etSplitURL(url, addr)
		// 노드 키는 (scheme, authority, port)뿐 — path·query는 키에서
		// 제외한다. 넣으면 동적 URL마다 노드가 생겨 상한이 즉시 포화한다.
		k := svc + "\x00" + scheme + "\x00" + authority
		p, ok := peers[k]
		if !ok {
			p = &etPeerRow{Svc: svc, Scheme: scheme, Authority: authority}
			peers[k] = p
		}
		p.RawRows += chInt(r["raw_rows"])
		p.Spans += chInt(r["spans"])
		p.Errs += chInt(r["errs"])
		if v := chInt(r["p50"]); v > p.P50 {
			p.P50 = v
		}
		if v := chInt(r["p95"]); v > p.P95 {
			p.P95 = v
		}
		if v := chInt(r["pmax"]); v > p.Max {
			p.Max = v
		}
		if path != "" && len(strings.Split(p.Path, "\n")) <= etPeerPathSample {
			if p.Path != "" {
				p.Path += "\n"
			}
			p.Path += path
		}
	}

	out := make([]etPeerRow, 0, len(peers))
	for _, p := range peers {
		out = append(out, *p)
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Svc != out[j].Svc {
			return out[i].Svc < out[j].Svc
		}
		return out[i].Authority < out[j].Authority
	})

	prods := make([]map[string]any, 0, len(prodByTopic))
	for k, n := range prodByTopic {
		parts := strings.SplitN(k, "\x00", 2)
		prods = append(prods, map[string]any{"service": parts[0], "topic": parts[1], "spans": n})
	}
	sort.Slice(prods, func(i, j int) bool {
		return fmt.Sprint(prods[i]["service"], prods[i]["topic"]) < fmt.Sprint(prods[j]["service"], prods[j]["topic"])
	})
	return out, prods, nil
}

// etSplitURL은 url.full에서 (scheme, authority, path)를 가른다. url이
// 없으면 server.address를 authority로 쓴다.
func etSplitURL(url, addr string) (string, string, string) {
	scheme, rest := "", url
	if i := strings.Index(url, "://"); i > 0 {
		scheme, rest = url[:i], url[i+3:]
	}
	authority, path := rest, ""
	if i := strings.IndexAny(rest, "/?"); i >= 0 {
		authority, path = rest[:i], rest[i:]
	}
	if authority == "" {
		authority = addr
	}
	if scheme == "" {
		scheme = "unknown"
	}
	return scheme, authority, path
}

func etAddPeer(res *etResult, inv *etInventory, svcOf map[string]etSvcID, p etPeerRow, hop int) []*etNode {
	src := res.addNode(etSvcNode(inv, svcOf, p.Svc, hop))
	dst := res.addNode(etPeerNode(inv, p, hop))
	var paths []string
	if p.Path != "" {
		paths = strings.Split(p.Path, "\n")
	}
	res.addEdge(&etEdge{
		Source: src.ID, Target: dst.ID, Kind: etKindPeer, Hop: hop,
		Observation: "caller_only", SpanKinds: "CLIENT(자식 없음)",
		RawRows: p.RawRows, UniqueSpans: p.Spans, RetriesCollapsed: false,
		CallerObserved: &etObs{Errors: p.Errs, Basis: "caller_client_duration",
			Latency: map[string]int64{"p50": p.P50, "p95": p.P95, "max": p.Max}},
		MissingReason:         "unknown — 미계측·타임아웃·샘플링·수집 유실·창 경계를 이 도구는 구별하지 못한다",
		WindowBasis:           "caller_timestamp",
		Paths:                 paths,
		Note:                  etPeerNote(dst),
		AggregationDimensions: map[string]string{"service": p.Svc, "scheme": p.Scheme, "authority": p.Authority},
	})
	return []*etNode{src, dst}
}

// etPeerNode는 고아 CLIENT의 목적지를 노드로 만든다. authority의 host가
// k8s Service 대상으로 풀리면 그 대상 노드를 쓴다 — 정체를 알 수 있는데
// 그림자 노드를 만들면 같은 의존이 sync_call(대상)과 apm_client_peer(peer)
// 두 정체로 중복된다(구현 스모크 실측: gateway→testbed-product가 그랬다).
func etPeerNode(inv *etInventory, p etPeerRow, hop int) *etNode {
	host := p.Authority
	if i := strings.IndexByte(host, ':'); i >= 0 {
		host = host[:i]
	}
	if amb, isAmb := inv.k8sAmb[host]; isAmb {
		return &etNode{ID: "peer:" + p.Scheme + "://" + p.Authority, Name: p.Authority, Type: "peer", Hop: hop,
			ResolutionStatus: "ambiguous", MatchBasis: "k8s_service_name",
			Confidence: "environment_observation", Scope: "peer_observed",
			Candidates: amb, CandidateCount: len(amb)}
	}
	if id, ok := inv.k8sSvc[host]; ok {
		if n := etTargetNode(inv, id, hop); n != nil {
			n.MatchBasis = "k8s_service_name"
			n.Confidence = "environment_observation"
			return n
		}
	}
	return &etNode{ID: "peer:" + p.Scheme + "://" + p.Authority, Name: p.Authority, Type: "peer", Hop: hop,
		ResolutionStatus: "unresolved", MatchBasis: "peer_authority",
		Confidence: "environment_observation", Scope: "peer_observed"}
}

func etPeerNote(dst *etNode) string {
	if dst.ResolutionStatus == "resolved" {
		return "상대 쪽 span이 이 창에 없다(타임아웃·샘플링·창 경계). 목적지 정체는 k8s Service로 해소됨 — 그림자 노드를 만들지 않는다"
	}
	return "상대 쪽 기록 없음. 관측된 목적지일 뿐 관제 대상이 아니다"
}

// etSelfObserved는 노드 자기 span의 kind별 에러 수를 붙인다(§12.6-2).
func etSelfObserved(ctx context.Context, ch *CH, res *etResult, svcs []string, from, to time.Time, acc *truncAcc) error {
	rows, tr, err := ch.Query(ctx, `
		SELECT service_name AS svc, span_kind AS kind,
		       uniqExactIf((trace_id, span_id), status_code = 'ERROR') AS errs
		FROM otel_traces_local
		WHERE timestamp >= {from:DateTime64(9)} AND timestamp < {to:DateTime64(9)}
		  AND has({svcs:Array(String)}, service_name)
		GROUP BY svc, kind
		HAVING errs > 0`,
		map[string]string{"from": chTime(from), "to": chTime(to), "svcs": chArray(svcs)})
	if err != nil {
		return fmt.Errorf("expand_topology 자기 관측 조회: %w", err)
	}
	acc.note(tr)
	for _, r := range rows {
		svc, kind, errs := chStr(r["svc"]), chStr(r["kind"]), chInt(r["errs"])
		for _, n := range res.nodes {
			if n.Name == svc || n.ID == "service:"+svc {
				if n.SelfObserved == nil {
					n.SelfObserved = map[string]int64{}
				}
				n.SelfObserved[kind] = errs
			}
		}
	}
	return nil
}

// ---------------------------------------------------------------- hosted_on

// etHostedOn은 앱↔물리 다리를 hop_cost=0 간선으로 놓는다(§12.5).
//
// 원천 경로는 구현 중 재실측으로 스펙과 달라졌다. 스펙이 적은
// `kcm_resource_targets(resource_kind='pod')` 경로는 **노드에 닿지
// 못한다** — pod 대상의 parent_target_id·host_target_id가 노드가 아니라
// 클러스터 UUID이고, span의 k8s_namespace·k8s_pod_name·k8s_node_name은
// 전부 빈 값이다(실측). 실제로 닿는 경로는:
//
//	span.host_name(=파드명) → kcm_resources(kind='pod', name=파드명)
//	  .data->>'nodeName'  →  targets(resource_kind='node', resource_key=그 이름)
//
// 실측 커버리지 55/55. data->>'nodeId'는 k8s UID라 우리 대상 UUID가
// 아니므로 nodeName을 거쳐야 한다.
func etHostedOn(ctx context.Context, pg *sql.DB, ch *CH, res *etResult, inv *etInventory,
	apps []*etNode, hop int, from, to time.Time, acc *truncAcc) ([]*etNode, error) {

	if len(apps) == 0 {
		return nil, nil
	}
	svcs := []string{}
	for _, n := range apps {
		if n.Name != "" {
			svcs = append(svcs, n.Name)
		}
	}
	sort.Strings(svcs)

	// 1) 서비스 → 파드명(span host_name).
	rows, tr, err := ch.Query(ctx, `
		SELECT service_name AS svc, host_name AS pod, count() AS n
		FROM otel_traces_local
		WHERE timestamp >= {from:DateTime64(9)} AND timestamp < {to:DateTime64(9)}
		  AND host_name != '' AND has({svcs:Array(String)}, service_name)
		GROUP BY svc, pod`,
		map[string]string{"from": chTime(from), "to": chTime(to), "svcs": chArray(svcs)})
	if err != nil {
		return nil, fmt.Errorf("expand_topology 파드 조회: %w", err)
	}
	acc.note(tr)
	podOf := map[string][]string{}
	for _, r := range rows {
		podOf[chStr(r["svc"])] = append(podOf[chStr(r["svc"])], chStr(r["pod"]))
	}
	if len(podOf) == 0 {
		return nil, nil
	}

	// 2) 파드명 → nodeName (PG kcm_resources).
	pods := []string{}
	for _, ps := range podOf {
		pods = append(pods, ps...)
	}
	nodeOf, err := etPodNodes(ctx, pg, pods)
	if err != nil {
		return nil, err
	}

	var grown []*etNode
	for _, app := range apps {
		seen := map[string]bool{}
		for _, pod := range podOf[app.Name] {
			nodeName, ok := nodeOf[pod]
			if !ok || seen[nodeName] {
				continue
			}
			seen[nodeName] = true
			nodeID, resolved := inv.nodes[nodeName]

			var host *etNode
			if resolved {
				host = etTargetNode(inv, nodeID, hop) // hop_cost=0 — 같은 층
				if host != nil {
					host.MatchBasis = "pod_name"
					host.Confidence = "environment_observation"
				}
			}
			if host == nil {
				host = &etNode{ID: "peer:node/" + nodeName, Name: nodeName, Type: "server", Hop: hop,
					ResolutionStatus: "unresolved", MatchBasis: "pod_name",
					Confidence: "environment_observation", Scope: "peer_observed"}
			}
			host = res.addNode(host)
			res.addEdge(&etEdge{
				Source: app.ID, Target: host.ID, Kind: etKindHosted, Hop: hop,
				Observation: "topology_record",
				Note:        "hop_cost=0 — 홉 거리를 올리지 않는다. 파드 " + pod + " 기준",
			})
			grown = append(grown, host)
		}
	}
	return grown, nil
}

func etPodNodes(ctx context.Context, pg *sql.DB, pods []string) (map[string]string, error) {
	out := map[string]string{}
	if len(pods) == 0 {
		return out, nil
	}
	rows, err := pg.QueryContext(ctx, `
		SELECT name, coalesce(data->>'nodeName','')
		FROM kcm_resources
		WHERE kind = 'pod' AND name = ANY($1)`, pods)
	if err != nil {
		return nil, fmt.Errorf("expand_topology 파드→노드 조회: %w", pgErr(err))
	}
	defer rows.Close()
	for rows.Next() {
		var pod, node string
		if err := rows.Scan(&pod, &node); err != nil {
			return nil, fmt.Errorf("expand_topology 파드→노드 행: %w", pgErr(err))
		}
		if node != "" {
			out[pod] = node
		}
	}
	return out, pgErr(rows.Err())
}

// ---------------------------------------------------------------- 네트워크

// etNetworkHop은 서버·네트워크 노드에서만 발동한다(§12.7). 두 표는
// upsert 현재상태라 from/to가 적용되지 않는다 — 응답에 밝힌다.
func etNetworkHop(ctx context.Context, pg *sql.DB, res *etResult, inv *etInventory, frontier []*etNode, hop int) ([]*etNode, error) {
	ids := []string{}
	for _, n := range frontier {
		// 미해소 노드는 조회 키(local_target_id)가 없어 실행 불가 —
		// not_applicable 고정이지 폴백 대상이 아니다(§12.7-3).
		if n.TargetID != "" {
			ids = append(ids, n.TargetID)
		}
	}
	if len(ids) == 0 {
		return nil, nil
	}
	var grown []*etNode

	// FDB — 원격끝은 mac/ip 문자열이다. ip가 targets.address와 정확
	// 일치할 때만 해소하고, mac만 있으면 미해소 고정(§12.7-5).
	rows, err := pg.QueryContext(ctx, `
		SELECT f.local_target_id::text, f.local_if_name, f.mac, coalesce(f.ip,''),
		       coalesce((SELECT t.id::text FROM targets t WHERE t.address = f.ip LIMIT 1), '')
		FROM network_fdb_hosts f WHERE f.local_target_id::text = ANY($1)`, ids)
	if err != nil {
		return nil, fmt.Errorf("expand_topology FDB 조회: %w", pgErr(err))
	}
	grown = append(grown, etScanNetRows(rows, res, inv, hop, etKindFDB)...)
	if err := rows.Err(); err != nil {
		return grown, pgErr(err)
	}

	// LLDP — 원격끝은 remote_mgmt_addr/chassis_id.
	rows2, err := pg.QueryContext(ctx, `
		SELECT n.local_target_id::text, n.local_if_name,
		       coalesce(nullif(n.remote_sys_name,''), n.remote_chassis_id), coalesce(n.remote_mgmt_addr,''),
		       coalesce((SELECT t.id::text FROM targets t WHERE t.address = n.remote_mgmt_addr LIMIT 1), '')
		FROM network_neighbors n WHERE n.local_target_id::text = ANY($1)`, ids)
	if err != nil {
		return grown, fmt.Errorf("expand_topology LLDP 조회: %w", pgErr(err))
	}
	grown = append(grown, etScanNetRows(rows2, res, inv, hop, etKindLLDP)...)
	return grown, pgErr(rows2.Err())
}

func etScanNetRows(rows *sql.Rows, res *etResult, inv *etInventory, hop int, kind string) []*etNode {
	defer rows.Close()
	var grown []*etNode
	type row struct{ local, ifname, label, addr, remoteID string }
	var all []row
	for rows.Next() {
		var r row
		if rows.Scan(&r.local, &r.ifname, &r.label, &r.addr, &r.remoteID) != nil {
			continue
		}
		all = append(all, r)
	}
	// 네트워크 간선에는 에러율도 호출수도 없다 — 결정론 tie-break로
	// 사전순 고정(§12.8 선정 순서).
	sort.Slice(all, func(i, j int) bool {
		if all[i].ifname != all[j].ifname {
			return all[i].ifname < all[j].ifname
		}
		return all[i].label < all[j].label
	})
	perLocal := map[string]int{}
	for _, r := range all {
		if perLocal[r.local] >= etMaxFanout {
			continue
		}
		perLocal[r.local]++
		src := etTargetNode(inv, r.local, hop)
		if src == nil {
			continue
		}
		src = res.addNode(src)

		var dst *etNode
		if r.remoteID != "" {
			if n := etTargetNode(inv, r.remoteID, hop); n != nil {
				n.MatchBasis, n.Confidence = "ip", "environment_observation"
				dst = n
			}
		}
		if dst == nil {
			basis := "mac"
			if r.addr != "" {
				basis = "ip"
			}
			dst = &etNode{ID: "peer:net/" + r.label, Name: r.label, Type: "network", Hop: hop,
				ResolutionStatus: "unresolved", MatchBasis: basis,
				Confidence: "environment_observation", Scope: "peer_observed"}
		}
		dst = res.addNode(dst)
		res.addEdge(&etEdge{
			Source: src.ID, Target: dst.ID, Kind: kind, Hop: hop,
			Observation: "topology_record",
			Note:        "포트 " + r.ifname + " · source_semantics: current_state_not_history(창 무관 현재 스냅샷)",
		})
		grown = append(grown, dst)
	}
	return grown
}

// ---------------------------------------------------------------- 봉투

func etEnvelope(res *etResult, seed *etNode, from, to time.Time, wbasis string,
	kindFilter []string, limit int, cov etCoverage, acc truncAcc) Envelope {

	want := map[string]bool{}
	for _, k := range kindFilter {
		want[k] = true
	}

	edges := make([]*etEdge, 0, len(res.edges))
	for _, e := range res.edges {
		if len(want) > 0 && !want[e.Kind] {
			continue
		}
		edges = append(edges, e)
	}
	// 정렬: 홉 오름차순 → 에러율 상위 → 호출수 상위 → 안정 tie-break.
	// 호출수 단독으로 자르면 호출량이 적고 에러율이 높은 간선이 탈락한다.
	sort.Slice(edges, func(i, j int) bool {
		a, b := edges[i], edges[j]
		if a.Hop != b.Hop {
			return a.Hop < b.Hop
		}
		ra, rb := etErrRate(a), etErrRate(b)
		if ra != rb {
			return ra > rb
		}
		if a.PairedSpanPairs+a.UniqueSpans != b.PairedSpanPairs+b.UniqueSpans {
			return a.PairedSpanPairs+a.UniqueSpans > b.PairedSpanPairs+b.UniqueSpans
		}
		return etEdgeKey(a) < etEdgeKey(b)
	})
	truncated := false
	if len(edges) > limit {
		edges, truncated = edges[:limit], true
	}

	keep := map[string]bool{seed.ID: true}
	for _, e := range edges {
		keep[e.Source], keep[e.Target] = true, true
	}
	nodes := make([]*etNode, 0, len(keep))
	for _, id := range res.order {
		if keep[id] {
			nodes = append(nodes, res.nodes[id])
		}
	}
	sort.SliceStable(nodes, func(i, j int) bool { return nodes[i].Hop < nodes[j].Hop })

	findings := make([]Finding, 0, len(nodes)+len(edges)+2)
	for _, n := range nodes {
		findings = append(findings, Finding{"kind": "node", "node": n})
	}
	for _, e := range edges {
		findings = append(findings, Finding{"kind": "edge", "edge": e})
	}
	if len(res.producer) > 0 {
		findings = append(findings, Finding{
			"kind": "unpaired_producer",
			"note": "발행 span에 소비자 짝이 없다. 목적지 속성이 없고 토픽만 있어 끝점을 만들지 않는다 — " +
				"토픽은 대상이 아니라 채널이다(§12.3). 비동기 계보는 부분 관측",
			"topics": res.producer,
		})
	}

	meta := Finding{
		"kind":         "coverage",
		"window_basis": wbasis,
		"sources":      res.sources,
		"identity_coverage": map[string]any{
			"spans": cov.Spans, "with_target_id": cov.WithID,
			"missing_services": cov.Missing, "multi_uuid_services": cov.Multi,
			"note": "lucida.target_id는 배포 시 env 주입 의존 — 계약이 아니다. 미주입 서비스는 service_name 폴백",
		},
		"limits": map[string]any{
			"max_nodes": etMaxNodes, "max_edges": limit, "max_fanout": etMaxFanout,
			"note": "상한은 실측 도출이 아니라 현 규모의 여유폭이다 — 이 환경에서는 발동하지 않으므로 잘림 표기는 미검증",
		},
		"retries_collapsed": false,
	}
	if res.truncAt >= 0 {
		meta["expansion_truncated_at_hop"] = res.truncAt
		meta["boundary_nodes"] = res.boundary
		meta["complete_through_hop"] = res.truncAt - 1
	}
	if len(res.notes) > 0 {
		meta["notes"] = res.notes
	}
	findings = append(findings, meta)

	status, reason := "normal", ""
	if len(edges) == 0 {
		status, reason = "no_data", NoDataZeroObservations
		for _, v := range res.sources {
			if strings.HasPrefix(v, "error:") {
				reason = NoDataUnknown
			}
		}
	}

	summary := fmt.Sprintf("%s 기준 노드 %d개·간선 %d개(홉 거리 포함, hosted_on은 홉 미소모).",
		seed.Name, len(nodes), len(edges))
	if len(edges) == 0 {
		summary = fmt.Sprintf("%s 주변에 이 창의 관측 간선 0건 — 의존 없음이 아니라 계측이 없을 수 있다.", seed.Name)
	}
	if truncated {
		summary += " 간선 상한으로 잘림."
	}

	return Envelope{
		Status: status, NoDataReason: reason, Summary: summary, Findings: findings,
		AssessmentBasis: "연결은 사실 관측 — 정상/이상 판정 없음. 간선마다 호출자·피호출자 관측을 병치하며 " +
			"observation_mismatch는 '피호출자가 실패를 자기 기록에 안 남김'이라는 사실이지 원인 지목이 아니다. " +
			"끝점 미해소는 확신도 4축(resolution_status·match_basis·confidence·scope)으로 표기",
		ObservedRange:  &TimeRange{From: from, To: to},
		Truncated:      truncated,
		QueryTruncated: bool(acc),
	}
}

func etErrRate(e *etEdge) float64 {
	den := e.PairedSpanPairs + e.UniqueSpans
	if den == 0 || e.CallerObserved == nil {
		return 0
	}
	return float64(e.CallerObserved.Errors) / float64(den)
}

// ---------------------------------------------------------------- 잡부

func chStr(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}

func chInt(v any) int64 {
	switch n := v.(type) {
	case float64:
		return int64(n)
	case int64:
		return n
	case string:
		var out int64
		fmt.Sscan(n, &out)
		return out
	}
	return 0
}

// chArray는 CH Array(String) 파라미터 표기다.
func chArray(ss []string) string {
	q := make([]string, len(ss))
	for i, s := range ss {
		q[i] = "'" + strings.ReplaceAll(s, "'", "\\'") + "'"
	}
	return "[" + strings.Join(q, ",") + "]"
}
