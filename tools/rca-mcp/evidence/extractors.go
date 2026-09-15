// 도구별 추출 — 봉투 finding 하나에서 Effect·Headline·품질 신호를 뽑는다.
//
// 도구마다 finding 구조가 다르므로 함수도 도구별이다. 공통으로 처리할 수 있는
// 것(사상표가 지목한 키 읽기)은 defaultExtract가 하고, 그 도구에서만 참인
// 사정(문자열 "미상", deviating 불리언, 기준선 상태 객체…)만 각 함수가 덮는다.
//
// **Headline은 기계 서술만이다**(§15.1-2) — 원문 직삽 금지. 원문 발췌는
// RawExcerpt 전용이며 sanitizeExcerpt를 지난다.
package evidence

import (
	"fmt"
	"strings"
	"time"
)

// extracted는 추출 결과다. 빈 필드는 "공통 규칙을 쓰라"는 뜻이다.
type extracted struct {
	Effect     Effect
	Headline   string
	RawExcerpt string
	SampleN    *int
	// Low — 이 finding 자체가 품질 강등을 부르는 사정을 싣고 있다
	// (기준선 빈약·부분 관측·표본 미달·간헐 방출 등).
	Low bool
	// EntityKey — 공통 조립(EntityCols)으로 만들 수 없을 때만.
	EntityKey string
	// ChangeFrom/To — 공통 규칙(OnsetFrom/To)으로 못 읽을 때만.
	ChangeFrom, ChangeTo *time.Time
}

type extractFn func(req ProjectRequest, row ClassRow, f RawFinding) (extracted, bool)

func extractorFor(src ObservationSource) extractFn {
	if fn, ok := extractors[src]; ok {
		return fn
	}
	return defaultExtract
}

var extractors = map[ObservationSource]extractFn{
	SrcScanMetrics:        extractScanMetrics,
	SrcReadTimeseries:     extractReadTimeseries,
	SrcListEvents:         extractListEvents,
	SrcSampleLogs:         extractSampleLogs,
	SrcDBSlowQueries:      extractDBSlowQueries,
	SrcDBBlocking:         extractDBBlocking,
	SrcBreakdownEndpoints: extractBreakdownEndpoints,
	SrcComparePeers:       extractComparePeers,
	SrcProcesses:          extractProcesses,
	SrcSNMPTraps:          extractSNMPTraps,
	SrcK8sState:           extractK8sState,
	SrcChange:             extractChange,
}

// ── 공통 ───────────────────────────────────────────────────────────

// baseEffect는 사상표 행이 지목한 키만 읽어 Effect를 만든다.
// Observed 키가 비어 있는 행은 개체 1건이 곧 관측이라 Observed=1이다
// (unpaired 경계 이벤트·grep 매치 줄·변경 1건 — 봉투에 세는 키가 없다).
func baseEffect(row ClassRow, f RawFinding) (Effect, bool) {
	var observed, baseline *float64
	if row.ObservedKey != "" {
		observed = f.nested(row.ObservedKey)
	} else if row.Kind != EffectCategorical {
		one := 1.0
		observed = &one
	}
	if row.BaselineKey != "" {
		baseline = f.nested(row.BaselineKey)
	}
	var toolMag *float64
	if row.MagnitudeKey != "" {
		toolMag = f.nested(row.MagnitudeKey)
	}
	mag, degraded := magnitudeOf(row.Kind, observed, baseline, toolMag)
	metric := row.MetricFixed
	if metric == "" {
		metric = f.str("metric")
	}
	return Effect{
		Kind: row.Kind, Metric: metric,
		Observed: observed, Baseline: baseline, Magnitude: mag,
		Direction: directionOf(observed, baseline),
	}, degraded
}

func defaultExtract(_ ProjectRequest, row ClassRow, f RawFinding) (extracted, bool) {
	eff, degraded := baseEffect(row, f)
	return extracted{Effect: eff, Low: degraded, Headline: genericHeadline(row, eff)}, true
}

