// describe_target — 정체·소속·한도 카탈로그 (재설계 §9, Q3+Q4).
// 추린 소수의 상세 조회 도구다 — 일괄 스크리닝 몫은 Triage 브리핑이
// 흡수(§9 결정 4). 소속 4원천 합성은 typed 관계로, 한도는 % 없는
// 카탈로그로(정독·%는 read_timeseries 몫 — §9 결정 2).
package tools

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"math"
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/llm"
)

// uuidRe — target_id 형식 검증(패키지 공유. 구 get_target_meta에서 승계).
var uuidRe = regexp.MustCompile(`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`)

// targetTypeCounts는 미등록 복구 정보(등록 대상 유형 분포)를 만든다 —
// 구 get_target_meta 관례 승계(§9 결정 5).
func targetTypeCounts(ctx context.Context, db *sql.DB) (string, error) {
	rows, err := db.QueryContext(ctx, `SELECT type::text, count(*) FROM targets GROUP BY 1`)
	if err != nil {
		return "", err
	}
	defer rows.Close()
	var parts []string
	for rows.Next() {
		var t string
		var n int
		if err := rows.Scan(&t, &n); err != nil {
			return "", err
		}
		parts = append(parts, fmt.Sprintf("%s=%d", t, n))
	}
	sort.Strings(parts)
	return strings.Join(parts, " "), pgErr(rows.Err())
}

// describeChildIDLimit: 하위가 이 수 이하면 UUID 목록 동반(§9 결정 1
// 하위 id 문턱 — find_targets 보류 상태에서 UUID 획득 경로 보장).
const describeChildIDLimit = 10

// NewDescribeTargetTool은 describe_target 도구를 만든다. firstEvent는
// 기본 창 기준점 — [firstEvent-60m, now)(§5.3 기준선과 같은 자동 창
// 철학, 인시던트 전~현재를 덮어 창 내 한도 변화가 잡히게).
func NewDescribeTargetTool(pg *sql.DB, vm *VM, firstEvent time.Time) llm.Tool {
	params, _ := json.Marshal(map[string]any{
		"type": "object",
		"properties": map[string]any{
			"target": map[string]any{
				"type":        "string",
				"description": "조회할 target_id(UUID) 하나 — 여러 대상의 정체 훑기는 조사 브리핑에 이미 있다",
			},
			"from": map[string]any{"type": "string",
				"description": "한도 관측 창 시작(RFC3339 또는 now-15m 꼴, 생략 시 첫 증상 60분 전)"},
			"to": map[string]any{"type": "string",
				"description": "한도 관측 창 끝(생략 시 now)"},
		},
		"required": []string{"target"},
	})
	return llm.Tool{
		Name: "describe_target",
		Description: "대상 하나의 신상명세 — 정체(유형·이름·주소·상태), 소속(호스트·K8s·서비스그룹 합성), " +
			"하위 요약, 한도 카탈로그(이 대상에서 한도를 아는 지표와 limit 값·창 내 변화). 사용률 % 정독은 read_timeseries로.",
		Parameters: params,
		Call: func(ctx context.Context, args json.RawMessage) (any, error) {
			var in struct {
				Target string `json:"target"`
				From   string `json:"from"`
				To     string `json:"to"`
			}
			if err := json.Unmarshal(args, &in); err != nil || in.Target == "" {
				return nil, fmt.Errorf(`인자 오류: {"target": "<uuid>"} 필요`)
			}
			if !uuidRe.MatchString(in.Target) {
				return nil, fmt.Errorf(
					"%q는 target_id가 아님 — target_id는 순수 UUID다(접두 없음). "+
						"topology 노드 ID(`db:oracle`류)일 수 있음 — seed 멤버·이전 도구 응답의 UUID를 쓰라", in.Target)
			}
			now := time.Now().UTC()
			from, to := firstEvent.Add(-60*time.Minute), now
			if in.From != "" {
				t, err := parseFlexTime(in.From, now)
				if err != nil {
					return nil, err
				}
				from = t
			}
			if in.To != "" {
				t, err := parseFlexTime(in.To, now)
				if err != nil {
					return nil, err
				}
				to = t
			}
			if !to.After(from) {
				return nil, fmt.Errorf("창 오류: from(%s) < to(%s) 여야 함", from.Format(time.RFC3339), to.Format(time.RFC3339))
			}
			return describeTarget(ctx, pg, vm, in.Target, from, to)
		},
	}
}

