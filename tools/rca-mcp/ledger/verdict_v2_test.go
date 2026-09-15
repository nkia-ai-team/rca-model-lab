package ledger

import (
	"testing"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
)

// ── 시험 재료 ───────────────────────────────────────────────────

var (
	winFrom = time.Date(2026, 8, 3, 3, 0, 0, 0, time.UTC)
	winTo   = time.Date(2026, 8, 3, 4, 0, 0, 0, time.UTC)
	nowT    = time.Date(2026, 8, 3, 4, 10, 0, 0, time.UTC)
)

func fv(v float64) *float64 { return &v }

// okRecord — 자격 게이트를 전부 통과하는 finding 레코드.
func okRecord(metric string, mag float64, dir evidence.Direction) evidence.EvidenceIndexRecord {
	return evidence.EvidenceIndexRecord{
		TargetID: "t1", Aspect: evidence.AspectMetric, RecordKind: evidence.KindFinding,
		FindingClass: "scan_metrics/shifted", Headline: "h", EntityKey: "",
		Effect: evidence.Effect{Kind: evidence.EffectRatio, Metric: metric,
			Observed: fv(320), Baseline: fv(100), Magnitude: fv(mag), Direction: dir},
		Window: evidence.TimeWindow{From: winFrom, To: winTo, Class: evidence.WindowFull,
			CollectLag: evidence.CollectLag{ValueS: 60, Status: evidence.TagObserved},
			ClockSkew:  evidence.ClockSkew{BoundS: 1, Status: evidence.TagObserved}},
		Quality: evidence.Quality{Status: evidence.StatusAnomalous,
			Availability: evidence.AvailObserved, Confidence: evidence.ConfOK},
		Provenance: evidence.Provenance{Source: evidence.SrcScanMetrics, LineageID: "LG-" + metric},
	}
}

// scopeRecord — 완전 조회 범위 행. Availability를 인자로 받아 행 0의
// 두 갈래(전부 0건 / 다른 개체는 나옴)를 각각 만든다.
func scopeRecord(metric string, av evidence.Availability, omitted int) evidence.EvidenceIndexRecord {
	r := okRecord(metric, 0, evidence.DirNA)
	r.RecordKind = evidence.KindQueryScope
	r.FindingClass = ""
	r.EntityKey = evidence.EntityAny
	r.Effect = evidence.Effect{Kind: evidence.EffectCount, Metric: metric, Direction: evidence.DirNA}
	r.Quality.Status = evidence.StatusNoData
	r.Quality.NoDataReason = evidence.NoDataZeroObservations
	r.Quality.Availability = av
	r.Scope = &evidence.QueryScope{
		Selector: r.Key(), Total: 10 + omitted, Returned: 10, Omitted: omitted}
	if omitted > 0 {
		r.Truncation = evidence.Truncation{Truncated: true, OmittedN: omitted}
	}
	return r
}

func newIx(t *testing.T, recs ...evidence.EvidenceIndexRecord) (*evidence.Index, *evidence.Ledger, GateContext) {
	t.Helper()
	ix := evidence.NewIndex()
	l := evidence.NewLedger(ix)
	for _, r := range recs {
		if _, err := ix.Append(r); err != nil {
			t.Fatalf("append: %v", err)
		}
	}
	return ix, l, GateContext{Index: ix, Now: nowT,
		RequiredWindow: &TimeRange{From: winFrom, To: winTo}}
}

func pred(metric string, p evidence.Predicate, thr *float64, exp Expectation, role PredRole) SignalPred {
	return SignalPred{
		PredID: "H1-P1", Tool: evidence.SrcScanMetrics, TargetID: "t1",
		Aspect: evidence.AspectMetric, Metric: metric, Predicate: p, Threshold: thr,
		Window: evidence.WindowFull, Expectation: exp, Role: role,
	}
}

// ── 진리표 전 행 ────────────────────────────────────────────────

