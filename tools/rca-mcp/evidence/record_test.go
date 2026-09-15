package evidence

import (
	"math"
	"strings"
	"testing"
)

func f(v float64) *float64 { return &v }

func okRecord() EvidenceIndexRecord {
	return EvidenceIndexRecord{
		EID: "EIX-0001", TargetID: "svc-1", Domain: "application",
		Aspect: AspectDB, RecordKind: KindFinding, FindingClass: "db_slow_queries",
		EntityKey: "sql_key:abc123", Headline: "orders 조회 대표 소요 3.2배",
		Effect:     Effect{Kind: EffectRatio, Metric: "elapsed_ms", Observed: f(320), Baseline: f(100), Magnitude: f(3.2), Direction: DirUp},
		Window:     TimeWindow{Class: WindowFull},
		Quality:    Quality{Status: StatusAnomalous, Availability: AvailObserved, Confidence: ConfOK},
		Provenance: Provenance{Source: SrcDBSlowQueries, EnvelopeRef: "ref-1"},
	}
}

func TestValidRecordPasses(t *testing.T) {
	if err := okRecord().Validate(); err != nil {
		t.Fatalf("정상 레코드 거부: %v", err)
	}
}

// B-11: baseline 0에서 ratio가 +Inf가 되면 JSON 직렬화가 실패하거나
// 정렬에서 무한대가 최상위로 올라간다. 비유한 값은 index에 들어갈 수 없다.
func TestNonFiniteEffectRejected(t *testing.T) {
	for name, v := range map[string]float64{
		"+Inf": math.Inf(1), "-Inf": math.Inf(-1), "NaN": math.NaN(),
	} {
		r := okRecord()
		r.Effect.Magnitude = f(v)
		err := r.Validate()
		if err == nil {
			t.Fatalf("%s magnitude가 통과됨", name)
		}
		if !strings.Contains(err.Error(), "비유한") {
			t.Fatalf("%s 반려 사유가 엉뚱함: %v", name, err)
		}
		// observed/baseline 축도 같은 관문을 진다.
		r = okRecord()
		r.Effect.Observed = f(v)
		if r.Validate() == nil {
			t.Fatalf("%s observed가 통과됨", name)
		}
	}
	// 1.0/0.0을 그대로 넣는 경로가 실제로 막히는지 — projector의 분모 0
	// 분기가 빠지면 여기서 걸린다.
	r := okRecord()
	r.Effect.Baseline = f(0)
	r.Effect.Magnitude = f(*r.Effect.Observed / 0)
	if r.Validate() == nil {
		t.Fatal("baseline 0의 나눗셈 결과가 index에 들어감")
	}
}

func TestCategoricalHasNoMagnitude(t *testing.T) {
	r := okRecord()
	r.Effect.Kind = EffectCategorical
	if r.Validate() == nil {
		t.Fatal("categorical에 magnitude가 붙었는데 통과됨(§5.1 정의식)")
	}
	r.Effect.Magnitude = nil
	if err := r.Validate(); err != nil {
		t.Fatalf("magnitude 없는 categorical 거부: %v", err)
	}
	if _, ok := KindRank(EffectCategorical); ok {
		t.Fatal("categorical이 정렬 서열에 있음 — Magnitude가 없어 정렬 불가")
	}
	if a, _ := KindRank(EffectRatio); a <= func() int { r, _ := KindRank(EffectCount); return r }() {
		t.Fatal("Kind 간 고정 우선순위가 ratio > count가 아님")
	}
}

func TestQueryScopeRecordShape(t *testing.T) {
	r := okRecord()
	r.RecordKind = KindQueryScope
	if r.Validate() == nil {
		t.Fatal("scope 없는 query_scope 레코드가 통과됨")
	}
	// selector는 레코드의 관측 키와 같은 것이어야 한다(1c 추가 검사) —
	// 갈리면 "덮는 범위 행" 조회와 supersede head 색인이 다른 키를 본다.
	r.Scope = &QueryScope{Selector: ObservationKey{}, Total: 0, Returned: 0, Omitted: 0}
	if r.Validate() == nil {
		t.Fatal("레코드 관측 키와 다른 selector가 통과됨 — C-3 해소 경로가 조용히 어긋난다")
	}
	r.Scope.Selector = r.Key()
	if err := r.Validate(); err != nil {
		t.Fatalf("query_scope 레코드 거부: %v", err)
	}
	// finding 레코드에는 scope를 붙일 수 없다.
	r.RecordKind = KindFinding
	if r.Validate() == nil {
		t.Fatal("finding에 scope가 붙었는데 통과됨")
	}
	if !(QueryScope{Omitted: 0}).Complete() {
		t.Fatal("절단 없는 조회가 불완전으로 판정됨")
	}
	if (QueryScope{Omitted: 3}).Complete() {
		t.Fatal("절단된 조회가 완전으로 판정됨 — absent 근거 자격이 새어나감")
	}
}