// genericHeadline은 값이 있는 것만 붙이는 기계 서술이다.
func genericHeadline(row ClassRow, eff Effect) string {
	var b strings.Builder
	fmt.Fprintf(&b, "%s", row.FindingClass())
	if eff.Metric != "" {
		fmt.Fprintf(&b, " %s", eff.Metric)
	}
	if eff.Observed != nil {
		fmt.Fprintf(&b, " 관측 %s", num(*eff.Observed))
	}
	if eff.Baseline != nil {
		fmt.Fprintf(&b, " / 기준선 %s", num(*eff.Baseline))
	}
	if eff.Magnitude != nil && row.Kind == EffectRatio {
		fmt.Fprintf(&b, " (%s배)", num(*eff.Magnitude))
	}
	return b.String()
}

// num은 사람이 읽을 수치 표기다 — 정수는 정수로.
func num(v float64) string {
	if v == float64(int64(v)) && v < 1e15 && v > -1e15 {
		return fmt.Sprintf("%d", int64(v))
	}
	return fmt.Sprintf("%.3g", v)
}

func intPtr(v int) *int { return &v }

// ── scan_metrics ───────────────────────────────────────────────────

func extractScanMetrics(_ ProjectRequest, row ClassRow, f RawFinding) (extracted, bool) {
	eff, degraded := baseEffect(row, f)
	ex := extracted{Effect: eff, Low: degraded}
	metric := eff.Metric
	unit := f.str("unit")
	switch row.Class {
	case "shifted":
		// z는 표시 클램프라 Magnitude로 승계하지 않는다 — Headline 참고값으로만.
		z := ""
		if v := f.num("z"); v != nil {
			z = fmt.Sprintf(", z %s", num(*v))
			if f["z_capped"] == true {
				z += "(표시 캡)"
			}
		}
		ex.Headline = fmt.Sprintf("지표 %s 중앙값 %s→%s%s%s", metric,
			numOr(eff.Baseline), numOr(eff.Observed), unitSuffix(unit), z)
	case "appeared":
		kind := f.str("representative_kind")
		ex.Headline = fmt.Sprintf("지표 %s 기준선 창에 없다가 등장(대표값 %s %s, 지속률 %s)",
			metric, numOr(eff.Observed), kind, numOr(f.num("presence_ratio")))
		if f["episodic"] == true {
			ex.Low = true // 창 안 일부 버킷에만 반짝 — 지속 이상이 아닐 수 있다
		}
	case "disappeared":
		ex.Headline = fmt.Sprintf("지표 %s 소멸(%s) — 기준선 존재율 %s",
			metric, f.str("scope"), numOr(eff.Observed))
		if f["intermittent_baseline"] == true {
			ex.Low = true // 기준선에서도 간헐 방출 — 소멸 신호가 아닐 수 있다
		}
	}
	return ex, true
}

func numOr(v *float64) string {
	if v == nil {
		return "미상"
	}
	return num(*v)
}

func unitSuffix(u string) string {
	if u == "" {
		return ""
	}
	return " " + u
}

// ── read_timeseries ────────────────────────────────────────────────

func extractReadTimeseries(_ ProjectRequest, row ClassRow, f RawFinding) (extracted, bool) {
	eff, degraded := baseEffect(row, f)
	ex := extracted{Effect: eff, Low: degraded}
	if eff.Observed == nil {
		// 창 내 관측 없음(note만 있는 행) — 판정 축이 없으니 레코드도 없다.
		return ex, false
	}
	onset := f.sub("onset")
	desc := ""
	switch {
	case onset.str("skipped") != "":
		desc = "onset 미산출"
	case onset["outside_window"] == true:
		desc = "창 시작부터 이미 이탈(발단은 창 이전)"
		ex.Low = true
	case onset.str("interval") != "":
		desc = "onset " + onset.str("confidence")
	}
	ex.Headline = fmt.Sprintf("시계열 %s 중앙값 %s / 기준선 %s%s", eff.Metric,
		numOr(eff.Observed), numOr(eff.Baseline), sep(desc))
	return ex, true
}

