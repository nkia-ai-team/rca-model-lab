// db_slow_queries 단위 가드(§14). 원천 없이 순수 부품만 시험한다 —
// 2단 접기·판정 두 팔·chronic·legacy 강등·본문 절단·자기관측 표식·
// 플랜 안정성 등급·정렬.
package tools

import (
	"testing"
	"time"
)

func sqTS(min int) time.Time {
	return time.Date(2026, 7, 27, 9, min, 0, 0, time.UTC)
}

func sqTestRow(min int, key, mode string, lat, execs float64) sqRow {
	return sqRow{ts: sqTS(min), sqlKey: key, mode: mode, lat: lat, execs: execs,
		io: map[string]float64{}, scope: map[string]string{}}
}

// 1단 접기는 실행횟수 가중이다 — 같은 폴의 변형 행을 산술평균하면
// 1회 실행된 느린 변형이 1000회 실행된 빠른 변형을 끌어올린다.
func TestSqFoldWeightsByExecutions(t *testing.T) {
	rows := []sqRow{
		sqTestRow(1, "k", "delta", 100, 1), // 느린 변형 1회
		sqTestRow(1, "k", "delta", 1, 999), // 빠른 변형 999회
	}
	groups, polls := sqFold(rows)
	if len(groups) != 1 || polls != 1 {
		t.Fatalf("그룹 %d개·폴 %d개", len(groups), polls)
	}
	g := groups[0]
	if len(g.polls) != 1 {
		t.Fatalf("폴 접기 실패: %+v", g.polls)
	}
	want := (100*1 + 1*999) / 1000.0
	if got := g.polls[0].lat; got < want-1e-9 || got > want+1e-9 {
		t.Fatalf("가중 평균 %v, 기대 %v(산술평균 50.5면 회귀)", got, want)
	}
	if g.dupPolls != 1 {
		t.Fatalf("한 폴 2행을 duplicate_rows_in_poll로 노출하지 않았다: %d", g.dupPolls)
	}
	if g.polls[0].execs != 1000 {
		t.Fatalf("실행횟수 합 %v", g.polls[0].execs)
	}
}

// 실행횟수가 0이면 가중이 불가하므로 산술평균으로 후퇴하고 그 사실을 센다.
func TestSqFoldZeroExecsFallback(t *testing.T) {
	groups, _ := sqFold([]sqRow{sqTestRow(1, "k", "delta", 10, 0), sqTestRow(1, "k", "delta", 20, 0)})
	g := groups[0]
	if g.polls[0].lat != 15 || !g.polls[0].unweighted || g.unweighted != 1 {
		t.Fatalf("0분모 후퇴 실패: lat=%v unweighted=%v n=%d", g.polls[0].lat, g.polls[0].unweighted, g.unweighted)
	}
}

// mode가 다르면 다른 행이다 — 생애 평균(legacy)과 구간 평균(delta)을
// 한 시계열로 합치면 뜻이 깨진다(§14.2, Codex 기여).
func TestSqFoldSplitsByMode(t *testing.T) {
	groups, _ := sqFold([]sqRow{sqTestRow(1, "k", "delta", 10, 5), sqTestRow(2, "k", "legacy", 900, 5)})
	if len(groups) != 2 {
		t.Fatalf("mode별 분리 실패: %d개", len(groups))
	}
}

func buildItem(t *testing.T, mode string, lats []float64, base sqBaseStat, basePolls, pollsInWindow int) *sqItem {
	t.Helper()
	var rows []sqRow
	for i, v := range lats {
		rows = append(rows, sqTestRow(i+1, "k", mode, v, 10))
	}
	groups, polls := sqFold(rows)
	if pollsInWindow > 0 {
		polls = pollsInWindow
	}
	return sqBuild(groups[0], map[string]sqBaseStat{"k\x00" + mode: base}, basePolls, polls,
		sqEngines["oracle"], "oracle", nil)
}

