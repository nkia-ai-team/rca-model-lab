// scan_metrics 판정식(§5) hermetic 단위 테스트 — VM 없이 classifyScan·
// robustZ·toIncreases를 겨눈다. 실측 검증(F04-R·F01-R)은 판정식 설계
// 단계에서 완료(스펙 §5.7) — 여기는 회귀 방지.
package tools

import (
	"encoding/json"
	"math"
	"testing"
	"time"
)

func window(from, to time.Time, series map[string][]float64) *scanWindow {
	w := &scanWindow{Series: series, From: from, To: to}
	for _, v := range series {
		if len(v) > w.Samples {
			w.Samples = len(v)
		}
	}
	return w
}

var (
	t0 = time.Date(2026, 7, 24, 6, 0, 0, 0, time.UTC)
	t1 = t0.Add(5 * time.Minute) // 현재 창 5분 = 기대 버킷 10(30s)
)

func flat(n int, v float64) []float64 {
	out := make([]float64, n)
	for i := range out {
		out[i] = v
	}
	return out
}

func TestClassifyScanFourClasses(t *testing.T) {
	base := window(t0.Add(-time.Hour), t0, map[string][]float64{
		"gone.metric":   flat(110, 5),        // disappeared (상시 방출이었음 — 110/120)
		"gone.hourly":   flat(2, 5),          // disappeared지만 간헐(2/120)
		"shift.metric":  flat(20, 100),       // shifted (cur 급등)
		"steady.metric": flat(20, 42),        // steady
		"noisy.bands":   flat(20, 1),         // 접힘
		"variant.raw":   flat(20, 1),         // 접힘
	})
	cur := window(t0, t1, map[string][]float64{
		"new.metric":    {7, 8},              // appeared, 2/10 버킷 = episodic
		"shift.metric":  flat(10, 900),
		"steady.metric": flat(10, 42.1),
		"noisy.bands":   flat(10, 99),
		"variant.raw":   flat(10, 99),
	})
	res := classifyScan(cur, base, nil)

	if len(res.Disappeared) != 2 {
		t.Fatalf("disappeared 이상: %v", res.Disappeared)
	}
	// 상시 방출이었다가 끊긴 것이 간헐보다 먼저(표시 캡 보호).
	if res.Disappeared[0].Metric != "gone.metric" || res.Disappeared[0].Intermittent {
		t.Fatalf("상시 소멸 판정 이상: %+v", res.Disappeared[0])
	}
	if res.Disappeared[1].Metric != "gone.hourly" || !res.Disappeared[1].Intermittent {
		t.Fatalf("간헐 태그 이상: %+v", res.Disappeared[1])
	}
	if res.TotalDisappearance {
		t.Fatal("일부만 끊겼는데 total로 판정")
	}
	if len(res.Appeared) != 1 {
		t.Fatalf("appeared 이상: %+v", res.Appeared)
	}
	a := res.Appeared[0]
	if a.Metric != "new.metric" || !a.Episodic || math.Abs(a.Presence-0.2) > 1e-9 {
		t.Fatalf("appeared 지속률·episodic 이상: %+v", a)
	}
	if a.Representative != 7.5 { // gauge → 중앙값
		t.Fatalf("appeared 대표값 이상: %v", a.Representative)
	}
	if len(res.Shifted) != 1 || res.Shifted[0].Metric != "shift.metric" {
		t.Fatalf("shifted 이상: %+v", res.Shifted)
	}
	if res.SteadyCount != 1 {
		t.Fatalf("steady 수 이상: %d", res.SteadyCount)
	}
}

func TestClassifyScanTotalDisappearance(t *testing.T) {
	base := window(t0.Add(-time.Hour), t0, map[string][]float64{
		"a.metric": flat(20, 1), "b.metric": flat(20, 2)})
	cur := window(t0, t1, map[string][]float64{})
	res := classifyScan(cur, base, nil)
	if !res.TotalDisappearance || len(res.Disappeared) != 2 {
		t.Fatalf("total 소멸 판정 이상: %+v", res)
	}
}

