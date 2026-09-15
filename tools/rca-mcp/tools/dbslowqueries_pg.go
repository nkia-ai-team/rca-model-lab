// db_slow_queries의 PG 쪽 조회와 봉투 문구(§14.6·14.7·14.9). 본문 사전
// (db_sql_text)·플랜(db_sql_plan)·빈손 분기·coverage/limits가 여기 있다.
package tools

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"time"
)

// sqDictText는 db_sql_text 한 행이다. write-once라 captured_at은 최초
// 저장 시각이며 최신 캡처가 아니다(§14.6).
type sqDictText struct {
	text       string
	capturedAt int64
}

// sqFetchTexts는 본문이 body에 없는 키를 사전에서 채운다. 반환의 두 번째
// 값은 사전에도 없던 키 수(결손을 감추지 않는다), 세 번째는 조회 실패의
// 분류 토큰이다(§15.2-3, 2b — 종전엔 실패를 miss 수로 셌다: "사전에 없음"
// 과 "사전을 못 봄"은 다른 결손이고, 후자를 전자로 싣는 것이 조용한
// 결손이다). 실패 시 miss는 0이다 — 미조회는 miss가 아니다.
func sqFetchTexts(ctx context.Context, pg *sql.DB, target string, keys []string) (map[string]sqDictText, int, string) {
	out := map[string]sqDictText{}
	if pg == nil || len(keys) == 0 {
		return out, len(keys), ""
	}
	rows, err := pg.QueryContext(ctx, `SELECT sql_key, sql_text, captured_at_ms FROM db_sql_text
		WHERE resource_id = $1 AND sql_key = ANY($2::text[])`, target, keys)
	if err != nil {
		return out, 0, beDetail(pgErr(err))
	}
	defer rows.Close()
	for rows.Next() {
		var k, t string
		var at int64
		if rows.Scan(&k, &t, &at) == nil {
			out[k] = sqDictText{text: t, capturedAt: at}
		}
	}
	return out, len(keys) - len(out), ""
}

// sqPlanSummary는 §14.7의 플랜 요약이다. 창과 무관하게 (resource_id,
// sql_id)로 집계한다 — 플랜 사전은 시계열이 아니라 캡처 모음이다.
type sqPlanSummary struct {
	count, distinctHash int
	latestAt            int64
}

// stability는 안정성 등급이다. 캡처가 적으면 등급을 매기지 않는다 —
// 캡처 1건이면 distinct/count가 항상 1.0이라 unstable로 오분류된다
// (§14.7 — 실측 Oracle ggrfcy439k2xd가 캡처 1건이었다).
func (s sqPlanSummary) stability() string {
	if s.count < sqPlanMinCaptures {
		return "unknown_single_capture"
	}
	r := float64(s.distinctHash) / float64(s.count)
	switch {
	case r >= 0.9:
		return "unstable"
	case r <= 0.2:
		return "stable"
	}
	return "mixed"
}

func (s sqPlanSummary) finding() map[string]any {
	m := map[string]any{
		"count": s.count, "distinct_plan_hash": s.distinctHash, "hash_stability": s.stability(),
	}
	if s.latestAt > 0 {
		m["latest_captured_at"] = time.UnixMilli(s.latestAt).UTC().Format(time.RFC3339)
	}
	return m
}

// 두 번째 반환은 조회 실패의 분류 토큰이다(2b — 실패를 빈 맵으로 삼키면
// "플랜 캡처 없음"으로 오독된다).
func sqFetchPlanSummaries(ctx context.Context, pg *sql.DB, target string, keys []string) (map[string]sqPlanSummary, string) {
	out := map[string]sqPlanSummary{}
	if pg == nil || len(keys) == 0 {
		return out, ""
	}
	rows, err := pg.QueryContext(ctx, `SELECT sql_id, count(*), count(DISTINCT plan_hash_value), max(captured_at_ms)
		FROM db_sql_plan WHERE resource_id = $1 AND sql_id = ANY($2::text[]) GROUP BY sql_id`,
		target, keys)
	if err != nil {
		return out, beDetail(pgErr(err))
	}
	defer rows.Close()
	for rows.Next() {
		var k string
		var c, d int
		var at sql.NullInt64
		if rows.Scan(&k, &c, &d, &at) == nil {
			out[k] = sqPlanSummary{count: c, distinctHash: d, latestAt: at.Int64}
		}
	}
	return out, ""
}