// relationRow는 상위 소속 typed 관계 한 줄이다(§9 결정 1 — 평평한
// 목록 금지). Consistency: confirmed | conflict | self_ignored | missing | unknown(대조 실패 — 못 봄≠없음).
type relationRow struct {
	RelationKind string `json:"relation_kind"` // host | direct_parent | ancestor | cluster | service_group
	TargetID     string `json:"target_id,omitempty"`
	Name         string `json:"name,omitempty"`
	Type         string `json:"type,omitempty"`
	GroupKey     string `json:"group_key,omitempty"` // service_group 전용 scope/kind/group_id
	Source       string `json:"source"`
	Consistency  string `json:"consistency,omitempty"`
}

func describeTarget(ctx context.Context, pg *sql.DB, vm *VM, target string, from, to time.Time) (any, error) {
	// ── 정체 ──
	var typ, name, disp, addr, status, metaHost string
	var maint bool
	err := pg.QueryRowContext(ctx, `
		SELECT type::text, name, coalesce(display_name,''), coalesce(address,''),
		       status::text, in_maintenance, coalesce(meta->>'host_target_id','')
		FROM targets WHERE id = $1::uuid`, target).
		Scan(&typ, &name, &disp, &addr, &status, &maint, &metaHost)
	if err == sql.ErrNoRows {
		// 미등록 — 오류가 아니라 정상 응답(§9 결정 5, 의도적 계약 변경).
		types, _ := targetTypeCounts(ctx, pg)
		return Envelope{Status: "normal",
			Summary: fmt.Sprintf("미등록 target_id: %s — targets 명부에 없다(가짜 topology 노드이거나 삭제된 대상일 수 있음). 등록 대상 분포: %s", target, types),
			Findings: []Finding{{"lookup_status": "not_found", "target_id": target}},
			AssessmentBasis: "명부 조회 — 대상이 정상이라는 뜻이 아니라 명부에 없다는 뜻",
			Refs:            []string{"pg:targets:" + target}}, nil
	}
	if err != nil {
		return nil, fmt.Errorf("describe_target 정체 조회: %w", pgErr(err))
	}
	identity := Finding{"section": "identity", "target_id": target, "type": typ,
		"name": name, "display_name": disp, "address": addr,
		"status": status, "in_maintenance": maint,
		"refs": []string{"pg:targets:" + target}}

	// ── 소속(위) — 4원천 합성, 현재 스냅샷 ──
	var relations []relationRow
	sources := map[string]string{} // 원천 → checked | not_applicable | error:<사유>
	addRel := func(r relationRow) {
		if r.TargetID == target { // self-reference(DDL host_target_id=self 주입) 제외
			r.Consistency = "self_ignored"
			r.TargetID, r.Name, r.Type = "", "(자기 자신 — 최상위)", ""
		}
		relations = append(relations, r)
	}

	// ① targets.meta host_target_id
	sources["meta_host"] = "checked"
	if metaHost != "" {
		addRel(relationRow{RelationKind: "host", TargetID: metaHost, Source: "targets.meta"})
	}
	// ② server_resources (하위 리소스 → 호스트·부모)
	var srHost, srParent, srKind, srKey string
	switch e := pg.QueryRowContext(ctx, `SELECT host_target_id::text, parent_target_id::text, resource_kind, resource_key
		FROM server_resources WHERE target_id = $1::uuid`, target).Scan(&srHost, &srParent, &srKind, &srKey); e {
	case nil:
		sources["server_resources"] = "checked"
		identity["resource_kind"], identity["resource_key"] = srKind, srKey // §8 유형 판별 정본 상속
		addRel(relationRow{RelationKind: "host", TargetID: srHost, Source: "server_resources"})
		if srParent != srHost {
			addRel(relationRow{RelationKind: "direct_parent", TargetID: srParent, Source: "server_resources"})
		}
	case sql.ErrNoRows:
		sources["server_resources"] = "not_applicable"
	default:
		sources["server_resources"] = "error: " + beDetail(pgErr(e)) // 토큰만(§15.3-2)
	}
	// ③ kcm_resource_targets — 부모 체인 루트까지(사이클 가드·깊이 한도)
	var kcmCluster string
	{
		cur, visited, depth := target, map[string]bool{target: true}, 0
		first := true
		for depth < 6 {
			var parent, cluster, kind, key string
			e := pg.QueryRowContext(ctx, `SELECT parent_target_id::text, cluster_target_id::text, resource_kind, resource_key
				FROM kcm_resource_targets WHERE target_id = $1::uuid`, cur).Scan(&parent, &cluster, &kind, &key)
			if e == sql.ErrNoRows {
				if first {
					sources["kcm_resource_targets"] = "not_applicable"
				}
				break
			}
			if e != nil {
				sources["kcm_resource_targets"] = "error: " + beDetail(pgErr(e))
				break
			}
			sources["kcm_resource_targets"] = "checked"
			if first {
				identity["resource_kind"], identity["resource_key"] = kind, key
				kcmCluster = cluster
			}
			rel := "ancestor"
			if first {
				rel = "direct_parent"
			}
			if parent != cur && !visited[parent] {
				addRel(relationRow{RelationKind: rel, TargetID: parent, Source: "kcm_resource_targets"})
				visited[parent] = true
				cur, first = parent, false
				depth++
				continue
			}
			break // self 부모(루트) 또는 사이클 — 중단
		}
		if kcmCluster != "" && kcmCluster != target {
			addRel(relationRow{RelationKind: "cluster", TargetID: kcmCluster, Source: "kcm_resource_targets"})
		}
	}
	// ④ asset_tree_members — 서비스그룹 멤버십
	if rows, e := pg.QueryContext(ctx, `SELECT m.scope, m.kind::text, m.group_id, coalesce(m.label,'')
		FROM asset_tree_members m WHERE m.target_id = $1::uuid`, target); e != nil {
		sources["asset_tree_members"] = "error: " + beDetail(pgErr(e))
	} else {
		sources["asset_tree_members"] = "checked"
		for rows.Next() {
			var scope, kind, gid, label string
			if rows.Scan(&scope, &kind, &gid, &label) == nil {
				addRel(relationRow{RelationKind: "service_group", Name: label,
					GroupKey: scope + "/" + kind + "/" + gid, Source: "asset_tree_members"})
			}
		}
		rows.Close()
	}

	// 원천 간 호스트 불일치 감지 — 판정하지 않고 표식만(§9 결정 1).
	hostByFirst := map[string]string{}
	for i := range relations {
		if relations[i].RelationKind != "host" || relations[i].TargetID == "" {
			continue
		}
		if prev, ok := hostByFirst["host"]; ok && prev != relations[i].TargetID {
			for j := range relations {
				if relations[j].RelationKind == "host" && relations[j].TargetID != "" {
					relations[j].Consistency = "conflict"
				}
			}
		} else {
			hostByFirst["host"] = relations[i].TargetID
		}
	}
	// 상위 이름·유형 보강 + 미등록 부모 표식.
	relTok := resolveRelationNames(ctx, pg, relations)

	membership := Finding{"section": "membership", "snapshot": "현재 소속(당시 이력 없음)",
		"relations": relations, "sources_checked": sources}
	if relTok != "" {
		membership["source_errors"] = map[string]string{"pg_parent_names": relTok}
		membership["degraded_note"] = "상위 명부 대조 실패 — consistency=unknown은 못 봤다는 뜻(missing 판정 불가)"
	}
	if n := countResolvedRelations(relations); n == 0 {
		membership["note"] = "상위 소속 기록 없음 — 4원천(meta_host·server_resources·kcm_resource_targets·asset_tree_members) 확인 내역은 sources_checked 참조"
	}

	// ── 하위(아래) — 개수+유형, 소수면 UUID 동반 ──
	children := describeChildren(ctx, pg, target, kcmCluster == "" /* 클러스터 자신이면 cluster_target_id로 */)

	// ── 한도 카탈로그 ──
	capacity := describeCapacity(ctx, vm, target, from, to)

	env := Envelope{
		Status: "normal",
		Summary: fmt.Sprintf("%s (%s) 신상명세 — 소속 관계 %d건, 한도 카탈로그 %d행. 사용률 %% 정독은 read_timeseries로.",
			firstNonEmpty(disp, name), typ, len(relations), capacityRowCount(capacity)),
		Findings:        []Finding{identity, membership, children, capacity},
		AssessmentBasis: "정체·소속·한도 카탈로그 조회 — 정상/이상 판정 없음",
		ObservedRange:   &TimeRange{From: from, To: to},
		Refs:            []string{"pg:targets:" + target},
	}
	return env, nil
}

