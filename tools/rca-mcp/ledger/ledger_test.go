package ledger

import (
	"fmt"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
)

var t0 = time.Date(2026, 7, 12, 9, 0, 0, 0, time.UTC)

func ts(min int) time.Time { return t0.Add(time.Duration(min) * time.Minute) }

func mustAppend(t *testing.T, l *Ledger, actor Actor, at time.Time, p Payload) Event {
	t.Helper()
	ev, err := l.Append(actor, at, p)
	if err != nil {
		t.Fatalf("Append(%T) 실패: %v", p, err)
	}
	return ev
}

// hypo — 예측 술어 n개를 단 가설. 셋째 인자는 종전 probe 계획 수였고,
// ProbeSpec 폐기(§14-4 4c) 후에는 **술어 수**다: probe 문장이 지목하는 것이
// 계획 슬롯에서 PredID로 바뀌었으므로 시험 재료도 그쪽으로 옮겼다.
func hypo(id string, prior Prior, preds int) HypothesisEntry {
	sigs := make([]SignalPred, preds)
	for i := range sigs {
		sigs[i] = testPred(fmt.Sprintf("%s-P%d", id, i+1), "target-"+id, "cpu_used_percent")
	}
	return HypothesisEntry{
		ID: id, TargetID: "target-" + id,
		Chain: []ChainStep{
			{Entity: "target-" + id, Effect: "원인 발생"},
			{Entity: "svc-order", Effect: "응답 지연"},
		},
		Sources: []HypoSource{SourceInvestigation}, Prior: prior, PriorRationale: "1차 조사",
		PredictedSignals: sigs,
	}
}

// testPred는 형식 검사를 통과하는 최소 술어다.
func testPred(predID, target, metric string) SignalPred {
	return SignalPred{
		PredID: predID, Tool: evidence.SrcScanMetrics, TargetID: target,
		Aspect: evidence.AspectMetric, Metric: metric,
		Predicate: PredPresent, Window: evidence.WindowFull,
		Expectation: ExpectMustHold, Role: RoleNecessary,
	}
}

// probeExec는 술어 하나를 지목하는 probe_executed다(§14-4 4c 계약).
func probeExec(predID string, outcome ProbeOutcome) ProbeExecuted {
	return ProbeExecuted{
		ObservationKey: "scan_metrics|" + predID, PredIDs: []string{predID},
		Outcome: outcome,
	}
}

// step은 사슬 구간 지목 포인터 리터럴이다.
func step(i int) *int { return &i }