// TestTruthTableAllRows — §6 진리표 행 0~7 전수. 각 행이 **자기 조건에서만**
// 발화하는지, 그리고 판정이 스펙 표와 일치하는지를 본다.
func TestTruthTableAllRows(t *testing.T) {
	cases := []struct {
		name    string
		recs    []evidence.EvidenceIndexRecord
		p       SignalPred
		mutate  func(*GateContext)
		wantRow int
		want    PredVerdict
	}{
		{
			name:    "행0 — 완전 조회 0건이 absent를 해소(그 뒤 행 3)",
			recs:    []evidence.EvidenceIndexRecord{scopeRecord("m", evidence.AvailObservedZero, 0)},
			p:       pred("m", evidence.PredAbsent, nil, ExpectMustHold, RoleCorroborating),
			wantRow: 3, want: VerdictSupportedQualified,
		},
		{
			name:    "행1 — 아무 관측도 없다",
			p:       pred("m", evidence.PredPresent, nil, ExpectMustHold, RoleCorroborating),
			wantRow: 1, want: VerdictInconclusive,
		},
		{
			name: "행1 — 같은 키에 active가 둘(복수 해소)",
			recs: []evidence.EvidenceIndexRecord{
				okRecord("m", 3.0, evidence.DirUp), okRecord("m", 4.0, evidence.DirUp)},
			p:       pred("m", evidence.PredPresent, nil, ExpectMustHold, RoleCorroborating),
			wantRow: 1, want: VerdictInconclusive,
		},
		{
			name:    "행1 — 덮는 범위가 절단됐다(잘린 꼬리에 있었을 수 있다)",
			recs:    []evidence.EvidenceIndexRecord{scopeRecord("m", evidence.AvailObservedZero, 19)},
			p:       pred("m", evidence.PredAbsent, nil, ExpectMustHold, RoleCorroborating),
			wantRow: 1, want: VerdictInconclusive,
		},
		{
			name: "행2 — Availability=missing은 지지·반증 양쪽 금지",
			recs: []evidence.EvidenceIndexRecord{func() evidence.EvidenceIndexRecord {
				r := okRecord("m", 3.0, evidence.DirUp)
				r.Quality.Availability = evidence.AvailMissing
				return r
			}()},
			p:       pred("m", evidence.PredPresent, nil, ExpectMustHold, RoleCorroborating),
			wantRow: 2, want: VerdictInconclusive,
		},
		{
			name: "행2 — class가 그 Predicate를 허용하지 않는다",
			recs: []evidence.EvidenceIndexRecord{func() evidence.EvidenceIndexRecord {
				// scan_metrics/appeared는 존재 술어만 허용한다(사상표) —
				// "창 안에서 새로 나타난 지표"라 방향·크기가 없다.
				r := okRecord("m", 3.0, evidence.DirUp)
				r.FindingClass = "scan_metrics/appeared"
				r.Effect.Kind = evidence.EffectCategorical
				r.Effect.Magnitude = nil
				return r
			}()},
			p:       pred("m", evidence.PredDirectionUp, nil, ExpectMustHold, RoleCorroborating),
			wantRow: 2, want: VerdictInconclusive,
		},
		{
			name:    "행2 — magnitude 술어인데 범위 해소(부재에는 크기가 없다)",
			recs:    []evidence.EvidenceIndexRecord{scopeRecord("m", evidence.AvailObservedZero, 0)},
			p:       pred("m", evidence.PredMagnitudeGE, fv(2), ExpectMustHold, RoleCorroborating),
			wantRow: 2, want: VerdictInconclusive,
		},
		{
			name:    "행3 — 일치 + 자격 충족",
			recs:    []evidence.EvidenceIndexRecord{okRecord("m", 3.0, evidence.DirUp)},
			p:       pred("m", evidence.PredMagnitudeGE, fv(2.0), ExpectMustHold, RoleCorroborating),
			wantRow: 3, want: VerdictSupportedQualified,
		},
		{
			name: "행4 — 일치 + 자격 미달(Confidence=low)",
			recs: []evidence.EvidenceIndexRecord{func() evidence.EvidenceIndexRecord {
				r := okRecord("m", 3.0, evidence.DirUp)
				r.Quality.Confidence = evidence.ConfLow
				return r
			}()},
			p:       pred("m", evidence.PredMagnitudeGE, fv(2.0), ExpectMustHold, RoleCorroborating),
			wantRow: 4, want: VerdictSupportedWeak,
		},
		{
			name:    "행4 — 요구 창 미주입은 unknown = 자격 미달(fail-closed)",
			recs:    []evidence.EvidenceIndexRecord{okRecord("m", 3.0, evidence.DirUp)},
			p:       pred("m", evidence.PredMagnitudeGE, fv(2.0), ExpectMustHold, RoleCorroborating),
			mutate:  func(gc *GateContext) { gc.RequiredWindow = nil },
			wantRow: 4, want: VerdictSupportedWeak,
		},
		{
			name: "행4 — CollectLag unknown은 자격 미달(§15.2-3)",
			recs: []evidence.EvidenceIndexRecord{func() evidence.EvidenceIndexRecord {
				r := okRecord("m", 3.0, evidence.DirUp)
				r.Window.CollectLag = evidence.CollectLag{Status: evidence.TagUnknown}
				return r
			}()},
			p:       pred("m", evidence.PredMagnitudeGE, fv(2.0), ExpectMustHold, RoleCorroborating),
			wantRow: 4, want: VerdictSupportedWeak,
		},
		{
			name:    "행5 — necessary 불일치 + 반증 자격",
			recs:    []evidence.EvidenceIndexRecord{okRecord("m", 3.0, evidence.DirUp)},
			p:       pred("m", evidence.PredMagnitudeGE, fv(10.0), ExpectMustHold, RoleNecessary),
			wantRow: 5, want: VerdictInvalidated,
		},
		{
			name: "행6 — necessary 불일치인데 반증 자격 미달(절단)",
			recs: []evidence.EvidenceIndexRecord{func() evidence.EvidenceIndexRecord {
				r := okRecord("m", 3.0, evidence.DirUp)
				r.Truncation = evidence.Truncation{Truncated: true, OmittedN: 7}
				return r
			}()},
			p:       pred("m", evidence.PredMagnitudeGE, fv(10.0), ExpectMustHold, RoleNecessary),
			wantRow: 6, want: VerdictInconclusive,
		},
		{
			name:    "행7 — corroborating 불일치는 지지 실패일 뿐",
			recs:    []evidence.EvidenceIndexRecord{okRecord("m", 3.0, evidence.DirUp)},
			p:       pred("m", evidence.PredMagnitudeGE, fv(10.0), ExpectMustHold, RoleCorroborating),
			wantRow: 7, want: VerdictInconclusive,
		},
		{
			name:    "must_not_hold 뒤집기 — 불일치가 지지가 된다",
			recs:    []evidence.EvidenceIndexRecord{okRecord("m", 3.0, evidence.DirUp)},
			p:       pred("m", evidence.PredMagnitudeGE, fv(10.0), ExpectMustNotHold, RoleNecessary),
			wantRow: 3, want: VerdictSupportedQualified,
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			_, l, gc := newIx(t, c.recs...)
			if c.mutate != nil {
				c.mutate(&gc)
			}
			j := JudgePredicate(c.p, l, gc)
			if j.Verdict != c.want || j.Row != c.wantRow {
				t.Fatalf("row=%d verdict=%s (기대 row=%d %s) · rows=%v derive=%s 해소=%s note=%s",
					j.Row, j.Verdict, c.wantRow, c.want, j.RowsFired, j.Derive, j.Resolution, j.Note)
			}
		})
	}
}