func sep(s string) string {
	if s == "" {
		return ""
	}
	return " — " + s
}

// ── list_events ────────────────────────────────────────────────────

func extractListEvents(_ ProjectRequest, row ClassRow, f RawFinding) (extracted, bool) {
	eff, degraded := baseEffect(row, f)
	ex := extracted{Effect: eff, Low: degraded}
	switch row.Class {
	case "episode":
		boundary := f.str("boundary")
		// duration_ms는 미해소 에피소드에서 문자열 "미상"이다 — num이 nil을
		// 돌려주므로 Observed·Magnitude가 부재로 남는다(0으로 채우지 않는다).
		if eff.Observed == nil {
			ex.Low = true
		}
		ex.Headline = fmt.Sprintf("에피소드 %s/%s 지속 %sms, 경계 %s, peak %s",
			f.str("detector"), f.str("reason"), numOr(eff.Observed), boundary, f.str("peak_severity"))
		if boundary != "complete" {
			ex.Low = true
		}
	case "unpaired":
		ex.Headline = fmt.Sprintf("짝 없는 경계 이벤트 %s/%s(%s) at %s",
			f.str("detector"), f.str("reason"), f.str("severity"), f.str("at"))
	case "k8s":
		ex.Headline = fmt.Sprintf("K8s 이벤트 %s/%s %s %s회(%s)",
			f.str("namespace"), f.str("name"), f.str("reason"), numOr(eff.Observed), f.str("event_type"))
		if f.str("match_basis") != "" {
			ex.Low = true // 이름 매칭 귀속 — 이름 재사용 시 오귀속 가능
		}
	}
	return ex, true
}

// ── sample_logs ────────────────────────────────────────────────────

func extractSampleLogs(req ProjectRequest, row ClassRow, f RawFinding) (extracted, bool) {
	eff, degraded := baseEffect(row, f)
	ex := extracted{Effect: eff, Low: degraded}
	if row.Class == "grep" {
		// EntityKey `at+bh`의 bh는 finding 키가 아니라 ref의 마지막 조각이다.
		bh := ""
		if refs := f.refs(); len(refs) > 0 {
			parts := strings.Split(refs[0], ":")
			bh = parts[len(parts)-1]
		}
		ex.EntityKey = f.str("at") + "|" + bh
		ex.RawExcerpt = f.str("body") // 원문 — sanitize는 호출자
		ex.Headline = fmt.Sprintf("grep 매치 1줄 at %s(%s, 귀속 %s)",
			f.str("at"), f.str("severity"), f.str("matched_by"))
		return ex, true
	}
	// map 4구획 — 템플릿이 개체다. 템플릿 문자열·대표 원문 줄은 둘 다 원문이라
	// Headline에 넣지 않는다(§15.1-2).
	ex.RawExcerpt = f.str("sample_first_line")
	ex.Headline = fmt.Sprintf("로그 템플릿 %s 구획 %s — 창 내 %s회 / 기준선 %s회(%s)",
		f.str("template_id"), row.Class, numOr(eff.Observed), numOr(eff.Baseline), f.str("baseline_seen"))
	if f.str("baseline_seen") == "baseline_unknown" {
		ex.Low = true
	}
	_ = req
	return ex, true
}

// ── db_slow_queries ────────────────────────────────────────────────

func extractDBSlowQueries(_ ProjectRequest, row ClassRow, f RawFinding) (extracted, bool) {
	eff, degraded := baseEffect(row, f)
	ex := extracted{Effect: eff, Low: degraded}
	if v := f.num("polls_in_window"); v != nil {
		ex.SampleN = intPtr(int(*v))
	}
	// sql_text는 정규화가 보장되지 않는 원문이다(도구 주석: dict_captured는
	// "주장하지 않는다") — RawExcerpt로 격리한다.
	ex.RawExcerpt = f.str("sql_text")
	ex.Headline = fmt.Sprintf("느린 쿼리 %s(%s) p50 %sms / 기준선 %sms, 판정 %s",
		f.str("sql_key"), f.str("engine"), numOr(eff.Observed), numOr(eff.Baseline), f.str("verdict"))
	if f["partial_presence"] == true {
		ex.Low = true // 창 폴의 절반 미만에만 등장
	}
	if f.num("baseline_polls") == nil || *f.num("baseline_polls") == 0 {
		ex.Low = true // 기준선 폴 0 — 회귀 판정 불가
	}
	return ex, true
}