// sqDrilldownText는 full_text_for 요청을 처리한다(§14.6). 목록의 절단본이
// 아니라 무절단(안전 상한까지) 본문을 별도 finding으로 준다.
func sqDrilldownText(ctx context.Context, items []*sqItem, want []string, pg *sql.DB, target string, dict map[string]sqDictText) []Finding {
	if len(want) == 0 {
		return nil
	}
	byKey := map[string]*sqItem{}
	for _, it := range items {
		byKey[it.sqlKey] = it
	}
	var out []Finding
	for _, k := range want {
		f := Finding{"kind": "sql_text_full", "sql_key": k}
		it, inList := byKey[k]
		full, src := "", "none"
		switch {
		case inList && it.textSource == "ch_body" && !it.textTrunc:
			full, src = it.text, "ch_body"
		case inList && it.textSource == "ch_body":
			// 목록에서 잘린 본문의 원문을 다시 얻는 경로가 없다 —
			// body 본문은 폴 행에 있고 이미 절단본만 남았다. 사전을 시도한다.
			if d, ok := dict[k]; ok && d.text != "" {
				full, src = d.text, "dictionary"
			} else if d2, _, tok := sqFetchTexts(ctx, pg, target, []string{k}); len(d2) > 0 {
				full, src = d2[k].text, "dictionary"
			} else if tok != "" {
				f["note"] = "사전 조회 실패(" + tok + ") — 없는 것이 아니라 못 본 것이다"
			} else {
				f["note"] = "목록 본문이 절단됐고 사전에도 없다 — 무절단 본문 경로 없음(§14.6 한계)"
			}
		case inList && it.textSource == "dictionary":
			if d, ok := dict[k]; ok {
				full, src = d.text, "dictionary"
			}
		default:
			// 목록 밖 키도 사전은 키만으로 조회된다 — 플랜 드릴다운은
			// 목록 밖 키를 받는데 본문만 막으면 근거 없는 비대칭이다.
			// 다만 body 경로는 이 창에서 읽은 행에만 있으므로 없다고 밝힌다.
			if d, _, tok := sqFetchTexts(ctx, pg, target, []string{k}); d[k].text != "" {
				full, src = d[k].text, "dictionary"
				f["note"] = "이 sql_key는 이번 응답의 목록에 없다 — 사전(db_sql_text)에서만 가져왔다(창 내 관측이 아니다)"
			} else if tok != "" {
				f["note"] = "사전 조회 실패(" + tok + ") — 없는 것이 아니라 못 본 것이다"
			} else {
				f["note"] = "이 sql_key는 이번 응답의 목록에 없고 사전에도 없다"
			}
		}
		if full != "" {
			f["sql_text_full_length"] = len(full)
			if len(full) > sqFullTextCap {
				full = full[:sqFullTextCap]
				f["truncated_at"] = sqFullTextCap
			}
			f["sql_text"] = full
		}
		f["sql_text_source"] = src
		out = append(out, f)
	}
	return out
}

