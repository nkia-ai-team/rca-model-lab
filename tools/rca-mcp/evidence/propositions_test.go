package evidence

import (
	"testing"
	"time"
)

func fv(v float64) *float64 { return &v }

// shiftedRecord — 사상표 scan_metrics/shifted 행(허용 Predicate 전부)의
// 최소 레코드. 6종 술어가 전부 파생되는 자리다.
func shiftedRecord(metric string, mag float64, dir Direction, w WindowClass) EvidenceIndexRecord {
	r := minimalRecord()
	r.FindingClass = "scan_metrics/shifted"
	r.Effect = Effect{Kind: EffectRatio, Metric: metric,
		Observed: fv(320), Baseline: fv(100), Magnitude: fv(mag), Direction: dir}
	r.Quality.Status = StatusAnomalous
	r.Window.Class = w
	return r
}

func newLedgerIndex() (*Index, *Ledger) {
	ix := NewIndex()
	l := NewLedger(ix)
	l.SetClock(func() time.Time { return time.Date(2026, 8, 3, 4, 0, 0, 0, time.UTC) })
	return ix, l
}

func keyOf(metric string, w WindowClass) ObservationKey {
	return ObservationKey{Source: SrcScanMetrics, TargetID: "t1", Aspect: AspectMetric,
		Metric: metric, Window: w}.Canonical()
}

// §6.0 ②: 장부는 truth를 저장하지 않는다 — 관측값만 싣고 조회 시 파생한다.
// 같은 관측 하나에서 Predicate·Threshold를 어떻게 바꿔도 전부 "기실측"이며,
// 그것이 반증 가설의 연산자 갈아타기 우회를 막는 방어선이다(등록 규칙 3).
func TestPredicateThresholdWindowVariantsAreAllMeasured(t *testing.T) {
	ix, l := newLedgerIndex()
	if _, err := ix.Append(shiftedRecord("cpu.usage", 3.0, DirUp, WindowFull)); err != nil {
		t.Fatal(err)
	}
	cases := []struct {
		pred  Predicate
		thr   *float64
		truth bool
	}{
		{PredPresent, nil, true},
		{PredAbsent, nil, false},
		{PredMagnitudeGE, fv(2.9), true},
		{PredMagnitudeGE, fv(3.0), true},
		{PredMagnitudeGE, fv(3.1), false},
		{PredMagnitudeLT, fv(3.1), true},
		{PredMagnitudeLT, fv(2.9), false},
		{PredDirectionUp, nil, true},
		{PredDirectionDown, nil, false},
	}
	// 창 축까지 훑는다 — 실측은 full에만 있는데 onset_narrow로 물어도
	// 기실측이어야 한다(4차 A-3의 2값 우회 봉쇄).
	for _, w := range []WindowClass{WindowFull, WindowOnsetNarrow} {
		for _, c := range cases {
			m := l.MeasuredAnyWindow(keyOf("cpu.usage", w), c.pred, c.thr)
			if !m.Measured {
				t.Errorf("창 %s · %s(thr=%v)가 미실측으로 나왔다(%s) — 변형 하나가 등록 규칙 3을 우회한다",
					w, c.pred, c.thr, m.Derive)
				continue
			}
			if m.Truth != c.truth {
				t.Errorf("창 %s · %s(thr=%v) truth=%v, 기대 %v", w, c.pred, c.thr, m.Truth, c.truth)
			}
			if m.Window != WindowFull {
				t.Errorf("실측이 발견된 창이 %s — 실제 관측은 full에만 있다", m.Window)
			}
		}
	}
	// Expectation 변형도 새 명제가 아니다 — 같은 truth를 뒤집어 읽을 뿐이다.
	m := l.MeasuredAnyWindow(keyOf("cpu.usage", WindowFull), PredMagnitudeGE, fv(2.9))
	if !Holds(m.Truth, ExpectMustHold) || Holds(m.Truth, ExpectMustNotHold) {
		t.Fatal("Expectation 대조가 어긋났다")
	}
	// 판정용 조회는 창을 구분한다(C-8: 두 정의를 코드에서 분리).
	if got := l.Resolve(keyOf("cpu.usage", WindowOnsetNarrow)); got.Status != Unmeasured {
		t.Fatalf("판정용 조회가 창을 구분하지 않는다: %s", got.Status)
	}
	if got := l.Resolve(keyOf("cpu.usage", WindowFull)); got.Status != ResolvedEntity {
		t.Fatalf("full 창 개체 해소 실패: %s", got.Status)
	}
}

