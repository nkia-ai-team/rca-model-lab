package pipeline

import (
	"strings"
	"testing"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/ledger"
)

// ── 시험 재료 ───────────────────────────────────────────────────

// admPred는 등록 규칙 1을 통과하는 기준 술어다 — 각 시험은 여기서 한 축만
// 어긋뜨려 그 축의 반려를 확인한다.
func admPred(mut ...func(*ledger.SignalPred)) ledger.SignalPred {
	p := ledger.SignalPred{
		Tool: evidence.SrcScanMetrics, TargetID: "db-1", Aspect: evidence.AspectMetric,
		Metric: "cpu.util", Predicate: evidence.PredDirectionUp,
		Window: evidence.WindowFull, Expectation: evidence.ExpectMustHold,
		Role: ledger.RoleNecessary,
	}
	for _, f := range mut {
		f(&p)
	}
	return p
}

// admCand는 기계 관문을 통과하는 기준 후보다.
func admCand(target, cause string, preds ...ledger.SignalPred) Candidate {
	return Candidate{
		TargetID: target,
		Chain:    []ledger.ChainStep{{Entity: target, Effect: "원인"}, {Entity: "app-1", Effect: "증상"}},
		Source:   ledger.SourceInvestigation, Prior: ledger.PriorMedium, PriorRationale: "근거",
		IdentityKey: ledger.HypoIdentity{
			CauseEntity: cause, Mechanism: "포화", TemporalClaim: ledger.TemporalPrecedes,
		},
		PredictedSignals: preds,
	}
}

// admRec는 index에 실을 관측 레코드다(사상표 행 scan_metrics/shifted).
func admRec(target, metric string, w evidence.WindowClass) evidence.EvidenceIndexRecord {
	return evidence.EvidenceIndexRecord{
		TargetID: target, RecordKind: evidence.KindFinding, Aspect: evidence.AspectMetric,
		FindingClass: "scan_metrics/shifted", Headline: target + " " + metric + " 상승",
		Effect: evidence.Effect{Kind: evidence.EffectRatio, Metric: metric, Direction: evidence.DirUp},
		Window: evidence.TimeWindow{Class: w},
		Quality: evidence.Quality{Status: evidence.StatusAnomalous,
			Availability: evidence.AvailObserved, Confidence: evidence.ConfOK},
		Provenance: evidence.Provenance{Source: evidence.SrcScanMetrics, EnvelopeRef: "EST-0001:scan_metrics"},
	}
}

// admInput은 어휘·명제 장부·index가 배선된 입력이다. recs를 실으면 그것이
// 곧 기실측 명제가 된다(§6.0 장부는 index에 부착된다).
func admInput(t *testing.T, vocab []string, recs ...evidence.EvidenceIndexRecord) (GenerateInput, []string) {
	t.Helper()
	ix := evidence.NewIndex()
	var eids []string
	for _, r := range recs {
		eid, err := ix.Append(r)
		if err != nil {
			t.Fatalf("index 적재: %v", err)
		}
		eids = append(eids, eid)
	}
	return GenerateInput{
		Evidence: ix, Vocab: NewTopologyVocab(vocab...), Propositions: evidence.NewLedger(ix),
	}, eids
}

// rejectedRules는 반려 사유의 규칙 지목만 뽑는다.
func rejectedRules(rep AdmissionReport) []string {
	var out []string
	for _, r := range rep.Rejected {
		for _, rj := range r.Rejects {
			out = append(out, rj.Rule)
		}
	}
	return out
}

func hasRule(rep AdmissionReport, rule string) bool {
	for _, got := range rejectedRules(rep) {
		if got == rule {
			return true
		}
	}
	return false
}

// ── 규칙 1 ⓐ~ⓓ ────────────────────────────────────────────────

