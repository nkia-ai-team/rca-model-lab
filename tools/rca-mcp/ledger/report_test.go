package ledger

import (
	"reflect"
	"strings"
	"testing"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
)

// terminateStory는 buildStory 수첩에 판정·종료를 기록한다 —
// TestStoryEndToEnd와 같은 마무리.
// **판정은 루프가 준다**(§14-5 5a — 구 ComputeStatus 폐기). 리포트 시험의
// 대상은 "종료 선언에서 두 출력을 뽑는 조립"이므로 판정 결과를 주입한다.
func terminateStory(t *testing.T, l *Ledger) Decision {
	t.Helper()
	d := Decision{Terminate: true, Status: StatusConfirmed, AdoptedID: "H1",
		InferiorIDs: []string{"H3"}, Reason: "§8 confirmed 요건 전수 충족",
		StopReason: StopCauseSufficient}
	mustAppend(t, l, ActorRule, ts(12), HypothesisAdopted{Hypothesis: d.AdoptedID, Reason: d.Reason})
	for _, id := range d.InferiorIDs {
		mustAppend(t, l, ActorRule, ts(12), HypothesisMarkedInferior{Hypothesis: id, Reason: "명백 열세"})
	}
	mustAppend(t, l, ActorRule, ts(12), LoopTerminated{Decision: d})
	return d
}

// storyContext는 buildStory의 H1 술어(H1-P1: scan_metrics·target-h1·
// cpu_used_percent·present)를 **자격 지지로** 해소하는 판정 문맥이다.
//
// 카드의 PROVEN/WEAKENED 입력이 구 심사 통과 링크 수에서 §8 계수로 바뀌면서
// (§14-5 5a) 리포트 시험도 명제 장부를 갖춰야 한다 — 링크 스트림은 판정에
// 더 이상 쓰이지 않는다.
func storyContext(t *testing.T, hypoIDs ...string) ReportContext {
	t.Helper()
	var recs []evidence.EvidenceIndexRecord
	for _, id := range hypoIDs {
		r := okRecord("cpu_used_percent", 3.2, evidence.DirUp)
		r.TargetID = "target-" + id
		r.Provenance.LineageID = "LG-" + id
		recs = append(recs, r)
	}
	ix, props, gc := newIx(t, recs...)
	return ReportContext{Propositions: props, Index: ix, Gate: gc}
}

// TestReportConfirmedStory — 모범 사건 수첩에서 두 출력을 추출한다.
func TestReportConfirmedStory(t *testing.T) {
	l := buildStory(t)
	terminateStory(t, l)

	result, ui, err := AssembleReport(l, storyContext(t, "H1"), ts(13), ts(14))
	if err != nil {
		t.Fatalf("AssembleReport 실패: %v", err)
	}

	// RcaResult — 결론부.
	if result.Status != "confirmed" {
		t.Errorf("status = %s, want confirmed", result.Status)
	}
	// confidence — §8.2 rubric v1(5c). 이 fixture는 confirmed 밴드 0.80에서
	// 가감 항이 하나도 발동하지 않는다(계보 2종·terminal 사영에 시간 판정
	// 없음·weak 0·필수 관점 기록 없음) — 밴드 그대로가 정답이다.
	if result.Cause.TargetID != "target-H1" || result.Cause.Confidence != 0.80 {
		t.Errorf("cause = %+v, want target-H1/confidence 0.80(밴드)", result.Cause)
	}
	// evidence — 통과 링크의 근거만(E1, E4, E3). 기각된 E2는 없다.
	var refs []string
	for _, e := range result.Evidence {
		refs = append(refs, e.Ref)
	}
	if !reflect.DeepEqual(refs, []string{"tool:db.top_sql#resp-441", "E4", "E3"}) {
		t.Errorf("evidence refs = %v", refs)
	}
	// causalChain — 사슬 구간 그대로 + 구간을 지목한 통과 관측 첨부.
	if len(result.CausalChain) != 2 || result.CausalChain[0].Entity != "target-H1" ||
		!strings.Contains(result.CausalChain[0].Note, "sql_hash abc123") ||
		result.CausalChain[1].Entity != "svc-order" ||
		!strings.Contains(result.CausalChain[1].Note, "p99 12s") {
		t.Errorf("causalChain = %+v", result.CausalChain)
	}
	// alternatives — H3(열세)만. 반증된 H2는 대안이 아니다. 열세 상수 0.15.
	if len(result.Alternatives) != 1 || result.Alternatives[0].Confidence != ConfInferiorCompetitor {
		t.Errorf("alternatives = %+v", result.Alternatives)
	}
	// missing·next_steps는 기계 조립이다(§14-4 4c). H1의 술어는 위 문맥이
	// 해소했으므로 남는 결손은 H3의 것뿐이다 — 반증된 H2의 것은 빠진다.
	if len(result.MissingEvidence) != 1 ||
		!strings.Contains(result.MissingEvidence[0], "미판정 명제(H3)") {
		t.Errorf("missingEvidence = %v", result.MissingEvidence)
	}
	// next_steps는 채택 가설(H1) 몫만인데 그 술어가 판정됐으므로 비어 있다.
	if len(ui.NextSteps) != 0 {
		t.Errorf("next_steps = %+v", ui.NextSteps)
	}

	// UIReport — 배너와 가설 카드.
	if ui.Diagnosis.Verdict != "CONCLUSIVE" || ui.Diagnosis.Confidence != 0.80 {
		t.Errorf("diagnosis = %+v (rubric 밴드 0.80)", ui.Diagnosis)
	}
	// §8.2-3 — 내역·계약 필드.
	if ui.ConfidenceContract != ConfidenceContract || ui.ConfidenceBreakdown == nil ||
		ui.ConfidenceBreakdown.Band != 0.80 || ui.ConfidenceBreakdown.Value != 0.80 {
		t.Errorf("confidence_breakdown = %+v / contract = %q", ui.ConfidenceBreakdown, ui.ConfidenceContract)
	}
	if len(ui.Hypotheses) != 3 {
		t.Fatalf("가설 카드 %d장, want 3", len(ui.Hypotheses))
	}
	h1, h3, h2 := ui.Hypotheses[0], ui.Hypotheses[1], ui.Hypotheses[2]
	if !h1.IsAdopted || h1.Rank != 1 || h1.Evaluation.Verdict != "PROVEN" {
		t.Errorf("H1 카드 = %+v", h1)
	}
	if !reflect.DeepEqual(h1.Culprits, []UICulprit{{Label: "sql_hash", Value: "abc123"}}) {
		t.Errorf("H1 culprits = %+v", h1.Culprits)
	}
	if h3.IsAdopted || h3.Evaluation.Verdict != "WEAKENED" {
		t.Errorf("H3 카드 = %+v", h3)
	}
	if h2.Evaluation.Verdict != "CONTRADICTED" || len(h2.Evaluation.Contradicts) != 1 {
		t.Errorf("H2 카드 = %+v", h2)
	}
	// ruled_out — 정상 관측이 배제에 쓰인 것(E3).
	if !reflect.DeepEqual(ui.DataCoverage.RuledOut, []string{"구간 RTT 평시 수준"}) {
		t.Errorf("ruled_out = %v", ui.DataCoverage.RuledOut)
	}

	// 감사 기록과 재생 동등성.
	evs := l.Events()
	if evs[len(evs)-1].Type != EvReportAssembled {
		t.Errorf("마지막 문장 = %s, want report_assembled", evs[len(evs)-1].Type)
	}
	if _, err := Replay(evs); err != nil {
		t.Fatalf("리포트 추출 후 재생 실패: %v", err)
	}
}