// sqDrilldownPlan은 plan_for 요청을 처리한다(§14.7). Oracle 계열은
// plan_hash별 최신 1건, MySQL 계열은 최대 지연 폴에 시간상 가장 가까운
// 캡처 1건이다 — MySQL 해시는 캡처마다 달라 "그 쿼리의 플랜"이라는 것이
// 존재하지 않는다.
func sqDrilldownPlan(ctx context.Context, pg *sql.DB, target, engine string, spec sqEngine, items []*sqItem, want []string) []Finding {
	if len(want) == 0 {
		return nil
	}
	if !spec.hasPlan {
		return []Finding{{"kind": "sql_plan", "sql_keys": want, "plans": nil,
			"note": fmt.Sprintf("engine=%s에는 플랜 원천이 없다(db_sql_plan 0행) — 플랜 부재는 플랜이 없다는 뜻이 아니라 수집되지 않는다는 뜻이다", engine)}}
	}
	if pg == nil {
		return []Finding{{"kind": "sql_plan", "sql_keys": want, "note": "PG 핸들 없음 — 플랜 조회 불가"}}
	}
	peak := map[string]time.Time{}
	for _, it := range items {
		peak[it.sqlKey] = it.latMaxAt
	}
	var out []Finding
	for _, k := range want {
		f := Finding{"kind": "sql_plan", "sql_key": k}
		var (
			rows *sql.Rows
			err  error
			sel  string
		)
		if engine == "mysql" || engine == "mariadb" {
			at := peak[k].UnixMilli()
			if peak[k].IsZero() {
				at = time.Now().UTC().UnixMilli()
			}
			sel = "nearest_to_peak_poll"
			rows, err = pg.QueryContext(ctx, `SELECT plan_hash_value, captured_at_ms, plan_json::text FROM db_sql_plan
				WHERE resource_id = $1 AND sql_id = $2
				ORDER BY abs(captured_at_ms - $3) LIMIT 1`, target, k, at)
		} else {
			sel = "latest_per_plan_hash"
			rows, err = pg.QueryContext(ctx, `SELECT plan_hash_value, captured_at_ms, plan_json::text FROM (
				  SELECT plan_hash_value, captured_at_ms, plan_json,
				         row_number() OVER (PARTITION BY plan_hash_value ORDER BY captured_at_ms DESC) rn
				  FROM db_sql_plan WHERE resource_id = $1 AND sql_id = $2
				) t WHERE rn = 1 ORDER BY captured_at_ms DESC LIMIT 3`, target, k)
		}
		f["plan_selection"] = sel
		if err != nil {
			// 분류 토큰만 — 원문 오류 문자열은 봉투 금지(§15.3-2).
			f["note"] = "플랜 조회 실패(" + beDetail(pgErr(err)) + ")"
			out = append(out, f)
			continue
		}
		var plans []map[string]any
		for rows.Next() {
			var hash, at int64
			var js string
			if rows.Scan(&hash, &at, &js) != nil {
				continue
			}
			plans = append(plans, map[string]any{
				"plan_hash_value": hash,
				"captured_at":     time.UnixMilli(at).UTC().Format(time.RFC3339),
				"nodes":           sqPrunePlan(js),
			})
		}
		rows.Close()
		if len(plans) == 0 {
			f["note"] = "이 sql_key의 플랜 캡처가 없다"
		}
		f["plans"] = plans
		out = append(out, f)
	}
	return out
}

// sqPrunePlan은 플랜 노드에서 해석 가능한 필드만 남긴다(§14.7). 실측
// 플랜 하나가 82,881자이고 대부분 필드가 null이다.
func sqPrunePlan(js string) []map[string]any {
	var nodes []map[string]any
	if json.Unmarshal([]byte(js), &nodes) != nil {
		return nil
	}
	keep := []string{"id", "operation", "options", "depth", "parentId", "cost", "estRows", "actRows", "objectName"}
	out := make([]map[string]any, 0, len(nodes))
	for _, n := range nodes {
		m := map[string]any{}
		for _, k := range keep {
			if v, ok := n[k]; ok && v != nil {
				m[k] = v
			}
		}
		for _, k := range []string{"accessPredicates", "filterPredicates"} {
			if s, ok := n[k].(string); ok && s != "" {
				if len(s) > 200 {
					s = s[:200] + "…"
				}
				m[k] = s
			}
		}
		out = append(out, m)
	}
	return out
}