// resolveRelationNames는 관계 행의 상위 대상 이름·유형을 일괄 보강하고
// 명부에 없는 부모는 missing 표식한다. 조회 실패는 분류 토큰으로
// 반환하고 전 부모를 unknown 표식한다(2b 검증 — 종전 조용한 return은
// "못 봄"을 침묵시켰고, 부분 읽기 후 rows.Err면 나머지가 missing으로
// 낙인됐다. missing="명부에 없음"은 완전 조회에서만 말할 자격이 있다).
func resolveRelationNames(ctx context.Context, pg *sql.DB, rels []relationRow) string {
	markUnknown := func() {
		for i := range rels {
			if rels[i].TargetID != "" {
				rels[i].Consistency = "unknown" // 조회 실패 — 못 봤다는 뜻, 없음이 아니다
			}
		}
	}
	var ids []string
	for _, r := range rels {
		if r.TargetID != "" {
			ids = append(ids, r.TargetID)
		}
	}
	if len(ids) == 0 {
		return ""
	}
	found := map[string][2]string{}
	rows, err := pg.QueryContext(ctx, `SELECT id::text, coalesce(nullif(display_name,''), name), type::text
		FROM targets WHERE id = ANY($1::uuid[])`, ids)
	if err != nil {
		markUnknown()
		return beDetail(pgErr(err))
	}
	defer rows.Close()
	for rows.Next() {
		var id, nm, ty string
		if rows.Scan(&id, &nm, &ty) == nil {
			found[id] = [2]string{nm, ty}
		}
	}
	if err := rows.Err(); err != nil {
		markUnknown()
		return beDetail(pgErr(err))
	}
	for i := range rels {
		if rels[i].TargetID == "" {
			continue
		}
		if v, ok := found[rels[i].TargetID]; ok {
			rels[i].Name, rels[i].Type = v[0], v[1]
			if rels[i].Consistency == "" {
				rels[i].Consistency = "confirmed"
			}
		} else {
			rels[i].Consistency = "missing" // 미등록 부모 — 명부에 없음
		}
	}
	return ""
}