// buildStory는 예시 사건 수첩 전체를 만든다: H1(진범, DB 느린 SQL),
// H2(네트워크 — 반증됨), H3(배포 — 열세), 반려 1건, 기각 근거 1건.
func buildStory(t *testing.T) *Ledger {
	t.Helper()
	l := New()

	// 묶음 A — 탄생
	mustAppend(t, l, ActorRule, ts(0), HypothesisCreated{Entry: hypo("H1", PriorHigh, 1)})
	mustAppend(t, l, ActorRule, ts(0), HypothesisCreated{Entry: hypo("H2", PriorMedium, 1)})
	mustAppend(t, l, ActorRule, ts(0), HypothesisCreated{Entry: hypo("H3", PriorLow, 1)})
	mustAppend(t, l, ActorRule, ts(1), HypothesisRejectedAtCreation{
		ProposalSummary: "메모리 비트 플립", ViolatedRule: "no_refutation_condition"})

	// 묶음 B — 조사
	mustAppend(t, l, ActorInvestigator, ts(2), probeExec("H1-P1", ProbeDone))
	mustAppend(t, l, ActorInvestigator, ts(3), EvidenceProposed{Entry: EvidenceEntry{
		ID: "E1", Observation: "sql_hash abc123 풀스캔, 평균 12s",
		ObservedWindow: &Window{From: ts(2), To: ts(10)},
		Refs:           []string{"tool:db.top_sql#resp-441"}, SourceStatus: SourceAnomalous,
		Links: []LinkClaim{{Hypothesis: "H1", Direction: DirSupports,
			DimensionClaim: &Dimension{Label: "sql_hash", Value: "abc123"}, ChainStep: step(0)}},
	}})
	mustAppend(t, l, ActorInvestigator, ts(4), EvidenceProposed{Entry: EvidenceEntry{
		ID: "E2", Observation: "CPU 사용률 상승", SourceStatus: SourceAnomalous,
		Links: []LinkClaim{{Hypothesis: "H1", Direction: DirSupports}},
	}})
	mustAppend(t, l, ActorInvestigator, ts(4), EvidenceProposed{Entry: EvidenceEntry{
		ID: "E4", Observation: "주문 API p99 12s로 상승", SourceStatus: SourceAnomalous,
		Links: []LinkClaim{{Hypothesis: "H1", Direction: DirSupports, ChainStep: step(1)}},
	}})
	mustAppend(t, l, ActorInvestigator, ts(5), probeExec("H2-P1", ProbeDone))
	mustAppend(t, l, ActorInvestigator, ts(6), EvidenceProposed{Entry: EvidenceEntry{
		ID: "E3", Observation: "구간 RTT 평시 수준", SourceStatus: SourceNormal,
		Links: []LinkClaim{{Hypothesis: "H2", Direction: DirRefutes}},
	}})
	mustAppend(t, l, ActorInvestigator, ts(7), probeExec("H3-P1", ProbeDone))

	// 묶음 C — 심사
	mustAppend(t, l, ActorVerifier, ts(8), EvidenceLinkPassed{Evidence: "E1", Hypothesis: "H1", Note: "과장 없음"})
	mustAppend(t, l, ActorVerifier, ts(8), EvidenceLinkRejected{Evidence: "E2", Hypothesis: "H1", Note: "관측 시각이 장애 이후"})
	mustAppend(t, l, ActorVerifier, ts(8), EvidenceLinkPassed{Evidence: "E4", Hypothesis: "H1", Note: "증상 구간 관측 정합"})
	mustAppend(t, l, ActorVerifier, ts(9), EvidenceLinkPassed{Evidence: "E3", Hypothesis: "H2", Note: "반증 조건 충족"})
	mustAppend(t, l, ActorRule, ts(9), HypothesisRefuted{Hypothesis: "H2", ByEvidence: "E3", Reason: "반증 조건 충족 링크 심사 통과"})

	// 묶음 D — 판정
	mustAppend(t, l, ActorRule, ts(10), HypothesisDimensionSpecified{
		Hypothesis: "H1", Dimension: Dimension{Label: "sql_hash", Value: "abc123"}, ByEvidence: "E1"})
	for _, axis := range []ObligationAxis{AxisBreadth, AxisDepth, AxisAlternatives,
		AxisSelfRefutation, AxisTemporal, AxisEvidenceQuality} {
		mustAppend(t, l, ActorRule, ts(11), ObligationUpdated{Axis: axis, Status: ObligationSatisfied})
	}
	return l
}

// 합의 검증 1 — 수첩 전체 흐름이 지난번 판정 함수와 이어진다:
// 예시 사건을 끝까지 기록하면 confirmed가 나온다.
func TestStoryEndToEnd(t *testing.T) {
	l := buildStory(t)

	// **판정은 여기서 계산하지 않는다**(§14-5 5a): 구 ComputeStatus·Snapshot
	// 표면은 폐기됐고 판정은 §8 게이트(status_v2)가 낸다 — 그 진리표 시험은
	// status_v2_test.go(TestConfirmedRequiresEveryGate·TestInferiorTruthTable·
	// TestInsufficientWhenNoSupport)에 있다. 이 시험이 지키는 것은 **수첩의
	// 생애주기**다: 판정을 되기록하면 상태가 따라 움직이고 종료 후에는 아무도
	// 쓸 수 없다. 그래서 판정 결과는 루프가 준 것으로 두고 주입한다.
	d := Decision{Terminate: true, Status: StatusConfirmed, AdoptedID: "H1",
		InferiorIDs: []string{"H3"}, Reason: "§8 confirmed 요건 전수 충족",
		StopReason: StopCauseSufficient}

	// 판정을 수첩에 되기록하고 종료·리포트까지.
	mustAppend(t, l, ActorRule, ts(12), HypothesisAdopted{Hypothesis: "H1", Reason: d.Reason})
	mustAppend(t, l, ActorRule, ts(12), HypothesisMarkedInferior{Hypothesis: "H3", Reason: "지지 근거 0 + probe 소진"})
	term := mustAppend(t, l, ActorRule, ts(12), LoopTerminated{Decision: d})
	mustAppend(t, l, ActorPipeline, ts(13), ReportAssembled{AsOfSeq: term.Seq})

	if h, _ := l.Hypothesis("H1"); h.Lifecycle != LifeAdopted {
		t.Errorf("H1 lifecycle = %s, want adopted", h.Lifecycle)
	}
	// 종료 후에는 리포트 추출 외 기록 불가.
	if _, err := l.Append(ActorInvestigator, ts(14), probeExec("H1-P1", ProbeDone)); err == nil {
		t.Error("종료 후 probe 기록이 거부되지 않음")
	}
}