// 규칙 1의 네 축이 각각 반려를 낳고, 위반 술어에 undecidable 딱지가 붙는다.
func TestAdmitRule1(t *testing.T) {
	in, _ := admInput(t, []string{"db-1"})
	cases := []struct {
		name string
		pred ledger.SignalPred
		rule string
	}{
		{"ⓐ 위상 어휘 밖", admPred(func(p *ledger.SignalPred) { p.TargetID = "떠도는-대상" }), "규칙 1ⓐ"},
		{"ⓑ (tool,aspect) 사상표 밖", admPred(func(p *ledger.SignalPred) { p.Aspect = evidence.AspectEvent }), "규칙 1ⓑ"},
		{"ⓒ 조합 무효 — must_hold 누락", admPred(func(p *ledger.SignalPred) { p.Expectation = "" }), "규칙 1ⓒ"},
		{"ⓒ 조합 무효 — magnitude에 threshold 없음",
			admPred(func(p *ledger.SignalPred) { p.Predicate = evidence.PredMagnitudeGE }), "규칙 1ⓒ"},
		{"ⓓ tool 허용 합집합 밖", admPred(func(p *ledger.SignalPred) {
			th := 2.0
			p.Tool, p.Aspect = evidence.SrcComparePeers, evidence.AspectPeer
			p.Predicate, p.Threshold = evidence.PredMagnitudeGE, &th
		}), "규칙 1ⓓ"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			rep := Admit([]Candidate{admCand("db-1", "db-1", c.pred, admPred())}, in)
			if len(rep.Admitted) != 0 {
				t.Fatalf("위반 술어가 통과함: %+v", rep.Admitted)
			}
			if !hasRule(rep, c.rule) {
				t.Fatalf("반려 사유 = %v, want %s", rejectedRules(rep), c.rule)
			}
			var undecidable int
			for _, tg := range rep.PredTags {
				if tg.Tag == TagUndecidable {
					undecidable++
				}
			}
			if undecidable == 0 {
				t.Error("undecidable 딱지가 없다 — 반려 사유의 술어 지목이 사라진다")
			}
		})
	}
}

// **Metric·EntityKey의 실재는 검사하지 않는다**(§6.2-1 명문) — prospective
// 술어(아직 조회 안 한 개체에 대한 예측)와 양립해야 한다.
func TestAdmitRule1ProspectiveNotChecked(t *testing.T) {
	in, _ := admInput(t, []string{"db-1"})
	c := admCand("db-1", "db-1", admPred(func(p *ledger.SignalPred) {
		p.Metric, p.EntityKey = "아무도.조회한.적.없는.지표", "sql_key=존재하지않음"
	}))
	c.SupportEIDs = nil
	rep := Admit([]Candidate{c}, in)
	// SupportEIDs 0으로 반려되더라도 **규칙 1 사유는 없어야** 한다.
	for _, r := range rejectedRules(rep) {
		if strings.HasPrefix(r, "규칙 1") {
			t.Fatalf("미실재 Metric·EntityKey를 규칙 1이 반려함(%s) — prospective 양립 위반", r)
		}
	}
}

// ── 규칙 2 (pre_satisfied) — 창 축 무시 ────────────────────────

// 창 변형(full↔onset_narrow) 우회 봉쇄: full 창에서 실측된 관측 키를
// onset_narrow로 바꿔 재등록해도 기실측이며 pre_satisfied가 붙는다(4차 A-3).
func TestAdmitRule2WindowBypassBlocked(t *testing.T) {
	in, eids := admInput(t, []string{"db-1"}, admRec("db-1", "cpu.util", evidence.WindowFull))
	narrow := admPred(func(p *ledger.SignalPred) {
		p.Window, p.EIDHint = evidence.WindowOnsetNarrow, eids[0]
	})
	c := admCand("db-1", "db-1", narrow)
	c.SupportEIDs = []string{eids[0]}

	rep := Admit([]Candidate{c}, in)
	var pre bool
	for _, tg := range rep.PredTags {
		if tg.Tag == TagPreSatisfied {
			pre = true
		}
	}
	if !pre {
		t.Fatalf("창만 바꾼 술어가 미실측으로 통과 — 2값 우회가 열려 있다. tags=%+v", rep.PredTags)
	}
	// 예측표 전체가 기실측이므로 규칙 3이 회고로 반려한다.
	if !hasRule(rep, "규칙 3") {
		t.Fatalf("반려 사유 = %v, want 규칙 3(회고)", rejectedRules(rep))
	}
}

// ── 규칙 3 ────────────────────────────────────────────────────