// supersede가 관측값을 뒤집으면 그 명제는 **즉시** 후계로 재판정된다 —
// 미실측으로 돌아가지 않는다(4차 A-3). 반증된 가설의 부활은 truth가 실제로
// 뒤집혔을 때만 열린다.
func TestSupersedeRejudgesAtomically(t *testing.T) {
	ix, l := newLedgerIndex()
	first, err := ix.Append(shiftedRecord("cpu.usage", 5.0, DirUp, WindowFull))
	if err != nil {
		t.Fatal(err)
	}
	k := keyOf("cpu.usage", WindowFull)
	m := l.MeasuredAnyWindow(k, PredMagnitudeGE, fv(3.0))
	if !m.Measured || !m.Truth {
		t.Fatalf("최초 판정이 참이 아니다: %+v", m)
	}
	// 재조회가 값을 뒤집는다.
	r := shiftedRecord("cpu.usage", 1.2, DirUp, WindowFull)
	r.Supersedes = first
	neu, err := ix.Append(r)
	if err != nil {
		t.Fatal(err)
	}
	m2 := l.MeasuredAnyWindow(k, PredMagnitudeGE, fv(3.0))
	if !m2.Measured {
		t.Fatal("supersede 뒤 명제가 미실측으로 돌아갔다 — 등록 규칙 3의 부활 문이 무단으로 열린다(4차 A-3)")
	}
	if m2.Truth {
		t.Fatal("후계 관측값으로 재판정되지 않았다 — 장부가 옛 truth를 붙들고 있다")
	}
	res := l.Resolve(k)
	if res.Status != ResolvedEntity || len(res.EIDs) != 1 || res.EIDs[0] != neu {
		t.Fatalf("장부의 최신 근거 EID가 후계가 아니다: %+v", res)
	}
	if len(l.Propositions()) != 1 {
		t.Fatalf("명제가 %d행 — supersede가 새 행을 만들었다", len(l.Propositions()))
	}
	// 부활 경로: 세 번째 재조회가 값을 되돌리면 truth도 되돌아온다.
	back := shiftedRecord("cpu.usage", 9.0, DirUp, WindowFull)
	back.Supersedes = neu
	if _, err := ix.Append(back); err != nil {
		t.Fatal(err)
	}
	if m3 := l.MeasuredAnyWindow(k, PredMagnitudeGE, fv(3.0)); !m3.Measured || !m3.Truth {
		t.Fatalf("반증이 뒤집히는 부활 경로가 막혔다: %+v", m3)
	}
}