// sqNoRows는 창에 행이 0개인 세 갈래를 가른다(§14.9).
func sqNoRows(ctx context.Context, ch *CH, r sqReq, engine, engineVer, engineSrc string, obs *TimeRange, acc *truncAcc, pgDegraded map[string]string) (any, error) {
	cov := Finding{"kind": "coverage", "engine": engine, "engine_version": engineVer,
		"engine_source": engineSrc, "window_basis": r.windowBasis}
	sqMarkPGDegraded(cov, pgDegraded)
	// min()/max()/count() 단일 행 집계(내부 LIMIT 1 서브쿼리 포함) — 절단 불가 표면이라 표식은 버린다.
	neighbor, _, err := ch.Query(ctx, `SELECT toString(min(timestamp)) AS lo, toString(max(timestamp)) AS hi, count() AS n FROM (
			SELECT timestamp FROM dpm_topsql_local
			WHERE target_id = {target:String}
			  AND timestamp >= parseDateTime64BestEffort({lo:String}, 9)
			  AND timestamp <  parseDateTime64BestEffort({hi:String}, 9)
			LIMIT 1)`,
		map[string]string{"target": r.target, "lo": chTime(r.from.Add(-sqNeighborLookup)), "hi": chTime(r.to.Add(sqNeighborLookup))})
	near := 0
	if err == nil && len(neighbor) > 0 {
		near = asInt(neighbor[0]["n"])
	}
	if near > 0 {
		cov["neighbor_rows_found"] = true
		return Envelope{
			Status: "no_data", NoDataReason: NoDataCollectorGap,
			Summary: fmt.Sprintf("창 내 top-SQL 행이 없다 — 다만 같은 대상의 창 밖(±30일)에는 행이 있다. "+
				"수집은 살아 있고 이 창만 비어 있다(수집기 정상 동작인 delta cold-start·reset·Δ실행수<=0 생략도 폴을 비운다 — 원천에 heartbeat가 없어 가를 수 없다). 창: %s", r.windowBasis),
			AssessmentBasis: "창 내 0행 + 창 밖 행 존재 — 배제 근거로 쓰면 안 되는 결손",
			ObservedRange:   obs, QueryTruncated: bool(*acc), Findings: []Finding{cov},
		}, nil
	}
	cov["neighbor_rows_found"] = false
	return Envelope{
		Status: "no_data", NoDataReason: NoDataZeroObservations,
		Summary: fmt.Sprintf("이 대상(engine=%s)의 top-SQL이 창 내에도 창 밖(±30일)에도 관측되지 않았다 — "+
			"수집기가 이 대상에 붙지 않았을 가능성이 크다. 창: %s", engine, r.windowBasis),
		AssessmentBasis: "창 내·밖 모두 0행 — 이 대상에 top-SQL 관측 이력 없음",
		ObservedRange:   obs, QueryTruncated: bool(*acc), Findings: []Finding{cov},
	}, nil
}

// sqMarkPGDegraded는 PG 부분 결손(2b)을 coverage finding에 명시한다 —
// source_errors는 부위별 분류 토큰, 문구는 "없음"과 "못 봄"의 구분이다.
func sqMarkPGDegraded(cov Finding, pgDegraded map[string]string) {
	if len(pgDegraded) == 0 {
		return
	}
	se := map[string]string{}
	for part, tok := range pgDegraded {
		se["pg_"+part] = tok
	}
	cov["source_errors"] = se
	cov["pg_degraded_note"] = "PG 원천 실패로 빠진 부위다(engine_registry=CH 관측 폴백 / " +
		"sql_text_dictionary=사전 본문 미조회, miss 아님 / plan_summaries=플랜 존재·안정성 미조회) — 부재가 아니라 미조회다."
}