// 중앙값 팔 — 기준선 중앙값 대비 3배.
func TestSqVerdictMedianArm(t *testing.T) {
	it := buildItem(t, "delta", []float64{30, 32, 35}, sqBaseStat{p50: 0.12, max: 269, polls: 1024}, 1412, 59)
	if it.verdict != "slower" || it.slowerBasis != "median" {
		t.Fatalf("verdict=%s basis=%s (실물 사건 271배가 안 잡히면 회귀)", it.verdict, it.slowerBasis)
	}
	if it.ratioP50 < 200 {
		t.Fatalf("ratio_p50=%v", it.ratioP50)
	}
	// 최대 팔은 기준선 최대가 자기 사건에 오염돼 있으면 침묵한다 —
	// 그래서 두 팔은 AND가 아니라 OR다(§14.3 실측).
	if it.ratioMax >= sqRatioThreshold {
		t.Fatalf("이 사례에서 최대 팔까지 발화하면 시나리오 전제가 틀렸다: %v", it.ratioMax)
	}
}

// 최대 팔 — 중앙값은 평평한데 최대만 튀는 실물 사례(mysql 07-28 23:30).
func TestSqVerdictMaxArmOnly(t *testing.T) {
	it := buildItem(t, "delta", []float64{226, 227, 2961}, sqBaseStat{p50: 253, max: 718, polls: 500}, 700, 30)
	if it.verdict != "slower" || it.slowerBasis != "max" {
		t.Fatalf("verdict=%s basis=%s — 최대 팔이 없으면 중앙값 평평한 정지를 놓친다", it.verdict, it.slowerBasis)
	}
}

// 만성은 이상이 아니다 — 이름만 붙인다(§14.3, 독립 Claude 기여).
func TestSqVerdictChronic(t *testing.T) {
	it := buildItem(t, "delta", []float64{324, 320, 330}, sqBaseStat{p50: 318, max: 1455, polls: 1180}, 1200, 30)
	if it.verdict != "chronic" {
		t.Fatalf("verdict=%s — 만성이 anomalous로 새면 인시던트마다 발화한다", it.verdict)
	}
}

// 기준선 등장률이 낮으면 만성이 아니라 stable이다.
func TestSqVerdictStableWhenBaselineSparse(t *testing.T) {
	it := buildItem(t, "delta", []float64{10, 11, 12}, sqBaseStat{p50: 10, max: 20, polls: 100}, 1200, 30)
	if it.verdict != "stable" {
		t.Fatalf("verdict=%s", it.verdict)
	}
}

func TestSqVerdictFasterAndGates(t *testing.T) {
	if it := buildItem(t, "delta", []float64{1, 1, 1}, sqBaseStat{p50: 30, max: 40, polls: 500}, 600, 30); it.verdict != "faster" {
		t.Fatalf("회복 관측이 faster로 나오지 않았다: %s", it.verdict)
	}
	// legacy는 생애 평균이라 배수가 회귀를 뜻하지 않는다.
	if it := buildItem(t, "legacy", []float64{100, 100, 100}, sqBaseStat{p50: 1, max: 2, polls: 500}, 600, 30); it.verdict != "not_assessable_legacy" {
		t.Fatalf("legacy 강등 실패: %s", it.verdict)
	}
	// 기준선 폴이 부족하면 판정하지 않는다.
	if it := buildItem(t, "delta", []float64{100, 100, 100}, sqBaseStat{p50: 1, max: 2, polls: 2}, 600, 30); it.verdict != "insufficient_baseline" {
		t.Fatalf("기준선 게이트 실패: %s", it.verdict)
	}
	// 창 폴이 부족하면 판정하지 않는다.
	if it := buildItem(t, "delta", []float64{100, 100}, sqBaseStat{p50: 1, max: 2, polls: 500}, 600, 30); it.verdict != "insufficient_baseline" {
		t.Fatalf("창 폴 게이트 실패: %s", it.verdict)
	}
}