// TestReportRequiresTermination — 진행 중 수첩에서는 추출 거부.
func TestReportRequiresTermination(t *testing.T) {
	l := buildStory(t)
	if _, _, err := AssembleReport(l, ReportContext{}, ts(13), ts(14)); err == nil {
		t.Fatal("종료 전 수첩인데 추출을 수락함")
	}
}

// TestReportInsufficient — 전 가설 반증 + 이행 불가 기록이 출력에
// 정직하게 실린다: CONTRADICTED 배너, 원인 없음, gaps·guards.
func TestReportInsufficient(t *testing.T) {
	l := New()
	mustAppend(t, l, ActorRule, ts(0), HypothesisCreated{Entry: hypo("H1", PriorMedium, 2)})
	mustAppend(t, l, ActorInvestigator, ts(1), probeExec("H1-P1", ProbeDone))
	mustAppend(t, l, ActorInvestigator, ts(1), probeExec("H1-P2", ProbeInfeasible))
	mustAppend(t, l, ActorInvestigator, ts(2), EvidenceProposed{Entry: EvidenceEntry{
		ID: "E1", Observation: "지표 평시 수준", SourceStatus: SourceNormal,
		Links: []LinkClaim{{Hypothesis: "H1", Direction: DirRefutes}},
	}})
	mustAppend(t, l, ActorVerifier, ts(3), EvidenceLinkPassed{Evidence: "E1", Hypothesis: "H1"})
	mustAppend(t, l, ActorRule, ts(3), HypothesisRefuted{Hypothesis: "H1", ByEvidence: "E1", Reason: "반증 조건 충족"})
	// 의무 6축 문장은 더 이상 리포트 원천이 아니다(§8 폐기, §14-5 5a) —
	// 한 줄 남겨 **과거 journal 재생이 깨지지 않는지**만 확인한다.
	mustAppend(t, l, ActorRule, ts(4), ObligationUpdated{
		Axis: AxisBreadth, Status: ObligationInfeasible, Note: "조사 수단 소진"})

	d := Decision{Terminate: true, Status: StatusInsufficient,
		Reason: "지지 0건(weak 포함)", StopReason: StopMeansExhausted}
	mustAppend(t, l, ActorRule, ts(5), LoopTerminated{Decision: d})

	result, ui, err := AssembleReport(l, storyContext(t), ts(5), ts(6))
	if err != nil {
		t.Fatalf("AssembleReport 실패: %v", err)
	}
	if result.Status != "insufficient" || result.Cause.TargetID != "" || len(result.Alternatives) != 0 {
		t.Errorf("결론부 = %+v", result)
	}
	if ui.Diagnosis.Verdict != "CONTRADICTED" || ui.Diagnosis.Confidence != 0 {
		t.Errorf("diagnosis = %+v", ui.Diagnosis)
	}
	// missingEvidence = probe 이행 불가뿐이다. 미판정 명제는 없고(유일한
	// 가설이 반증됐다), 의무 축 줄은 6축 폐기와 함께 사라졌다 — 그 자리는
	// §8 미충족 요건이 잇는다(배선 5c).
	if len(result.MissingEvidence) != 1 || !strings.Contains(result.MissingEvidence[0], "H1-P2") {
		t.Errorf("missingEvidence = %v", result.MissingEvidence)
	}
	if len(ui.DataCoverage.Gaps) != 1 || !strings.Contains(ui.DataCoverage.Gaps[0], "[H1-P2]") {
		t.Errorf("gaps = %v", ui.DataCoverage.Gaps)
	}
	// 반증된 가설 카드도 남는다 — "왜 아닌지"의 기록.
	if len(ui.Hypotheses) != 1 || ui.Hypotheses[0].Evaluation.Verdict != "CONTRADICTED" {
		t.Errorf("hypotheses = %+v", ui.Hypotheses)
	}
}

