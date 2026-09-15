package ledger

import (
	"testing"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
)

// qualifiedJudgment은 자격 지지 하나를 만든다(계보·사상표 행을 인자로 —
// §8 다양성·계보 요건의 계수 재료).
func qualifiedJudgment(id, class, lineage, eid string) PredJudgment {
	return PredJudgment{
		PredID: id, Verdict: VerdictSupportedQualified, Row: 3, RowsFired: []int{3},
		Holds: true, Role: RoleCorroborating, EIDs: []string{eid},
		FindingClasses: []string{class}, LineageIDs: []string{lineage},
		EntityKeys: []string{"sql-42"},
	}
}

// fullyConfirmable는 §8 요건을 전수 충족하는 가설이다 — 시험이 여기서
// 조건을 **하나씩 빼면서** 각 요건이 실제로 게이트를 지는지 본다.
func fullyConfirmable() HypothesisV2 {
	nec := qualifiedJudgment("H1-P1", "db_blocking/event", "LG-a", "EIX-0001")
	nec.Role = RoleNecessary
	cor := qualifiedJudgment("H1-P2", "db_slow_queries/sql", "LG-b", "EIX-0002")
	return HypothesisV2{
		ID:         "H1",
		Identity:   HypoIdentity{CauseEntity: "db1", TemporalClaim: TemporalPrecedes},
		Judgments:  []PredJudgment{nec, cor},
		Preds:      []SignalPred{{PredID: "H1-P1"}, {PredID: "H1-P2"}},
		Chain:      []ChainClaim{{ClaimID: "C1", Kind: ChainTerminalEntity, EntityKey: "SQL-42", Effect: "e", Origin: OriginProjector}},
		NewObservationEIDs:      map[string]bool{"EIX-0002": true},
		Temporal:                []TemporalJudged{{Verdict: TemporalPrecedesVerdict}},
		AuditAPassed:            true,
		TopologyAligned:         true,
		RecoveryDone:            true,
		DiscriminationExhausted: true,
	}
}

func confirmedView(h HypothesisV2) ViewV2 {
	return ViewV2{Hypotheses: []HypothesisV2{h}, LayerAComplete: true}
}

// TestConfirmedRequiresEveryGate — 요건을 하나씩 무너뜨리면 그 요건 이름이
// 미충족 목록에 뜨고 판정이 probable로 내려간다.
func TestConfirmedRequiresEveryGate(t *testing.T) {
	if d := ProjectStatus(confirmedView(fullyConfirmable())); d.Internal != IntConfirmed {
		t.Fatalf("기준선이 confirmed가 아니다: %s · 미충족 %v", d.Internal, d.MissingRequirements)
	}

	cases := []struct {
		name string
		want string
		bend func(*HypothesisV2, *ViewV2)
	}{
		{"자격 지지 1건", ReqQualifiedSupports, func(h *HypothesisV2, _ *ViewV2) {
			h.Judgments = h.Judgments[:1]
			h.Chain[0].EntityKey = "sql-42"
		}},
		{"다양성 축 1종", ReqDiversity, func(h *HypothesisV2, _ *ViewV2) {
			h.Judgments[1].FindingClasses = h.Judgments[0].FindingClasses
		}},
		{"계보 1종", ReqLineage, func(h *HypothesisV2, _ *ViewV2) {
			h.Judgments[1].LineageIDs = h.Judgments[0].LineageIDs
		}},
		{"신규 관측 0", ReqNewObservation, func(h *HypothesisV2, _ *ViewV2) {
			h.NewObservationEIDs = nil
		}},
		{"시간 판정 없음", ReqTemporal, func(h *HypothesisV2, _ *ViewV2) {
			h.Temporal = nil
		}},
		{"시간 주장 unknown", ReqTemporal, func(h *HypothesisV2, _ *ViewV2) {
			h.Identity.TemporalClaim = TemporalUnknown
		}},
		{"말단 개체가 근거와 안 맞음", ReqTerminalDepth, func(h *HypothesisV2, _ *ViewV2) {
			h.Chain[0].EntityKey = "다른개체"
		}},
		{"necessary가 미해결로 남음", ReqNecessaryResolved, func(h *HypothesisV2, _ *ViewV2) {
			h.Judgments[0].Verdict = VerdictInconclusive
			h.Judgments[0].Row = 6
		}},
		{"심사점 A 미통과", ReqAuditA, func(h *HypothesisV2, _ *ViewV2) { h.AuditAPassed = false }},
		{"위상 불부합(역방향·무경로)", ReqTopologyDirection, func(h *HypothesisV2, _ *ViewV2) { h.TopologyAligned = false }},
		{"회수 의무 미이행", ReqRecovery, func(h *HypothesisV2, _ *ViewV2) { h.RecoveryDone = false }},
		{"계층 A 미완주", ReqLayerAComplete, func(_ *HypothesisV2, v *ViewV2) { v.LayerAComplete = false }},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			h := fullyConfirmable()
			v := confirmedView(h)
			c.bend(&v.Hypotheses[0], &v)
			d := ProjectStatus(v)
			if d.Internal != IntProbable {
				t.Fatalf("status=%s (미충족 %v)", d.Internal, d.MissingRequirements)
			}
			found := false
			for _, m := range d.MissingRequirements {
				if m == c.want {
					found = true
				}
			}
			if !found {
				t.Fatalf("미충족 목록 %v에 %s가 없다", d.MissingRequirements, c.want)
			}
			if d.Status != StatusProvisional {
				t.Fatalf("공개 사상이 %s — probable은 provisional이다", d.Status)
			}
		})
	}
}