// legacy 행은 누적 카운터를 폴별로 더하지 않는다(중복 계산 금지).
func TestSqLegacyDropsTotals(t *testing.T) {
	it := buildItem(t, "legacy", []float64{100, 100, 100}, sqBaseStat{polls: 0}, 600, 30)
	if it.execsTotal != 0 || it.totalMS != 0 || it.totalSrc != "unavailable_legacy" {
		t.Fatalf("legacy 총량 강등 실패: execs=%v total=%v src=%s", it.execsTotal, it.totalMS, it.totalSrc)
	}
}

// total_ms는 원천 키가 없으면 파생이고, 그 사실을 밝힌다.
func TestSqTotalMSDerivedWhenNoSourceKey(t *testing.T) {
	rows := []sqRow{sqTestRow(1, "k", "delta", 10, 3), sqTestRow(2, "k", "delta", 20, 2)}
	groups, polls := sqFold(rows)
	it := sqBuild(groups[0], nil, 0, polls, sqEngines["mysql"], "mysql", nil)
	if it.totalSrc != "derived" || it.totalMS != 10*3+20*2 {
		t.Fatalf("파생 total 실패: src=%s total=%v", it.totalSrc, it.totalMS)
	}
}

// presence는 폴 결손을 그대로 드러낸다 — 지연 사건 중 Δ실행수<=0으로
// 행이 버려지므로 결손 자체가 증거다(§14.4).
func TestSqPresence(t *testing.T) {
	it := buildItem(t, "delta", []float64{1, 1, 1, 1}, sqBaseStat{polls: 10, p50: 1, max: 1}, 100, 20)
	if it.presence < 0.19 || it.presence > 0.21 {
		t.Fatalf("presence=%v (4/20 기대)", it.presence)
	}
}

// 본문은 800자에서 자르고 원 길이를 남긴다. 사전 결손은 none으로 드러난다.
func TestSqTextTruncateAndSources(t *testing.T) {
	long := ""
	for len(long) < 1200 {
		long += "select 1 from dual union all "
	}
	groups, polls := sqFold([]sqRow{{ts: sqTS(1), sqlKey: "k", mode: "delta", lat: 1, execs: 1,
		io: map[string]float64{}, scope: map[string]string{}, text: long}})
	it := sqBuild(groups[0], nil, 0, polls, sqEngines["mysql"], "mysql", nil)
	if !it.textTrunc || len(it.text) != sqTextTrunc || it.textLen != len(long) {
		t.Fatalf("절단 실패: trunc=%v len=%d full=%d", it.textTrunc, len(it.text), it.textLen)
	}
	if it.textSource != "ch_body" || it.textKind != "digest_normalized" {
		t.Fatalf("본문 출처/성격 표기 실패: %s/%s", it.textSource, it.textKind)
	}
	// Oracle은 body에 본문이 없고 사전이 유일한 경로다.
	groups, polls = sqFold([]sqRow{sqTestRow(1, "ora", "delta", 1, 1)})
	it = sqBuild(groups[0], nil, 0, polls, sqEngines["oracle"], "oracle",
		map[string]sqDictText{"ora": {text: "select 1 from dual"}})
	if it.textSource != "dictionary" || it.textKind != "dict_captured" {
		t.Fatalf("사전 경로 실패: %s/%s", it.textSource, it.textKind)
	}
	groups, polls = sqFold([]sqRow{sqTestRow(1, "miss", "delta", 1, 1)})
	it = sqBuild(groups[0], nil, 0, polls, sqEngines["oracle"], "oracle", nil)
	if it.textSource != "none" || it.selfKnown {
		t.Fatalf("본문 결손 표기 실패: %s selfKnown=%v (본문 없으면 자기관측을 단정하지 않는다)", it.textSource, it.selfKnown)
	}
}

