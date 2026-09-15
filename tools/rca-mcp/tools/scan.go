// scan_metrics — 새 표면 Ⅲ-① 지표 발굴 (spec-tool-redesign §3.3-①,
// 판정식 §5). "이 대상, 지금 무엇이 이상한가"를 존재 4분류
// (disappeared/appeared/shifted/steady) + robust z로 답한다.
//
// discover_signals(구 표면)의 판정식 실패 2건(§5.1 — 인접 기준선 오염,
// 소멸 무보고)을 대체한다. 기준선은 인시던트 first_event 직전 고정 창
// (§5.3)이며 도구가 인시던트에 바인딩되므로 코드가 잡는다.
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

// §5.7 임시 수치 — F04-R 실측 주도 확정 + F01-R 교차 검증 유지.
// 운영 조정은 평가 주행 축적 후.
const (
	scanStep        = 30 * time.Second // 버킷
	scanZThreshold  = 3.0              // shifted 문턱
	scanShowCap     = 20               // shifted·appeared·disappeared 표시 상한
	scanZShowCap    = 20.0             // z 표시 캡 — 정렬은 uncapped(§5.2)
	scanEpisodic    = 0.30             // appeared 지속률 미만이면 episodic
	scanLookback    = 60 * time.Minute // 기준선 기본 lookback(§5.3)
	scanThinSamples = 10               // 기준선 버킷 미만이면 "빈약" 표식(임시)
)

// NewScanMetricsTool은 scan_metrics 도구를 만든다. firstEvent는 seed의
// 첫 증상 시각 — 기준선 기본 창 [firstEvent-60m, firstEvent)의 기준점.
// db(PG)는 counter 판별·단위 결합용, nil이면 전부 gauge로 다룬다.
func NewScanMetricsTool(vm *VM, db *sql.DB, firstEvent time.Time) llm.Tool {
	params, _ := json.Marshal(map[string]any{
		"type": "object",
		"properties": map[string]any{
			"target":        map[string]string{"type": "string", "description": "target_id(UUID)"},
			"from":          map[string]string{"type": "string", "description": "현재 창 시작 — UTC RFC3339, 반개구간 [from,to)"},
			"to":            map[string]string{"type": "string", "description": "현재 창 끝(제외)"},
			"baseline_from": map[string]string{"type": "string", "description": "기준선 창 override 시작(선택 — 기본은 인시던트 첫 증상 직전 60분. 탐지 지연이 커서 직전 60분도 이미 병들었다고 볼 때만)"},
			"baseline_to":   map[string]string{"type": "string", "description": "기준선 창 override 끝(선택, baseline_from과 함께)"},
		},
		"required": []string{"target", "from", "to"},
	})
	return llm.Tool{
		Name: "scan_metrics",
		Description: "대상의 전 지표를 기준선(인시던트 전 고정 창)과 대조해 존재 4분류로 준다 — " +
			"disappeared(사라짐)/appeared(새로 등장)/shifted(robust z 이상)/steady(건수). " +
			"뭐가 있는지 모르는 대상의 조사 시작점. 여기 나온 지표 이름으로 read_timeseries를 부른다.",
		Parameters: params,
		Call: func(ctx context.Context, args json.RawMessage) (any, error) {
			in, err := parseTargetWindow(args)
			if err != nil {
				return nil, err
			}
			var ex struct {
				BaselineFrom string `json:"baseline_from"`
				BaselineTo   string `json:"baseline_to"`
			}
			_ = json.Unmarshal(args, &ex)
			baseFrom, baseTo := firstEvent.Add(-scanLookback), firstEvent
			if ex.BaselineFrom != "" || ex.BaselineTo != "" {
				var e1, e2 error
				baseFrom, e1 = time.Parse(time.RFC3339, ex.BaselineFrom)
				baseTo, e2 = time.Parse(time.RFC3339, ex.BaselineTo)
				if e1 != nil || e2 != nil || !baseFrom.Before(baseTo) {
					return nil, fmt.Errorf("baseline override 오류: baseline_from·baseline_to 둘 다 UTC RFC3339, from < to 필요")
				}
			}
			return scanMetrics(ctx, vm, db, in.Target, in.FromT, in.ToT, baseFrom, baseTo)
		},
	}
}