// ── db_blocking ────────────────────────────────────────────────────

func extractDBBlocking(req ProjectRequest, row ClassRow, f RawFinding) (extracted, bool) {
	eff, degraded := baseEffect(row, f)
	ex := extracted{Effect: eff, Low: degraded}
	if v := f.num("polls"); v != nil {
		ex.SampleN = intPtr(int(*v))
	}
	ex.Headline = fmt.Sprintf("블로킹 사건 %s %s~%s 대기 최대 %s세션(폴 %s, 루트이동 %v)",
		f.str("engine"), shortTime(f.str("first_seen")), shortTime(f.str("last_seen")),
		numOr(eff.Observed), numOr(f.num("polls")), f["root_migrated"] == true)
	if f.num("gap_polls") != nil {
		ex.Low = true // 사건 도중 폴 결손
	}
	_ = req
	return ex, true
}

// shortTime은 Headline용 짧은 시각 표기다(날짜 없는 HH:MM:SS).
func shortTime(s string) string {
	if t, ok := parseAt(s); ok {
		return t.Format("15:04:05")
	}
	return s
}

// ── breakdown_endpoints ────────────────────────────────────────────

func extractBreakdownEndpoints(_ ProjectRequest, row ClassRow, f RawFinding) (extracted, bool) {
	eff, degraded := baseEffect(row, f)
	// 실패율 행은 표본 0(count=0)에서 봉투에 failed_rate 자체가 없다 —
	// 그때는 레코드를 만들지 않는다(없는 관측을 0으로 만들지 않는다).
	if eff.Observed == nil {
		return extracted{}, false
	}
	ex := extracted{Effect: eff, Low: degraded}
	if v := f.num("count"); v != nil {
		ex.SampleN = intPtr(int(*v))
	}
	verdict := f.str("verdict")
	ex.Headline = fmt.Sprintf("진입점 %s/%s(%s) %s %s / 기준선 %s — %s",
		f.str("section"), f.str("label"), f.str("axis"),
		eff.Metric, numOr(eff.Observed), numOr(eff.Baseline), verdict)
	if verdict == "insufficient_sample" {
		ex.Low = true
	}
	if base := f.sub("baseline"); base != nil && base.str("state") != "" && base.str("state") != "ok" {
		ex.Low = true // 기준선 창 부재·빈약
	}
	return ex, true
}

// ── compare_peers ──────────────────────────────────────────────────

func extractComparePeers(_ ProjectRequest, row ClassRow, f RawFinding) (extracted, bool) {
	// 판정은 이탈 여부(0/1)다. **크기 비교는 도구가 금지 선언한 참고값**이라
	// Effect로 승계하지 않는다 — 하네스가 도구의 금지를 되살리는 경로 차단.
	dev := 0.0
	if f["deviating"] == true {
		dev = 1
	}
	eff := Effect{Kind: row.Kind, Metric: row.MetricFixed, Observed: &dev, Direction: DirNA}
	switch f.str("direction") {
	case "up":
		eff.Direction = DirUp
	case "down":
		eff.Direction = DirDn
	}
	ex := extracted{Effect: eff}
	if v := f.num("samples"); v != nil {
		ex.SampleN = intPtr(int(*v))
	}
	if row.Class == "self" && f["observed"] == false {
		// 자기 관측 자체가 없으면 판정 축이 없다.
		return ex, false
	}
	ex.Headline = fmt.Sprintf("또래 비교 %s %s — 자기 기준선 대비 이탈 %v(방향 %s)",
		row.Class, f.str("target_id"), f["deviating"] == true, eff.Direction)
	if f.str("excluded") != "" {
		ex.Low = true
	}
	return ex, true
}

