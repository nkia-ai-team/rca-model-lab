package ledger

import (
	"testing"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
)

func newWithHypo(t *testing.T) *Ledger {
	t.Helper()
	l := New()
	mustAppend(t, l, ActorRule, ts(0), HypothesisCreated{Entry: hypo("H1", PriorHigh, 1)})
	return l
}

func claim(id, sup string) ChainClaim {
	return ChainClaim{ClaimID: id, Kind: ChainTerminalEntity, EntityKey: "sql_key:abc",
		Effect: "대표 루트 세션 보유", Origin: OriginProjector, Supersedes: sup}
}

// 주체 고정(장부 원칙 1)이 신 문장에도 걸린다. 심사점 R만 확인자다.
func TestNewEventActors(t *testing.T) {
	cases := []struct {
		p     Payload
		actor Actor
	}{
		{PredicateAudited{Hypothesis: "H1", PredID: "H1-P1", Note: "자명하지 않음"}, ActorVerifier},
		{ChainClaimAsserted{Hypothesis: "H1", Claim: claim("C1", "")}, ActorRule},
		{TemporalJudged{Hypothesis: "H1", CauseEID: "EIX-1", SymptomEID: "EIX-2",
			Claim: TemporalPrecedes, Verdict: TemporalPrecedesVerdict}, ActorRule},
		{RequiredViewsExtended{Phase: PhaseLayerA, Keys: []RequiredViewKey{{TargetID: "svc-1", Tool: evidence.SrcScanMetrics}}}, ActorRule},
		{RequiredViewNotApplicable{Key: RequiredViewKey{TargetID: "sw-1", Tool: evidence.SrcProcesses}, Reason: "not_collected"}, ActorRule},
	}
	for _, c := range cases {
		l := newWithHypo(t)
		if _, err := l.Append(c.actor, ts(1), c.p); err != nil {
			t.Errorf("%T를 %s가 못 씀: %v", c.p, c.actor, err)
		}
		// 다른 주체는 전부 거부돼야 한다.
		for _, other := range []Actor{ActorRule, ActorVerifier, ActorInvestigator, ActorPipeline} {
			if other == c.actor {
				continue
			}
			l2 := newWithHypo(t)
			if _, err := l2.Append(other, ts(1), c.p); err == nil {
				t.Errorf("%T를 %s가 씀 — 주체 고정 실패", c.p, other)
			}
		}
	}
}

// 심사 결과는 가설 실재를 요구하지 않는다 — R은 등록 과정에서 돌고,
// 반려된 후보는 hypothesis_created가 없기 때문이다.
func TestPredicateAuditedNeedsNoHypothesis(t *testing.T) {
	l := New()
	if _, err := l.Append(ActorVerifier, ts(1), PredicateAudited{
		Hypothesis: "cand-9", PredID: "cand-9-P1",
		Verdict: PredicateVerdict{Pertinent: false, Substantive: true}, Note: "무관"}); err != nil {
		t.Fatalf("반려 후보의 심사 기록이 막힘: %v", err)
	}
	l2 := New()
	if _, err := l2.Append(ActorVerifier, ts(1), PredicateAudited{PredID: "P", Note: ""}); err == nil {
		t.Fatal("note 없는 심사가 통과(§6.2-5 note 먼저)")
	}
	if _, err := l2.Append(ActorVerifier, ts(1), PredicateAudited{PredID: "", Note: "x"}); err == nil {
		t.Fatal("pred_id 없는 심사가 통과")
	}
}

// C-2: 미실측 술어는 RoleValid 심사 대상이 아니다 — nil로 구분된다.
func TestRoleValidTriState(t *testing.T) {
	no := false
	cases := []struct {
		v     PredicateVerdict
		valid bool
	}{
		{PredicateVerdict{Pertinent: true, Substantive: true}, true},                 // RoleValid nil = 심사 제외
		{PredicateVerdict{Pertinent: true, Substantive: true, RoleValid: &no}, true}, // 강등 사유이지 무효가 아니다
		{PredicateVerdict{Pertinent: true}, false},
		{PredicateVerdict{Substantive: true}, false},
	}
	for i, c := range cases {
		if got := c.v.Valid(); got != c.valid {
			t.Errorf("case %d: Valid() = %v, want %v", i, got, c.valid)
		}
	}
}

