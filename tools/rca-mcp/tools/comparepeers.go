// compare_peers — "혼자 이탈인가, 또래도 같이 이탈인가"(재설계 §3.4 Q12,
// 구현 설계 §11). 이 도구의 본체는 시계열이 아니라 **또래 명단**이다(§11.1)
// — 복수 대상 시계열·onset·선후는 read_timeseries가 이미 한다.
//
// 판정 척도는 각자 **자기 시간 기준선 대비** 이탈이고(§11.2), 규칙·상수는
// scan_metrics/read_timeseries의 것을 그대로 상속한다 — 고유 임계 0개.
package tools

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"log"
	"sort"
	"strings"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/llm"
)

const (
	peerJudgeCap = 40 // 판정 모집단 상한(§11.6 — 근거는 실무 감뿐, 실주행 분포로 고칠 값)
	peerSampleN  = 5  // 접힌 묶음에 표본으로 싣는 UUID 수
)

// NewComparePeersTool은 compare_peers 도구를 만든다(get_cohort 자리 교체).
func NewComparePeersTool(db *sql.DB, vm *VM, firstEvent time.Time, nowFn func() time.Time) llm.Tool {
	if nowFn == nil {
		nowFn = time.Now
	}
	params, _ := json.Marshal(map[string]any{
		"type": "object",
		"properties": map[string]any{
			"target": map[string]string{"type": "string", "description": "target_id(UUID)"},
			"metric": map[string]string{"type": "string",
				"description": "비교할 지표 이름 1개 — scan_metrics가 알려준 것(부분 일치 안 됨)"},
			"from": map[string]string{"type": "string", "description": "UTC RFC3339 또는 now/now-15m, 반개구간 시작"},
			"to":   map[string]string{"type": "string", "description": "UTC RFC3339 또는 now"},
			"baseline_from": map[string]string{"type": "string",
				"description": "기준선 override 시작(선택 — 기본은 인시던트 첫 증상 직전 60분)"},
			"baseline_to": map[string]string{"type": "string", "description": "기준선 override 끝(선택)"},
		},
		"required": []string{"target", "metric", "from", "to"},
	})
	return llm.Tool{
		Name: "compare_peers",
		Description: "대상의 지표를 또래와 대조해 **혼자 이탈인지 또래도 같이 이탈인지** 답한다. " +
			"판정은 각자 자기 기준선 대비 이탈 여부이지 값 크기 비교가 아니며, 좋고 나쁨은 판정하지 않는다. " +
			"또래 집합 근거와 관측 없는 또래는 응답에 명시된다.",
		Parameters: params,
		Call: func(ctx context.Context, args json.RawMessage) (any, error) {
			var in struct {
				Target       string `json:"target"`
				Metric       string `json:"metric"`
				From         string `json:"from"`
				To           string `json:"to"`
				BaselineFrom string `json:"baseline_from"`
				BaselineTo   string `json:"baseline_to"`
			}
			if err := json.Unmarshal(args, &in); err != nil {
				return nil, fmt.Errorf(`인자 오류: {"target":"<uuid>", "metric":"<지표>", "from":"<RFC3339|now-15m>", "to":"<RFC3339|now>"} 필요`)
			}
			if !uuidRe.MatchString(in.Target) {
				return nil, fmt.Errorf("%q는 target_id가 아님 — target_id는 순수 UUID다(접두 없음)", in.Target)
			}
			if in.Metric == "" {
				return nil, fmt.Errorf("인자 오류: metric 필수 — 지표 이름은 scan_metrics로 발굴하라")
			}
			now := nowFn().UTC()
			fromT, err := parseFlexTime(in.From, now)
			if err != nil {
				return nil, fmt.Errorf("from 시간 오류: %v", err)
			}
			toT, err := parseFlexTime(in.To, now)
			if err != nil {
				return nil, fmt.Errorf("to 시간 오류: %v", err)
			}
			if !fromT.Before(toT) {
				return nil, fmt.Errorf("시간창 오류: from < to 필요 (받은 값 from=%q to=%q)", in.From, in.To)
			}
			baseFrom, baseTo := firstEvent.Add(-scanLookback), firstEvent
			if in.BaselineFrom != "" || in.BaselineTo != "" {
				var e1, e2 error
				baseFrom, e1 = parseFlexTime(in.BaselineFrom, now)
				baseTo, e2 = parseFlexTime(in.BaselineTo, now)
				if e1 != nil || e2 != nil || !baseFrom.Before(baseTo) {
					return nil, fmt.Errorf("baseline override 오류: baseline_from·baseline_to 둘 다, from < to 필요")
				}
			}
			return comparePeers(ctx, db, vm, in.Target, in.Metric, fromT, toT, baseFrom, baseTo)
		},
	}
}