// 합의 검증 2 — 재생하면 실시간 상태와 똑같다 (event sourcing).
func TestReplayEquivalence(t *testing.T) {
	l := buildStory(t)
	r, err := Replay(l.Events())
	if err != nil {
		t.Fatalf("Replay 실패: %v", err)
	}
	if !reflect.DeepEqual(l.hypos, r.hypos) || !reflect.DeepEqual(l.evidence, r.evidence) ||
		l.terminated != r.terminated {
		t.Error("재생한 내부 상태가 실시간과 다름")
	}
	if !reflect.DeepEqual(l.Events(), r.Events()) {
		t.Error("재생한 이벤트 열이 실시간과 다름")
	}
}

// 합의 검증 3 — 작성 주체 강제: 조사자는 판정 줄을 쓸 수 없다.
func TestActorEnforcement(t *testing.T) {
	l := buildStory(t)
	violations := []struct {
		actor Actor
		p     Payload
	}{
		{ActorInvestigator, HypothesisAdopted{Hypothesis: "H1", Reason: "형사의 월권"}},
		{ActorInvestigator, EvidenceLinkPassed{Evidence: "E1", Hypothesis: "H1"}},
		{ActorInvestigator, HypothesisRefuted{Hypothesis: "H3", ByEvidence: "E1"}},
		{ActorVerifier, HypothesisCreated{Entry: hypo("H9", PriorLow, 0)}},
		{ActorVerifier, probeExec("H1-P1", ProbeDone)},
		{ActorRule, EvidenceProposed{Entry: EvidenceEntry{ID: "E9",
			Links: []LinkClaim{{Hypothesis: "H1", Direction: DirSupports}}}}},
	}
	before := len(l.Events())
	for _, v := range violations {
		if _, err := l.Append(v.actor, ts(20), v.p); err == nil {
			t.Errorf("%s의 %T 기록이 거부되지 않음", v.actor, v.p)
		}
	}
	if len(l.Events()) != before {
		t.Error("거부된 문장이 수첩에 남음")
	}
}

// 합의 검증 4 — 차원은 확정 이벤트로만 채워진다.
func TestDimensionOnlyViaEvent(t *testing.T) {
	l := New()
	mustAppend(t, l, ActorRule, ts(0), HypothesisCreated{Entry: hypo("H1", PriorHigh, 0)})
	mustAppend(t, l, ActorInvestigator, ts(1), EvidenceProposed{Entry: EvidenceEntry{
		ID: "E1", Observation: "느린 SQL", SourceStatus: SourceAnomalous,
		Links: []LinkClaim{{Hypothesis: "H1", Direction: DirSupports,
			DimensionClaim: &Dimension{Label: "sql_hash", Value: "abc123"}}},
	}})

	// 주장만 있고 심사 전 — 차원 확정 시도는 거부.
	if _, err := l.Append(ActorRule, ts(2), HypothesisDimensionSpecified{
		Hypothesis: "H1", Dimension: Dimension{Label: "sql_hash", Value: "abc123"}, ByEvidence: "E1"}); err == nil {
		t.Error("심사 전 링크로 차원 확정이 거부되지 않음")
	}

	mustAppend(t, l, ActorVerifier, ts(3), EvidenceLinkPassed{Evidence: "E1", Hypothesis: "H1"})

	// 도장은 받았지만 확정 줄이 없으면 차원은 여전히 빈 칸.
	if h, _ := l.Hypothesis("H1"); h.Dimension != nil {
		t.Error("확정 이벤트 없이 차원이 채워짐")
	}

	mustAppend(t, l, ActorRule, ts(4), HypothesisDimensionSpecified{
		Hypothesis: "H1", Dimension: Dimension{Label: "sql_hash", Value: "abc123"}, ByEvidence: "E1"})
	if h, _ := l.Hypothesis("H1"); h.Dimension == nil {
		t.Error("확정 이벤트 후에도 차원이 비어 있음")
	}
}