func countResolvedRelations(rels []relationRow) int {
	n := 0
	for _, r := range rels {
		if r.Consistency != "self_ignored" {
			n++
		}
	}
	return n
}

// describeChildren은 하위 요약을 만든다 — server_resources(host 귀속)와
// kcm(parent 귀속, 클러스터 대상은 cluster 귀속 전체)의 유형별 개수,
// 총합이 문턱 이하면 UUID 목록 동반(§9 결정 1).
func describeChildren(ctx context.Context, pg *sql.DB, target string, notCluster bool) Finding {
	type childRow struct{ id, kind, source string }
	var all []childRow
	collect := func(q, source string) error {
		rows, err := pg.QueryContext(ctx, q, target)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var id, kind string
			if rows.Scan(&id, &kind) == nil {
				all = append(all, childRow{id, kind, source})
			}
		}
		return pgErr(rows.Err())
	}
	var errs []string
	if err := collect(`SELECT target_id::text, resource_kind FROM server_resources
		WHERE host_target_id = $1::uuid AND target_id <> host_target_id`, "server_resources"); err != nil {
		errs = append(errs, "server_resources: "+beDetail(pgErr(err)))
	}
	kcmQ := `SELECT target_id::text, resource_kind FROM kcm_resource_targets
		WHERE parent_target_id = $1::uuid AND target_id <> parent_target_id`
	if !notCluster { // kcm 행이 없는 대상 = 클러스터 자신일 수 있음 — cluster 귀속 전체
		kcmQ = `SELECT target_id::text, resource_kind FROM kcm_resource_targets
			WHERE cluster_target_id = $1::uuid AND target_id <> cluster_target_id`
	}
	if err := collect(kcmQ, "kcm_resource_targets"); err != nil {
		errs = append(errs, "kcm_resource_targets: "+beDetail(pgErr(err)))
	}

	counts := map[string]int{}
	for _, c := range all {
		counts[c.source+"/"+c.kind]++
	}
	f := Finding{"section": "children", "total": len(all), "counts_by_kind": counts}
	if len(all) == 0 {
		f["note"] = "하위 대상 0"
	} else if len(all) <= describeChildIDLimit {
		list := make([]map[string]string, 0, len(all))
		for _, c := range all {
			list = append(list, map[string]string{"target_id": c.id, "kind": c.kind, "source": c.source})
		}
		f["children"] = list
	} else {
		f["truncated"] = true
		f["note"] = fmt.Sprintf("하위 %d개 — 유형별 개수만(UUID 목록은 %d개 이하일 때)", len(all), describeChildIDLimit)
	}
	if len(errs) > 0 {
		f["errors"] = errs
	}
	return f
}