// ── 또래 명단 (§11.4) ──

// peerSet은 또래 집합과 그 근거다. 근거는 산문이 아니라 typed 값이다.
type peerSet struct {
	Basis      string // service_group | type_fallback
	Confidence string // high | low
	GroupKey   string // scope/kind/group_id — service_group일 때만
	TargetType string
	Peers      []string // self 제외
}

// peerCohort는 또래를 열거한다. 두 번째 반환이 non-nil이면 조기 종료
// 봉투다(또래 개념이 성립하지 않는 경우).
//
// 규율(§11.4): ①"그룹 없음"과 "그룹은 있으나 동형 또래 없음"은 다르다 —
// 후자는 fallback하지 않는다. ②다중 그룹 소속은 임의 합집합을 만들지
// 않는다. ③targets.status로 거르지 않는다(생존자 편향).
func peerCohort(ctx context.Context, db *sql.DB, target string) (*peerSet, *Envelope, error) {
	var tType string
	switch err := db.QueryRowContext(ctx, `SELECT type FROM targets WHERE id = $1::uuid`, target).Scan(&tType); {
	case err == sql.ErrNoRows:
		return nil, &Envelope{
			Status:       "no_data",
			NoDataReason: NoDataNotCollected,
			Summary:      "이 target_id가 관제 대상 명부(targets)에 없다 — 또래 개념이 성립하지 않는다.",
		}, nil
	case err != nil:
		return nil, nil, fmt.Errorf("compare_peers 대상 유형 조회: %w", pgErr(err))
	}

	// ① 소속 service 그룹 — 복수면 합치지 않는다.
	rows, err := db.QueryContext(ctx, `
		SELECT scope, group_id::text FROM asset_tree_members
		WHERE kind = 'service' AND target_id = $1::uuid
		ORDER BY scope, group_id`, target)
	if err != nil {
		return nil, nil, fmt.Errorf("compare_peers 서비스 그룹 조회: %w", pgErr(err))
	}
	type grp struct{ scope, id string }
	var groups []grp
	for rows.Next() {
		var g grp
		if err := rows.Scan(&g.scope, &g.id); err != nil {
			rows.Close()
			return nil, nil, fmt.Errorf("compare_peers 그룹 행 읽기: %w", pgErr(err))
		}
		groups = append(groups, g)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return nil, nil, fmt.Errorf("compare_peers 그룹 조회: %w", pgErr(err))
	}

	if len(groups) > 1 {
		keys := make([]string, 0, len(groups))
		for _, g := range groups {
			keys = append(keys, g.scope+"/service/"+g.id)
		}
		return nil, &Envelope{
			Status:       "no_data",
			NoDataReason: NoDataUnknown,
			Summary: fmt.Sprintf("이 대상이 service 그룹 %d개에 동시 소속 — 어느 쪽이 또래인지 원천이 정하지 않는다. "+
				"임의 합집합을 만들지 않는다. 그룹: %s", len(groups), strings.Join(keys, ", ")),
			AssessmentBasis: "또래 집합 모호 — 판정 없음(§11.4)",
		}, nil
	}

	if len(groups) == 1 {
		g := groups[0]
		peers, err := scanPeerIDs(db.QueryContext(ctx, `
			SELECT m.target_id::text FROM asset_tree_members m
			JOIN targets t ON t.id = m.target_id
			-- group_id는 uuid가 아니라 varchar다(119 실측 2026-08-03 —
			-- ::uuid 캐스팅이 varchar=uuid 비교가 되어 42883으로 죽었다.
			-- §7.4 재주행에서 LLM이 이 도구를 부르지 않아 잠복했고, 배터리
			-- B2가 기계 호출 20회로 처음 밟았다).
			WHERE m.kind = 'service' AND m.scope = $1 AND m.group_id = $2
			  AND t.type = $3 AND m.target_id <> $4::uuid
			ORDER BY m.target_id`, g.scope, g.id, tType, target))
		if err != nil {
			return nil, nil, err
		}
		if len(peers) == 0 {
			// fallback 금지 — "그룹 없음"이 아니라 "그룹 안에 동형 또래 없음"이다.
			return nil, &Envelope{
				Status:       "no_data",
				NoDataReason: NoDataNotCollected,
				Summary: fmt.Sprintf("서비스 그룹(%s/service/%s)에 같은 유형(%s) 또래가 없다 — "+
					"그룹이 있는데 동형 또래가 없는 것은 '그룹 없음'과 다르므로 유형 전체로 넓히지 않는다.",
					g.scope, g.id, tType),
				AssessmentBasis: "또래 없음 — 판정 없음(§11.4)",
			}, nil
		}
		return &peerSet{Basis: "service_group", Confidence: "high",
			GroupKey: g.scope + "/service/" + g.id, TargetType: tType, Peers: peers}, nil, nil
	}

	// ② 그룹 소속이 전혀 없을 때만 같은 유형 전체.
	peers, err := scanPeerIDs(db.QueryContext(ctx, `
		SELECT id::text FROM targets WHERE type = $1 AND id <> $2::uuid ORDER BY id`, tType, target))
	if err != nil {
		return nil, nil, err
	}
	if len(peers) == 0 {
		return nil, &Envelope{
			Status:       "no_data",
			NoDataReason: NoDataNotCollected,
			Summary: fmt.Sprintf("같은 유형(%s) 대상이 이 대상뿐 — 또래 비교가 원래 성립하지 않는다.", tType),
			AssessmentBasis: "또래 없음 — 판정 없음(§11.4)",
		}, nil
	}
	return &peerSet{Basis: "type_fallback", Confidence: "low",
		TargetType: tType, Peers: peers}, nil, nil
}