// TestRow0IsPrefixNotVerdict — 행 0은 판정이 아니라 해소 경로다.
// 발화 순서가 [0, 3~7]이어야 §6의 "행 1·2를 면제하고 행 3 이하로 진행"이
// 지켜진 것이다. first-match 계약의 검사이기도 하다.
func TestRow0IsPrefixNotVerdict(t *testing.T) {
	_, l, gc := newIx(t, scopeRecord("m", evidence.AvailObservedZero, 0))
	j := JudgePredicate(pred("m", evidence.PredAbsent, nil, ExpectMustHold, RoleCorroborating), l, gc)
	if len(j.RowsFired) != 2 || j.RowsFired[0] != 0 {
		t.Fatalf("발화 행 %v — 행 0 뒤에 판정 행이 따라와야 한다", j.RowsFired)
	}
	// 개체 해소는 행 0을 거치지 않는다.
	_, l2, gc2 := newIx(t, okRecord("m", 3.0, evidence.DirUp))
	j2 := JudgePredicate(pred("m", evidence.PredPresent, nil, ExpectMustHold, RoleCorroborating), l2, gc2)
	if len(j2.RowsFired) != 1 || j2.RowsFired[0] == 0 {
		t.Fatalf("개체 해소인데 발화 행 %v", j2.RowsFired)
	}
}