// TestPreSatisfiedExcludedFromQualifiedCount — C-4·1b 인계 ②.
//
// index selector를 그대로 베낀 회고 술어는 진리표 행 3을 정의상 통과한다.
// 그것이 "자격 지지 ≥2 · 다양성 2종 · 계보 2종"을 **무상으로** 채우면
// confirmed가 공짜가 된다. 계수에서 빼는 분기가 그 문을 닫는다.
func TestPreSatisfiedExcludedFromQualifiedCount(t *testing.T) {
	h := fullyConfirmable()
	// 두 지지 중 하나를 회고(pre_satisfied)로 바꾼다 — 판정 등급은 그대로
	// qualified다(진리표는 딱지를 보지 않는다).
	h.Judgments[1].PreSatisfied = true
	tally := h.Count()
	if tally.QualifiedSupports != 1 || tally.PreSatisfiedSupports != 1 {
		t.Fatalf("계수 %+v — 회고 지지가 자격 계수에 남았다", tally)
	}
	// 다양성·계보도 회고 술어의 것을 물려받지 않는다.
	if len(tally.DiversityClasses) != 1 || len(tally.Lineages) != 1 {
		t.Fatalf("다양성 %v · 계보 %v — 회고 술어의 축이 셈에 들었다",
			tally.DiversityClasses, tally.Lineages)
	}
	d := ProjectStatus(confirmedView(h))
	if d.Internal != IntProbable {
		t.Fatalf("status=%s — 회고 술어만으로 confirmed에 도달했다", d.Internal)
	}
	// 다만 probable의 "passed ≥1"에는 든다(§8 — 지지 실패가 아니다).
	if tally.PassedAny() != 2 {
		t.Fatalf("passed=%d — 회고 지지가 probable 계수에서도 빠졌다", tally.PassedAny())
	}
}

// TestNewObservationRequiresProducedEID — C-9. 같은 결론을 재보고해도
// 신규 관측이 아니다 — 실행이 낳은 EID에 결박돼야 한다.
func TestNewObservationRequiresProducedEID(t *testing.T) {
	h := fullyConfirmable()
	h.NewObservationEIDs = map[string]bool{"EIX-9999": true} // 이 가설과 무관한 EID
	if n := h.Count().NewObservations; n != 0 {
		t.Fatalf("신규 관측 %d — 실행이 낳지 않은 레코드가 셈에 들었다", n)
	}
	h.NewObservationEIDs = map[string]bool{"EIX-0001": true}
	if n := h.Count().NewObservations; n != 1 {
		t.Fatalf("신규 관측 %d", n)
	}
	// pre_satisfied 술어는 produced_eids에 있어도 신규가 아니다.
	h.Judgments[0].PreSatisfied = true
	if n := h.Count().NewObservations; n != 0 {
		t.Fatalf("신규 관측 %d — 회고 술어가 신규로 셈됐다", n)
	}
}

// TestDiversityAxisIsFindingClassNotAspect — C-5.
//
// db_slow_queries와 db_blocking은 Aspect가 둘 다 db다. 다양성 축을 Aspect로
// 잡으면 **락 경합 정답의 가장 직접적인 두 증거**가 원리적으로 요건을
// 충족할 수 없다. 사상표 행(도구×class)으로 잡으면 갈린다.
func TestDiversityAxisIsFindingClassNotAspect(t *testing.T) {
	// 실물 사상표에서 두 행의 Aspect가 실제로 같은지부터 확인한다 —
	// 지적의 전제가 코드에서 여전히 참이어야 이 선택이 의미가 있다.
	blocking, ok1 := evidence.RowFor("db_blocking/event")
	slow, ok2 := evidence.RowFor("db_slow_queries/sql")
	if !ok1 || !ok2 {
		t.Fatal("사상표에 두 행이 없다")
	}
	if blocking.Aspect != slow.Aspect {
		t.Skip("사상표가 바뀌어 두 행의 Aspect가 갈렸다 — C-5의 전제 소멸")
	}
	h := fullyConfirmable() // 두 지지가 정확히 이 두 행이다
	if d := ProjectStatus(confirmedView(h)); d.Internal != IntConfirmed {
		t.Fatalf("Aspect가 같은 두 증거가 다양성 요건에서 죽었다: %v", d.MissingRequirements)
	}
}

