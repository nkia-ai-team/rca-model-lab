// [4] 재생성 시험 — §14-4 4d 완료 기준 ③(§7.5·§7.6)과 대응한다.
//
//	index 소비 출처만 재호출  TestRegenerateCallsOnlyIndexConsumingSources
//	규칙 3의 delta 문맥 해석   TestRule3DeltaContextRevivesNecessary
package pipeline

import (
	"context"
	"strings"
	"testing"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/ledger"
)

// markedSource는 ConsumesIndex 표식이 있는 출처 대역이다.
type markedSource struct {
	consumes bool
	called   *int
	seen     *GenerateInput
}

func (m *markedSource) ConsumesIndex() bool { return m.consumes }

// Propose는 **첫 라운드 입력만** 남긴다 — 규칙 6의 2라운드가 Feedback을
// 반려 사유로 덮어쓰므로, 마지막 호출을 보면 6요소가 사라진 뒤를 본다.
func (m *markedSource) Propose(_ context.Context, in GenerateInput) ([]Candidate, error) {
	*m.called++
	if *m.called == 1 {
		*m.seen = in
	}
	return nil, nil
}

// 표식 없는 출처 — 재생성이 부르지 않는다(fail-closed).
type unmarkedSource struct{ called *int }

func (u *unmarkedSource) Propose(context.Context, GenerateInput) ([]Candidate, error) {
	*u.called++
	return nil, nil
}

// **§7.6 43k 모양**: 재생성은 index 소비 출처 + Grouper만 다시 부른다.
// 변경·멤버 출처는 인시던트 창의 함수라 index가 갱신돼도 같은 값이다.
func TestRegenerateCallsOnlyIndexConsumingSources(t *testing.T) {
	var idxCalls, changeCalls, unmarkedCalls int
	var seen GenerateInput
	idx := &markedSource{consumes: true, called: &idxCalls, seen: &seen}
	chg := &markedSource{consumes: false, called: &changeCalls, seen: &seen}
	unk := &unmarkedSource{called: &unmarkedCalls}

	l := ledger.New()
	base := GenerateInput{Vocab: NewTopologyVocab("t1"), Propositions: evidence.NewLedger(evidence.NewIndex())}
	g := grouperFunc(func(c []Candidate) ([][]int, error) { return nil, nil })
	r := NewRegenerator(base, []CandidateSource{idx, chg, unk}, g, l,
		GenerateConfig{Auditor: passAuditor{}})
	if r == nil {
		t.Fatal("index 소비 출처가 있는데 재생성기가 nil이다")
	}
	in := RegenerateInput{
		Index: evidence.NewIndex(), Projection: []HypoBrief{},
		Refuted: []RefutedBrief{}, ProbeLog: []ProbeLogEntry{},
		Unexplored: []UnexploredArea{}, Undecided: []UndecidedProposition{{Key: "k1", NewlyDecidable: true}},
		Round: 1,
	}
	// 후보 0개 → 규칙 6이 라운드 2까지 돌고 admission 실패로 끝난다.
	// 여기서 검사하는 것은 **누가 불렸는가**다.
	_, _ = r.Regenerate(context.Background(), in)

	if idxCalls == 0 {
		t.Fatal("index 소비 출처가 안 불렸다")
	}
	if changeCalls != 0 {
		t.Fatalf("표식이 false인 출처가 %d회 불렸다 — 같은 입력에 43k를 두 번 낸다", changeCalls)
	}
	if unmarkedCalls != 0 {
		t.Fatalf("표식 없는 출처가 %d회 불렸다 — fail-closed 위반", unmarkedCalls)
	}
	// 6요소는 Feedback 채널로 프롬프트에 실린다(LLM 쪽에 새 입구 없음).
	joined := strings.Join(seen.Feedback, "\n")
	if !strings.Contains(joined, "재생성 라운드 1") || !strings.Contains(joined, "[미판정 명제] k1") {
		t.Fatalf("6요소가 후보 입력에 안 실렸다:\n%s", joined)
	}
	if !strings.Contains(joined, "widening delta로 새로 판정 가능") {
		t.Fatalf("newly_decidable 딱지가 서식에 없다:\n%s", joined)
	}
	if seen.DeltaKeys != nil && len(seen.DeltaKeys) != 0 {
		t.Fatalf("delta 키가 없는데 집합이 비지 않았다: %v", seen.DeltaKeys)
	}
}

// 재호출할 출처가 하나도 없으면 재생성기는 nil이다 — 빈 출처로 Generate를
// 돌리면 후보 0개 → admission 실패로 run이 죽는다.
func TestRegeneratorNilWithoutIndexSources(t *testing.T) {
	var n int
	g := grouperFunc(func([]Candidate) ([][]int, error) { return nil, nil })
	if r := NewRegenerator(GenerateInput{}, []CandidateSource{&unmarkedSource{called: &n}},
		g, ledger.New(), GenerateConfig{}); r != nil {
		t.Fatal("index 소비 출처가 없는데 재생성기가 만들어졌다")
	}
}

// **§7.5 규칙 3의 delta 문맥 해석**: material delta로 새로 판정 가능해진
// necessary 술어는 pre_satisfied 딱지가 있어도 "살아 있는" 반증력으로 센다.
// 이것이 없으면 재생성 가설이 규칙 3에 전멸한다(C-6).
func TestRule3DeltaContextRevivesNecessary(t *testing.T) {
	k := evidence.ObservationKey{Source: evidence.SrcScanMetrics, TargetID: "t1",
		Aspect: evidence.AspectMetric, Metric: "m1", Window: evidence.WindowFull}
	preds := []ledger.SignalPred{
		{PredID: "P1", Tool: k.Source, TargetID: k.TargetID, Aspect: k.Aspect, Metric: k.Metric,
			Window: k.Window, Predicate: evidence.PredDirectionUp,
			Expectation: evidence.ExpectMustHold, Role: ledger.RoleNecessary, PreSatisfied: true},
		{PredID: "P2", Tool: k.Source, TargetID: k.TargetID, Aspect: k.Aspect, Metric: "m2",
			Window: k.Window, Predicate: evidence.PredDirectionUp,
			Expectation: evidence.ExpectMustHold, Role: ledger.RoleCorroborating},
	}
	measured := []bool{true, false} // P2가 미실측이라 ①(전부 기실측)은 안 걸린다

	// delta 문맥 없음 — 미실측 necessary 0개로 반려된다(종전 판정 그대로).
	got := rule3Violations(preds, measured, nil)
	if len(got) != 1 || !strings.Contains(got[0].Reason, "necessary") {
		t.Fatalf("delta 없이 반려가 안 났다: %+v", got)
	}
	// delta 문맥 있음 — 그 키가 새로 판정 가능해졌으므로 반증력이 산다.
	if got := rule3Violations(preds, measured,
		map[evidence.ObservationKey]bool{k.Canonical(): true}); len(got) != 0 {
		t.Fatalf("delta로 되살아난 necessary가 여전히 반려됐다: %+v", got)
	}
	// **①(전부 기실측=회고)은 좁히지 않는다** — delta가 있어도 회고는 회고다.
	all := rule3Violations(preds[:1], []bool{true},
		map[evidence.ObservationKey]bool{k.Canonical(): true})
	if len(all) != 1 || !strings.Contains(all[0].Reason, "회고") {
		t.Fatalf("전부 기실측인데 회고 반려가 안 났다: %+v", all)
	}
}