// scanWindow는 한 창의 조회 결과다 — 지표별 버킷 값 + 실관측 범위.
type scanWindow struct {
	Series        map[string][]float64
	From, To      time.Time // 요청 창
	Observed      *TimeRange
	Samples       int // 지표당 최대 버킷 수(기준선 빈약 판정 재료)
	GradeMismatch int // grade 복제 가정 위반 (시리즈그룹,버킷) 수
}

func scanQueryWindow(ctx context.Context, vm *VM, target string, from, to time.Time) (*scanWindow, error) {
	// raw 시리즈로 받는다 — PromQL 집계(max without 등)는 __name__을
	// 버려서 라벨셋이 같은 서로 다른 지표(disk.io vs disk.operations)를
	// 합쳐버린다(실측: keep_metric_names 미지원, rollup만 이름 유지).
	// grade 접기·지표당 집계는 코드가 한다(D10 — 코드가 눈).
	expr := fmt.Sprintf(`avg_over_time({target_id=%q}[%ds])`, target, int(scanStep.Seconds()))
	series, err := vm.RangeQuery(ctx, expr, from, to, scanStep)
	if err != nil {
		return nil, fmt.Errorf("scan_metrics 창 조회: %w", err)
	}
	w := foldScan(series)
	w.From, w.To = from, to
	return w, nil
}

// foldScan은 raw 시리즈를 2단 접기로 지표당 버킷 시계열로 만든다(§5.6):
// ① grade 접기 = grade 제외 전 라벨이 같은 그룹의 버킷별 max — grade는
// 알람 등급별 의도 복제라 값이 같아야 하고(같은 버킷 값 불일치 =
// GradeMismatch로 정확 계수), avg는 복제 가정이 깨질 때 조용히 왜곡된다.
// ② 남는 실차원(device·process 등)은 지표당 버킷별 avg — 분해는
// read_timeseries의 분해 라벨 몫(§3.3 Q7 갈림).
func foldScan(series []VMSeries) *scanWindow {
	w := &scanWindow{Series: map[string][]float64{}}
	folded, mismatch := foldGrade(series)
	w.GradeMismatch = mismatch
	byName := map[string][]*foldedSeries{}
	var names []string
	for _, g := range folded {
		if byName[g.Name] == nil {
			names = append(names, g.Name)
		}
		byName[g.Name] = append(byName[g.Name], g)
	}
	sort.Strings(names)
	for _, name := range names {
		times, vals := bucketAvg(byName[name])
		w.Series[name] = vals
		if len(vals) > w.Samples {
			w.Samples = len(vals)
		}
		for _, ts := range times {
			if w.Observed == nil {
				w.Observed = &TimeRange{From: ts, To: ts}
			} else {
				if ts.Before(w.Observed.From) {
					w.Observed.From = ts
				}
				if ts.After(w.Observed.To) {
					w.Observed.To = ts
				}
			}
		}
	}
	return w
}