// C-3: 존재하지 않는 개체를 지목한 absent 술어는 개체 행으로는 영원히
// 해소되지 않는다. 완전 조회 query_scope가 그 관측을 장부에 싣는 유일한
// 경로이며(§6 진리표 행 0), 이 경로가 없으면 반증된 가설이 EntityKey만
// 바꿔 무한 재등록된다.
func TestAbsentEntityResolvedByCompleteScope(t *testing.T) {
	ix, l := newLedgerIndex()
	sc := minimalRecord()
	sc.RecordKind = KindQueryScope
	sc.FindingClass = "scan_metrics/appeared"
	sc.EntityKey = EntityAny
	sc.Effect = Effect{Kind: EffectCount, Metric: EntityAny, Direction: DirNA}
	sc.Quality = Quality{Status: StatusNoData, NoDataReason: NoDataZeroObservations,
		Availability: AvailObservedZero, Confidence: ConfOK}
	sc.Scope = &QueryScope{Total: 0, Returned: 0, Omitted: 0}
	sc.Scope.Selector = sc.Key()
	if _, err := ix.Append(sc); err != nil {
		t.Fatal(err)
	}
	k := keyOf("없는.지표", WindowFull)
	res := l.Resolve(k)
	if res.Status != ResolvedScope {
		t.Fatalf("부재 개체가 범위 관측으로 해소되지 않았다: %s", res.Status)
	}
	if res.Value.Availability != AvailObservedZero || res.Value.Present {
		t.Fatalf("범위 해소의 관측값이 어긋났다: %+v", res.Value)
	}
	truth, st := DeriveTruth(res.Value, PredAbsent, nil, res.Status)
	if st != DeriveOK || !truth {
		t.Fatalf("absent가 참으로 파생되지 않았다: %v %s", truth, st)
	}
	if truth, st := DeriveTruth(res.Value, PredPresent, nil, res.Status); st != DeriveOK || truth {
		t.Fatalf("present가 거짓으로 파생되지 않았다: %v %s", truth, st)
	}
	// 그 술어는 이제 기실측이다 — 등록 규칙 3이 재등록을 막는다.
	if m := l.MeasuredAnyWindow(k, PredAbsent, nil); !m.Measured || !m.Truth {
		t.Fatalf("범위 관측이 기실측으로 세어지지 않았다: %+v", m)
	}
	// 크기·방향은 부재에서 파생되지 않는다 — "없으니 작다"가 지지 근거로
	// 둔갑하는 문을 닫는다.
	if _, st := DeriveTruth(res.Value, PredMagnitudeLT, fv(1), res.Status); st != DeriveNoMagnitude {
		t.Fatalf("부재에서 magnitude_lt가 파생됐다: %s", st)
	}
	if _, st := DeriveTruth(res.Value, PredDirectionUp, nil, res.Status); st != DeriveNoDirection {
		t.Fatalf("부재에서 방향이 파생됐다: %s", st)
	}
}

// 덮는 범위 행이 여럿이고 하나라도 절단됐으면 부재는 실측이 아니다 —
// 잘린 꼬리에 그 개체가 있었을 수 있다(§5.1 계약 1).
//
// 1c 실측이 이 검사를 필수로 만들었다: 라이브 scan_metrics 봉투에서
// shifted(omitted=0)·appeared(0건)·disappeared(omitted=19) 세 구획의 범위
// 행이 **한 관측 키로 접힌다**. "완전한 형제 하나"로 absent를 인정하면
// 절단된 형제가 감춘 개체가 부재로 확정된다.
func TestTruncatedSiblingScopeBlocksAbsent(t *testing.T) {
	c := fixtureCase{file: "envelope_scan_metrics.json", source: SrcScanMetrics, tool: "scan_metrics"}
	res, err := Project(testRequest(c), loadFixture(t, c.file))
	if err != nil {
		t.Fatal(err)
	}
	ix, l := newLedgerIndex()
	if _, err := ix.AppendAll(res.Records); err != nil {
		t.Fatal(err)
	}
	k := ObservationKey{Source: SrcScanMetrics, TargetID: fixtureTarget, Aspect: AspectMetric,
		Metric: "존재하지.않는.지표", Window: WindowFull}
	r := l.Resolve(k)
	if r.Status != ScopeIncomplete {
		t.Fatalf("절단된 형제 구획이 있는데 해소 상태가 %s다 — 거짓 부재가 만들어진다", r.Status)
	}
	if m := l.MeasuredAnyWindow(k, PredAbsent, nil); m.Measured {
		t.Fatal("절단된 조회로 absent가 기실측이 됐다")
	}
	// 감사용 ScopeFor는 완전한 행 하나를 여전히 찾아 준다 — 자격 판정의
	// 정본이 장부라는 것을 이 차이가 보여 준다.
	if _, ok := ix.ScopeFor(k); !ok {
		t.Fatal("감사용 범위 조회가 덮는 행을 찾지 못했다")
	}
}