func scanPeerIDs(rows *sql.Rows, qerr error) ([]string, error) {
	if qerr != nil {
		return nil, fmt.Errorf("compare_peers 또래 조회: %w", pgErr(qerr))
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, fmt.Errorf("compare_peers 또래 행 읽기: %w", pgErr(err))
		}
		if uuidRe.MatchString(id) { // 정규식 주입 전 검증(현행 습관 유지)
			out = append(out, id)
		}
	}
	return out, pgErr(rows.Err())
}

// ── 판정 (§11.2·§11.5) ──

// 제외 사유 — 관측 없는 또래를 정상 또래로 세지 않기 위한 구분(§11.5).
const (
	peerExcNoObservation  = "no_observation"  // 창 내 이 지표 표본 0
	peerExcEpisodic       = "episodic"        // 간헐 방출 — 이탈 판정 불가
	peerExcBaselineMissig = "baseline_missing" // 기준선 창 관측 없음
)

// peerRow는 대상 하나의 판정 결과다.
type peerRow struct {
	ID        string
	Self      bool
	Observed  bool
	Excluded  string // 비면 판정 모집단
	Deviating bool
	Direction string // 상향 | 하향 | 동일 (표기용 — 판정에 쓰지 않음)
	CurMedian float64
	BaseMed   float64
	Samples   int
	Onset     onsetResult
}

