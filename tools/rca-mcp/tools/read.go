// read_timeseries — 새 표면 Ⅲ-② 시계열 정독 + 차원 분해 + 봉투 부착
// 2건 (spec-tool-redesign §3.3-②, 구현 설계 §6). Ⅲ의 중추.
//
// 응답 = 다운샘플 버킷 배열 + 코드 파생값(§6.1 A안): 모양 독해(계단/
// 반짝/추세)는 조사자, 산술(onset·한도%·선후)은 코드. 지표 이름은
// scan_metrics가 추려준 것을 쓴다는 전제(깔때기 — §6.4).
package tools

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"log"
	"math"
	"sort"
	"strings"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/llm"
)

// §6.2 합의 수치.
const (
	readMaxCombos   = 8  // 대상×지표 조합 상한 — 8시리즈×40버킷이 26B 상한선
	readShowBuckets = 40 // 시리즈당 표시 버킷 상한 — 초과 시 균등 병합
	readGroupTopN   = 5  // group_by 라벨 값 상한(합계 기준) + other 합산
)

// NewReadTimeseriesTool은 read_timeseries 도구를 만든다. firstEvent는
// 기준선 기본 창의 기준점(scan_metrics와 동일 §5.3), nowFn은 "now" 상대
// 표기의 기준 시계(nil이면 time.Now — 테스트 주입용).
func NewReadTimeseriesTool(vm *VM, db *sql.DB, firstEvent time.Time, nowFn func() time.Time) llm.Tool {
	if nowFn == nil {
		nowFn = time.Now
	}
	params, _ := json.Marshal(map[string]any{
		"type": "object",
		"properties": map[string]any{
			"targets": map[string]any{"type": "array", "items": map[string]string{"type": "string"},
				"description": "target_id(UUID) 목록 — 복수면 선후(onset) 비교가 자동 부착됨"},
			"metrics": map[string]any{"type": "array", "items": map[string]string{"type": "string"},
				"description": "지표 이름 목록 — scan_metrics가 알려준 것. 대상×지표 조합 최대 8"},
			"from": map[string]string{"type": "string", "description": "UTC RFC3339 또는 now/now-15m 상대 표기, 반개구간 시작"},
			"to":   map[string]string{"type": "string", "description": "UTC RFC3339 또는 now — '지금도 그런가'는 to:now로"},
			"group_by": map[string]string{"type": "string",
				"description": "분해 라벨(선택 — 예: pod, name, device). 지정 시 지표 1개만, 라벨 값 상위 5 + other"},
			"baseline_from": map[string]string{"type": "string", "description": "기준선 override 시작(선택 — 기본은 인시던트 첫 증상 직전 60분)"},
			"baseline_to":   map[string]string{"type": "string", "description": "기준선 override 끝(선택)"},
		},
		"required": []string{"targets", "metrics", "from", "to"},
	})
	return llm.Tool{
		Name: "read_timeseries",
		Description: "대상들의 지표 시계열을 버킷 값 배열로 준다(모양 독해용) + 코드 파생값 부착: " +
			"각 시계열의 onset(평소 대비 이탈 시작 구간)과 시계열 간 선후 타임라인, 한도 짝이 있으면 한도 대비 %. " +
			"차원 분해는 group_by 라벨로.",
		Parameters: params,
		Call: func(ctx context.Context, args json.RawMessage) (any, error) {
			var in struct {
				Targets      []string `json:"targets"`
				Metrics      []string `json:"metrics"`
				From         string   `json:"from"`
				To           string   `json:"to"`
				GroupBy      string   `json:"group_by"`
				BaselineFrom string   `json:"baseline_from"`
				BaselineTo   string   `json:"baseline_to"`
			}
			if err := json.Unmarshal(args, &in); err != nil {
				return nil, fmt.Errorf(`인자 오류: {"targets":["<uuid>"], "metrics":["<지표>"], "from":"<RFC3339|now-15m>", "to":"<RFC3339|now>"} 필요`)
			}
			if len(in.Targets) == 0 || len(in.Metrics) == 0 {
				return nil, fmt.Errorf("인자 오류: targets·metrics는 비어 있으면 안 됨 — 지표 이름은 scan_metrics로 발굴하라")
			}
			for _, t := range in.Targets {
				if !uuidRe.MatchString(t) {
					return nil, fmt.Errorf("%q는 target_id가 아님 — target_id는 순수 UUID다(접두 없음)", t)
				}
			}
			if n := len(in.Targets) * len(in.Metrics); n > readMaxCombos {
				return nil, fmt.Errorf("대상×지표 조합 %d개 — 상한 %d. 가설에 걸린 조합만 남기고 쪼개서 부르라", n, readMaxCombos)
			}
			if in.GroupBy != "" && len(in.Metrics) != 1 {
				return nil, fmt.Errorf("group_by 지정 시 metrics는 1개만 — 분해×복수지표 교차는 응답 폭발")
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
			return readTimeseries(ctx, vm, db, in.Targets, in.Metrics, in.GroupBy, fromT, toT, baseFrom, baseTo)
		},
	}
}

// parseFlexTime은 RFC3339 또는 now/now-<기간>(now-15m, now-1h)을 푼다.
func parseFlexTime(s string, now time.Time) (time.Time, error) {
	if s == "now" {
		return now, nil
	}
	if rest, ok := strings.CutPrefix(s, "now-"); ok {
		d, err := time.ParseDuration(rest)
		if err != nil {
			return time.Time{}, fmt.Errorf("%q — now-15m, now-1h 꼴 필요", s)
		}
		return now.Add(-d), nil
	}
	t, err := time.Parse(time.RFC3339, s)
	if err != nil {
		return time.Time{}, fmt.Errorf("%q — UTC RFC3339(예: 2026-07-21T06:40:00Z) 또는 now/now-15m", s)
	}
	return t, nil
}

// readSeries는 응답 시계열 한 줄이다(조합 하나, group_by면 라벨 값 하나).
type readSeries struct {
	Target, Metric, Group string
	Times                 []time.Time // 원본 30s 버킷(다운샘플 전)
	Values                []float64
	IsCounter             bool
	BaseVals              []float64 // 기준선 같은 접기 결과(counter면 increase)
	Onset                 onsetResult
}

// onsetResult는 §6.4 onset 산출이다. Skipped가 비면 산출 성공.
type onsetResult struct {
	IntervalStart, IntervalEnd time.Time
	Confidence                 string
	OutsideWindow              bool   // 창 시작부터 이미 이탈 — onset은 창 이전
	Skipped                    string // 미산출 사유
}

func readTimeseries(ctx context.Context, vm *VM, db *sql.DB, targets, metrics []string, groupBy string,
	from, to, baseFrom, baseTo time.Time) (any, error) {

	// pg=semantic_auxiliary(deps.go, 2b): 판별 실패 시 전부 gauge 폴백 +
	// 봉투 결손 명시 — scan_metrics와 같은 처방(같은 판별 입력).
	vt, vtErr := metricValueTypes(ctx, db, metrics)
	units, _ := metricUnits(ctx, db, metrics)

	var series []*readSeries
	gradeMismatch := 0
	baseSamplesMax := 0
	groupTruncated := false
	for _, tg := range targets {
		for _, m := range metrics {
			sel := fmt.Sprintf(`{__name__=%q,target_id=%q}`, m, tg)
			expr := fmt.Sprintf(`avg_over_time(%s[%ds])`, sel, int(scanStep.Seconds()))
			curRaw, err := vm.RangeQuery(ctx, expr, from, to, scanStep)
			if err != nil {
				return nil, fmt.Errorf("read_timeseries 조회(%s): %w", m, err)
			}
			baseRaw, err := vm.RangeQuery(ctx, expr, baseFrom, baseTo, scanStep)
			if err != nil {
				return nil, fmt.Errorf("read_timeseries 기준선 조회(%s): %w", m, err)
			}
			curFold, mm1 := foldGrade(curRaw)
			baseFold, mm2 := foldGrade(baseRaw)
			gradeMismatch += mm1 + mm2
			isCounter := strings.HasPrefix(vt[m], "counter")

			if groupBy == "" {
				_, baseVals := bucketAvg(baseFold)
				if len(baseVals) > baseSamplesMax {
					baseSamplesMax = len(baseVals)
				}
				times, vals := bucketAvg(curFold)
				series = append(series, &readSeries{Target: tg, Metric: m,
					Times: times, Values: vals, IsCounter: isCounter, BaseVals: baseVals})
				continue
			}
			// 분해: 라벨 값별 그룹, 합계 상위 N + other 합산(§6.2).
			groups, truncated := splitByLabel(curFold, groupBy, readGroupTopN)
			groupTruncated = groupTruncated || truncated
			baseGroups, _ := splitByLabel(baseFold, groupBy, 0) // 기준선은 전 라벨 값
			baseByVal := map[string][]float64{}
			for _, bg := range baseGroups {
				_, bvals := bucketAvg(bg.members)
				baseByVal[bg.value] = bvals
				if len(bvals) > baseSamplesMax {
					baseSamplesMax = len(bvals)
				}
			}
			for _, g := range groups {
				times, vals := bucketAvg(g.members)
				if g.value == readGroupOther {
					times, vals = bucketSum(g.members)
				}
				series = append(series, &readSeries{Target: tg, Metric: m, Group: g.value,
					Times: times, Values: vals, IsCounter: isCounter, BaseVals: baseByVal[g.value]})
			}
		}
	}
	if gradeMismatch > 0 {
		log.Printf("tools: read_timeseries grade 복제 가정 위반 — 값 불일치 (그룹,버킷) %d건", gradeMismatch)
	}

	nonEmpty := 0
	for _, s := range series {
		if len(s.Values) > 0 {
			nonEmpty++
		}
	}
	if nonEmpty == 0 {
		return Envelope{
			Status:       "no_data",
			NoDataReason: NoDataUnknown,
			Summary: "요청한 대상×지표 조합 전부 창 내 관측 없음 — 지표 이름이 맞는지 scan_metrics로 확인하고, " +
				"수집 결손 여부는 get_data_coverage로 판별하라.",
			ObservedRange: &TimeRange{From: from.UTC(), To: to.UTC()},
		}, nil
	}

	// onset(§6.4) — 원본 30s 버킷에서 계산, counter는 increase 후.
	expected := int(to.Sub(from) / scanStep)
	if expected < 1 {
		expected = 1
	}
	baselineThin := baseSamplesMax < scanThinSamples
	for _, s := range series {
		vals, times, base := s.Values, s.Times, s.BaseVals
		if s.IsCounter {
			vals = toIncreases(vals)
			base = toIncreases(base)
			if len(times) > 1 {
				times = times[1:]
			}
		}
		s.Onset = detectOnset(times, vals, base, expected, baselineThin)
	}

	return readEnvelope(ctx, vm, db, series, groupBy, units, from, to, baseFrom, baseTo,
		baseSamplesMax, baselineThin, gradeMismatch, groupTruncated, vtErr), nil
}

// ── 분해 보조 ──

const readGroupOther = "(other)"

type labelGroup struct {
	value   string
	members []*foldedSeries
	sum     float64
}

// splitByLabel은 접힌 그룹들을 라벨 값별로 묶고 합계 상위 topN + other로
// 자른다. topN=0이면 전부(자름 없음).
func splitByLabel(folded []*foldedSeries, label string, topN int) ([]labelGroup, bool) {
	byVal := map[string]*labelGroup{}
	var order []string
	for _, g := range folded {
		v := g.Labels[label]
		lg := byVal[v]
		if lg == nil {
			lg = &labelGroup{value: v}
			byVal[v] = lg
			order = append(order, v)
		}
		lg.members = append(lg.members, g)
		for _, x := range g.Buckets {
			lg.sum += x
		}
	}
	groups := make([]labelGroup, 0, len(byVal))
	for _, v := range order {
		groups = append(groups, *byVal[v])
	}
	sort.Slice(groups, func(i, j int) bool {
		if groups[i].sum != groups[j].sum {
			return groups[i].sum > groups[j].sum
		}
		return groups[i].value < groups[j].value
	})
	if topN <= 0 || len(groups) <= topN {
		return groups, false
	}
	head := groups[:topN]
	other := labelGroup{value: readGroupOther}
	for _, g := range groups[topN:] {
		other.members = append(other.members, g.members...)
	}
	return append(head, other), true
}

// bucketSum은 bucketAvg의 합산 변형 — other 묶음용(§6.2 "other 합산").
func bucketSum(groups []*foldedSeries) (times []time.Time, values []float64) {
	sums := map[int64]float64{}
	for _, g := range groups {
		for u, v := range g.Buckets {
			sums[u] += v
		}
	}
	if len(sums) == 0 {
		return nil, nil
	}
	ts := make([]int64, 0, len(sums))
	for u := range sums {
		ts = append(ts, u)
	}
	sort.Slice(ts, func(i, j int) bool { return ts[i] < ts[j] })
	for _, u := range ts {
		times = append(times, time.Unix(u, 0).UTC())
		values = append(values, sums[u])
	}
	return times, values
}

// ── onset (§6.4) ──

// detectOnset은 첫 지속 이탈 구간을 찾는다: z≥3 첫 버킷 + 그 버킷 포함
// 연속 3버킷 중 ≥2가 z≥3(단발 스파이크 오인 방지). 반환은 시각이 아닌
// [마지막 정상 관측, 첫 이탈 관측] 구간 — 관측 갭만큼 정직하게 넓어진다.
func detectOnset(times []time.Time, values []float64, baseVals []float64, expected int, baselineThin bool) onsetResult {
	if len(values) == 0 {
		return onsetResult{Skipped: "창 내 관측 없음"}
	}
	if float64(len(values))/float64(expected) < scanEpisodic {
		return onsetResult{Skipped: fmt.Sprintf("간헐 방출(존재율 %.0f%% < 30%%) — 첫 표본을 발단으로 오인할 수 있어 onset 미산출",
			100*float64(len(values))/float64(expected))}
	}
	if len(baseVals) == 0 {
		return onsetResult{Skipped: "기준선 창 관측 없음 — 평소 수준을 몰라 이탈 판정 불가"}
	}
	medBase, denom := zDenom(baseVals, values)
	over := func(i int) bool { return math.Abs(values[i]-medBase)/denom >= scanZThreshold }
	conf := "high"
	if baselineThin {
		conf = "low"
	}
	for i := range values {
		if !over(i) {
			continue
		}
		// 지속 확인: i 포함 연속 3버킷(존재분) 중 ≥2가 이탈.
		cnt, n := 1, 1
		for j := i + 1; j <= i+2 && j < len(values); j++ {
			n++
			if over(j) {
				cnt++
			}
		}
		if n >= 2 && cnt < 2 {
			continue // 단발 스파이크 — 다음 후보로
		}
		if n == 1 {
			continue // 창 마지막 버킷 단독 — 지속 확인 불가, 보수적으로 미인정
		}
		if i == 0 {
			return onsetResult{OutsideWindow: true, Confidence: conf}
		}
		return onsetResult{IntervalStart: times[i-1], IntervalEnd: times[i], Confidence: conf}
	}
	return onsetResult{Skipped: "창 내 지속 이탈 없음(z<3)"}
}

// ── 다운샘플 (§6.2) ──

// downsampleBuckets는 cap 초과 버킷을 균등 병합한다 — gauge는 avg,
// counter(increase)는 합. 반환 bucketSec = 병합 후 버킷 폭.
func downsampleBuckets(times []time.Time, values []float64, cap int, sum bool) ([]time.Time, []float64, int) {
	if len(values) <= cap {
		return times, values, int(scanStep.Seconds())
	}
	g := (len(values) + cap - 1) / cap
	var outT []time.Time
	var outV []float64
	for i := 0; i < len(values); i += g {
		j := i + g
		if j > len(values) {
			j = len(values)
		}
		total := 0.0
		for _, v := range values[i:j] {
			total += v
		}
		if !sum {
			total /= float64(j - i)
		}
		outT = append(outT, times[i])
		outV = append(outV, total)
	}
	return outT, outV, g * int(scanStep.Seconds())
}

// ── 봉투 조립 ──

func readEnvelope(ctx context.Context, vm *VM, db *sql.DB, series []*readSeries, groupBy string,
	units map[string]string, from, to, baseFrom, baseTo time.Time,
	baseSamplesMax int, baselineThin bool, gradeMismatch int, groupTruncated bool, vtErr error) Envelope {

	var findings []Finding
	var refs []string
	anomalous := false
	name := func(s *readSeries) string {
		n := s.Target[:8] + "…/" + s.Metric
		if s.Group != "" {
			n += "{" + groupBy + "=" + s.Group + "}"
		}
		return n
	}
	for _, s := range series {
		ref := fmt.Sprintf("vm:%s{target_id=%s}:%s/%s", s.Metric, s.Target,
			from.UTC().Format(time.RFC3339), to.UTC().Format(time.RFC3339))
		refs = append(refs, ref)
		f := Finding{"series": name(s), "target_id": s.Target, "metric": s.Metric,
			"unit": units[s.Metric], "refs": []string{ref}}
		if s.Group != "" {
			f["group"] = map[string]string{groupBy: s.Group}
		}
		vals, times := s.Values, s.Times
		kind := "gauge"
		if s.IsCounter {
			vals = toIncreases(vals)
			if len(times) > 1 {
				times = times[1:]
			}
			kind = "increase_per_bucket"
		}
		if len(vals) == 0 {
			f["note"] = "창 내 관측 없음"
			findings = append(findings, f)
			continue
		}
		dsT, dsV, bucketSec := downsampleBuckets(times, vals, readShowBuckets, s.IsCounter)
		f["value_kind"] = kind
		f["bucket_seconds"] = bucketSec
		f["start"] = dsT[0].Format(time.RFC3339)
		f["values"] = dsV
		f["stats"] = map[string]any{"median": median(vals), "min": minOf(vals), "max": maxOf(vals)}
		if len(s.BaseVals) > 0 {
			b := s.BaseVals
			if s.IsCounter {
				b = toIncreases(b)
			}
			if len(b) > 0 {
				f["baseline_median"] = median(b)
			}
		}
		switch {
		case s.Onset.Skipped != "":
			f["onset"] = map[string]any{"skipped": s.Onset.Skipped}
		case s.Onset.OutsideWindow:
			anomalous = true
			f["onset"] = map[string]any{"outside_window": true, "confidence": s.Onset.Confidence,
				"note": "창 시작부터 이미 이탈 — 발단은 창 이전. 창을 앞으로 넓혀 재조회하라"}
		default:
			anomalous = true
			f["onset"] = map[string]any{
				"interval":   s.Onset.IntervalStart.Format(time.RFC3339) + "/" + s.Onset.IntervalEnd.Format(time.RFC3339),
				"method":     "sustained robust z>=3 (연속 3버킷 중 2)",
				"confidence": s.Onset.Confidence,
			}
		}
		// Q11 한도 부착 — 분해 없는 조회에서만(§6.3 v1).
		if groupBy == "" {
			if frag := attachCapacity(ctx, vm, db, s.Target, s.Metric, from, to); frag != nil {
				f["capacity"] = frag
			}
		}
		findings = append(findings, f)
	}

	// Q13 선후 타임라인 — onset 산출된 시계열이 2개 이상일 때(§6.4).
	type tlEntry struct {
		s *readSeries
	}
	var tl []tlEntry
	for _, s := range series {
		if s.Onset.Skipped == "" && !s.Onset.OutsideWindow {
			tl = append(tl, tlEntry{s})
		}
	}
	if len(tl) >= 2 {
		sort.Slice(tl, func(i, j int) bool {
			a, b := tl[i].s.Onset, tl[j].s.Onset
			if !a.IntervalStart.Equal(b.IntervalStart) {
				return a.IntervalStart.Before(b.IntervalStart)
			}
			return name(tl[i].s) < name(tl[j].s)
		})
		var lines []map[string]any
		for i, e := range tl {
			line := map[string]any{"order": i + 1, "series": name(e.s),
				"interval": e.s.Onset.IntervalStart.Format(time.RFC3339) + "/" + e.s.Onset.IntervalEnd.Format(time.RFC3339)}
			if i > 0 {
				prev := tl[i-1].s.Onset
				cur := e.s.Onset
				if cur.IntervalStart.Before(prev.IntervalEnd) {
					line["vs_prev"] = "indeterminate — 구간 겹침, 선후 판정 불가"
				} else {
					minGap := cur.IntervalStart.Sub(prev.IntervalEnd).Seconds()
					maxGap := cur.IntervalEnd.Sub(prev.IntervalStart).Seconds()
					line["vs_prev"] = fmt.Sprintf("이전 항목보다 %.0f~%.0f초 뒤", minGap, maxGap)
				}
			}
			lines = append(lines, line)
		}
		findings = append(findings, Finding{"class": "onset_timeline", "lines": lines,
			"note": "onset 구간 시작 순 정렬 — 겹침은 억지 선후를 만들지 않고 indeterminate"})
	}

	meta := Finding{"class": "read_meta",
		"baseline_window":      baseFrom.UTC().Format(time.RFC3339) + "/" + baseTo.UTC().Format(time.RFC3339),
		"baseline_samples_max": baseSamplesMax,
	}
	if baselineThin {
		meta["baseline_thin"] = true
		meta["baseline_note"] = "기준선 표본 빈약 — onset 확신도 low, 보수적으로 읽을 것"
	}
	if gradeMismatch > 0 {
		meta["grade_value_mismatch"] = gradeMismatch
	}
	if groupTruncated {
		meta["group_truncated"] = true
	}
	// 부분 강등(§15.2-3, 2b): counter 판별·단위·한도 짝의 PG 원천 실패 —
	// VM 관측·onset은 유지, 결손은 토큰만(scan_metrics counts와 동일 처방).
	if vtErr != nil {
		meta["source_errors"] = map[string]string{"pg": beDetail(pgErr(vtErr))}
		meta["value_type_fallback"] = "all_gauge"
		meta["value_type_note"] = "counter 판별 불가(PG 실패) — 전 시계열 gauge로 취급했다(value_kind=increase_per_bucket 미적용). " +
			"단위·한도 짝(capacity)도 같은 원천이라 함께 결손일 수 있다."
	}
	findings = append(findings, meta)

	status := "normal"
	if anomalous {
		status = "anomalous"
	}
	summary := fmt.Sprintf("시계열 %d개 정독 — onset 산출 %d개.", len(series), len(tl))
	if len(tl) >= 2 {
		summary += " 선후 타임라인은 onset_timeline 참조."
	}
	if baselineThin {
		summary += " 기준선 표본 빈약."
	}
	if vtErr != nil {
		summary += " counter 판별 결손(PG 실패) — 전 시계열 gauge 취급(read_meta.source_errors 참조)."
	}
	return Envelope{
		Status: status,
		AssessmentBasis: "onset = sustained robust z≥3(§5.2 척도 공유, 연속 3버킷 중 2), 구간 반환·겹침은 indeterminate. " +
			"counter는 30s 버킷 increase. 한도%는 검증된 짝 표에서만(§6.3 — 계산만, 판정 없음)",
		Summary:       summary,
		Findings:      findings,
		Refs:          refs,
		Truncated: groupTruncated,
		// 판별 결손은 의미 강등이다(D-3) — projector가 파생 레코드를 ConfLow로.
		DegradedSources: degradedIf(vtErr),
		ObservedRange:   &TimeRange{From: from.UTC(), To: to.UTC()},
	}
}

func minOf(vals []float64) float64 {
	m := vals[0]
	for _, v := range vals[1:] {
		if v < m {
			m = v
		}
	}
	return m
}

func maxOf(vals []float64) float64 {
	m := vals[0]
	for _, v := range vals[1:] {
		if v > m {
			m = v
		}
	}
	return m
}