// TestReportGapsAreMachineAssembled — next_steps·missing의 두 기계 원천이
// 각각 제 몫을 하는가(§14-4 4c). 자유문 Purpose가 사라진 자리를 이 둘이
// 대신하므로, **판정된 술어는 빠지고 미판정만 남는가**와 **관측이 있는
// 필수 관점은 빠지는가**가 계약의 전부다.
func TestReportGapsAreMachineAssembled(t *testing.T) {
	ix, props, gc := newIx(t, okRecord("m", 3.2, evidence.DirUp))

	decided := pred("m", evidence.PredDirectionUp, nil, ExpectMustHold, RoleNecessary)
	undecided := pred("없는지표", evidence.PredPresent, nil, ExpectMustHold, RoleCorroborating)
	undecided.PredID = "H1-P2"
	muted := pred("m2", evidence.PredPresent, nil, ExpectMustHold, RoleCorroborating)
	muted.PredID, muted.AuditedOut = "H1-P3", true

	l := New()
	e := hypo("H1", PriorHigh, 0)
	e.PredictedSignals = []SignalPred{decided, undecided, muted}
	mustAppend(t, l, ActorRule, ts(0), HypothesisCreated{Entry: e})
	// 필수 관점 셋 — 하나는 관측 있음(okRecord의 t1/scan_metrics), 하나는
	// not_applicable, 하나는 미조사.
	mustAppend(t, l, ActorRule, ts(1), RequiredViewsExtended{Phase: PhaseLayerA, Reason: "admission",
		Keys: []RequiredViewKey{
			{TargetID: "t1", Tool: evidence.SrcScanMetrics},
			{TargetID: "t1", Tool: evidence.SrcProcesses},
			{TargetID: "t1", Tool: evidence.SrcListEvents},
		}})
	mustAppend(t, l, ActorRule, ts(1), RequiredViewNotApplicable{
		Key: RequiredViewKey{TargetID: "t1", Tool: evidence.SrcProcesses}, Reason: "not_collected"})
	mustAppend(t, l, ActorRule, ts(2), LoopTerminated{Decision: Decision{
		Terminate: true, Status: StatusProvisional, AdoptedID: "H1", Reason: "시험"}})

	result, ui, err := AssembleReport(l, ReportContext{Propositions: props, Index: ix, Gate: gc}, ts(3), ts(4))
	if err != nil {
		t.Fatalf("AssembleReport 실패: %v", err)
	}
	// 미판정 명제 1건(H1-P2)만 — 판정된 H1-P1도, 무효 딱지 H1-P3도 아니다.
	var steps []string
	for _, s := range ui.NextSteps {
		steps = append(steps, s.Title)
	}
	if len(steps) != 2 || !strings.Contains(steps[0], "H1-P2") ||
		!strings.Contains(steps[1], string(evidence.SrcListEvents)) {
		t.Fatalf("next_steps = %v (미판정 H1-P2 + 미조사 list_events여야)", steps)
	}
	for _, s := range steps {
		if strings.Contains(s, "H1-P1") || strings.Contains(s, "H1-P3") ||
			strings.Contains(s, string(evidence.SrcProcesses)) {
			t.Errorf("빠져야 할 항목이 next_steps에 있음: %s", s)
		}
	}
	// missing은 같은 원천이다 — 한쪽에만 나타나는 결손은 없다.
	joined := strings.Join(result.MissingEvidence, "\n")
	if !strings.Contains(joined, "H1-P2") || !strings.Contains(joined, string(evidence.SrcListEvents)) {
		t.Errorf("missingEvidence = %v", result.MissingEvidence)
	}
	if len(ui.Hypotheses) != 1 || len(ui.Hypotheses[0].Evaluation.Missing) != 1 {
		t.Errorf("가설 카드 missing = %+v", ui.Hypotheses)
	}
}
