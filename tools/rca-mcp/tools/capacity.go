// Q11 한도 부착 — 검증된 짝 고정표 (spec-tool-redesign §6.3).
//
// 성격 = 보험(필수 아님): 한도는 모르는 게 기본 상태고, 짝이 표에 있고
// 한도가 창 내 관측될 때만 %가 붙는다. 표는 실측 검증분만(fuzzy 금지 —
// _max의 절반은 한도가 아니라 관측 최대값), 확장은 평가 실측 발동
// 조건부, 근본 해결은 카탈로그 청구(§2.9-4) — 이행 시 이 표를 걷어낸다.
package tools

import (
	"context"
	"database/sql"
	"fmt"
	"sort"
	"strings"
	"time"
)

// capacityPair는 사용량↔한도 짝 하나다. 단위는 표에 내장 — 통과 지표
// (db.client.*)는 카탈로그에 없어 대조할 사전 자체가 없다(§6.3 조사).
type capacityPair struct {
	Usage       string
	UsageFilter [2]string // {라벨, 값} — 예: state=used만 한도와 비교
	Limit       string
	JoinLabels  []string // 이 라벨 단위로 짝을 맞춘다(잉여 라벨은 합산)
	Unit        string   // 내장 단위 — 카탈로그에 있으면 보조 대조
	LimitSource string   // 원천 등급(§9 결정 2) — 간접값 표식의 전제
}

// LimitSource 등급 — exporter가 보고한 설정 한도와 물리 용량은 증거
// 성격이 다르다(§9 검토: "메트릭에서 왔으므로 전부 간접"은 뭉뚱그림).
const (
	limitSrcConfigured = "reported_config"    // exporter가 보고한 설정 한도(풀 크기·컨테이너 limit 등)
	limitSrcPhysical   = "physical_capacity"  // 물리 용량(총 메모리 등)
	limitSrcUnverified = "reported_unverified" // 보고값이나 서버측 설정과의 동일성 미실측
)

// 짝 고정표 v1 — 2026-07-24~26 격리 F01-R 세트 실측 검증분만.
var capacityPairs = []capacityPair{
	{Usage: "db.client.connections.usage", UsageFilter: [2]string{"state", "used"},
		Limit: "db.client.connections.max", JoinLabels: []string{"pool_name", "process_pid"},
		Unit: "connections", LimitSource: limitSrcConfigured},
	{Usage: "apm.agent.otel.java.jvm.memory.used",
		Limit: "apm.agent.otel.java.jvm.memory.limit",
		JoinLabels: []string{"jvm_memory_pool_name", "jvm_memory_type", "process_pid"},
		Unit:       "B", LimitSource: limitSrcConfigured},
	{Usage: "system.memory.usage", UsageFilter: [2]string{"state", "used"},
		Limit: "system.memory.limit", Unit: "B", LimitSource: limitSrcPhysical},
	{Usage: "kcm.container.cpu_usage", Limit: "kcm.container.cpu_limit",
		JoinLabels: []string{"container_id"}, Unit: "mCPU", LimitSource: limitSrcConfigured},
	{Usage: "kcm.container.mem_usage", Limit: "kcm.container.mem_limit",
		JoinLabels: []string{"container_id"}, Unit: "B", LimitSource: limitSrcConfigured},
	{Usage: "postgresql.db.connections.active", Limit: "postgresql.db.connections.max",
		// 서버측 max_connections와 동일값 실측 확인(2026-07-29 — AP 119
		// SHOW max_connections=200 = 메트릭 200, §9 검증 항목 해소).
		Unit: "count", LimitSource: limitSrcConfigured}, // 조인 라벨 없음 = 인스턴스 단위, db_name은 합산됨
	{Usage: "dpm.oracle.tablespace.used", Limit: "dpm.oracle.tablespace.max",
		JoinLabels: []string{"tablespace"}, Unit: "bytes", LimitSource: limitSrcConfigured},
}

// 수집이 이미 비율을 주는 지표 — 짝 계산 대신 직접 읽으라고 안내
// (이중 계산으로 어긋난 값 두 개가 도는 것 방지).
var ratioMetricHints = map[string]string{
	"system.memory.utilization":            "이미 % 지표",
	"system.cpus.utilization":              "이미 % 지표(lucida 파생 집계)",
	"system.filesystems.utilization":       "이미 % 지표",
	"kcm.pod.cpu_utilization_by_limit":     "이미 % 지표",
	"kcm.pod.mem_utilization_by_limit":     "이미 % 지표",
	"dpm.oracle.tablespace.max_utilization": "이미 % 지표",
}

// capacityMatch는 한 조인 키의 짝 계산 결과다.
type capacityMatch struct {
	MatchedOn  string  `json:"matched_on"`
	Percent    float64 `json:"percent"`
	UsageValue float64 `json:"usage_value"`
	LimitValue float64 `json:"limit_value"`
}

