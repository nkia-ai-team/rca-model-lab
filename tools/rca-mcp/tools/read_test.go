// read_timeseries(§6) hermetic 단위 테스트 — VM 없이 순수 함수를 겨눈다:
// parseFlexTime·downsampleBuckets·detectOnset·splitByLabel·joinCapacity·
// 타임라인 조립. 실측 대조는 케이스 검증(스모크·F01-R)이 담당.
package tools

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"
)

func TestParseFlexTime(t *testing.T) {
	now := time.Date(2026, 7, 27, 12, 0, 0, 0, time.UTC)
	if v, err := parseFlexTime("now", now); err != nil || !v.Equal(now) {
		t.Fatalf("now 파싱 이상: %v %v", v, err)
	}
	if v, err := parseFlexTime("now-15m", now); err != nil || !v.Equal(now.Add(-15*time.Minute)) {
		t.Fatalf("now-15m 파싱 이상: %v %v", v, err)
	}
	if v, err := parseFlexTime("2026-07-21T06:41:46Z", now); err != nil || v.Hour() != 6 {
		t.Fatalf("RFC3339 파싱 이상: %v %v", v, err)
	}
	if _, err := parseFlexTime("어제쯤", now); err == nil {
		t.Fatal("잘못된 표기가 통과")
	}
}

func mkTimes(n int, start time.Time) []time.Time {
	out := make([]time.Time, n)
	for i := range out {
		out[i] = start.Add(time.Duration(i) * scanStep)
	}
	return out
}

func TestDownsampleBuckets(t *testing.T) {
	start := time.Date(2026, 7, 27, 6, 0, 0, 0, time.UTC)
	// 상한 이하 — 그대로.
	ts, vs, sec := downsampleBuckets(mkTimes(20, start), flat(20, 5), 40, false)
	if len(vs) != 20 || sec != 30 {
		t.Fatalf("상한 이하 변형됨: %d개 %ds", len(vs), sec)
	}
	// 120버킷 → 40으로 병합(gauge avg).
	ts, vs, sec = downsampleBuckets(mkTimes(120, start), flat(120, 6), 40, false)
	if len(vs) != 40 || sec != 90 || vs[0] != 6 {
		t.Fatalf("gauge 병합 이상: %d개 %ds v0=%v", len(vs), sec, vs[0])
	}
	if !ts[1].Equal(start.Add(90 * time.Second)) {
		t.Fatalf("병합 버킷 시각 이상: %v", ts[1])
	}
	// counter(increase)는 합.
	_, vs, _ = downsampleBuckets(mkTimes(120, start), flat(120, 2), 40, true)
	if vs[0] != 6 { // 3버킷 × 2
		t.Fatalf("counter 병합 이상: v0=%v", vs[0])
	}
}

func TestDetectOnset(t *testing.T) {
	start := time.Date(2026, 7, 27, 6, 0, 0, 0, time.UTC)
	base := flat(60, 10)

	// 평탄 → 20번째 버킷부터 지속 급등: onset 구간 = [19번째, 20번째].
	vals := append(flat(20, 10), flat(10, 500)...)
	r := detectOnset(mkTimes(30, start), vals, base, 30, false)
	if r.Skipped != "" || r.OutsideWindow {
		t.Fatalf("정상 onset 미산출: %+v", r)
	}
	if !r.IntervalStart.Equal(start.Add(19*scanStep)) || !r.IntervalEnd.Equal(start.Add(20*scanStep)) {
		t.Fatalf("onset 구간 이상: %v/%v", r.IntervalStart, r.IntervalEnd)
	}
	if r.Confidence != "high" {
		t.Fatalf("확신도 이상: %s", r.Confidence)
	}

	// 단발 스파이크(1버킷)는 onset 아님.
	spike := append(append(flat(10, 10), 500), flat(19, 10)...)
	if r := detectOnset(mkTimes(30, start), spike, base, 30, false); r.Skipped == "" {
		t.Fatalf("단발 스파이크가 onset으로: %+v", r)
	}

	// 창 시작부터 이탈 — onset은 창 밖.
	if r := detectOnset(mkTimes(30, start), flat(30, 500), base, 30, false); !r.OutsideWindow {
		t.Fatalf("창 밖 판정 실패: %+v", r)
	}

	// 간헐 방출(존재율 낮음) — 미산출 + 사유.
	if r := detectOnset(mkTimes(3, start), flat(3, 500), base, 30, false); !strings.Contains(r.Skipped, "간헐") {
		t.Fatalf("간헐 차단 실패: %+v", r)
	}

	// 기준선 없음 — 미산출.
	if r := detectOnset(mkTimes(30, start), vals, nil, 30, false); !strings.Contains(r.Skipped, "기준선") {
		t.Fatalf("기준선 부재 처리 이상: %+v", r)
	}

	// 기준선 빈약 — 산출하되 low.
	if r := detectOnset(mkTimes(30, start), vals, base, 30, true); r.Confidence != "low" {
		t.Fatalf("빈약 확신도 이상: %+v", r)
	}
}

func fs(name string, labels map[string]string, vals map[int64]float64) *foldedSeries {
	return &foldedSeries{Name: name, Labels: labels, Buckets: vals}
}