// 합의 검증 5 — 모순 문장은 거부된다 (생성 규칙·링크 심사).
// 종전 "반증 조건 없는 가설" 케이스는 §14-3의 RefutationCondition 폐기로
// 사라졌다 — 반증력의 관문은 등록 규칙 3(pipeline/admission.go)이다.
func TestValidationRejections(t *testing.T) {
	l := buildStory(t)
	noChain := hypo("H10", PriorLow, 0)
	noChain.Chain = nil
	wrongRoot := hypo("H11", PriorLow, 0)
	wrongRoot.Chain[0].Entity = "target-딴데"
	cases := []struct {
		name  string
		actor Actor
		p     Payload
		want  string // 오류 메시지에 포함될 문구
	}{
		{"중복 가설 id", ActorRule, HypothesisCreated{Entry: hypo("H1", PriorLow, 0)}, "중복"},
		{"활성 가설에 없는 술어 지목", ActorInvestigator, probeExec("H8-P1", ProbeDone), "없음"},
		{"관측 키 없는 probe", ActorInvestigator, ProbeExecuted{
			PredIDs: []string{"H1-P1"}, Outcome: ProbeDone}, "관측 키 필수"},
		{"지목 없는 probe", ActorInvestigator, ProbeExecuted{
			ObservationKey: "k", Outcome: ProbeDone}, "pred_id 지목 필수"},
		{"링크 없는 근거", ActorInvestigator, EvidenceProposed{Entry: EvidenceEntry{ID: "E9"}}, "링크 없는"},
		{"이미 심사된 링크 재심사", ActorVerifier, EvidenceLinkPassed{Evidence: "E1", Hypothesis: "H1"}, "기대"},
		{"supports 링크로 반증 전이", ActorRule, HypothesisRefuted{Hypothesis: "H1", ByEvidence: "E1"}, "refutes가 아님"},
		{"차원 주장 없는 링크로 차원 확정", ActorRule, HypothesisDimensionSpecified{
			Hypothesis: "H1", Dimension: Dimension{Label: "x", Value: "y"}, ByEvidence: "E2"}, ""},
		{"active 아닌 가설 채택", ActorRule, HypothesisAdopted{Hypothesis: "H2"}, "active 아님"},
		{"미정의 의무 축", ActorRule, ObligationUpdated{Axis: "styling", Status: ObligationSatisfied}, "미정의"},
		{"인과사슬 없는 가설", ActorRule, HypothesisCreated{Entry: noChain}, "인과사슬"},
		{"첫 구간 ≠ 의심 대상", ActorRule, HypothesisCreated{Entry: wrongRoot}, "첫 구간"},
		{"사슬 범위 밖 구간 지목", ActorInvestigator, EvidenceProposed{Entry: EvidenceEntry{
			ID: "E8", Observation: "x", Links: []LinkClaim{{Hypothesis: "H1", Direction: DirSupports, ChainStep: step(9)}},
		}}, "구간 9 없음"},
		{"refutes 링크에 구간 지목", ActorInvestigator, EvidenceProposed{Entry: EvidenceEntry{
			ID: "E8", Observation: "x", Links: []LinkClaim{{Hypothesis: "H1", Direction: DirRefutes, ChainStep: step(0)}},
		}}, "supports 링크만"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := l.Append(tc.actor, ts(20), tc.p)
			if err == nil {
				t.Fatalf("거부되지 않음")
			}
			if tc.want != "" && !strings.Contains(err.Error(), tc.want) {
				t.Errorf("오류 %q에 %q 미포함", err, tc.want)
			}
		})
	}
}

// 합의 검증 6 — 기각된 근거는 집계에서 빠진다 (심사 통과만 결론에 쓰임).
// 집계 자리가 구 Snapshot에서 리포트 조립으로 옮겨졌다(§14-5 5a).
func TestRejectedLinkNotCounted(t *testing.T) {
	l := buildStory(t)
	if got := passedObservations(l, "H1", DirSupports); len(got) != 2 {
		t.Errorf("H1 통과 관측 %d건, want 2 (E1·E4 — 기각된 E2는 제외): %v", len(got), got)
	}
}

// 예산·수확 체감의 집계는 구 Snapshot(BudgetConfig)에 있었고 §14-5 5a에서
// 폐기됐다 — 예산은 루프가 자기 계정으로 지고(§7.6), 그 시험은
// loop/budget_test.go(TestDerivedCaps·TestWideningTrigger 계열)에 있다.

// probe_added(동적 probe 추가)는 §14-4 4c에서 폐기됐다 — 검증 계획이
// 자유문 슬롯 목록이 아니라 예측 술어 집합이 된 뒤로, 루프가 여는 슬롯이
// 없다. 그 시험이 지키던 재생 동등성은 TestReplayEquivalence가 이미 진다.

func TestRejectionInHistoryOnly(t *testing.T) {
	l := buildStory(t)
	found := false
	for _, ev := range l.Events() {
		if ev.Type == EvHypothesisRejectedAtCreation {
			found = true
		}
	}
	if !found {
		t.Error("반려 기록이 history에 없음")
	}
	if len(l.hypoOrder) != 3 {
		t.Error("반려된 가설이 상태에 들어감")
	}
}