// §5.1 Availability 규칙 계산. observed_zero는 "완전 조회 + 0건"에서만
// 나온다 — 절단된 0건은 missing이다(잘린 꼬리에 있었을 수 있다).
func TestAvailabilityOf(t *testing.T) {
	cases := []struct {
		st       Status
		reason   NoDataReason
		complete bool
		want     Availability
	}{
		{StatusAnomalous, "", true, AvailObserved},
		{StatusNormal, "", false, AvailObserved},
		{StatusNoData, NoDataZeroObservations, true, AvailObservedZero},
		{StatusNoData, NoDataZeroObservations, false, AvailMissing},
		{StatusNoData, NoDataNotCollected, true, AvailNotApplicable},
		{StatusNoData, NoDataCollectorGap, true, AvailMissing},
		{StatusNoData, NoDataTTLExpired, true, AvailMissing},
		{StatusNoData, NoDataBackendError, true, AvailMissing},
		{StatusNoData, NoDataUnknown, true, AvailMissing},
	}
	for _, c := range cases {
		if got := AvailabilityOf(c.st, c.reason, c.complete); got != c.want {
			t.Errorf("AvailabilityOf(%s,%s,%v) = %s, want %s", c.st, c.reason, c.complete, got, c.want)
		}
	}
}

// 4차 A-2: tagged 타입은 "실측 0초"와 "미정"을 구분한다. zero value가
// 산식 호출을 통과시키면 안 된다.
func TestTaggedZeroValueIsNotObserved(t *testing.T) {
	var lag CollectLag
	var skew ClockSkew
	if lag.Usable() || skew.Usable() {
		t.Fatal("zero value가 observed로 통과 — fail-open 재발")
	}
	if !(CollectLag{ValueS: 0, Status: TagObserved}).Usable() {
		t.Fatal("실측 0초가 거부됨")
	}
	if (ClockSkew{BoundS: 5, Status: TagExceeded}).Usable() {
		t.Fatal("허용치 초과 skew가 시간 판정을 통과")
	}
}

func TestTruncationInvariant(t *testing.T) {
	r := okRecord()
	r.Truncation.OmittedN = 5
	if r.Validate() == nil {
		t.Fatal("omitted>0인데 truncated=false가 통과 — 절단 사실 누락")
	}
	r.Truncation.Truncated = true
	if err := r.Validate(); err != nil {
		t.Fatalf("정상 절단 기록 거부: %v", err)
	}
}

func TestTextCaps(t *testing.T) {
	r := okRecord()
	r.Headline = strings.Repeat("가", 121)
	if r.Validate() == nil {
		t.Fatal("headline 121자가 통과")
	}
	r = okRecord()
	r.RawExcerpt = strings.Repeat("x", 201)
	if r.Validate() == nil {
		t.Fatal("raw_excerpt 201자가 통과")
	}
}

func TestSupersedeAndRefineAreExclusive(t *testing.T) {
	r := okRecord()
	r.Supersedes, r.Refines = "EIX-0000", "EIX-0000"
	if r.Validate() == nil {
		t.Fatal("supersede와 refine 동시 주장이 통과(§5.7)")
	}
}

func TestRecordKeyUsesEntityAxis(t *testing.T) {
	r := okRecord()
	k := r.Key()
	if k.Source != SrcDBSlowQueries || k.EntityKey != "sql_key:abc123" || k.Metric != "elapsed_ms" {
		t.Fatalf("레코드 키가 어긋남: %v", k)
	}
	if k != k.Canonical() {
		t.Fatal("Key가 정규화되지 않음")
	}
}

func TestEnumGuards(t *testing.T) {
	if ValidAspect("trace") {
		t.Fatal("trace가 Aspect에 있음 — §10이 현 표면에서 쓰지 않는다고 못박음")
	}
	if ValidRecordKind("rollup") || ValidEffectKind("z") || ValidDirection("sideways") {
		t.Fatal("미정의 enum이 통과")
	}
	if !ValidNoDataReason(NoDataBackendError) {
		t.Fatal("backend_error가 no_data 사유에서 빠짐(§15.2 신설)")
	}
}