func comparePeers(ctx context.Context, db *sql.DB, vm *VM, target, metric string, from, to, baseFrom, baseTo time.Time) (any, error) {
	set, early, err := peerCohort(ctx, db, target)
	if err != nil {
		return nil, err
	}
	if early != nil {
		early.ObservedRange = &TimeRange{From: from.UTC(), To: to.UTC()}
		return *early, nil
	}

	candidates := len(set.Peers)
	peers := set.Peers
	truncated := candidates > peerJudgeCap
	if truncated {
		peers = peers[:peerJudgeCap]
	}
	ids := append([]string{target}, peers...)

	// pg=required(deps.go) — 판별 실패는 강등이 아니라 도구 실패다(2b):
	// 명단이 이미 PG에서 왔는데 판별만 조용히 gauge 폴백하면 counter
	// 지표의 이탈 판정이 소리 없이 다른 산식이 된다.
	vt, vtErr := metricValueTypes(ctx, db, []string{metric})
	if vtErr != nil {
		return nil, fmt.Errorf("compare_peers counter 판별 조회: %w", pgErr(vtErr))
	}
	isCounter := strings.HasPrefix(vt[metric], "counter")
	cur, base, mismatch, err := fetchPeerSeries(ctx, vm, metric, ids, from, to, baseFrom, baseTo)
	if err != nil {
		return nil, err
	}
	if mismatch > 0 {
		log.Printf("tools: compare_peers grade 복제 가정 위반 — 값 불일치 (그룹,버킷) %d건", mismatch)
	}

	expected := int(to.Sub(from) / scanStep)
	if expected < 1 {
		expected = 1
	}
	baseSamplesMax := 0
	for _, b := range base {
		if len(b.vals) > baseSamplesMax {
			baseSamplesMax = len(b.vals)
		}
	}
	baselineThin := baseSamplesMax < scanThinSamples

	rowByID := map[string]*peerRow{}
	var rows []*peerRow
	for _, id := range ids {
		r := &peerRow{ID: id, Self: id == target}
		c, b := cur[id], base[id]
		times, vals := c.times, c.vals
		bvals := b.vals
		if isCounter {
			vals = toIncreases(vals)
			bvals = toIncreases(bvals)
			if len(times) > 1 {
				times = times[1:]
			}
		}
		r.Samples = len(vals)
		switch {
		case len(vals) == 0:
			r.Excluded = peerExcNoObservation
		default:
			r.Observed = true
			r.CurMedian, r.BaseMed = median(vals), median(bvals)
			r.Onset = detectOnset(times, vals, bvals, expected, baselineThin)
			switch {
			case strings.HasPrefix(r.Onset.Skipped, "간헐 방출"):
				r.Excluded = peerExcEpisodic
			case strings.HasPrefix(r.Onset.Skipped, "기준선"):
				r.Excluded = peerExcBaselineMissig
			case r.Onset.Skipped == "":
				r.Deviating = true
			}
			switch {
			case r.CurMedian > r.BaseMed:
				r.Direction = "상향"
			case r.CurMedian < r.BaseMed:
				r.Direction = "하향"
			default:
				r.Direction = "동일"
			}
		}
		rows = append(rows, r)
		rowByID[id] = r
	}

	return comparePeersEnvelope(set, rows, rowByID[target], metric, candidates, truncated,
		mismatch, isCounter, baselineThin, from, to, baseFrom, baseTo), nil
}

// ── VM 조회 (§11.7 — instant avg by(target_id) 폐기, range + foldGrade) ──

type peerSeries struct {
	times []time.Time
	vals  []float64
}

// fetchPeerSeries는 대상들의 같은 지표를 현재 창·기준선 창으로 조회해
// target_id별 버킷 시계열로 접는다. grade는 foldGrade(버킷별 max)로 접고
// 남은 실차원은 bucketAvg — read_timeseries와 같은 경로다.
func fetchPeerSeries(ctx context.Context, vm *VM, metric string, ids []string, from, to, baseFrom, baseTo time.Time) (
	cur, base map[string]peerSeries, mismatch int, err error) {

	one := func(f, t time.Time) (map[string]peerSeries, int, error) {
		out := map[string]peerSeries{}
		total := 0
		for i := 0; i < len(ids); i += peerJudgeCap {
			j := min(i+peerJudgeCap, len(ids))
			expr := fmt.Sprintf(`avg_over_time({__name__=%q,target_id=~%q}[%ds])`,
				metric, "^("+strings.Join(ids[i:j], "|")+")$", int(scanStep.Seconds()))
			raw, err := vm.RangeQuery(ctx, expr, f, t, scanStep)
			if err != nil {
				return nil, 0, fmt.Errorf("compare_peers 지표 조회(%s): %w", metric, err)
			}
			folded, mm := foldGrade(raw)
			total += mm
			byTarget := map[string][]*foldedSeries{}
			for _, g := range folded {
				id := g.Labels["target_id"]
				byTarget[id] = append(byTarget[id], g)
			}
			for id, gs := range byTarget {
				times, vals := bucketAvg(gs)
				out[id] = peerSeries{times: times, vals: vals}
			}
		}
		return out, total, nil
	}

	c, m1, err := one(from, to)
	if err != nil {
		return nil, nil, 0, err
	}
	b, m2, err := one(baseFrom, baseTo)
	if err != nil {
		return nil, nil, 0, err
	}
	return c, b, m1 + m2, nil
}