func scanMetrics(ctx context.Context, vm *VM, db *sql.DB, target string, curFrom, curTo, baseFrom, baseTo time.Time) (any, error) {
	cur, err := scanQueryWindow(ctx, vm, target, curFrom, curTo)
	if err != nil {
		return nil, err
	}
	base, err := scanQueryWindow(ctx, vm, target, baseFrom, baseTo)
	if err != nil {
		return nil, err
	}
	if len(cur.Series) == 0 && len(base.Series) == 0 {
		return Envelope{
			Status:       "no_data",
			NoDataReason: NoDataUnknown,
			Summary: "이 대상은 현재 창·기준선 창 모두 지표가 없음 — 메트릭 미수집 대상" +
				"(application 등 CH 원천 도메인)이거나 수집 결손. get_data_coverage로 판별하라.",
			ObservedRange: &TimeRange{From: curFrom.UTC(), To: curTo.UTC()},
		}, nil
	}

	names := make([]string, 0, len(cur.Series)+len(base.Series))
	seen := map[string]bool{}
	for n := range cur.Series {
		seen[n] = true
		names = append(names, n)
	}
	for n := range base.Series {
		if !seen[n] {
			names = append(names, n)
		}
	}
	sort.Strings(names)
	// pg=semantic_auxiliary(deps.go): 판별 실패 시 조회는 진행하되 전부
	// gauge 취급으로 폴백하고, 결손을 봉투에 분류 토큰으로 명시한다.
	vt, vtErr := metricValueTypes(ctx, db, names)
	units, _ := metricUnits(ctx, db, names) // 실패해도 진행(단위는 보조 정보 — vtErr와 같은 원천이라 결손 명시가 겸한다)

	res := classifyScan(cur, base, vt)

	// 소멸이 total이면 동시성 검사(§5.5-2): 같은 창 다른 대상들의 방출 여부.
	var concurrency Finding
	if res.TotalDisappearance {
		concurrency = scanConcurrency(ctx, vm, target, cur, base)
	}
	// grade 복제 가정 감시(§5.6): foldScan이 정확 계수 — 위반 시 경고, 판정은 계속.
	gradeMismatch := cur.GradeMismatch + base.GradeMismatch
	if gradeMismatch > 0 {
		log.Printf("tools: scan_metrics grade 복제 가정 위반 — target=%s 값 불일치 (그룹,버킷) %d건 (max로 접었으니 판정은 안전, 가정 재검토 필요)", target, gradeMismatch)
	}

	return scanEnvelope(target, cur, base, res, concurrency, gradeMismatch, units, vtErr), nil
}

// ── 판정식 (§5.2) — VM 무관 순수 함수. 단위 테스트가 여기를 겨눈다. ──

type scanShift struct {
	Metric           string
	Z                float64 // uncapped — 정렬용
	CurMed, BaseMed  float64
	IncreaseCompared bool // counter라 버킷 increase로 비교했음
}

type scanAppeared struct {
	Metric         string
	Presence       float64 // 현재 창 버킷 중 표본 비율
	Episodic       bool
	Representative float64 // gauge=중앙값, counter=창 내 증가 합(§5.7)
	IsCounter      bool
}

// scanGone은 disappeared 한 건이다. BaselinePresence가 낮으면 "소멸"이
// 아니라 원래 간헐 방출(시간당 1회 recording rule 등)일 개연성 — 라이브
// 실측에서 15분 창 대비 소멸 27건 전부가 lucida:*:1h류 간헐 지표였다.
type scanGone struct {
	Metric           string
	BaselinePresence float64 // 기준선 창 버킷 중 표본 비율
	Intermittent     bool    // 기준선 존재율 < scanEpisodic
}

type scanResult struct {
	Disappeared        []scanGone // 기준선 有, 현재 無
	Appeared           []scanAppeared
	Shifted            []scanShift // z 내림차순
	SteadyCount        int
	SkippedCount       int  // counter인데 표본<2라 increase 계산 불가
	TotalDisappearance bool // 현재 창에 지표가 하나도 없음(§5.5-1 total)
}