// TestRow0AdmitsObservedNotOnlyObservedZero — 1c 인계 ③의 결정.
//
// 완전 조회인데 다른 개체는 나왔고 이 개체만 없는 경우(Availability=observed)도
// 행 0으로 인정한다. 근거는 JudgePredicate 주석 3항 — 특히 §8 지지 자격의
// Availability 화이트리스트가 observed를 명시하므로, observed_zero로 좁히면
// 범위 해소 근거가 §8을 통과할 수 없는 값만 남는다.
func TestRow0AdmitsObservedNotOnlyObservedZero(t *testing.T) {
	// 같은 범위 키를 덮되 다른 개체가 실측된 상황: 범위 행의 Availability가
	// observed다.
	_, l, gc := newIx(t, scopeRecord("m", evidence.AvailObserved, 0))
	j := JudgePredicate(pred("m", evidence.PredAbsent, nil, ExpectMustHold, RoleCorroborating), l, gc)
	if j.Verdict != VerdictSupportedQualified || j.RowsFired[0] != 0 {
		t.Fatalf("verdict=%s rows=%v — observed 범위 해소가 행 0에서 막혔다", j.Verdict, j.RowsFired)
	}
	// 반대로 절단된 범위는 두 Availability 어느 쪽이든 해소가 아니다.
	for _, av := range []evidence.Availability{evidence.AvailObserved, evidence.AvailObservedZero} {
		_, l2, gc2 := newIx(t, scopeRecord("m", av, 3))
		j2 := JudgePredicate(pred("m", evidence.PredAbsent, nil, ExpectMustHold, RoleCorroborating), l2, gc2)
		if j2.Row != 1 {
			t.Fatalf("av=%s 절단 범위가 행 %d에서 해소됐다", av, j2.Row)
		}
	}
}