// joinCapacity는 grade 접힌 usage·limit 그룹을 조인 라벨로 짝짓는다 —
// 순수 함수(테스트 표적). 잉여 라벨은 조인 키 안에서 합산(§6.3 처방:
// postgresql active의 db_name 등). limit 없는 키는 unmatched로 계수
// (실측: JVM Metaspace — 한도 미설정 풀).
func joinCapacity(usage, limit []*foldedSeries, pair capacityPair) (matches []capacityMatch, unmatched int) {
	key := func(g *foldedSeries) string {
		parts := make([]string, 0, len(pair.JoinLabels))
		for _, l := range pair.JoinLabels {
			parts = append(parts, l+"="+g.Labels[l])
		}
		return strings.Join(parts, ",")
	}
	last := func(g *foldedSeries) (float64, bool) {
		var u int64
		var v float64
		found := false
		for ts, val := range g.Buckets {
			if !found || ts > u {
				u, v, found = ts, val, true
			}
		}
		return v, found
	}
	usageSum := map[string]float64{}
	for _, g := range usage {
		if v, ok := last(g); ok {
			usageSum[key(g)] += v // 잉여 라벨 합산
		}
	}
	limitSum := map[string]float64{}
	limitSeen := map[string]bool{}
	for _, g := range limit {
		if v, ok := last(g); ok {
			limitSum[key(g)] += v
			limitSeen[key(g)] = true
		}
	}
	for k, uv := range usageSum {
		lv, ok := limitSum[k]
		if !ok || !limitSeen[k] {
			unmatched++
			continue
		}
		if lv <= 0 {
			unmatched++ // 한도 0/음수 — 비율 무의미
			continue
		}
		label := k
		if label == "" {
			label = "(대상 전체)"
		}
		matches = append(matches, capacityMatch{
			MatchedOn: label, Percent: uv / lv * 100, UsageValue: uv, LimitValue: lv})
	}
	sort.Slice(matches, func(i, j int) bool {
		if matches[i].Percent != matches[j].Percent {
			return matches[i].Percent > matches[j].Percent
		}
		return matches[i].MatchedOn < matches[j].MatchedOn
	})
	return matches, unmatched
}

// attachCapacity는 조회 지표에 한도 짝이 있으면 % 부착 조각을 만든다.
// 짝이 표에 없으면 nil(한도 모름이 기본 상태 — §6.3). 계산 시도 후
// 실패하면 침묵 대신 capacity_match=unknown + 사유.
func attachCapacity(ctx context.Context, vm *VM, db *sql.DB, target, metric string, from, to time.Time) map[string]any {
	if hint, ok := ratioMetricHints[metric]; ok {
		return map[string]any{"capacity_match": "ratio_metric",
			"note": hint + " — 한도 대비 %는 이 지표 값을 직접 읽으면 됨"}
	}
	var pair *capacityPair
	for i := range capacityPairs {
		if capacityPairs[i].Usage == metric {
			pair = &capacityPairs[i]
			break
		}
	}
	if pair == nil {
		return nil
	}
	unknown := func(reason string) map[string]any {
		return map[string]any{"capacity_match": "unknown",
			"limit_metric": pair.Limit, "reason": reason}
	}
	// 보조 대조: 카탈로그에 단위가 있으면 내장 단위와 대조 — 불일치면
	// 계산 포기(§6.3 검사 ①). 카탈로그 부재(통과 지표)는 표가 정본.
	if units, err := metricUnits(ctx, db, []string{pair.Usage, pair.Limit}); err == nil {
		for _, m := range []string{pair.Usage, pair.Limit} {
			if u, ok := units[m]; ok && u != "" && u != pair.Unit {
				return unknown(fmt.Sprintf("단위 불일치: 카탈로그 %s=%q vs 짝 표 %q — 표 재검증 필요", m, u, pair.Unit))
			}
		}
	}
	w := windowSeconds(from, to)
	filter := ""
	if pair.UsageFilter[0] != "" {
		filter = fmt.Sprintf(",%s=%q", pair.UsageFilter[0], pair.UsageFilter[1])
	}
	query := func(name, extra string) ([]*foldedSeries, error) {
		expr := fmt.Sprintf(`last_over_time({__name__=%q,target_id=%q%s}[%s])`, name, target, extra, w)
		samples, err := vm.InstantQuery(ctx, expr, to.Add(-time.Second))
		if err != nil {
			return nil, err
		}
		series := make([]VMSeries, 0, len(samples))
		for _, s := range samples {
			if s.Labels["__name__"] == "" {
				s.Labels["__name__"] = name // last_over_time은 이름 유지되나 방어
			}
			series = append(series, VMSeries{Labels: s.Labels,
				Times: []time.Time{s.At}, Values: []float64{s.Value}})
		}
		folded, _ := foldGrade(series)
		return folded, nil
	}
	usage, err := query(pair.Usage, filter)
	if err != nil {
		return unknown("사용량 조회 실패(" + beDetail(err) + ")") // 토큰만(§15.3-2)
	}
	limit, err := query(pair.Limit, "")
	if err != nil {
		return unknown("한도 조회 실패(" + beDetail(err) + ")")
	}
	if len(limit) == 0 {
		return unknown("한도 지표가 창 내 미관측 — 한도 미설정(무제한)이거나 미수집. 오래된 값으로 계산하지 않음(검사 ③)")
	}
	matches, unmatched := joinCapacity(usage, limit, *pair)
	if len(matches) == 0 {
		return unknown(fmt.Sprintf("조인 불성립(검사 ②): 사용량 %d그룹·한도 %d그룹이 %v 기준으로 안 맞물림",
			len(usage), len(limit), pair.JoinLabels))
	}
	shown := matches
	if len(shown) > 3 {
		shown = shown[:3] // % 상위 3개만 — 나머지는 건수로
	}
	frag := map[string]any{
		"capacity_match": "ok",
		"limit_metric":   pair.Limit,
		"unit":           pair.Unit,
		"matches":        shown,
		"basis":          "창 내 last 값, grade 접기 후 " + strings.Join(pair.JoinLabels, "+") + " 조인(잉여 라벨 합산) — 계산만, 높낮이 판정은 조사자 몫",
	}
	if pair.UsageFilter[0] != "" {
		frag["usage_filter"] = pair.UsageFilter[0] + "=" + pair.UsageFilter[1]
	}
	if len(matches) > 3 {
		frag["matches_total"] = len(matches)
	}
	if unmatched > 0 {
		frag["unmatched_keys"] = unmatched // 예: 한도 미설정 JVM 풀
	}
	return frag
}