// TestInferiorTruthTable — §8 경쟁 서열. **결손은 unresolved다.**
func TestInferiorTruthTable(t *testing.T) {
	adopted := fullyConfirmable()
	at := adopted.Count()

	base := func() HypothesisV2 {
		nec := PredJudgment{PredID: "H2-P1", Verdict: VerdictInvalidated, Row: 5,
			Role: RoleNecessary, EIDs: []string{"EIX-0100"}}
		return HypothesisV2{ID: "H2", Judgments: []PredJudgment{nec},
			Preds: []SignalPred{{PredID: "H2-P1"}}, DiscriminationExhausted: true}
	}
	// refuted는 inferior가 아니다(별도 상태).
	c := base()
	if IsInferiorV2(c, c.Count(), at) {
		t.Fatal("refuted가 inferior로 접혔다")
	}

	// necessary가 판정된 적 없으면 inferior 자격 없음 — unresolved다.
	c = base()
	c.Judgments[0].Verdict = VerdictInconclusive
	c.Judgments[0].Row = 6
	if IsInferiorV2(c, c.Count(), at) {
		t.Fatal("미실측 necessary 가설이 inferior로 접혔다 — '안 본 것'과 '진 것'의 구분이 무너진다")
	}

	// 감별 소진 전이면 inferior가 아니다(예산 소진 시 전원 inferior 방지).
	c = base()
	c.Judgments[0].Verdict = VerdictSupportedWeak
	c.Judgments[0].Row = 4
	c.DiscriminationExhausted = false
	if IsInferiorV2(c, c.Count(), at) {
		t.Fatal("감별이 남았는데 inferior")
	}
	// 감별 소진 + qualified 지지 0 + 지지 수 열세 → inferior.
	c.DiscriminationExhausted = true
	if !IsInferiorV2(c, c.Count(), at) {
		t.Fatalf("inferior 요건을 전부 채웠는데 아니다: %+v", c.Count())
	}
	// qualified 지지가 하나라도 있으면 inferior가 아니다.
	c.Judgments[0].Verdict = VerdictSupportedQualified
	c.Judgments[0].Row = 3
	if IsInferiorV2(c, c.Count(), at) {
		t.Fatal("qualified 지지 보유 가설이 inferior")
	}
}

// TestUnresolvedCompetitorBlocksConfirmed — unresolved가 남으면 confirmed 불가.
func TestUnresolvedCompetitorBlocksConfirmed(t *testing.T) {
	adopted := fullyConfirmable()
	rival := HypothesisV2{ID: "H2", CreatedSeq: 1,
		Judgments: []PredJudgment{{PredID: "H2-P1", Verdict: VerdictInconclusive, Row: 6,
			Role: RoleNecessary}},
		Preds: []SignalPred{{PredID: "H2-P1"}}}
	v := ViewV2{Hypotheses: []HypothesisV2{adopted, rival}, LayerAComplete: true}
	d := ProjectStatus(v)
	if d.AdoptedID != "H1" {
		t.Fatalf("채택 %s", d.AdoptedID)
	}
	if d.Competitors["H2"] != CompUnresolved {
		t.Fatalf("경쟁 상태 %s", d.Competitors["H2"])
	}
	if d.Internal != IntProbable {
		t.Fatalf("unresolved 경쟁이 남았는데 %s", d.Internal)
	}
	// 그 경쟁이 반증되면 confirmed가 열린다.
	v.Hypotheses[1].Judgments[0].Verdict = VerdictInvalidated
	v.Hypotheses[1].Judgments[0].Row = 5
	d = ProjectStatus(v)
	if d.Internal != IntConfirmed || d.Competitors["H2"] != CompRefuted {
		t.Fatalf("status=%s comp=%s 미충족=%v", d.Internal, d.Competitors["H2"], d.MissingRequirements)
	}
}