func TestClassifyScanCounterIncrease(t *testing.T) {
	vt := map[string]string{"req.total": "counter_int"}
	// 기준선: 버킷당 +10 씩 단조 증가(리셋 1회 포함 — 음수는 0 처리).
	baseVals := []float64{0, 10, 20, 30, 5, 15, 25, 35, 45, 55}
	// 현재: 버킷당 +1000 — raw 값 비교면 기준선과 큰 차이가 안 나게 시작값을 잇대지만
	// increase 비교면 10 vs 1000으로 크게 갈린다.
	curVals := []float64{60, 1060, 2060, 3060, 4060}
	base := window(t0.Add(-time.Hour), t0, map[string][]float64{"req.total": baseVals})
	cur := window(t0, t1, map[string][]float64{"req.total": curVals})

	res := classifyScan(cur, base, vt)
	if len(res.Shifted) != 1 || !res.Shifted[0].IncreaseCompared {
		t.Fatalf("counter shifted 이상: %+v", res)
	}
	if res.Shifted[0].CurMed != 1000 || res.Shifted[0].BaseMed != 10 {
		t.Fatalf("increase 중앙값 이상: %+v", res.Shifted[0])
	}
	// 표본 1개 counter는 increase 불가 → skipped로 정직 기록.
	cur2 := window(t0, t1, map[string][]float64{"req.total": {60}})
	res2 := classifyScan(cur2, base, vt)
	if res2.SkippedCount != 1 || len(res2.Shifted) != 0 {
		t.Fatalf("counter 표본 부족 처리 이상: %+v", res2)
	}
}

func TestToIncreasesReset(t *testing.T) {
	inc := toIncreases([]float64{10, 15, 3, 8})
	want := []float64{5, 0, 5}
	if len(inc) != len(want) {
		t.Fatalf("길이 이상: %v", inc)
	}
	for i := range want {
		if inc[i] != want[i] {
			t.Fatalf("리셋 처리 이상: %v", inc)
		}
	}
}

func TestRobustZFloors(t *testing.T) {
	// 평소 출렁임 0(MAD=0) 지표의 급등 — 분모 바닥값이 0-나눗셈/Inf를 막고
	// 유한한 큰 z를 준다(F01 마찰 (c)).
	z, _, _ := robustZ(flat(10, 200), flat(20, 100))
	if math.IsInf(z, 0) || math.IsNaN(z) {
		t.Fatalf("z가 유한하지 않음: %v", z)
	}
	if z < scanZThreshold {
		t.Fatalf("명백한 급등의 z가 문턱 미만: %v", z)
	}
	// 전부 0인 기준선(scale 바닥 1.0)도 폭발하지 않는다.
	z2, _, _ := robustZ(flat(10, 3), flat(20, 0))
	if math.IsInf(z2, 0) || math.IsNaN(z2) {
		t.Fatalf("0 기준선 z가 유한하지 않음: %v", z2)
	}
}

func TestShiftedSortUncapped(t *testing.T) {
	// 표시 캡(20)을 넘는 z 두 개 — 정렬은 uncapped라 큰 쪽이 1등이어야
	// 한다(§5.2 — 캡 동점 안 알파벳 순위 방지, F04 실측 14등→7등).
	base := window(t0.Add(-time.Hour), t0, map[string][]float64{
		"a.small.spike": flat(20, 10),
		"z.big.spike":   flat(20, 10),
	})
	cur := window(t0, t1, map[string][]float64{
		"a.small.spike": flat(10, 500),   // z ≈ 큰 값
		"z.big.spike":   flat(10, 50000), // z ≈ 훨씬 큰 값 — 이름은 뒤지만 1등이어야
	})
	res := classifyScan(cur, base, nil)
	if len(res.Shifted) != 2 || res.Shifted[0].Metric != "z.big.spike" {
		t.Fatalf("uncapped 정렬 이상: %+v", res.Shifted)
	}
}