// sqCoverage는 절단·계약·결손을 싣는 마지막 finding이다(§14.4·14.12).
func sqCoverage(engine, engineVer, engineSrc string, spec sqEngine, r sqReq,
	rowsFetched int, rowsHit bool, otherEngine, pollsInWindow, basePolls, total int,
	truncated bool, dictMiss, excludedSelf int, items []*sqItem, pgDegraded map[string]string) Finding {

	axes := sqAxesFor(engine)
	f := Finding{
		"kind": "coverage", "engine": engine, "engine_version": engineVer, "engine_source": engineSrc,
		"engine_contract": spec.contract, "window_basis": r.windowBasis,
		"population":             "top_n_union_per_poll",
		"selection_axes":         axes,
		"selection_axis_per_row": "unavailable",
		"polls_in_window":        pollsInWindow,
		"baseline_window": map[string]string{
			"from": r.from.Add(-sqBaselineSpan).UTC().Format(time.RFC3339),
			"to":   r.from.UTC().Format(time.RFC3339),
		},
		"baseline_polls_in_window": basePolls,
		"source_rows":              rowsFetched,
		"sql_total":                total,
		"self_monitoring_patterns": sqSelfPatterns,
		"sort":                     r.sort,
		"limits": map[string]any{
			"ratio_threshold": sqRatioThreshold,
			"min_polls":       sqMinPolls,
			"text_truncate":   sqTextTrunc,
			"top_n":           r.topN,
			"notes": []string{
				"모집단은 폴별 다축 top-5 합집합이다 — 창의 모든 SQL이 아니다. 어느 축이 각 행을 뽑았는지는 원천에 없다.",
				"presence < 1은 미실행일 수도, 축에서 밀린 것일 수도, 지연 사건 중 Δ실행수<=0으로 버려진 것일 수도 있다 — presence 하락은 미실행의 증거가 아니다.",
				"first_seen·last_seen은 폴 시각이며 쿼리 실행 시각이 아니다.",
				"본문은 정규화·마스킹된 텍스트다 — 어느 파라미터 값에서 느렸는지는 이 도구로 답할 수 없다. 사전 본문은 write-once로 최초 관측본이다.",
				"판정 문턱 3배는 라이브 배경(창쌍 1,448개·중앙값 비 최대 2.61배)에서 얻었다. 절대 하한이 없어 서브밀리초 쿼리의 배수 발화가 가능하다 — 크기는 latency_ms_*로 확인하라.",
				"최대 팔의 기준선은 24시간 중 한 번의 급등으로 정해진다 — 긴 사건에서는 자기 사건이 기준선을 채워 최대 팔이 침묵한다(중앙값 팔이 받는다).",
				"기준선도 같은 절단 원천으로 계산된다. 회귀는 인시던트 원인의 증명이 아니다.",
			},
		},
	}
	if spec.latUnit == "unknown" {
		f["unit_contract"] = "unknown — 이 engine의 소요 키·단위 계약이 미확인이다(값을 변환하지 않았다)"
	}
	if spec.idKind == "" && spec.contract != "" {
		f["id_contract"] = "unverified — 이 engine의 식별자 저장 위치·형식이 미확인이다"
	}
	if !spec.hasPlan {
		f["plan_source"] = fmt.Sprintf("absent — engine=%s는 db_sql_plan 행이 없다(플랜 미수집)", engine)
	} else if engine == "mysql" || engine == "mariadb" {
		f["plan_hash_note"] = "MySQL 계열 plan_hash_value는 캡처마다 달라 플랜 변경을 판별할 수 없다"
	}
	if truncated {
		f["truncated_to"] = r.topN
	}
	if rowsHit {
		f["source_rows_capped_at"] = sqRowCap
	}
	if otherEngine > 0 {
		f["dropped_other_engine_rows"] = otherEngine
	}
	if dictMiss > 0 {
		f["sql_text_dictionary_miss"] = dictMiss
	}
	if excludedSelf > 0 {
		f["self_monitoring_excluded"] = excludedSelf
	}
	sqMarkPGDegraded(f, pgDegraded)
	modes := map[string]int{}
	partial := 0
	for _, it := range items {
		modes[it.mode] += it.polls
		if it.presence < 0.5 {
			partial++
		}
	}
	f["mode_polls"] = modes
	if partial > 0 {
		f["partial_presence_rows"] = partial
	}
	return f
}