// TestInsufficientWhenNoSupport — 지지 0건(weak 포함)이면 insufficient.
func TestInsufficientWhenNoSupport(t *testing.T) {
	h := HypothesisV2{ID: "H1",
		Judgments: []PredJudgment{{PredID: "H1-P1", Verdict: VerdictInconclusive, Row: 1}},
		Preds:     []SignalPred{{PredID: "H1-P1"}}}
	d := ProjectStatus(ViewV2{Hypotheses: []HypothesisV2{h}})
	if d.Internal != IntInsufficient || d.Status != StatusInsufficient {
		t.Fatalf("%s / %s", d.Internal, d.Status)
	}
	// 전 가설 반증이면 생존 가설이 없다.
	h.Judgments[0].Verdict = VerdictInvalidated
	h.Judgments[0].Role = RoleNecessary
	d = ProjectStatus(ViewV2{Hypotheses: []HypothesisV2{h}})
	if d.Internal != IntInsufficient || d.AdoptedID != "" {
		t.Fatalf("%s / 채택 %q", d.Internal, d.AdoptedID)
	}
}

// TestInvalidPredicatesAreNotCounted — Undecidable·AuditedOut 딱지가 붙은
// 술어는 셈·정렬에서 빠진다(§6 기계 딱지).
func TestInvalidPredicatesAreNotCounted(t *testing.T) {
	h := fullyConfirmable()
	h.Preds[1].AuditedOut = true
	if n := h.Count().QualifiedSupports; n != 1 {
		t.Fatalf("자격 지지 %d — 심사 탈락 술어가 셈에 남았다", n)
	}
	h.Preds[1].AuditedOut = false
	h.Preds[1].Undecidable = true
	if n := h.Count().QualifiedSupports; n != 1 {
		t.Fatalf("자격 지지 %d — 판정 불능 술어가 셈에 남았다", n)
	}
}

// TestRankOrder — §8 Rank 총순서(qualified → weak → 미반증 necessary 비율 →
// 시간·말단 요건 → 등록순).
func TestRankOrder(t *testing.T) {
	mk := func(id string, seq, qual, weak int) HypothesisV2 {
		h := HypothesisV2{ID: id, CreatedSeq: seq}
		for i := 0; i < qual; i++ {
			h.Judgments = append(h.Judgments, qualifiedJudgment(id, "c", "l", "e"))
		}
		for i := 0; i < weak; i++ {
			h.Judgments = append(h.Judgments,
				PredJudgment{PredID: id, Verdict: VerdictSupportedWeak, Row: 4, Role: RoleCorroborating})
		}
		return h
	}
	got := RankV2([]HypothesisV2{
		mk("C", 3, 1, 5), mk("A", 1, 2, 0), mk("D", 4, 1, 5), mk("B", 2, 1, 9),
	})
	want := []string{"A", "B", "C", "D"}
	for i, h := range got {
		if h.ID != want[i] {
			t.Fatalf("순위 %d = %s, 기대 %s (전체 %v)", i, h.ID, want[i], ids(got))
		}
	}
}

// TestChangedKeysIsComputable — C-9의 나머지 절반: "직전 바퀴 대비 무엇이
// 바뀌었는가"가 실제로 계산되는지. 같은 결론의 재보고는 delta가 아니다.
func TestChangedKeysIsComputable(t *testing.T) {
	ix := evidence.NewIndex()
	l := evidence.NewLedger(ix)
	if _, err := ix.Append(okRecord("m1", 3.0, evidence.DirUp)); err != nil {
		t.Fatal(err)
	}
	before := LedgerSnapshot(l)
	if len(before) != 1 {
		t.Fatalf("사영 %d건", len(before))
	}
	// 같은 관측을 다시 "보고"해도 장부가 바뀌지 않으면 delta는 0이다.
	if n := len(ChangedKeys(before, LedgerSnapshot(l))); n != 0 {
		t.Fatalf("변화 %d — 재보고가 진전으로 셈됐다", n)
	}
	// 새 관측 키가 생기면 delta다.
	if _, err := ix.Append(okRecord("m2", 2.0, evidence.DirUp)); err != nil {
		t.Fatal(err)
	}
	if n := len(ChangedKeys(before, LedgerSnapshot(l))); n != 1 {
		t.Fatalf("변화 %d건", n)
	}
	// supersede로 근거가 바뀌어도 delta다(결론이 같아도 근거가 다르다).
	cur := LedgerSnapshot(l)
	sup := okRecord("m1", 9.0, evidence.DirUp)
	sup.Supersedes = "EIX-0001"
	if _, err := ix.Append(sup); err != nil {
		t.Fatal(err)
	}
	if n := len(ChangedKeys(cur, LedgerSnapshot(l))); n != 1 {
		t.Fatalf("supersede 변화 %d건", n)
	}
}

func ids(hs []HypothesisV2) []string {
	out := make([]string, 0, len(hs))
	for _, h := range hs {
		out = append(out, h.ID)
	}
	return out
}