// describeCapacity는 한도 카탈로그 섹션을 만든다(§9 결정 2·3).
// % 계산 없음 — 창 내 limit 관측치 전 표본을 스캔해 변화(다중·원복
// 포함)를 1급으로 표시하고, "동일"은 관측 표본 수만큼만 주장한다.
func describeCapacity(ctx context.Context, vm *VM, target string, from, to time.Time) Finding {
	f := Finding{"section": "capacity", "current_not_checked": true,
		"notice": "이 표는 검증된 짝 고정표(현재 " + fmt.Sprint(len(capacityPairs)) + "짝)만 다룬다 — " +
			"여기 없다고 한도가 없는 게 아니다. 사용률 % 정독은 read_timeseries가 부착한다."}

	// 창 내 관측 지표 인벤토리 — 한 번의 instant 조회(last_over_time).
	w := windowSeconds(from, to)
	samples, err := vm.InstantQuery(ctx, fmt.Sprintf(`last_over_time({target_id=%q}[%s])`, target, w), to.Add(-time.Second))
	if err != nil {
		// vm=optional(deps.go, 2b): 정체·소속·하위(PG)는 유지하고 한도
		// 실측치만 뺀다 — 분류 토큰만(§15.3-2), 부재는 "한도 없음"이 아니다.
		f["status"] = "error"
		f["source_errors"] = map[string]string{"vm": beDetail(err)}
		f["note"] = "지표 인벤토리 조회 실패(VM) — 한도 실측치 없이 진행. 이 표의 부재는 한도가 없다는 뜻이 아니라 못 봤다는 뜻이다."
		return f
	}
	names := map[string]bool{}
	for _, s := range samples {
		if n := s.Labels["__name__"]; n != "" {
			// .bands·.raw 변종은 인벤토리 계수에서 접는다(§9 — N 부풀림 방지).
			if strings.Contains(n, ".bands") || strings.HasSuffix(n, ".raw") {
				continue
			}
			names[n] = true
		}
	}
	if len(names) == 0 {
		f["status"] = "no_data"
		f["no_data_reason"] = NoDataUnknown
		f["note"] = "창 내 이 대상의 지표 관측 0 — 대상 정지와 수집 공백은 모양이 같아 여기서 판별하지 않는다. 사유 판별은 get_data_coverage로."
		return f
	}

	var rows []Finding
	pairsKnown := 0
	for i := range capacityPairs {
		p := &capacityPairs[i]
		usageSeen, limitSeen := names[p.Usage], names[p.Limit]
		if !usageSeen && !limitSeen {
			continue
		}
		pairsKnown++
		row := Finding{"usage_metric": p.Usage, "limit_metric": p.Limit, "unit": p.Unit,
			"limit_source": p.LimitSource, "usage_observed": usageSeen}
		if !limitSeen {
			row["limit"] = "미관측 — 한도 미설정(무제한)이거나 미수집(§6.3 검사 ③: 오래된 값 안 씀)"
			rows = append(rows, row)
			continue
		}
		row["limit_values"] = capacityLimitTimeline(ctx, vm, target, p, from, to)
		rows = append(rows, row)
	}
	f["metrics_observed"] = len(names)
	f["pairs_known"] = pairsKnown
	f["summary"] = fmt.Sprintf("관측 지표 %d종(접은 후) 중 한도 짝을 아는 것 %d종", len(names), pairsKnown)
	if len(rows) > 0 {
		f["catalog"] = rows
	}
	return f
}