// §7.3: 정밀화는 교체가 아니라 append다. ClaimID는 가설 내 유일하고
// 한 구간을 둘이 대체하는 fork는 거부된다(§5.7과 같은 규율).
func TestChainClaimIdentity(t *testing.T) {
	l := newWithHypo(t)
	mustAppend(t, l, ActorRule, ts(1), ChainClaimAsserted{Hypothesis: "H1", Claim: claim("C1", "")})

	if _, err := l.Append(ActorRule, ts(2), ChainClaimAsserted{Hypothesis: "H1", Claim: claim("C1", "")}); err == nil {
		t.Fatal("ClaimID 중복이 통과")
	}
	if _, err := l.Append(ActorRule, ts(2), ChainClaimAsserted{Hypothesis: "H1", Claim: claim("C2", "C9")}); err == nil {
		t.Fatal("없는 구간을 대체한다는 주장이 통과")
	}
	mustAppend(t, l, ActorRule, ts(3), ChainClaimAsserted{Hypothesis: "H1", Claim: claim("C2", "C1")})
	if _, err := l.Append(ActorRule, ts(4), ChainClaimAsserted{Hypothesis: "H1", Claim: claim("C3", "C1")}); err == nil {
		t.Fatal("한 구간을 둘이 대체하는 fork가 통과")
	}
	if _, err := l.Append(ActorRule, ts(4), ChainClaimAsserted{Hypothesis: "H9", Claim: claim("C4", "")}); err == nil {
		t.Fatal("없는 가설에 사슬 구간이 붙음")
	}
	// 재생이 같은 상태를 복원한다 — 사슬 정체성도 history의 함수다.
	if _, err := Replay(l.Events()); err != nil {
		t.Fatalf("재생 실패: %v", err)
	}
}

// C-6: 판정을 TemporalClaim 축으로 분기하려면 이벤트에 축이 실려야 한다.
func TestTemporalJudgedCarriesClaim(t *testing.T) {
	l := newWithHypo(t)
	ev := mustAppend(t, l, ActorRule, ts(1), TemporalJudged{
		Hypothesis: "H1", CauseEID: "EIX-1", SymptomEID: "EIX-2",
		Claim: TemporalCoincides, Verdict: TemporalOverlapVerdict})
	got := ev.Payload.(TemporalJudged)
	if got.Claim != TemporalCoincides {
		t.Fatal("TemporalClaim 축이 payload에 없음 — coincides 가설이 영구 confirmed 불가")
	}

	bad := []TemporalJudged{
		{Hypothesis: "H1", CauseEID: "", SymptomEID: "EIX-2", Claim: TemporalPrecedes, Verdict: TemporalPrecedesVerdict},
		{Hypothesis: "H1", CauseEID: "EIX-1", SymptomEID: "", Claim: TemporalPrecedes, Verdict: TemporalPrecedesVerdict},
		{Hypothesis: "H1", CauseEID: "EIX-1", SymptomEID: "EIX-2", Claim: "before", Verdict: TemporalPrecedesVerdict},
		{Hypothesis: "H1", CauseEID: "EIX-1", SymptomEID: "EIX-2", Claim: TemporalPrecedes, Verdict: "maybe"},
		{Hypothesis: "H9", CauseEID: "EIX-1", SymptomEID: "EIX-2", Claim: TemporalPrecedes, Verdict: TemporalPrecedesVerdict},
	}
	for i, p := range bad {
		l2 := newWithHypo(t)
		if _, err := l2.Append(ActorRule, ts(1), p); err == nil {
			t.Errorf("잘못된 시간 판정 %d가 통과", i)
		}
	}
}

// §4 4차 A-5: 키는 (TargetID, Tool)이고 compare_peers만 Metric을 갖는다.
func TestRequiredViewKeyShape(t *testing.T) {
	l := newWithHypo(t)
	mustAppend(t, l, ActorRule, ts(1), RequiredViewsExtended{
		Phase: PhaseLayerB, Reason: "계층 A 완료",
		Keys: []RequiredViewKey{
			{TargetID: "svc-1", Tool: evidence.SrcSampleLogs},
			{TargetID: "svc-1", Tool: evidence.SrcComparePeers, Metric: "http.latency.p95"},
		}})

	bad := []RequiredViewsExtended{
		{Phase: "layer_c", Keys: []RequiredViewKey{{TargetID: "svc-1", Tool: evidence.SrcScanMetrics}}},
		{Phase: PhaseLayerA},
		{Phase: PhaseLayerA, Keys: []RequiredViewKey{{TargetID: "", Tool: evidence.SrcScanMetrics}}},
		// 술어 불가 도구는 rubric 분모에 들지 않는다(§4).
		{Phase: PhaseLayerA, Keys: []RequiredViewKey{{TargetID: "svc-1", Tool: "expand_topology"}}},
		// compare_peers는 metric 필수, 나머지는 metric 금지.
		{Phase: PhaseLayerB, Keys: []RequiredViewKey{{TargetID: "svc-1", Tool: evidence.SrcComparePeers}}},
		{Phase: PhaseLayerA, Keys: []RequiredViewKey{{TargetID: "svc-1", Tool: evidence.SrcListEvents, Metric: "x"}}},
	}
	for i, p := range bad {
		l2 := newWithHypo(t)
		if _, err := l2.Append(ActorRule, ts(1), p); err == nil {
			t.Errorf("잘못된 필수 관점 확장 %d가 통과", i)
		}
	}
}