// §6 진리표 행 2: Availability가 missing·not_applicable이면 파생 자체가
// 불가다 — 지지·반증 양쪽 금지. "원래 없는 지표"가 absent 예측의 지지로
// 둔갑하는 문을 닫는 자리다.
func TestUnusableAvailabilityBlocksDerivation(t *testing.T) {
	for _, av := range []Availability{AvailMissing, AvailNotApplicable} {
		ix, l := newLedgerIndex()
		r := shiftedRecord("cpu.usage", 3.0, DirUp, WindowFull)
		r.Quality.Status = StatusNoData
		r.Quality.NoDataReason = NoDataCollectorGap
		r.Quality.Availability = av
		if _, err := ix.Append(r); err != nil {
			t.Fatal(err)
		}
		k := keyOf("cpu.usage", WindowFull)
		if res := l.Resolve(k); res.Status != ResolvedEntity {
			t.Fatalf("%s: 해소 자체는 되어야 한다(무엇을 조회했는지는 기록이다): %s", av, res.Status)
		}
		for _, p := range []Predicate{PredPresent, PredAbsent, PredDirectionUp} {
			if m := l.MeasuredAnyWindow(k, p, nil); m.Measured {
				t.Errorf("%s인데 %s가 기실측으로 세어졌다", av, p)
			} else if m.Derive != DeriveUnusable {
				t.Errorf("%s의 파생 사유가 %s", av, m.Derive)
			}
		}
	}
}

// class 층 검사(§5.1 2층 강제 중 ②): 그 class가 허용하지 않는 술어는
// 파생 불능이고, 파생 불능은 관측 부재와 같다 — 기실측으로 세지 않는다.
func TestClassPermissionBlocksDerivation(t *testing.T) {
	ix, l := newLedgerIndex()
	r := minimalRecord()
	// appeared 행은 present·absent만 허용한다(Magnitude 자체가 없다).
	r.FindingClass = "scan_metrics/appeared"
	r.Effect = Effect{Kind: EffectCategorical, Metric: "새.지표", Direction: DirNA}
	if _, err := ix.Append(r); err != nil {
		t.Fatal(err)
	}
	k := keyOf("새.지표", WindowFull)
	if m := l.MeasuredAnyWindow(k, PredPresent, nil); !m.Measured || !m.Truth {
		t.Fatalf("허용 술어가 파생되지 않았다: %+v", m)
	}
	for _, p := range []Predicate{PredMagnitudeGE, PredDirectionUp} {
		m := l.MeasuredAnyWindow(k, p, fv(1))
		if m.Measured {
			t.Errorf("%s가 appeared class에서 파생됐다 — 사상표의 허용 열이 사문이 된다", p)
		} else if m.Derive != DeriveClassNotAllowed {
			t.Errorf("%s의 파생 사유가 %s", p, m.Derive)
		}
	}
}

// 복수 해소(§6 진리표 행 1)는 감추지 않는다 — 하나를 골라 답하면 판정이
// 조용히 임의의 레코드에 걸린다.
func TestAmbiguousResolutionIsReported(t *testing.T) {
	ix, l := newLedgerIndex()
	a := shiftedRecord("q.depth", 3.0, DirUp, WindowFull)
	a.EntityKey = "pod-a"
	b := shiftedRecord("q.depth", 9.0, DirUp, WindowFull)
	b.EntityKey = "pod-b"
	for _, r := range []EvidenceIndexRecord{a, b} {
		if _, err := ix.Append(r); err != nil {
			t.Fatal(err)
		}
	}
	// EntityKey를 지목하지 않은 술어는 두 개체 모두에 걸린다.
	open := keyOf("q.depth", WindowFull)
	res := l.Resolve(open)
	if res.Status != Ambiguous || len(res.EIDs) != 2 {
		t.Fatalf("복수 해소가 보고되지 않았다: %+v", res)
	}
	if m := l.MeasuredAnyWindow(open, PredPresent, nil); m.Measured || m.Derive != DeriveUnresolved {
		t.Fatalf("복수 해소가 기실측으로 세어졌다: %+v", m)
	}
	// 개체를 지목하면 정확히 해소된다.
	narrow := open
	narrow.EntityKey = "pod-b"
	if r := l.Resolve(narrow); r.Status != ResolvedEntity || r.Value.Magnitude == nil || *r.Value.Magnitude != 9 {
		t.Fatalf("개체 지목 해소가 어긋났다: %+v", r)
	}
}