// 자기관측은 표식하고 근거 패턴을 함께 싣는다 — 근거 없는 불리언 금지.
func TestSqSelfMonitoringMarking(t *testing.T) {
	groups, polls := sqFold([]sqRow{{ts: sqTS(1), sqlKey: "k", mode: "delta", lat: 1, execs: 1,
		io: map[string]float64{}, scope: map[string]string{},
		text: "SELECT * FROM sys.x$processlist a LEFT JOIN sys.INNODB_LOCK_WAITS b"}})
	it := sqBuild(groups[0], nil, 0, polls, sqEngines["mysql"], "mysql", nil)
	if !it.selfMon || it.selfPattern == "" {
		t.Fatalf("자기관측 표식 실패: %v %q", it.selfMon, it.selfPattern)
	}
}

// 자기관측 행은 같은 정렬 키에서 뒤로 간다 — top_n 상한을 잡아먹지 않게.
func TestSqSortDemotesSelfMonitoring(t *testing.T) {
	a := &sqItem{sqlKey: "self", totalMS: 100, selfMon: true}
	b := &sqItem{sqlKey: "app", totalMS: 100}
	items := []*sqItem{a, b}
	sqSort(items, "total_ms")
	if items[0] != b {
		t.Fatalf("자기관측 행이 앞섰다: %s", items[0].sqlKey)
	}
	// 정렬축은 값 크기 순 — 자기관측이 아니면 값이 이긴다.
	c := &sqItem{sqlKey: "big", totalMS: 500}
	items = []*sqItem{b, c}
	sqSort(items, "total_ms")
	if items[0] != c {
		t.Fatalf("정렬축 무시: %s", items[0].sqlKey)
	}
}

// 플랜 안정성은 캡처가 적으면 등급을 매기지 않는다 — 캡처 1건이면
// distinct/count가 항상 1.0이라 unstable로 오분류된다(§14.7 실측).
func TestSqPlanStabilityGuard(t *testing.T) {
	cases := []struct {
		count, distinct int
		want            string
	}{
		{1, 1, "unknown_single_capture"},
		{4, 4, "unknown_single_capture"},
		{100, 100, "unstable"},
		{100, 1, "stable"},
		{100, 50, "mixed"},
	}
	for _, c := range cases {
		if got := (sqPlanSummary{count: c.count, distinctHash: c.distinct}).stability(); got != c.want {
			t.Fatalf("count=%d distinct=%d → %s, 기대 %s", c.count, c.distinct, got, c.want)
		}
	}
}

// nearest-rank 중앙값 — CH quantileExact와 위치 규약이 같아야 비가 뜻을 갖는다.
func TestSqP50NearestRank(t *testing.T) {
	if got := sqP50([]float64{1, 2, 3}); got != 2 {
		t.Fatalf("홀수 %v", got)
	}
	if got := sqP50([]float64{1, 2, 3, 4}); got != 3 {
		t.Fatalf("짝수는 보간하지 않고 위쪽 원소를 쓴다: %v", got)
	}
	if got := sqP50(nil); got != 0 {
		t.Fatalf("빈 입력 %v", got)
	}
}

// engine별 절단축은 생산자 코드 계약이다 — 행별 역추정은 금지.
func TestSqAxesContract(t *testing.T) {
	if len(sqAxesFor("postgresql")) != 5 || len(sqAxesFor("mysql")) != 4 {
		t.Fatal("절단축 목록 계약 회귀")
	}
	if sqAxesFor("mssql") != nil {
		t.Fatal("자료 없는 engine의 축을 지어내면 안 된다")
	}
}

// 창 해석 — seed 창 폴백과 잘못된 창 거절.
func TestSqWindow(t *testing.T) {
	now := time.Date(2026, 7, 30, 4, 0, 0, 0, time.UTC)
	first := now.Add(-3 * time.Hour)
	last := now.Add(-time.Hour)
	from, to, basis, err := sqWindow("", "", first, last, now)
	if err != nil || !from.Equal(first) || !to.Equal(last) {
		t.Fatalf("seed 창 폴백 실패: %v %v %v", from, to, err)
	}
	if basis == "" {
		t.Fatal("창 근거 문구 없음")
	}
	if _, _, _, err := sqWindow("now", "now-1h", time.Time{}, time.Time{}, now); err == nil {
		t.Fatal("from >= to를 통과시켰다")
	}
}