// C-12: not_applicable은 집합에서 빼는 게 아니라 딱지다 — 사유 없는
// 딱지는 분모 축소의 근거가 되지 못한다.
func TestRequiredViewNotApplicableNeedsReason(t *testing.T) {
	l := newWithHypo(t)
	if _, err := l.Append(ActorRule, ts(1), RequiredViewNotApplicable{
		Key: RequiredViewKey{TargetID: "sw-1", Tool: evidence.SrcProcesses}}); err == nil {
		t.Fatal("사유 없는 not_applicable이 통과")
	}
}

// probe_executed는 (관측 키, PredIDs)를 지목하고 produced_eids를 싣는다
// (§7.1·§14-4 4c). 지목이 활성 가설의 술어가 아니면 반려다 — 종전 "가설의
// 몇 번째 probe인가" 검사가 있던 자리이며, 이 시험이 그 계약을 승계한다.
func TestProbeExecutedTargetsPredIDs(t *testing.T) {
	l := newWithHypo(t)
	ev := mustAppend(t, l, ActorInvestigator, ts(1), ProbeExecuted{
		ObservationKey: "scan_metrics|H1", PredIDs: []string{"H1-P1"},
		Outcome: ProbeDone, ProducedEIDs: []string{"EIX-7", "EIX-8"}})
	if got := ev.Payload.(ProbeExecuted).ProducedEIDs; len(got) != 2 {
		t.Fatalf("produced_eids = %v", got)
	}
	bad := []ProbeExecuted{
		{ObservationKey: "k", PredIDs: []string{"H9-P1"}, Outcome: ProbeDone},   // 미지 술어
		{ObservationKey: "", PredIDs: []string{"H1-P1"}, Outcome: ProbeDone},    // 관측 키 없음
		{ObservationKey: "k", Outcome: ProbeDone},                               // 지목 없음
		{ObservationKey: "k", PredIDs: []string{"H1-P1", "H1-P1"}, Outcome: ProbeDone}, // 중복
	}
	for i, p := range bad {
		if _, err := l.Append(ActorInvestigator, ts(2), p); err == nil {
			t.Errorf("[%d] 계약 위반 probe_executed가 통과: %+v", i, p)
		}
	}
}

// HypothesisEntry의 v2 3필드는 additive다 — 쓰였을 때만 검사한다.
func TestHypothesisEntryV2FieldsAdditive(t *testing.T) {
	l := New()
	e := hypo("H2", PriorHigh, 1)
	e.IdentityKey = HypoIdentity{CauseEntity: "orders-db", Mechanism: "락 경합", TemporalClaim: TemporalPrecedes}
	e.PredictedSignals = AssignPredIDs("H2", []SignalPred{okPred(), okPred()})
	e.SupportEIDs = []string{"EIX-3"}
	mustAppend(t, l, ActorRule, ts(0), HypothesisCreated{Entry: e})

	// PredID 중복은 개설에서 막힌다.
	l2 := New()
	dup := hypo("H3", PriorHigh, 1)
	dup.PredictedSignals = []SignalPred{okPred(), okPred()}
	if _, err := l2.Append(ActorRule, ts(0), HypothesisCreated{Entry: dup}); err == nil {
		t.Fatal("PredID 중복 술어를 단 가설이 개설됨")
	}
	// 형식 위반 술어도 막힌다.
	l3 := New()
	badForm := hypo("H4", PriorHigh, 1)
	p := okPred()
	p.Predicate = PredMagnitudeGE // threshold 누락
	badForm.PredictedSignals = []SignalPred{p}
	if _, err := l3.Append(ActorRule, ts(0), HypothesisCreated{Entry: badForm}); err == nil {
		t.Fatal("형식 위반 술어를 단 가설이 개설됨")
	}
	// 미정의 TemporalClaim도 막힌다.
	l4 := New()
	badClaim := hypo("H5", PriorHigh, 1)
	badClaim.IdentityKey.TemporalClaim = "after"
	if _, err := l4.Append(ActorRule, ts(0), HypothesisCreated{Entry: badClaim}); err == nil {
		t.Fatal("미정의 temporal_claim이 통과")
	}
}