// capacityLimitTimeline은 limit 지표의 창 내 전 표본을 조인 키 단위로
// 스캔해 값·변화·표본 밀도를 만든다(§9 결정 3 — joinCapacity의 last
// 재사용 불가, 새 스캔).
func capacityLimitTimeline(ctx context.Context, vm *VM, target string, p *capacityPair, from, to time.Time) []Finding {
	step := time.Minute
	series, err := vm.RangeQuery(ctx,
		fmt.Sprintf(`last_over_time({__name__=%q,target_id=%q}[%ds])`, p.Limit, target, int(step.Seconds())),
		from, to, step)
	if err != nil {
		return []Finding{{"error": "한도 조회 실패(" + beDetail(err) + ")"}} // 토큰만(§15.3-2)
	}
	folded, _ := foldGrade(series)
	return limitTimeline(folded, p, from, to, step)
}

// limitTimeline은 접힌 limit 그룹들의 전 표본 스캔 — 순수 함수(테스트
// 표적). 조인 키로 그룹하되 잉여 라벨 합산은 안 한다(값 추적이라
// 합치면 가짜 변화가 생김 — 키가 겹치면 그대로 별도 행).
func limitTimeline(folded []*foldedSeries, p *capacityPair, from, to time.Time, step time.Duration) []Finding {
	expected := int(to.Sub(from) / step)
	var out []Finding
	for _, g := range folded {
		keyParts := make([]string, 0, len(p.JoinLabels))
		for _, l := range p.JoinLabels {
			if v := g.Labels[l]; v != "" {
				keyParts = append(keyParts, l+"="+v)
			}
		}
		label := strings.Join(keyParts, ",")
		if label == "" {
			label = "(대상 전체)"
		}
		ts := make([]int64, 0, len(g.Buckets))
		for u := range g.Buckets {
			ts = append(ts, u)
		}
		sort.Slice(ts, func(i, j int) bool { return ts[i] < ts[j] })
		if len(ts) == 0 {
			continue
		}
		var changes []Finding
		prev := g.Buckets[ts[0]]
		for _, u := range ts[1:] {
			v := g.Buckets[u]
			if math.Abs(v-prev) > 1e-9*math.Max(math.Abs(v), math.Abs(prev)) {
				changes = append(changes, Finding{"before": prev, "after": v,
					"observed_at": time.Unix(u, 0).UTC().Format(time.RFC3339)})
				prev = v
			}
		}
		row := Finding{"matched_on": label, "last_value": g.Buckets[ts[len(ts)-1]],
			"sample_count":      len(ts),
			"first_observed_at": time.Unix(ts[0], 0).UTC().Format(time.RFC3339),
			"last_observed_at":  time.Unix(ts[len(ts)-1], 0).UTC().Format(time.RFC3339)}
		switch {
		case len(changes) > 0:
			row["stability"] = "changed"
			row["changes"] = changes // 다중 변경·원복 포함 전 표본 스캔
		case expected > 0 && len(ts)*10 < expected*3: // 존재율 <30% — §6.4 episodic 문턱 상속
			row["stability"] = "unknown_sparse"
			row["note"] = fmt.Sprintf("관측 희박(%d/%d 버킷) — 불변 단정 불가", len(ts), expected)
		default:
			row["stability"] = "observed_constant"
			row["note"] = fmt.Sprintf("관측된 %d표본 동일 — 창 밖·표본 사이 변경은 모름", len(ts))
		}
		out = append(out, row)
	}
	return out
}

func capacityRowCount(f Finding) int {
	if rows, ok := f["catalog"].([]Finding); ok {
		return len(rows)
	}
	return 0
}

func firstNonEmpty(a, b string) string {
	if a != "" {
		return a
	}
	return b
}