// ── get_processes ──────────────────────────────────────────────────

func extractProcesses(_ ProjectRequest, row ClassRow, f RawFinding) (extracted, bool) {
	eff, degraded := baseEffect(row, f)
	ex := extracted{Effect: eff, Low: degraded}
	// avg는 게이지 평균이라 Kind=count로 사상할 수 없다 — Headline에만 싣는다.
	ex.Headline = fmt.Sprintf("프로세스 %s(pid %s) 점유 %s, 평균값 %s",
		f.str("process"), f.str("pid"), numOr(eff.Observed), numOr(f.num("avg")))
	return ex, true
}

// ── get_snmp_traps ─────────────────────────────────────────────────

func extractSNMPTraps(_ ProjectRequest, row ClassRow, f RawFinding) (extracted, bool) {
	eff, degraded := baseEffect(row, f)
	ex := extracted{Effect: eff, Low: degraded}
	ex.RawExcerpt = f.str("sample_varbinds") // 장비가 보낸 원문
	ex.Headline = fmt.Sprintf("SNMP trap %s(%s) %s건 %s~%s",
		f.str("trap_type"), f.str("trap_oid"), numOr(eff.Observed),
		shortTime(f.str("first_at")), shortTime(f.str("last_at")))
	return ex, true
}

// ── get_k8s_state ──────────────────────────────────────────────────

func extractK8sState(_ ProjectRequest, row ClassRow, f RawFinding) (extracted, bool) {
	eff, degraded := baseEffect(row, f)
	ex := extracted{Effect: eff, Low: degraded}
	// 상태 게이지 3종은 max_over_time>0의 불리언이고 pod 단위 합이라 값이
	// 2일 수 있다 — 판정은 "1"이 아니라 "0 초과"다(B-13).
	ex.Headline = fmt.Sprintf("K8s %s %s/%s 값 %s(%s)",
		row.Class, f.str("namespace"), f.str("pod"), numOr(eff.Observed), f.str("desc"))
	return ex, true
}

// ── change ([2] 합성 · list_changes) ───────────────────────────────

func extractChange(req ProjectRequest, row ClassRow, f RawFinding) (extracted, bool) {
	eff, _ := baseEffect(row, f)
	ex := extracted{Effect: eff}
	// 같은 change 행에 두 경로가 투영된다(§5.1 change 행): [2]의 합성 봉투와
	// 조사자 표면 list_changes. **키가 어긋난다** — 합성 쪽에는 target_id가
	// 있고 list_changes 쪽에는 없다(창 전역 조회라 대상은 필터가 아니라 정렬
	// 힌트다, listchanges.go). 두 경로가 같은 canonical 키를 갖도록 봉투의
	// target_id가 없으면 호출 대상으로 채운다.
	target := f.str("target_id")
	if target == "" {
		target = req.TargetID
	}
	ex.EntityKey = f.str("kind") + "|" + target + "|" + f.str("at")
	// Detail·title은 외부 입력이다(§15.1 taint 목록) — Headline이 아니라
	// RawExcerpt로 격리한다. 합성 봉투는 detail, list_changes는 title.
	ex.RawExcerpt = f.str("detail")
	if ex.RawExcerpt == "" {
		ex.RawExcerpt = f.str("title")
	}
	late := ""
	if f["late"] == true {
		late = " (첫 증상 이후)"
	}
	ex.Headline = fmt.Sprintf("변경 %s 대상 %s at %s%s", f.str("kind"), target, f.str("at"), late)
	// 접힌 변경(list_changes의 count>1)은 구간이 있다.
	if f.num("count") != nil && f.str("first_at") != "" {
		if a, ok := parseAt(f.str("first_at")); ok {
			ex.ChangeFrom = &a
		}
		if b, ok := parseAt(f.str("last_at")); ok {
			ex.ChangeTo = &b
		}
	}
	return ex, true
}