// ── 봉투 조립 (§11.3·§11.6·§11.7) ──

func comparePeersEnvelope(set *peerSet, rows []*peerRow, self *peerRow, metric string,
	candidates int, truncated bool, mismatch int, isCounter, baselineThin bool,
	from, to, baseFrom, baseTo time.Time) Envelope {

	ref := func(id string) string {
		return fmt.Sprintf("vm:%s{target_id=%s}:%s/%s", metric, id,
			from.UTC().Format(time.RFC3339), to.UTC().Format(time.RFC3339))
	}

	var compared, deviating int
	var devRows, quietRows []*peerRow
	excluded := map[string][]string{}
	var allRefs []string
	for _, r := range rows {
		if r.Self {
			continue
		}
		if r.Excluded != "" {
			excluded[r.Excluded] = append(excluded[r.Excluded], r.ID)
			continue
		}
		compared++
		if r.Deviating {
			deviating++
			devRows = append(devRows, r)
			allRefs = append(allRefs, ref(r.ID))
		} else {
			quietRows = append(quietRows, r)
		}
	}

	// verdict(§11.3) — 방향 없는 4값. 실증된 경계는 "이탈 또래 0" 하나뿐.
	verdict, status, reason := "", "", ""
	switch {
	case self.Excluded != "" || !self.Observed:
		verdict, status, reason = "undecidable", "no_data", NoDataUnknown
	case compared == 0:
		verdict, status, reason = "undecidable", "no_data", NoDataUnknown
	case !self.Deviating:
		verdict, status = "self_not_deviating", "normal"
	case deviating == 0:
		verdict, status = "alone", "anomalous"
	default:
		verdict, status = "shared", "anomalous"
	}

	scope := "full_cohort"
	if len(excluded) > 0 {
		scope = "observed_peers_only"
	}

	selfF := Finding{"class": "self", "target_id": self.ID, "observed": self.Observed,
		"samples": self.Samples, "deviating": self.Deviating, "refs": []string{ref(self.ID)}}
	if self.Observed {
		selfF["window_median"], selfF["baseline_median"] = self.CurMedian, self.BaseMed
		selfF["direction"] = self.Direction
		selfF["onset"] = onsetField(self.Onset)
	}
	if self.Excluded != "" {
		selfF["excluded"] = self.Excluded
	}
	allRefs = append(allRefs, ref(self.ID))

	verdictF := Finding{"class": "verdict", "verdict": verdict,
		"self_deviating": self.Deviating, "comparison_scope": scope,
		"peers_eligible": candidates, "peers_compared": compared, "peers_deviating": deviating,
		"note": verdictNote(verdict)}
	if len(excluded) > 0 {
		exc := Finding{}
		for k, v := range excluded {
			exc[k] = len(v)
		}
		verdictF["peers_excluded"] = exc
	}
	if compared > 0 {
		var vals []float64
		for _, r := range rows {
			if !r.Self && r.Excluded == "" {
				vals = append(vals, r.CurMedian)
			}
		}
		sort.Float64s(vals)
		verdictF["magnitude"] = Finding{
			"self_median": self.CurMedian,
			"peer_min":    vals[0], "peer_median": median(vals), "peer_max": vals[len(vals)-1],
			"note": "크기 비교는 참고용 — 판정에 쓰지 않는다(방향 정보가 원천에 없음).",
		}
	}

	peerSetF := Finding{"class": "peer_set", "basis": set.Basis, "confidence": set.Confidence,
		"target_type": set.TargetType, "membership_snapshot": "current",
		"candidates_total": candidates, "judged": len(rows) - 1, "status_filter": "미적용(생존자 편향 방지)"}
	if set.GroupKey != "" {
		peerSetF["group_key"] = set.GroupKey
	}
	if set.Basis == "type_fallback" {
		peerSetF["warning"] = "같은 유형 전체 — 한 클러스터에 업무 도메인이 혼재한다. " +
			"다른 도메인의 대상을 또래로 세고 있을 수 있다(약한 비교)."
	}
	if truncated {
		peerSetF["truncated_note"] = fmt.Sprintf("후보 %d개 중 %d개만 판정했다 — 결론은 본 만큼만의 주장이다.",
			candidates, peerJudgeCap)
	}

	findings := []Finding{verdictF, peerSetF, selfF}
	for _, r := range devRows {
		findings = append(findings, Finding{"class": "peer", "target_id": r.ID,
			"deviating": true, "direction": r.Direction, "samples": r.Samples,
			"window_median": r.CurMedian, "baseline_median": r.BaseMed,
			"onset": onsetField(r.Onset), "refs": []string{ref(r.ID)}})
	}
	if len(quietRows) > 0 {
		vals := make([]float64, 0, len(quietRows))
		ids := make([]string, 0, peerSampleN)
		for i, r := range quietRows {
			vals = append(vals, r.CurMedian)
			if i < peerSampleN {
				ids = append(ids, r.ID)
			}
		}
		sort.Float64s(vals)
		findings = append(findings, Finding{"class": "peers_folded", "count": len(quietRows),
			"deviating": false,
			"value_range": Finding{"min": vals[0], "median": median(vals), "max": vals[len(vals)-1]},
			"target_ids":  ids, "note": "자기 기준선 대비 지속 이탈 없음(z<3) — 개별 행은 접었다."})
	}
	for _, reasonKey := range []string{peerExcNoObservation, peerExcEpisodic, peerExcBaselineMissig} {
		list := excluded[reasonKey]
		if len(list) == 0 {
			continue
		}
		ids := list
		if len(ids) > peerSampleN {
			ids = ids[:peerSampleN]
		}
		findings = append(findings, Finding{"class": "peers_excluded", "reason": reasonKey,
			"count": len(list), "target_ids": ids, "note": peerExcNote(reasonKey)})
	}

	valueKind := "gauge"
	if isCounter {
		valueKind = "increase_per_bucket"
	}
	metaF := Finding{"class": "compare_meta", "metric": metric, "value_kind": valueKind,
		"bucket_seconds":  int(scanStep.Seconds()),
		"baseline_window": baseFrom.UTC().Format(time.RFC3339) + "/" + baseTo.UTC().Format(time.RFC3339),
		"baseline_thin":   baselineThin, "time_basis": "metric_source_time",
		"next_step": nextStep(verdict, self.ID, devRows, metric)}
	if mismatch > 0 {
		metaF["grade_value_mismatch"] = mismatch
	}
	findings = append(findings, metaF)

	env := Envelope{
		Status:  status,
		Summary: comparePeersSummary(verdict, metric, self, set, compared, deviating, excluded, candidates),
		AssessmentBasis: fmt.Sprintf("각 대상 자기 기준선 대비 robust z≥%.0f 지속 이탈(연속 3버킷 중 2, §5·§6 척도 공유) — "+
			"이탈자 수 대조. 값 크기 비교·좋고 나쁨 판정 없음. normal은 건강을 뜻하지 않는다.", scanZThreshold),
		Findings:      findings,
		ObservedRange: &TimeRange{From: from.UTC(), To: to.UTC()},
		Truncated:     truncated,
		Refs:          allRefs,
		// 또래 판정 축 절단(§5.1 계약 2) — 조회 단위가 대상×지표라
		// Metric까지 실어야 selector가 유일해진다. 반환은 self 제외 판정 수.
		Scopes: []QueryScope{qscopeMetric("peer", metric, candidates, len(rows)-1)},
	}
	if reason != "" {
		env.NoDataReason = reason
	}
	return env
}