func classifyScan(cur, base *scanWindow, valueTypes map[string]string) scanResult {
	var res scanResult
	res.TotalDisappearance = len(cur.Series) == 0 && len(base.Series) > 0
	expected := int(cur.To.Sub(cur.From) / scanStep)
	if expected < 1 {
		expected = 1
	}
	expectedBase := int(base.To.Sub(base.From) / scanStep)
	if expectedBase < 1 {
		expectedBase = 1
	}
	names := map[string]bool{}
	for n := range cur.Series {
		names[n] = true
	}
	for n := range base.Series {
		names[n] = true
	}
	for n := range names {
		// .bands(등급 경계 562종)·.raw 변종은 인벤토리·판정 모두 접는다(§3.3 ①).
		if strings.Contains(n, ".bands") || strings.HasSuffix(n, ".raw") {
			continue
		}
		c, b := cur.Series[n], base.Series[n]
		isCounter := strings.HasPrefix(valueTypes[n], "counter")
		switch {
		case len(b) > 0 && len(c) == 0:
			p := float64(len(b)) / float64(expectedBase)
			res.Disappeared = append(res.Disappeared, scanGone{
				Metric: n, BaselinePresence: p, Intermittent: p < scanEpisodic})
		case len(c) > 0 && len(b) == 0:
			rep := median(c)
			if isCounter {
				rep = sum(toIncreases(c))
			}
			res.Appeared = append(res.Appeared, scanAppeared{
				Metric:         n,
				Presence:       float64(len(c)) / float64(expected),
				Episodic:       float64(len(c))/float64(expected) < scanEpisodic,
				Representative: rep,
				IsCounter:      isCounter,
			})
		case len(c) > 0 && len(b) > 0:
			if isCounter {
				c, b = toIncreases(c), toIncreases(b)
				if len(c) == 0 || len(b) == 0 {
					res.SkippedCount++
					continue
				}
			}
			z, mc, mb := robustZ(c, b)
			if z >= scanZThreshold {
				res.Shifted = append(res.Shifted, scanShift{
					Metric: n, Z: z, CurMed: mc, BaseMed: mb, IncreaseCompared: isCounter})
			} else {
				res.SteadyCount++
			}
		}
	}
	// 진짜 신호(상시 방출이었다가 끊김)가 먼저, 간헐은 뒤 — 표시 캡에
	// 간헐이 자리를 먹고 진짜 소멸이 잘리는 것 방지.
	sort.Slice(res.Disappeared, func(i, j int) bool {
		a, b := res.Disappeared[i], res.Disappeared[j]
		if a.Intermittent != b.Intermittent {
			return !a.Intermittent
		}
		return a.Metric < b.Metric
	})
	// appeared는 줄 세우지 않는다(§5.4 — 단위가 섞여 크기 비교 무의미).
	// 이름순은 순위가 아니라 결정론적 출력 순서다.
	sort.Slice(res.Appeared, func(i, j int) bool { return res.Appeared[i].Metric < res.Appeared[j].Metric })
	// shifted 정렬은 uncapped z(§5.2 — 캡 동점 안 알파벳 순위 방지), 동점만 이름.
	sort.Slice(res.Shifted, func(i, j int) bool {
		if res.Shifted[i].Z != res.Shifted[j].Z {
			return res.Shifted[i].Z > res.Shifted[j].Z
		}
		return res.Shifted[i].Metric < res.Shifted[j].Metric
	})
	return res
}

// robustZ = |med_cur − med_base| / max(1.4826·MAD_base, 5%·|med_base|,
// 1%·scale, ε). 분모 바닥값이 "평소 출렁임 0" 지표의 0-나눗셈 폭발
// (F01 마찰 (c))을 막는다.
func robustZ(curVals, baseVals []float64) (z, medCur, medBase float64) {
	medBase, denom := zDenom(baseVals, curVals)
	medCur = median(curVals)
	return math.Abs(medCur-medBase) / denom, medCur, medBase
}

// zDenom은 §5.2 분모(기준선 median/MAD + 바닥값 3종)를 계산한다 —
// scan의 창 대 창 z와 read의 버킷별 z(onset, §6.4)가 같은 척도를 쓴다.
func zDenom(baseVals, curVals []float64) (medBase, denom float64) {
	medBase = median(baseVals)
	devs := make([]float64, len(baseVals))
	for i, v := range baseVals {
		devs[i] = math.Abs(v - medBase)
	}
	mad := median(devs)
	scale := 0.0
	for _, v := range baseVals {
		if a := math.Abs(v); a > scale {
			scale = a
		}
	}
	for _, v := range curVals {
		if a := math.Abs(v); a > scale {
			scale = a
		}
	}
	if scale == 0 {
		scale = 1.0
	}
	denom = math.Max(math.Max(1.4826*mad, 0.05*math.Abs(medBase)), math.Max(0.01*scale, 1e-9))
	return medBase, denom
}

// toIncreases는 counter 버킷값을 인접 증가분으로 바꾼다(리셋=음수는 0).
func toIncreases(vals []float64) []float64 {
	if len(vals) < 2 {
		return nil
	}
	inc := make([]float64, 0, len(vals)-1)
	for i := 1; i < len(vals); i++ {
		d := vals[i] - vals[i-1]
		if d < 0 {
			d = 0
		}
		inc = append(inc, d)
	}
	return inc
}

func median(vals []float64) float64 {
	if len(vals) == 0 {
		return 0
	}
	s := append([]float64{}, vals...)
	sort.Float64s(s)
	if n := len(s); n%2 == 1 {
		return s[n/2]
	} else {
		return (s[n/2-1] + s[n/2]) / 2
	}
}