func TestAdmitRule3(t *testing.T) {
	// 실측 레코드 하나(db-1 cpu.util) + 미실측 축(mem.util)을 함께 둔다.
	in, eids := admInput(t, []string{"db-1"}, admRec("db-1", "cpu.util", evidence.WindowFull))
	measuredPred := admPred(func(p *ledger.SignalPred) { p.EIDHint = eids[0] })
	freshPred := admPred(func(p *ledger.SignalPred) {
		p.Metric, p.Role = "mem.util", ledger.RoleCorroborating
	})

	t.Run("전부 기실측이면 회고 반려", func(t *testing.T) {
		c := admCand("db-1", "db-1", measuredPred)
		c.SupportEIDs = []string{eids[0]}
		rep := Admit([]Candidate{c}, in)
		if len(rep.Admitted) != 0 || !hasRule(rep, "규칙 3") {
			t.Fatalf("회고 가설이 통과: admitted=%d rules=%v", len(rep.Admitted), rejectedRules(rep))
		}
	})

	t.Run("necessary 전부 pre_satisfied면 죽지 않는 가설 반려", func(t *testing.T) {
		// necessary는 기실측·충족(pre_satisfied), 미판정은 corroborating뿐.
		c := admCand("db-1", "db-1", measuredPred, freshPred)
		c.SupportEIDs = []string{eids[0]}
		rep := Admit([]Candidate{c}, in)
		if len(rep.Admitted) != 0 {
			t.Fatalf("죽지 않는 가설이 통과함: %+v", rep.Admitted)
		}
		var found bool
		for _, r := range rep.Rejected {
			for _, rj := range r.Rejects {
				if strings.Contains(rj.Reason, "죽지 않는 가설") {
					found = true
				}
			}
		}
		if !found {
			t.Fatalf("반려 사유에 necessary 요건이 없음: %v", rep.Rejected)
		}
	})

	t.Run("미판정 necessary가 있으면 통과", func(t *testing.T) {
		live := admPred(func(p *ledger.SignalPred) { p.Metric = "mem.util" })
		c := admCand("db-1", "db-1", measuredPred, live)
		c.SupportEIDs = []string{eids[0]}
		rep := Admit([]Candidate{c}, in)
		if len(rep.Admitted) != 1 {
			t.Fatalf("정상 후보가 반려됨: %v", rep.Rejected)
		}
		// 기실측 술어에는 pre_satisfied가, 미실측 술어에는 아무 딱지도 없다.
		if !rep.Admitted[0].PredictedSignals[0].PreSatisfied ||
			rep.Admitted[0].PredictedSignals[1].PreSatisfied {
			t.Errorf("pre_satisfied 딱지 위치가 어긋남: %+v", rep.Admitted[0].PredictedSignals)
		}
	})
}

// ── 규칙 4 (식별력 행렬) ───────────────────────────────────────

func TestAdmitCohortDiscrimination(t *testing.T) {
	same := admPred()
	opposite := admPred(func(p *ledger.SignalPred) { p.Expectation = evidence.ExpectMustNotHold })

	t.Run("같은 명제 키에서 Expectation이 갈리면 식별력 있음", func(t *testing.T) {
		out, _ := AdmitCohort([]AdmittedCandidate{
			{Candidate: admCand("db-1", "db-1", same)},
			{Candidate: admCand("app-1", "app-1", opposite)},
		})
		for i, c := range out {
			if len(c.Tags) != 0 {
				t.Errorf("[%d] 갈리는데 딱지가 붙음: %v", i, c.Tags)
			}
		}
	})

	t.Run("안 갈리면 non_discriminating 기록(반려 아님)", func(t *testing.T) {
		out, _ := AdmitCohort([]AdmittedCandidate{
			{Candidate: admCand("db-1", "db-1", same)},
			{Candidate: admCand("app-1", "app-1", same)},
		})
		if len(out) != 2 {
			t.Fatalf("식별력 미달이 반려됨: %d개", len(out))
		}
		for i, c := range out {
			if len(c.Tags) != 1 || c.Tags[0] != TagNonDiscriminating {
				t.Errorf("[%d] tags = %v, want non_discriminating", i, c.Tags)
			}
		}
	})
}

// ── SupportEIDs 결박 ──────────────────────────────────────────