func onsetField(o onsetResult) any {
	switch {
	case o.Skipped != "":
		return Finding{"skipped": o.Skipped}
	case o.OutsideWindow:
		return Finding{"outside_window": true, "confidence": o.Confidence,
			"note": "창 시작부터 이미 이탈 — 발단은 창 이전"}
	default:
		return Finding{"interval": o.IntervalStart.Format(time.RFC3339) + "/" + o.IntervalEnd.Format(time.RFC3339),
			"confidence": o.Confidence}
	}
}

func peerExcNote(reason string) string {
	switch reason {
	case peerExcNoObservation:
		return "창 내 이 지표 표본 0 — 진짜 0이 아니라 미관측이다. 정상 또래로 세지 않았다. " +
			"수집 결손인지 미적용인지는 get_data_coverage로 판별하라."
	case peerExcEpisodic:
		return "간헐 방출(존재율 30% 미만) — 첫 표본을 발단으로 오인할 수 있어 이탈 판정에서 제외."
	default:
		return "기준선 창 관측 없음 — 평소 수준을 몰라 이탈 판정 불가(신규 배포 등)."
	}
}

func verdictNote(v string) string {
	switch v {
	case "alone":
		return "비교된 또래 중 이탈자 0 — 국소 원인 후보. 단 '또래가 조용하다'가 미관측 때문은 아닌지 peers_excluded를 보라."
	case "shared":
		return "또래도 같이 이탈 — 이 대상 단독 원인일 가능성은 낮아진다. 공통 상류(호스트·클러스터·의존 상류)를 보라. " +
			"이 도구는 상류를 지목하지 않는다."
	case "self_not_deviating":
		return "대상 자신이 자기 기준선 대비 이탈하지 않았다 — 이 지표·창에선 또래 질문이 성립하지 않는다."
	default:
		return "비교 가능한 또래가 없다 — 또래 축으로는 답이 나오지 않는다. 다른 축으로 가라."
	}
}