func sum(vals []float64) float64 {
	t := 0.0
	for _, v := range vals {
		t += v
	}
	return t
}

// ── 부속 조회 2건 — 실패해도 판정은 계속(보조 정보) ──

// scanConcurrency는 total 소멸의 동시성 검사다(§5.5-2): 현재 창에서
// 방출 중인 대상 집합 vs 기준선 창의 집합 → isolated/widespread.
func scanConcurrency(ctx context.Context, vm *VM, target string, cur, base *scanWindow) Finding {
	emitters := func(at time.Time, from time.Time) (map[string]bool, error) {
		w := windowSeconds(from, at)
		samples, err := vm.InstantQuery(ctx,
			fmt.Sprintf(`count by(target_id)(count_over_time({__name__!=""}[%s]))`, w), at.Add(-time.Second))
		if err != nil {
			return nil, err
		}
		set := map[string]bool{}
		for _, s := range samples {
			set[s.Labels["target_id"]] = true
		}
		return set, nil
	}
	curSet, err1 := emitters(cur.To, cur.From)
	baseSet, err2 := emitters(base.To, base.From)
	if err1 != nil || err2 != nil {
		return Finding{"class": "disappearance_concurrency", "verdict": "unknown",
			"note": "동시성 검사 조회 실패 — isolated/widespread 판별 불가"}
	}
	var missing []string
	for t := range baseSet {
		if !curSet[t] {
			missing = append(missing, t)
		}
	}
	sort.Strings(missing)
	verdict := "widespread"
	note := "다수 대상 동시 전멸 — 수집 결손 의심, 배제 근거로 쓰지 말 것"
	if len(missing) <= 1 {
		verdict = "isolated"
		note = "이 대상만 방출 중단 — 대상 측 사건(정지) 개연성. 최종 판별은 list_events(pod Killing 등)·sample_logs로"
	}
	if len(missing) > scanShowCap {
		missing = missing[:scanShowCap]
	}
	return Finding{"class": "disappearance_concurrency", "verdict": verdict, "note": note,
		"emitting_targets_baseline": len(baseSet), "emitting_targets_current": len(curSet),
		"silent_targets": missing}
}

// ── 봉투 조립 ──

