// §7.7 심사점 A 소비 시험 — adoption_audited 이벤트에서 AuditAPassed와
// 탈락 구간 강등이 재생되는지(§14-5 5c).
package ledger

import "testing"

func adoptHV(l *Ledger) HypothesisV2 {
	return HypothesisV2{
		ID:    "H1",
		Chain: ChainClaimsOf(l, "H1"),
		Judgments: []PredJudgment{{
			PredID: "H1-P1", Verdict: VerdictSupportedQualified,
			EntityKeys: []string{CanonEntityKey("sql_key:abc")},
		}},
	}
}

func TestAdoptionAuditConsumption(t *testing.T) {
	l := newWithHypo(t)
	mustAppend(t, l, ActorRule, ts(1), ChainClaimAsserted{Hypothesis: "H1", Claim: claim("C1", "")})

	// 심사 전 — fail-closed false, 강등 없음.
	hv := adoptHV(l)
	if adoptionAuditOf(l, "H1", &hv, GateContext{}) {
		t.Fatal("심사 없이 AuditAPassed")
	}
	if hv.Judgments[0].Verdict != VerdictSupportedQualified {
		t.Fatal("심사 없이 강등됐다")
	}

	// 구간 탈락 + 요약 불통과 — 강등이 걸리고 통과는 아니다.
	mustAppend(t, l, ActorVerifier, ts(2), AdoptionAudited{Hypothesis: "H1", ClaimID: "C1", Passed: false, Note: "지지 실질 없음"})
	mustAppend(t, l, ActorVerifier, ts(3), AdoptionAudited{Hypothesis: "H1", Passed: false, Note: "요약"})
	hv = adoptHV(l)
	if adoptionAuditOf(l, "H1", &hv, GateContext{}) {
		t.Fatal("불통과 요약인데 AuditAPassed")
	}
	if hv.Judgments[0].Verdict != VerdictSupportedWeak {
		t.Fatalf("탈락 구간 지지가 강등되지 않았다: %s", hv.Judgments[0].Verdict)
	}

	// 정밀화가 구간을 supersede — 새 ClaimID는 미심사이므로 구 탈락의
	// 강등 효력이 접힌다.
	mustAppend(t, l, ActorRule, ts(4), ChainClaimAsserted{Hypothesis: "H1", Claim: claim("C2", "C1")})
	hv = adoptHV(l)
	adoptionAuditOf(l, "H1", &hv, GateContext{})
	if hv.Judgments[0].Verdict != VerdictSupportedQualified {
		t.Fatal("supersede된 구간의 탈락이 계속 강등을 만든다")
	}

	// 재심사 라운드 전 구간 통과 — 마지막 요약이 이긴다.
	mustAppend(t, l, ActorVerifier, ts(5), AdoptionAudited{Hypothesis: "H1", ClaimID: "C2", Passed: true, Note: "지지 실질 확인"})
	mustAppend(t, l, ActorVerifier, ts(6), AdoptionAudited{Hypothesis: "H1", Passed: true, Note: "요약"})
	hv = adoptHV(l)
	if !adoptionAuditOf(l, "H1", &hv, GateContext{}) {
		t.Fatal("통과 요약인데 AuditAPassed=false")
	}

	// 장부 검증 — 구간 판정의 note 필수, 없는 가설 반려.
	if _, err := l.Append(ActorVerifier, ts(7), AdoptionAudited{Hypothesis: "H1", ClaimID: "C2", Passed: true}); err == nil {
		t.Fatal("note 없는 구간 판정이 통과")
	}
	if _, err := l.Append(ActorVerifier, ts(7), AdoptionAudited{Hypothesis: "H9", Passed: true}); err == nil {
		t.Fatal("없는 가설의 심사가 통과")
	}
}