// C-2: R 심사에 첨부할 관측값은 미실측이면 nil이어야 한다 — "관측값 없음"과
// "있음"이 구분돼야 미실측 necessary 술어를 자명성 심사에서 뺄 수 있고,
// 그래야 "불확실은 불리 방향" 규율이 전 가설 반려로 번지지 않는다.
func TestObservationAttachmentIsNilWhenUnmeasured(t *testing.T) {
	ix, l := newLedgerIndex()
	if _, err := ix.Append(shiftedRecord("cpu.usage", 3.0, DirUp, WindowFull)); err != nil {
		t.Fatal(err)
	}
	if v := l.Observation(keyOf("cpu.usage", WindowFull)); v == nil || v.Magnitude == nil || *v.Magnitude != 3 {
		t.Fatalf("기실측 술어에 관측값이 첨부되지 않았다: %+v", v)
	}
	if v := l.Observation(keyOf("아직.조회.안한.지표", WindowFull)); v != nil {
		t.Fatalf("미실측 술어에 관측값이 붙었다: %+v", v)
	}
}

// 장부는 index에 이미 실린 레코드에서 재생되며, 대체된 레코드는 재생에서
// 빠진다(부착 시점이 판정을 바꾸지 않는다).
func TestLedgerReplaysExistingRecords(t *testing.T) {
	ix := NewIndex()
	first, _ := ix.Append(shiftedRecord("cpu.usage", 5.0, DirUp, WindowFull))
	r := shiftedRecord("cpu.usage", 1.0, DirUp, WindowFull)
	r.Supersedes = first
	if _, err := ix.Append(r); err != nil {
		t.Fatal(err)
	}
	l := NewLedger(ix)
	res := l.Resolve(keyOf("cpu.usage", WindowFull))
	if res.Status != ResolvedEntity || res.Value.Magnitude == nil || *res.Value.Magnitude != 1 {
		t.Fatalf("재생이 대체된 레코드를 살렸다: %+v", res)
	}
}

// 라이브 봉투 16종의 투영 산출 전량이 supersede 계약을 통과하고 장부에
// 실린다 — 계약이 합성 레코드에서만 성립하는 것이 아님을 실물로 건다.
// 개체 행은 자기 키로 해소되거나(정상) 복수 해소로 **보고**돼야 한다.
func TestLedgerOverLiveFixtures(t *testing.T) {
	ix, l := newLedgerIndex()
	findings, resolved, ambiguous := 0, 0, 0
	for _, c := range fixtures {
		if c.source == "" {
			continue
		}
		recs := projectFixture(t, c)
		if _, err := ix.AppendAll(recs); err != nil {
			t.Fatalf("%s: 실물 레코드가 index 계약에 걸렸다: %v", c.file, err)
		}
		for _, r := range recs {
			if r.RecordKind != KindFinding {
				continue
			}
			findings++
			switch st := l.Resolve(r.Key()).Status; st {
			case ResolvedEntity:
				resolved++
			case Ambiguous:
				ambiguous++
			default:
				t.Errorf("%s의 개체 행이 자기 키로 해소되지 않았다(%s): %s", c.file, st, r.Key())
			}
		}
	}
	if findings == 0 {
		t.Fatal("실물 finding이 0건 — 시험이 아무것도 안 걸고 있다")
	}
	t.Logf("실물 finding %d건: 단일 해소 %d · 복수 해소 %d · 명제 %d행",
		findings, resolved, ambiguous, len(l.Propositions()))
}

// meta 레코드는 술어 판정 대상이 아니라 장부에 들어가지 않는다(§5.1).
func TestMetaRecordsAreNotPropositions(t *testing.T) {
	ix, l := newLedgerIndex()
	m := minimalRecord()
	m.RecordKind = KindMeta
	m.FindingClass = ""
	m.Window.Class = ""
	if _, err := ix.Append(m); err != nil {
		t.Fatal(err)
	}
	if n := len(l.Propositions()); n != 0 {
		t.Fatalf("meta가 명제로 실렸다: %d행", n)
	}
}