func TestJoinCapacity(t *testing.T) {
	pair := capacityPair{Usage: "u", Limit: "l", JoinLabels: []string{"pool"}, Unit: "connections"}
	usage := []*foldedSeries{
		fs("u", map[string]string{"pool": "A"}, map[int64]float64{100: 8, 130: 10}), // last=10
		fs("u", map[string]string{"pool": "B"}, map[int64]float64{130: 2}),
		fs("u", map[string]string{"pool": "C"}, map[int64]float64{130: 5}), // 한도 없음
	}
	limit := []*foldedSeries{
		fs("l", map[string]string{"pool": "A"}, map[int64]float64{130: 10}),
		fs("l", map[string]string{"pool": "B"}, map[int64]float64{130: 20}),
	}
	matches, unmatched := joinCapacity(usage, limit, pair)
	if len(matches) != 2 || unmatched != 1 {
		t.Fatalf("조인 이상: %+v unmatched=%d", matches, unmatched)
	}
	// % 내림차순: A=100%, B=10%.
	if matches[0].MatchedOn != "pool=A" || matches[0].Percent != 100 {
		t.Fatalf("1위 이상: %+v", matches[0])
	}

	// 잉여 라벨 합산(postgresql: 조인 라벨 없음, db_name 상이).
	pair2 := capacityPair{Usage: "u", Limit: "l", Unit: "count"}
	usage2 := []*foldedSeries{
		fs("u", map[string]string{"db_name": "lucida"}, map[int64]float64{130: 116}),
		fs("u", map[string]string{"db_name": "postgres"}, map[int64]float64{130: 4}),
	}
	limit2 := []*foldedSeries{fs("l", map[string]string{}, map[int64]float64{130: 200})}
	m2, _ := joinCapacity(usage2, limit2, pair2)
	if len(m2) != 1 || m2[0].Percent != 60 || m2[0].UsageValue != 120 {
		t.Fatalf("합산 조인 이상: %+v", m2)
	}

	// 한도 0 — 비율 안 만듦.
	limit3 := []*foldedSeries{fs("l", map[string]string{"pool": "A"}, map[int64]float64{130: 0})}
	if m3, un3 := joinCapacity(usage[:1], limit3, pair); len(m3) != 0 || un3 != 1 {
		t.Fatalf("한도 0 처리 이상: %+v %d", m3, un3)
	}
}

func TestSplitByLabel(t *testing.T) {
	var folded []*foldedSeries
	for i, v := range []float64{100, 90, 80, 70, 60, 50, 40} {
		folded = append(folded, fs("m", map[string]string{"pod": string(rune('a' + i))},
			map[int64]float64{100: v}))
	}
	groups, truncated := splitByLabel(folded, "pod", 5)
	if !truncated || len(groups) != 6 {
		t.Fatalf("top5+other 이상: %d개 truncated=%v", len(groups), truncated)
	}
	if groups[0].value != "a" || groups[5].value != readGroupOther || len(groups[5].members) != 2 {
		t.Fatalf("정렬·other 이상: %+v", groups)
	}
}

func TestReadEnvelopeTimeline(t *testing.T) {
	start := time.Date(2026, 7, 27, 6, 0, 0, 0, time.UTC)
	mk := func(target, metric string, onset onsetResult) *readSeries {
		return &readSeries{Target: target, Metric: metric,
			Times: mkTimes(10, start), Values: flat(10, 1), BaseVals: flat(20, 1), Onset: onset}
	}
	uuidA := "aaaaaaaa-0000-4000-8000-000000000000"
	uuidB := "bbbbbbbb-0000-4000-8000-000000000000"
	series := []*readSeries{
		// B가 A보다 늦게 시작(비겹침) → "N~M초 뒤".
		mk(uuidB, "m.late", onsetResult{IntervalStart: start.Add(120 * time.Second),
			IntervalEnd: start.Add(150 * time.Second), Confidence: "high"}),
		mk(uuidA, "m.early", onsetResult{IntervalStart: start,
			IntervalEnd: start.Add(30 * time.Second), Confidence: "high"}),
		// 겹침 → indeterminate.
		mk(uuidA, "m.overlap", onsetResult{IntervalStart: start.Add(140 * time.Second),
			IntervalEnd: start.Add(170 * time.Second), Confidence: "high"}),
		// onset 미산출 — 타임라인 제외.
		mk(uuidB, "m.none", onsetResult{Skipped: "창 내 지속 이탈 없음(z<3)"}),
	}
	env := readEnvelope(context.Background(), nil, nil, series, "", nil,
		start, start.Add(5*time.Minute), start.Add(-time.Hour), start, 20, false, 0, false, nil)
	if _, err := json.Marshal(env); err != nil {
		t.Fatalf("봉투 직렬화 실패: %v", err)
	}
	var tl Finding
	for _, f := range env.Findings {
		if f["class"] == "onset_timeline" {
			tl = f
		}
	}
	if tl == nil {
		t.Fatal("타임라인 없음")
	}
	lines := tl["lines"].([]map[string]any)
	if len(lines) != 3 {
		t.Fatalf("타임라인 항목 수 이상: %d", len(lines))
	}
	if s, _ := lines[0]["series"].(string); !strings.Contains(s, "m.early") {
		t.Fatalf("정렬 이상: %+v", lines[0])
	}
	if v, _ := lines[1]["vs_prev"].(string); !strings.Contains(v, "90~150초 뒤") {
		t.Fatalf("시차 계산 이상: %+v", lines[1])
	}
	if v, _ := lines[2]["vs_prev"].(string); !strings.Contains(v, "indeterminate") {
		t.Fatalf("겹침 처리 이상: %+v", lines[2])
	}
	if env.Status != "anomalous" {
		t.Fatalf("status 이상: %s", env.Status)
	}
}