// TestGateClauseBoundaries — 자격 게이트 조항별 경계.
func TestGateClauseBoundaries(t *testing.T) {
	base := okRecord("m", 3.0, evidence.DirUp)
	p := pred("m", evidence.PredMagnitudeGE, fv(2.0), ExpectMustHold, RoleCorroborating)

	t.Run("not_applicable은 지지 화이트리스트 밖", func(t *testing.T) {
		r := base
		r.Quality.Availability = evidence.AvailNotApplicable
		_, l, gc := newIx(t, r)
		// 파생 자체가 막히므로 행 2다 — "원래 없는 지표"가 지지로 둔갑하는
		// 문은 진리표가 먼저 닫는다.
		if j := JudgePredicate(p, l, gc); j.Row != 2 {
			t.Fatalf("row=%d", j.Row)
		}
	})
	// 창 커버는 **술어 방향에 의존한다**(§8, 사용자 결정 2026-08-04).
	// 경계 3종: 부분 커버 present=자격 / 부분 커버 absent=미달 / 목격 창 밖=미달.
	t.Run("부분 커버 + present류는 자격", func(t *testing.T) {
		r := base
		r.Window.From = winFrom.Add(10 * time.Minute) // 요구 창의 앞부분이 빈다
		_, l, gc := newIx(t, r)
		j := JudgePredicate(p, l, gc) // magnitude_ge must_hold = "그런 값을 목격했다"
		if j.Verdict != VerdictSupportedQualified {
			t.Fatalf("%s · %v — 목격은 부분 커버로 약해지지 않는다", j.Verdict, j.SupportGate.Failed())
		}
		if !hasClause(j.SupportGate, ClauseWindowCover, ClausePass) {
			t.Fatalf("창 조항 상태: %+v", j.SupportGate.Clauses)
		}
	})
	t.Run("부분 커버 + absent류는 미달", func(t *testing.T) {
		// 같은 부분 커버 레코드를 "없었다"의 근거로 쓰면 전체 커버가 필요하다.
		r := scopeRecord("m", evidence.AvailObservedZero, 0)
		r.Window.From = winFrom.Add(10 * time.Minute)
		absent := pred("m", evidence.PredAbsent, nil, ExpectMustHold, RoleCorroborating)
		_, l, gc := newIx(t, r)
		j := JudgePredicate(absent, l, gc)
		if j.Verdict != VerdictSupportedWeak || !hasClause(j.SupportGate, ClauseWindowCover, ClauseFail) {
			t.Fatalf("%s · %v — 창의 절반만 보고 부재를 말할 수 없다", j.Verdict, j.SupportGate.Failed())
		}
	})
	t.Run("present류라도 목격 시각이 창 밖이면 미달", func(t *testing.T) {
		r := base
		// 관측 구간이 통째로 요구 창 뒤에 있다.
		r.Window.From = winTo.Add(time.Hour)
		r.Window.To = winTo.Add(2 * time.Hour)
		_, l, gc := newIx(t, r)
		j := JudgePredicate(p, l, gc)
		if j.Verdict != VerdictSupportedWeak || !hasClause(j.SupportGate, ClauseWindowCover, ClauseFail) {
			t.Fatalf("%s · %v — 창 밖의 목격은 그 창의 증거가 아니다", j.Verdict, j.SupportGate.Failed())
		}
	})
	t.Run("반증 자격은 방향을 보지 않는다(전체 커버 유지)", func(t *testing.T) {
		// present must_hold인 necessary 술어가 불일치(관측 부재)한 경우가
		// 아니라, 여기서는 §5.5 게이트 자체를 직접 돌려 대조한다.
		r := base
		r.Window.From = winFrom.Add(10 * time.Minute)
		ix, _, gc := newIx(t, r)
		rec := ix.Active()[0]
		if g := evaluateGate(rec, gc, gateCounter, claimExistential); g.Qualified {
			t.Fatalf("부분 커버가 반증 자격을 얻었다: %+v", g.Clauses)
		}
	})
	t.Run("수집 지연 컷오프 미도달", func(t *testing.T) {
		r := base
		r.Window.CollectLag = evidence.CollectLag{ValueS: 3600, Status: evidence.TagObserved}
		_, l, gc := newIx(t, r)
		j := JudgePredicate(p, l, gc)
		if j.Verdict != VerdictSupportedWeak || !hasClause(j.SupportGate, ClauseCollectLag, ClauseFail) {
			t.Fatalf("%s · %v", j.Verdict, j.SupportGate.Failed())
		}
	})
	t.Run("표본 미상은 미달로 세지 않는다(원천 부재)", func(t *testing.T) {
		_, l, gc := newIx(t, base)
		j := JudgePredicate(p, l, gc)
		if j.Verdict != VerdictSupportedQualified {
			t.Fatalf("%s — 원천 없는 조항이 게이트를 죽였다", j.Verdict)
		}
		if !hasClause(j.SupportGate, ClauseSample, ClauseNoSource) {
			t.Fatalf("표본 조항 상태 기록 없음: %+v", j.SupportGate.Clauses)
		}
	})
	t.Run("표본 하한 설정 시 실제로 강제된다", func(t *testing.T) {
		r := base
		n := 3
		r.Quality.SampleN = &n
		_, l, gc := newIx(t, r)
		min := 10
		gc.MinSampleN = &min
		j := JudgePredicate(p, l, gc)
		if j.Verdict != VerdictSupportedWeak || !hasClause(j.SupportGate, ClauseSample, ClauseFail) {
			t.Fatalf("%s · %v", j.Verdict, j.SupportGate.Failed())
		}
	})
	t.Run("superseded 근거는 자격 없음", func(t *testing.T) {
		ix := evidence.NewIndex()
		evidence.NewLedger(ix)
		e1, err := ix.Append(base)
		if err != nil {
			t.Fatal(err)
		}
		r2 := okRecord("m", 3.0, evidence.DirUp)
		r2.Supersedes = e1
		if _, err := ix.Append(r2); err != nil {
			t.Fatal(err)
		}
		// 장부는 후계로 재판정했으므로 판정 자체는 살아 있다. 죽은 EID를
		// 직접 게이트에 넣으면 active 조항이 잡는다.
		gc := GateContext{Index: ix, Now: nowT, RequiredWindow: &TimeRange{From: winFrom, To: winTo}}
		dead, _ := ix.Get(e1)
		g := evaluateGate(dead, gc, gateSupport, claimExistential)
		if g.Qualified || !hasClause(g, ClauseActive, ClauseFail) {
			t.Fatalf("superseded 레코드가 자격을 얻었다: %+v", g.Clauses)
		}
	})
}

// TestScopeResolutionRequiresAllEvidenceQualified — 범위 해소는 근거가
// 여럿일 수 있고, 하나만 통과하면 자격이 아니다(1c의 absent 논리와 같은 규율).
func TestScopeResolutionRequiresAllEvidenceQualified(t *testing.T) {
	good := scopeRecord("m", evidence.AvailObservedZero, 0)
	bad := scopeRecord("m", evidence.AvailObservedZero, 0)
	bad.Quality.Confidence = evidence.ConfLow
	_, l, gc := newIx(t, good, bad)
	j := JudgePredicate(pred("m", evidence.PredAbsent, nil, ExpectMustHold, RoleCorroborating), l, gc)
	if j.Verdict != VerdictSupportedWeak {
		t.Fatalf("verdict=%s — 형제 하나가 미달인데 자격 지지가 됐다(EIDs=%v)", j.Verdict, j.EIDs)
	}
}

func hasClause(g GateResult, name string, st ClauseState) bool {
	for _, c := range g.Clauses {
		if c.State != st {
			continue
		}
		if c.Name == name || len(c.Name) > len(name) && c.Name[len(c.Name)-len(name):] == name {
			return true
		}
	}
	return false
}
