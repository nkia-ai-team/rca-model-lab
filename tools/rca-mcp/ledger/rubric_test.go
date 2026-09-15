// §8.2 rubric v1 시험 — 표의 값 그대로, 등급 역전 불가는 산술로 검사한다.
package ledger

import "testing"

func rubricHypo(temporal bool) (HypothesisV2, Tally) {
	h := HypothesisV2{Identity: HypoIdentity{TemporalClaim: TemporalPrecedes}}
	if temporal {
		h.Temporal = []TemporalJudged{{SymptomEID: "EIX-0001", Verdict: TemporalPrecedesVerdict}}
	}
	return h, Tally{QualifiedSupports: 2, Lineages: []string{"a", "b"}}
}

func TestRubricBandsAndItems(t *testing.T) {
	h, tl := rubricHypo(false)
	// 기본 밴드 — 가감 없음.
	if b := RubricConfidence(IntConfirmed, h, tl, 0, 0); b.Value != 0.80 || len(b.Items) != 0 {
		t.Fatalf("confirmed 기본: %+v", b)
	}
	if b := RubricConfidence(IntProbable, h, tl, 0, 0); b.Value != 0.50 {
		t.Fatalf("probable 기본: %+v", b)
	}
	// insufficient — 밴드 없음(§8.2-2), 값 0.
	if b := RubricConfidence(IntInsufficient, h, tl, 0, 0); b.Value != 0 {
		t.Fatalf("insufficient: %+v", b)
	}

	// 계보 ≥3 (+0.05).
	tl.Lineages = []string{"a", "b", "c"}
	if b := RubricConfidence(IntConfirmed, h, tl, 0, 0); b.Value != 0.85 {
		t.Fatalf("lineage_ge3: %+v", b)
	}
	// 시간 확인 (+0.05) — 합산은 클램프 상한 +0.10 안.
	h2, _ := rubricHypo(true)
	if b := RubricConfidence(IntConfirmed, h2, tl, 0, 0); b.Value != 0.90 {
		t.Fatalf("temporal 가산: %+v", b)
	}
	// weak 비중 (−0.05).
	weak := Tally{QualifiedSupports: 1, WeakSupports: 2, Lineages: []string{"a", "b"}}
	if b := RubricConfidence(IntProbable, h, weak, 0, 0); b.Value != 0.45 {
		t.Fatalf("weak_majority: %+v", b)
	}
	// 미조사 > 1/3 (−0.05) — 분모=RequiredViewKeys. 정확히 1/3은 미적용.
	// (앞 단계에서 tl.Lineages를 3종으로 늘렸으므로 기본 tally를 새로 쓴다.)
	_, base := rubricHypo(false)
	if b := RubricConfidence(IntProbable, h, base, 1, 3); b.Value != 0.50 {
		t.Fatalf("1/3 경계는 미적용: %+v", b)
	}
	if b := RubricConfidence(IntProbable, h, base, 2, 3); b.Value != 0.45 {
		t.Fatalf("unexamined_over_third: %+v", b)
	}
	// 분모 0 — 비율 미정의라 항 미적용.
	if b := RubricConfidence(IntProbable, h, base, 5, 0); b.Value != 0.50 {
		t.Fatalf("분모 0: %+v", b)
	}
}

// 등급 역전 불가(§8.2-2): probable 최대 0.60 < confirmed 실효 최저 0.75.
func TestRubricNoBandInversion(t *testing.T) {
	// probable 최대 — 가산 2항 전부.
	h, _ := rubricHypo(true)
	best := Tally{QualifiedSupports: 3, Lineages: []string{"a", "b", "c"}}
	maxProbable := RubricConfidence(IntProbable, h, best, 0, 0).Value
	if maxProbable != 0.60 {
		t.Fatalf("probable 최대 = %v, want 0.60", maxProbable)
	}
	// confirmed 최저 — 감산 2항 전부에 시간 항 없음(이론 하한; 실제
	// confirmed 게이트에선 시간 요건이 시간 항을 항상 참으로 만든다).
	worst := Tally{QualifiedSupports: 1, WeakSupports: 5, Lineages: []string{"a", "b"}}
	hNoT, _ := rubricHypo(false)
	minConfirmed := RubricConfidence(IntConfirmed, hNoT, worst, 2, 3).Value
	if minConfirmed != 0.70 {
		t.Fatalf("confirmed 클램프 하한 = %v, want 0.70", minConfirmed)
	}
	if maxProbable >= minConfirmed {
		t.Fatalf("등급 역전: probable %v >= confirmed %v", maxProbable, minConfirmed)
	}
}