func scanEnvelope(target string, cur, base *scanWindow, res scanResult,
	concurrency Finding, gradeMismatch int, units map[string]string, vtErr error) Envelope {

	ref := func(name string, w *scanWindow) string {
		return fmt.Sprintf("vm:%s{target_id=%s}:%s/%s", name, target,
			w.From.UTC().Format(time.RFC3339), w.To.UTC().Format(time.RFC3339))
	}
	var findings []Finding
	var refs []string

	shown := res.Shifted
	if len(shown) > scanShowCap {
		shown = shown[:scanShowCap]
	}
	for i, s := range shown {
		r := ref(s.Metric, cur)
		refs = append(refs, r)
		f := Finding{"class": "shifted", "rank": i + 1, "metric": s.Metric,
			"unit": units[s.Metric], "z": math.Min(s.Z, scanZShowCap),
			"current_median": s.CurMed, "baseline_median": s.BaseMed,
			"refs": []string{r}}
		if s.Z > scanZShowCap {
			f["z_capped"] = true // 표시만 캡 — 순서는 uncapped z(§5.2)
		}
		if s.IncreaseCompared {
			f["note"] = "counter — 30s 버킷 증가분(increase) 비교"
		}
		findings = append(findings, f)
	}

	appearedShown := res.Appeared
	if len(appearedShown) > scanShowCap {
		appearedShown = appearedShown[:scanShowCap]
	}
	for _, a := range appearedShown {
		r := ref(a.Metric, cur)
		refs = append(refs, r)
		f := Finding{"class": "appeared", "metric": a.Metric, "unit": units[a.Metric],
			"presence_ratio": a.Presence, "representative": a.Representative,
			"refs": []string{r},
			"note": "기준선 창에 없다가 등장 — 순위 없음(단위가 섞여 크기 비교 무의미)"}
		if a.IsCounter {
			f["representative_kind"] = "window_increase"
		} else {
			f["representative_kind"] = "median"
		}
		if a.Episodic {
			f["episodic"] = true
			f["note"] = "창 안 일부 버킷에만 반짝(지속률 낮음) — 지속 이상이 아니라 '사건이 일어난 흔적'(재조인·재시작류)일 수 있음"
		}
		findings = append(findings, f)
	}

	scope := "partial"
	scopeNote := "일부 계열만 끊기고 나머지 지표는 계속 — 같은 에이전트에서 일부만 끊길 수집 사유는 없으므로 해당 컴포넌트 멈춤의 강한 신호"
	if res.TotalDisappearance {
		scope = "total"
		scopeNote = "이 대상의 전 지표가 끊김 — 대상 정지 vs 수집 결손은 여기서 단정 안 함, disappearance_concurrency 참조"
	}
	disappearedShown := res.Disappeared
	if len(disappearedShown) > scanShowCap {
		disappearedShown = disappearedShown[:scanShowCap]
	}
	for _, g := range disappearedShown {
		r := ref(g.Metric, base)
		refs = append(refs, r)
		f := Finding{"class": "disappeared", "metric": g.Metric,
			"unit": units[g.Metric], "scope": scope,
			"baseline_presence_ratio": g.BaselinePresence,
			"note": scopeNote, "refs": []string{r}}
		if g.Intermittent {
			f["intermittent_baseline"] = true
			f["note"] = "기준선에서도 간헐 방출(주기 물질화 지표 등) — 현재 창에 없는 게 소멸 신호가 아닐 수 있음, 소멸 근거로 쓰려면 read_timeseries로 방출 주기 확인"
		}
		findings = append(findings, f)
	}
	if concurrency != nil {
		findings = append(findings, concurrency)
	}
	intermittent := 0
	for _, g := range res.Disappeared {
		if g.Intermittent {
			intermittent++
		}
	}

	// 훑기 자체의 조회면 ref — 전부 steady인 normal 봉투도 "이상 없음"
	// 관측으로 인용 가능해야 한다.
	scanRef := fmt.Sprintf("vm:scan{target_id=%s}:%s/%s", target,
		cur.From.UTC().Format(time.RFC3339), cur.To.UTC().Format(time.RFC3339))
	refs = append(refs, scanRef)
	counts := Finding{"class": "counts", "refs": []string{scanRef},
		"disappeared": len(res.Disappeared), "disappeared_intermittent": intermittent,
		"appeared": len(res.Appeared),
		"shifted": len(res.Shifted), "steady": res.SteadyCount,
		"baseline_window": fmt.Sprintf("%s/%s", base.From.UTC().Format(time.RFC3339), base.To.UTC().Format(time.RFC3339)),
		"baseline_samples_max": base.Samples,
		"aggregation":          "grade 접기(max) 후 지표당 실차원 avg 집계 — 차원 분해는 read_timeseries의 분해 라벨로",
	}
	if base.Observed != nil {
		counts["baseline_observed"] = fmt.Sprintf("%s/%s",
			base.Observed.From.Format(time.RFC3339), base.Observed.To.Format(time.RFC3339))
	}
	baselineThin := base.Samples < scanThinSamples
	if baselineThin {
		counts["baseline_thin"] = true
		counts["baseline_note"] = "기준선 표본 빈약 — 분류(특히 appeared/shifted)를 보수적으로 읽을 것"
	}
	if res.SkippedCount > 0 {
		counts["skipped_counters"] = res.SkippedCount
	}
	if gradeMismatch > 0 {
		counts["grade_value_mismatch_series"] = gradeMismatch
	}
	// 부분 강등(§15.2-3, 2b): counter 판별 원천(PG) 실패 — VM 관측은
	// 유지하되 결손을 분류 토큰으로 명시한다(원문 금지 §15.3-2). 판단
	// 지점: 전 지표 low 강등은 하지 않는다 — 어느 지표가 counter 후보였는지
	// 자체가 죽은 판별의 몫이라 특정 불가이고, 대부분은 gauge라 전면
	// 강등이 과잉이다. 대신 판별 의존 소비 지점(shifted의 increase 비교·
	// appeared 대표값)을 문구로 지목한다.
	if vtErr != nil {
		counts["source_errors"] = map[string]string{"pg": beDetail(pgErr(vtErr))}
		counts["value_type_fallback"] = "all_gauge"
		counts["value_type_note"] = "counter 판별 불가(PG 실패) — 전 지표 gauge로 취급했다. " +
			"counter 지표라면 shifted 비교가 increase가 아니라 원시값이고 appeared 대표값이 합이 아니라 중앙값이다 — rate 해석 주의."
	}
	findings = append(findings, counts)

	status := "normal"
	if len(res.Disappeared) > 0 || len(res.Appeared) > 0 || len(res.Shifted) > 0 {
		status = "anomalous"
	}
	truncated := len(res.Shifted) > scanShowCap || len(res.Appeared) > scanShowCap ||
		len(res.Disappeared) > scanShowCap

	summary := fmt.Sprintf("존재 4분류: disappeared %d(간헐 방출 %d) / appeared %d / shifted(z≥%.0f) %d / steady %d.",
		len(res.Disappeared), intermittent, len(res.Appeared), scanZThreshold, len(res.Shifted), res.SteadyCount)
	if truncated {
		summary += fmt.Sprintf(" 각 분류 표시 상한 %d(truncated).", scanShowCap)
	}
	if baselineThin {
		summary += " 기준선 표본 빈약 — 보수적으로 읽을 것."
	}
	if vtErr != nil {
		summary += " counter 판별 결손(PG 실패) — 전 지표 gauge 취급, rate 해석 주의(counts.source_errors 참조)."
	}
	return Envelope{
		Status: status,
		AssessmentBasis: fmt.Sprintf(
			"존재 4분류 + robust z(median/MAD, 분모 바닥값, 문턱 %.0f, 정렬 uncapped·표시 캡 %.0f), counter는 %.0fs 버킷 increase 비교, 기준선 = 인시던트 첫 증상 직전 %.0f분(override 시 인자 창)",
			scanZThreshold, scanZShowCap, scanStep.Seconds(), scanLookback.Minutes()),
		Summary:       summary,
		Findings:      findings,
		Refs:          refs,
		Truncated:     truncated,
		// 판별 결손은 의미 강등이다(D-3) — projector가 파생 레코드를 ConfLow로.
		DegradedSources: degradedIf(vtErr),
		ObservedRange:   &TimeRange{From: cur.From.UTC(), To: cur.To.UTC()},
		// 구획별 절단(§5.1 계약 2) — counts finding에 총수가 이미 있지만
		// 그것은 사람이 읽는 요약이고, projector가 읽는 것은 이 구조다.
		// steady는 구획 총수만 있고 표시 자체를 하지 않으므로 반환 0이다.
		Scopes: []QueryScope{
			qscope("shifted", len(res.Shifted), len(shown)),
			qscope("appeared", len(res.Appeared), len(appearedShown)),
			qscope("disappeared", len(res.Disappeared), len(disappearedShown)),
		},
	}
}

// metricValueTypes는 PG metric_definitions에서 값 유형(counter 판별)을
// 가져온다. db nil = 판별 원천을 아예 안 단 구성(오류 아님, 전부 gauge).
// 조회 실패는 오류로 돌려준다(§15.2-3, 2b) — 종전엔 빈 맵으로 삼켜
// "PG 죽음"과 "정의 없음"이 구분 불능이었다(조용한 결손 실물, 2b 수리).
// 소비자별 처분: scan·read는 pg=semantic_auxiliary라 진행하되 봉투에
// 결손을 명시하고, compare_peers는 pg=required라 전파한다.
func metricValueTypes(ctx context.Context, db *sql.DB, names []string) (map[string]string, error) {
	vt := map[string]string{}
	if db == nil {
		return vt, nil
	}
	rows, err := db.QueryContext(ctx, `SELECT metric_key, coalesce(value_type, '') FROM metric_definitions
		WHERE metric_key = ANY($1)`, names)
	if err != nil {
		return vt, err
	}
	defer rows.Close()
	for rows.Next() {
		var k, v string
		if err := rows.Scan(&k, &v); err != nil {
			return vt, err
		}
		vt[k] = v
	}
	return vt, rows.Err()
}