// sqAxesFor는 생산자 코드 계약인 절단축 목록이다(§14.1 정정 2). 행별로는
// 알 수 없고 engine별 목록만 알 수 있다.
func sqAxesFor(engine string) []string {
	switch engine {
	case "postgresql":
		return []string{"avgExecTime", "totalExecTime", "sharedHit", "sharedRead", "walBytes"}
	case "oracle", "tibero":
		return []string{"avgElapsedTime", "totalElapsedTime", "executions", "avgCpuTime", "avgLogicalReads"}
	case "mysql", "mariadb":
		return []string{"avgExecTime", "calls", "avgLockTime", "rowExamined"}
	}
	return nil
}

func sqSummary(items []*sqItem, total int, truncated bool, status, engine string, r sqReq) string {
	if len(items) == 0 {
		return fmt.Sprintf("수집된 top-SQL 집합에서 반환할 행이 없다(engine=%s, 정렬 %s). 창: %s", engine, r.sort, r.windowBasis)
	}
	var slower, chronic []string
	for _, it := range items {
		switch it.verdict {
		case "slower":
			tag := it.sqlKey
			if it.selfMon {
				tag += "(컬렉터 자기 쿼리)"
			}
			slower = append(slower, fmt.Sprintf("%s %.1f배(%s, p50 %.2f→%.2fms)", tag, it.ratioP50, it.slowerBasis, it.base.p50, it.latP50))
		case "chronic":
			chronic = append(chronic, fmt.Sprintf("%s p50 %.1fms", it.sqlKey, it.latP50))
		}
	}
	b := &strings.Builder{}
	fmt.Fprintf(b, "수집된 top-SQL %d개 중 %d개 반환(engine=%s, 정렬 %s)", total, len(items), engine, r.sort)
	if truncated {
		b.WriteString(", 상한 절단")
	}
	b.WriteString(". ")
	if status == "anomalous" {
		fmt.Fprintf(b, "기준선 대비 회귀: %s. ", strings.Join(slower, " · "))
	} else if len(slower) > 0 {
		fmt.Fprintf(b, "회귀 표식은 있으나 전부 컬렉터 자기 쿼리다: %s. ", strings.Join(slower, " · "))
	} else {
		b.WriteString("직전 24시간 기준선 대비 회귀 없음. ")
	}
	if len(chronic) > 0 {
		fmt.Fprintf(b, "만성(창 이전부터 같은 수준): %s. ", strings.Join(chronic, " · "))
	}
	fmt.Fprintf(b, "창: %s", r.windowBasis)
	return b.String()
}

func sqBasis(items []*sqItem, pollsInWindow, basePolls int) string {
	counts := map[string]int{}
	for _, it := range items {
		counts[it.verdict]++
	}
	keys := make([]string, 0, len(counts))
	for k := range counts {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, fmt.Sprintf("%s %d", k, counts[k]))
	}
	basis := fmt.Sprintf("창 폴 %d개의 SQL별 통계를 직전 24시간 같은 SQL 통계와 비교(기준선 폴 %d개). "+
		"판정: 중앙값 %.0f배 또는 최대 %.0f배 — 라이브 배경 창쌍 1,448개에서 중앙값 비 최대 2.61배·3배 초과 0건에서 얻은 문턱. "+
		"만성(절대 소요가 큰 것)과 사건(변화가 큰 것)은 분리해 표시한다. 판정 분포: %s",
		pollsInWindow, basePolls, sqRatioThreshold, sqRatioThreshold, strings.Join(parts, ", "))
	if pollsInWindow < sqMinPolls {
		basis += fmt.Sprintf(" — 창 폴이 %d개(<%d)라 판정 없이 관측만 준다", pollsInWindow, sqMinPolls)
	}
	return basis
}