func TestScanEnvelopeSerializable(t *testing.T) {
	base := window(t0.Add(-time.Hour), t0, map[string][]float64{
		"gone.metric": flat(20, 5), "shift.metric": flat(20, 0)})
	cur := window(t0, t1, map[string][]float64{
		"new.metric": {1}, "shift.metric": flat(10, 10)})
	res := classifyScan(cur, base, nil)
	env := scanEnvelope("00000000-0000-0000-0000-000000000000", cur, base, res, nil, -1, nil, nil)
	// 봉투는 도구 루프에서 그대로 JSON이 된다 — Inf/NaN 금지.
	if _, err := json.Marshal(env); err != nil {
		t.Fatalf("봉투 직렬화 실패: %v", err)
	}
	if env.Status != "anomalous" {
		t.Fatalf("status 이상: %s", env.Status)
	}
	if len(env.Refs) == 0 {
		t.Fatal("refs 없음 — 봉투의 모든 관측은 인용 가능해야 한다")
	}
	// 기준선 20표본 ≥ 문턱 10 — 빈약 표식이 붙으면 안 된다.
	counts := env.Findings[len(env.Findings)-1]
	if counts["class"] != "counts" {
		t.Fatalf("마지막 finding이 counts가 아님: %+v", counts)
	}
	if _, thin := counts["baseline_thin"]; thin {
		t.Fatal("기준선 정상인데 빈약 표식")
	}
}

func TestFoldScan(t *testing.T) {
	ts := []time.Time{t0, t0.Add(scanStep)}
	series := []VMSeries{
		// grade 복제 3벌 — 값 동일이면 불일치 0으로 접힘.
		{Labels: map[string]string{"__name__": "cpu.used", "grade": "1"}, Times: ts, Values: []float64{10, 20}},
		{Labels: map[string]string{"__name__": "cpu.used", "grade": "2"}, Times: ts, Values: []float64{10, 20}},
		{Labels: map[string]string{"__name__": "cpu.used", "grade": "3"}, Times: ts, Values: []float64{10, 20}},
		// 실차원(device) 2벌 — 지표당 버킷별 avg로 접힘.
		{Labels: map[string]string{"__name__": "disk.io", "device": "vda"}, Times: ts, Values: []float64{100, 200}},
		{Labels: map[string]string{"__name__": "disk.io", "device": "vdb"}, Times: ts, Values: []float64{300, 400}},
		// 라벨셋이 같은 다른 지표 — 절대 섞이면 안 된다(PromQL 집계의
		// __name__ 소실 함정 회귀 방지 — 라이브 실측 거짓 양성 153개).
		{Labels: map[string]string{"__name__": "disk.ops", "device": "vda"}, Times: ts, Values: []float64{1, 2}},
		// 이름 없는 시리즈는 스킵.
		{Labels: map[string]string{"device": "vda"}, Times: ts, Values: []float64{9, 9}},
	}
	w := foldScan(series)
	if w.GradeMismatch != 0 {
		t.Fatalf("동일 복제인데 불일치 계수: %d", w.GradeMismatch)
	}
	if got := w.Series["cpu.used"]; len(got) != 2 || got[0] != 10 || got[1] != 20 {
		t.Fatalf("grade 접기 이상: %v", got)
	}
	if got := w.Series["disk.io"]; len(got) != 2 || got[0] != 200 || got[1] != 300 {
		t.Fatalf("실차원 avg 이상: %v", got)
	}
	if got := w.Series["disk.ops"]; len(got) != 2 || got[0] != 1 {
		t.Fatalf("다른 지표가 섞임: %v", got)
	}
	if len(w.Series) != 3 {
		t.Fatalf("지표 수 이상(이름 없는 시리즈 유입?): %v", len(w.Series))
	}

	// grade 간 값 불일치 — 계수되고, max로 접힌다.
	series2 := []VMSeries{
		{Labels: map[string]string{"__name__": "m.x", "grade": "1"}, Times: ts[:1], Values: []float64{5}},
		{Labels: map[string]string{"__name__": "m.x", "grade": "2"}, Times: ts[:1], Values: []float64{8}},
	}
	w2 := foldScan(series2)
	if w2.GradeMismatch != 1 || w2.Series["m.x"][0] != 8 {
		t.Fatalf("불일치 계수·max 접기 이상: mismatch=%d vals=%v", w2.GradeMismatch, w2.Series["m.x"])
	}
}

func TestScanBaselineThin(t *testing.T) {
	base := window(t0.Add(-time.Hour), t0, map[string][]float64{"m.a": flat(3, 5)})
	cur := window(t0, t1, map[string][]float64{"m.a": flat(10, 5)})
	res := classifyScan(cur, base, nil)
	env := scanEnvelope("00000000-0000-0000-0000-000000000000", cur, base, res, nil, -1, nil, nil)
	counts := env.Findings[len(env.Findings)-1]
	if _, thin := counts["baseline_thin"]; !thin {
		t.Fatal("기준선 3표본인데 빈약 표식 없음")
	}
}