func TestAdmitSupportEIDBinding(t *testing.T) {
	in, eids := admInput(t, []string{"db-1"},
		admRec("db-1", "cpu.util", evidence.WindowFull),
		admRec("db-1", "mem.util", evidence.WindowFull))
	live := admPred(func(p *ledger.SignalPred) { p.Metric = "disk.util" })

	t.Run("결박된 EID만 근거로 센다", func(t *testing.T) {
		bound := admPred(func(p *ledger.SignalPred) { p.EIDHint = eids[0] })
		c := admCand("db-1", "db-1", bound, live)
		// eids[1]은 어느 술어도 지목하지 않는다 — 불산입.
		c.SupportEIDs = []string{eids[0], eids[1]}
		rep := Admit([]Candidate{c}, in)
		if len(rep.Admitted) != 1 {
			t.Fatalf("반려됨: %v", rep.Rejected)
		}
		if got := rep.Admitted[0].SupportEIDs; len(got) != 1 || got[0] != eids[0] {
			t.Errorf("SupportEIDs = %v, want [%s]만", got, eids[0])
		}
		if len(rep.Unbound) != 1 || rep.Unbound[0].EID != eids[1] {
			t.Errorf("불산입 기록 = %+v, want %s 1건", rep.Unbound, eids[1])
		}
	})

	t.Run("selector 불일치는 불산입", func(t *testing.T) {
		// eids[0]은 cpu.util 레코드인데 술어는 disk.util을 지목한다.
		mismatched := admPred(func(p *ledger.SignalPred) {
			p.Metric, p.EIDHint = "disk.util", eids[0]
		})
		c := admCand("db-1", "db-1", mismatched)
		c.SupportEIDs = []string{eids[0]}
		rep := Admit([]Candidate{c}, in)
		if len(rep.Admitted) != 0 {
			t.Fatalf("selector 불일치 근거로 개설됨: %+v", rep.Admitted)
		}
		if len(rep.Unbound) != 1 || !strings.Contains(rep.Unbound[0].Reason, "selector") {
			t.Errorf("불산입 사유 = %+v, want selector 불일치", rep.Unbound)
		}
	})

	t.Run("유효 0이면 개설 반려", func(t *testing.T) {
		c := admCand("db-1", "db-1", live)
		c.SupportEIDs = []string{"EIX-9999"} // index에 없는 EID
		rep := Admit([]Candidate{c}, in)
		if len(rep.Admitted) != 0 || !hasRule(rep, "SupportEIDs") {
			t.Fatalf("근거 0으로 개설됨: admitted=%d rules=%v", len(rep.Admitted), rejectedRules(rep))
		}
	})
}

// ── 다양성 소프트 · 개수 ──────────────────────────────────────

// sentinel(unknown:*)은 다양성 계수에서 제외된다 — 대항 가설은 다양성
// 충족 수단이 아니다(§6.2).
func TestAdmitCohortDiversitySentinelExcluded(t *testing.T) {
	p1 := admPred()
	p2 := admPred(func(p *ledger.SignalPred) { p.Expectation = evidence.ExpectMustNotHold })
	real1 := AdmittedCandidate{Candidate: admCand("db-1", "db-1", p1)}
	real2 := AdmittedCandidate{Candidate: admCand("app-1", "app-1", p2)}
	sentinel := AdmittedCandidate{Candidate: admCand("app-1", ledger.UnknownCause("app-1"), p2)}

	_, tags := AdmitCohort([]AdmittedCandidate{real1, sentinel, sentinel})
	if !hasTag(tags, TagDiversityUnmet) {
		t.Errorf("sentinel이 다양성을 채웠다: %v", tags)
	}
	_, tags = AdmitCohort([]AdmittedCandidate{real1, real2, sentinel})
	if hasTag(tags, TagDiversityUnmet) {
		t.Errorf("실 원인 2종인데 미충족: %v", tags)
	}
	// 개수 3 미만은 기록 후 진행(반려가 아니다 — 처분은 규칙 6 = 3b).
	out, tags := AdmitCohort([]AdmittedCandidate{real1, real2})
	if len(out) != 2 || !hasTag(tags, TagCountBelowMin) {
		t.Errorf("개수 하한 기록 없음: out=%d tags=%v", len(out), tags)
	}
}

// hasTag는 §14-4 4c에서 본체(generate.go)로 옮겼다 — 다양성 재요구 판정이
// 같은 조회를 쓴다.

// 위상 어휘는 append-only다 — 확장은 버전을 올리고, nil은 공집합이다.
func TestTopologyVocab(t *testing.T) {
	v := NewTopologyVocab("a", "b", "a")
	if !v.Has("a") || v.Has("c") || len(v.IDs()) != 2 || v.Version() != 1 {
		t.Fatalf("동결 어휘 = %v (v%d)", v.IDs(), v.Version())
	}
	v.Extend("b")
	if v.Version() != 1 {
		t.Errorf("이미 있는 값에 버전이 올랐다: v%d", v.Version())
	}
	v.Extend("c")
	if !v.Has("c") || v.Version() != 2 || len(v.IDs()) != 3 {
		t.Errorf("W3 확장 실패: %v (v%d)", v.IDs(), v.Version())
	}
	var nilv *TopologyVocab
	if nilv.Has("a") {
		t.Error("nil 어휘가 통과시켰다 — fail-closed 위반")
	}
}