func nextStep(verdict, self string, devRows []*peerRow, metric string) string {
	if verdict != "shared" || len(devRows) == 0 {
		return "선후(누가 먼저 이탈했나)는 read_timeseries가 계산한다 — 이 도구는 하지 않는다."
	}
	ids := []string{self}
	for i, r := range devRows {
		if i >= 3 {
			break
		}
		ids = append(ids, r.ID)
	}
	return fmt.Sprintf(`이탈 또래 %d개 — 누가 먼저였는지는 read_timeseries(targets=["%s"], metrics=["%s"])로 보라.`,
		len(devRows), strings.Join(ids, `","`), metric)
}

func comparePeersSummary(verdict, metric string, self *peerRow, set *peerSet,
	compared, deviating int, excluded map[string][]string, candidates int) string {

	basis := "같은 서비스 그룹 ∩ 동일 유형(강함)"
	if set.Basis == "type_fallback" {
		basis = "같은 유형 전체(약함 — 도메인 혼재)"
	}
	excN := 0
	for _, v := range excluded {
		excN += len(v)
	}
	head := ""
	switch verdict {
	case "alone":
		head = fmt.Sprintf("%s: 대상만 이탈(자기 기준선 중앙값 %.4g → 창 중앙값 %.4g, %s). "+
			"비교한 또래 %d개 중 이탈 0", metric, self.BaseMed, self.CurMedian, self.Direction, compared)
	case "shared":
		head = fmt.Sprintf("%s: 대상 이탈(%s) — 또래도 %d/%d 이탈", metric, self.Direction, deviating, compared)
	case "self_not_deviating":
		head = fmt.Sprintf("%s: 대상이 자기 기준선 대비 이탈하지 않음(창 중앙값 %.4g, 기준선 %.4g)",
			metric, self.CurMedian, self.BaseMed)
	default:
		head = fmt.Sprintf("%s: 비교 가능한 또래 없음", metric)
	}
	if excN > 0 {
		head += fmt.Sprintf(", 제외 %d개(관측 없음·간헐·기준선 없음)", excN)
	}
	return fmt.Sprintf("%s. 또래 근거: %s, 후보 %d개. → **%s**", head, basis, candidates, verdict)
}
